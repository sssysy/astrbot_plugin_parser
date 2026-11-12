from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Literal

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    DurationLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer
from .utils import transcode_video_audio_to_lc


@dataclass(slots=True)
class DeliveryReport:
    """Count acknowledged sends separately from unavailable media or preparation.

    A truthy report means at least one message was accepted by ``event.send``;
    callers must inspect ``status`` before claiming that every item was sent.
    """

    sent_count: int = 0
    failed_count: int = 0
    preview_sent_count: int = 0
    download_failed_count: int = 0
    skipped_count: int = 0
    preparation_failed_count: int = 0

    @property
    def status(self) -> Literal["sent", "partial", "empty", "failed"]:
        incomplete = any((self.failed_count, self.download_failed_count,
                          self.skipped_count, self.preparation_failed_count))
        if self.sent_count:
            return "partial" if incomplete else "sent"
        return "failed" if incomplete else "empty"

    def __bool__(self) -> bool:
        return self.sent_count > 0

    def include(self, other: "DeliveryReport") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def to_dict(self) -> dict[str, int | str]:
        return {"status": self.status, **{
            name: getattr(self, name) for name in self.__dataclass_fields__
        }}


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _image_from_path(path: Path) -> Image:
        return Image.fromFileSystem(str(path))

    @staticmethod
    def _video_from_path(path: Path) -> Video:
        return Video.fromFileSystem(str(path))

    @staticmethod
    def _record_from_path(path: Path) -> Record:
        return Record.fromFileSystem(str(path))

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 基础卡片渲染条件：单条重媒体配置或显式指定
        is_single_heavy = len(heavy) == 1 and not light
        render_card = is_single_heavy and self.cfg.single_heavy_render_card
        if render_card_override is not None:
            render_card = render_card_override

        # 实际媒体段数量
        media_count = len(light) + len(heavy)

        # 达到阈值后，强制合并转发，避免刷屏
        force_merge = (media_count + (1 if render_card else 0)) >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        # 合并转发时始终内联预览卡片（除非显式禁用）
        if force_merge and render_card_override is not False:
            render_card = True

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }

    async def _send_preview_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ) -> DeliveryReport:
        """
        发送预览卡片（独立消息）

        场景：
        - 只有一个重媒体
        - 未触发合并转发
        - 卡片作为“预览”，不与正文混合
        """
        report = DeliveryReport()
        if not plan["preview_card"]:
            return report

        try:
            image_path = await self.renderer.render_card(result)
            if image_path is None:
                report.preparation_failed_count += 1
                return report
            segs = [self._image_from_path(image_path)]
        except Exception as exc:
            report.preparation_failed_count += 1
            self._log_failure("preview_prepare", exc)
            return report
        try:
            await event.send(event.chain_result(segs))
        except Exception as exc:
            report.failed_count += 1
            self._log_failure("preview_send", exc, segs)
        else:
            report.sent_count += 1
            report.preview_sent_count += 1
        return report

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
        report: DeliveryReport | None = None,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []
        if report is None:
            report = DeliveryReport()

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        if plan["render_card"] and plan["force_merge"]:
            try:
                image_path = await self.renderer.render_card(result)
                if image_path is not None:
                    segs.append(self._image_from_path(image_path))
                else:
                    report.preparation_failed_count += 1
            except Exception as exc:
                report.preparation_failed_count += 1
                self._log_failure("inline_card_prepare", exc)

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(Plain(cont.text))
                continue

            try:
                path: Path = await cont.get_path()
            except DownloadLimitException:
                report.skipped_count += 1
                continue
            except ZeroSizeException:
                report.download_failed_count += 1
                continue
            except DownloadException:
                report.download_failed_count += 1
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    segs.append(self._image_from_path(path))
                case GraphicsContent() as g:
                    segs.append(self._image_from_path(path))
                    # GraphicsContent 允许携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except (SizeLimitException, DurationLimitException) as exc:
                report.skipped_count += 1
                if self.cfg.show_download_fail_tip:
                    message = (
                        "此项媒体超过时长限制"
                        if isinstance(exc, DurationLimitException)
                        else "此项媒体超过大小限制"
                    )
                    segs.append(Plain(message))
                continue
            except DownloadException as exc:
                if isinstance(exc, DownloadLimitException):
                    report.skipped_count += 1
                else:
                    report.download_failed_count += 1
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    path = await transcode_video_audio_to_lc(path)
                    segs.append(self._video_from_path(path))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=self._to_file_uri(path))
                        if self.cfg.audio_to_file
                        else self._record_from_path(path)
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))

        return [nodes]

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
    ) -> DeliveryReport:
        report = DeliveryReport()
        try:
            plan = self._build_send_plan(
                result,
                group.contents,
                force_merge_override=group.force_merge,
                render_card_override=group.render_card,
            )
            report.include(await self._send_preview_card(event, result, plan))
            segs = await self._build_segments(result, plan, report)
            segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])
        except Exception as exc:
            report.preparation_failed_count += 1
            self._log_failure("group_prepare", exc)
            return report

        if not segs:
            return report

        try:
            await event.send(event.chain_result(segs))
        except Exception as exc:
            report.failed_count += 1
            self._log_failure("group_send", exc, segs)
        else:
            report.sent_count += 1
        return report

    @classmethod
    def _log_failure(cls, phase: str, exc: Exception, segs=()) -> None:
        logger.error(
            f"发送解析结果失败: phase={phase}, error={type(exc).__name__}, "
            f"segments={cls._collect_seg_meta(segs)}"
        )

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """Log component types only, never media paths, URLs or message bodies."""
        return [{"type": type(seg).__name__} for seg in segs]

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ) -> DeliveryReport:
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送
        """
        groups = self._resolve_groups(result)

        report = DeliveryReport()
        for group in groups:
            report.include(await self._send_group(event, result, group))

        # Preserve the existing single text fallback when no content group was
        # delivered. A preview card does not replace the content or this fallback.
        if report.sent_count == report.preview_sent_count:
            segs = self._build_text_fallback(result)
            if not segs:
                logger.warning("发送结果为空，不执行发送")
                return report

            try:
                await event.send(event.chain_result(segs))
            except Exception as exc:
                report.failed_count += 1
                self._log_failure("fallback_send", exc, segs)
            else:
                report.sent_count += 1
        return report

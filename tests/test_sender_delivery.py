"""Synthetic delivery receipts; no QQ, downloads, renderer I/O or models."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import Image, Nodes, Plain, Video

from astrbot_plugin_parser.core import sender as sender_module
from astrbot_plugin_parser.core.data import (
    ImageContent, ParseResult, Platform, SendGroup, TextContent, VideoContent,
)
from astrbot_plugin_parser.core.exception import DownloadException, SizeLimitException
from astrbot_plugin_parser.core.sender import DeliveryReport, MessageSender
from astrbot_plugin_parser.core import render as render_module


PRIVATE_MARKER = "fixture-private-not-for-log"
PRIVATE_ERROR = "https://example.invalid/media?token=" + PRIVATE_MARKER


class FakeEvent:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.attempts = []
        self.delivered = []

    @staticmethod
    def chain_result(segments):
        return segments

    @staticmethod
    def get_self_id():
        return "99999"

    async def send(self, segments):
        self.attempts.append(segments)
        error = self.failures.get(len(self.attempts))
        if error is not None:
            raise error
        self.delivered.append(segments)


class SenderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.renderer = SimpleNamespace(render_card=AsyncMock(return_value=Path("/fixture/card.png")))
        self.config = SimpleNamespace(
            single_heavy_render_card=False, forward_threshold=10,
            show_download_fail_tip=False, audio_to_file=True,
        )
        self.sender = MessageSender(self.config, self.renderer)
        self.transcode = AsyncMock(side_effect=lambda path: path)
        self.transcode_patch = patch.object(sender_module, "transcode_video_audio_to_lc", self.transcode)
        self.transcode_patch.start()
        self.addCleanup(self.transcode_patch.stop)

    @staticmethod
    def result(*contents, header="fixture platform", groups=None):
        return ParseResult(
            platform=Platform("fixture", header), contents=list(contents),
            send_groups=list(groups or []),
        )

    @staticmethod
    def failed_media(error, media_type=ImageContent):
        future = asyncio.get_running_loop().create_future()
        future.set_exception(error)
        return media_type(future)

    async def test_text_delivery_has_a_truthy_complete_receipt(self):
        event = FakeEvent()
        report = await self.sender.send_parse_result(event, self.result(TextContent("fixture text")))
        self.assertIsInstance(report, DeliveryReport)
        self.assertEqual(report.status, "sent")
        self.assertTrue(report)
        self.assertEqual(report.sent_count, 1)
        self.assertEqual(report.failed_count, 0)
        self.assertEqual(report.to_dict()["status"], "sent")
        self.assertEqual(event.delivered[0][0].text, "fixture text")
        self.renderer.render_card.assert_not_awaited()

    async def test_empty_result_and_header_only_fallback_are_distinct(self):
        event = FakeEvent()
        empty = await self.sender.send_parse_result(event, self.result(header=""))
        self.assertEqual(empty.status, "empty")
        self.assertFalse(empty)
        self.assertEqual(event.attempts, [])
        fallback = await self.sender.send_parse_result(event, self.result())
        self.assertEqual(fallback.status, "sent")
        self.assertEqual(fallback.sent_count, 1)
        self.assertEqual(len(event.attempts), 1)
        self.assertEqual(event.delivered[0][0].text, "fixture platform")

    async def test_merged_output_retains_one_send_and_inline_card(self):
        self.config.forward_threshold = 2
        event = FakeEvent()
        report = await self.sender.send_parse_result(
            event, self.result(TextContent("first"), TextContent("second")),
        )
        self.assertEqual(report.status, "sent")
        self.assertEqual((report.sent_count, report.preview_sent_count), (1, 0))
        self.assertEqual(len(event.attempts), 1)
        merged = event.delivered[0][0]
        self.assertIsInstance(merged, Nodes)
        self.assertEqual(len(merged.nodes), 3)
        self.assertIsInstance(merged.nodes[0].content[0], Image)
        self.renderer.render_card.assert_awaited_once()

    async def test_preview_and_video_success_are_two_acknowledged_sends(self):
        self.config.single_heavy_render_card = True
        event = FakeEvent()
        report = await self.sender.send_parse_result(event, self.result(VideoContent(Path("/fixture/video.mp4"))))
        self.assertEqual(report.status, "sent")
        self.assertEqual((report.sent_count, report.preview_sent_count), (2, 1))
        self.assertEqual(len(event.attempts), 2)
        self.assertIsInstance(event.delivered[0][0], Image)
        self.assertIsInstance(event.delivered[1][0], Video)
        self.renderer.render_card.assert_awaited_once()

    async def test_preview_success_and_media_send_failure_stay_partial_with_one_fallback(self):
        self.config.single_heavy_render_card = True
        event = FakeEvent({2: RuntimeError(PRIVATE_ERROR)})
        report = await self.sender.send_parse_result(event, self.result(VideoContent(Path("/fixture/video.mp4"))))
        self.assertEqual(report.status, "partial")
        self.assertTrue(report)
        self.assertEqual((report.sent_count, report.failed_count, report.preview_sent_count), (2, 1, 1))
        self.assertEqual(len(event.attempts), 3)
        self.assertIsInstance(event.attempts[2][0], Plain)
        self.renderer.render_card.assert_awaited_once()

    async def test_preview_send_failure_does_not_prevent_media_or_duplicate_fallback(self):
        self.config.single_heavy_render_card = True
        event = FakeEvent({1: RuntimeError(PRIVATE_ERROR)})
        report = await self.sender.send_parse_result(event, self.result(VideoContent(Path("/fixture/video.mp4"))))
        self.assertEqual(report.status, "partial")
        self.assertEqual((report.sent_count, report.failed_count, report.preview_sent_count), (1, 1, 0))
        self.assertEqual(len(event.attempts), 2)
        self.assertIsInstance(event.delivered[0][0], Video)
        self.renderer.render_card.assert_awaited_once()

    async def test_renderer_failure_keeps_media_and_reports_missing_card(self):
        self.config.single_heavy_render_card = True
        for outcome in (None, RuntimeError(PRIVATE_ERROR)):
            self.renderer.render_card.reset_mock()
            self.renderer.render_card.side_effect = outcome if isinstance(outcome, Exception) else None
            self.renderer.render_card.return_value = None
            event = FakeEvent()
            with self.subTest(outcome=type(outcome).__name__):
                report = await self.sender.send_parse_result(event, self.result(VideoContent(Path("/fixture/video.mp4"))))
                self.assertEqual(report.status, "partial")
                self.assertEqual((report.sent_count, report.preparation_failed_count), (1, 1))
                self.assertEqual(len(event.attempts), 1)
                self.assertIsInstance(event.delivered[0][0], Video)

    async def test_inline_card_failure_keeps_text_and_reports_partial(self):
        self.config.forward_threshold = 2
        self.renderer.render_card.return_value = None
        event = FakeEvent()
        report = await self.sender.send_parse_result(event, self.result(TextContent("first"), TextContent("second")))
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.preparation_failed_count, 1)
        self.assertEqual(len(event.delivered[0][0].nodes), 2)

    async def test_download_failure_is_separate_from_successful_tip_or_fallback(self):
        for show_tip in (False, True):
            self.config.show_download_fail_tip = show_tip
            event = FakeEvent()
            with self.subTest(show_tip=show_tip):
                report = await self.sender.send_parse_result(
                    event, self.result(self.failed_media(DownloadException(PRIVATE_ERROR))),
                )
                self.assertEqual(report.status, "partial")
                self.assertEqual((report.sent_count, report.failed_count, report.download_failed_count), (1, 0, 1))
                self.assertEqual(len(event.attempts), 1)
                self.assertEqual(event.delivered[0][0].text, "此项媒体下载失败" if show_tip else "fixture platform")

    async def test_preview_alone_does_not_hide_failed_download_or_repeat_card(self):
        self.config.single_heavy_render_card = True
        event = FakeEvent()
        report = await self.sender.send_parse_result(
            event, self.result(self.failed_media(DownloadException(PRIVATE_ERROR), VideoContent)),
        )
        self.assertEqual(report.status, "partial")
        self.assertEqual((report.sent_count, report.preview_sent_count, report.download_failed_count), (2, 1, 1))
        self.assertEqual(len(event.attempts), 2)
        self.renderer.render_card.assert_awaited_once()

    async def test_download_size_limit_is_reported_without_changing_tip_policy(self):
        event = FakeEvent()
        report = await self.sender.send_parse_result(event, self.result(self.failed_media(SizeLimitException(), VideoContent)))
        self.assertEqual(report.status, "partial")
        self.assertEqual((report.skipped_count, report.download_failed_count), (1, 0))
        self.assertEqual(event.delivered[0][0].text, "fixture platform")

    async def test_unavailable_media_without_any_output_is_failed_not_empty(self):
        event = FakeEvent()
        report = await self.sender.send_parse_result(
            event, self.result(self.failed_media(DownloadException(PRIVATE_ERROR)), header=""),
        )
        self.assertEqual(report.status, "failed")
        self.assertFalse(report)
        self.assertEqual(event.attempts, [])

    async def test_all_send_failures_are_false_even_after_fallback_attempt(self):
        event = FakeEvent({1: RuntimeError(PRIVATE_ERROR), 2: RuntimeError(PRIVATE_ERROR)})
        report = await self.sender.send_parse_result(event, self.result(TextContent("fixture text")))
        self.assertEqual(report.status, "failed")
        self.assertFalse(report)
        self.assertEqual((report.sent_count, report.failed_count), (0, 2))
        self.assertEqual(len(event.attempts), 2)

    async def test_one_failed_group_does_not_hide_success_or_add_global_fallback(self):
        event = FakeEvent({1: RuntimeError(PRIVATE_ERROR)})
        report = await self.sender.send_parse_result(event, self.result(groups=[
            SendGroup([TextContent("first")]), SendGroup([TextContent("second")]),
        ]))
        self.assertEqual(report.status, "partial")
        self.assertEqual((report.sent_count, report.failed_count), (1, 1))
        self.assertEqual(len(event.attempts), 2)
        self.assertEqual(event.delivered[0][0].text, "second")

    async def test_failure_logs_only_include_error_and_segment_types(self):
        event = FakeEvent({1: RuntimeError(PRIVATE_ERROR), 2: RuntimeError(PRIVATE_ERROR)})
        media = ImageContent(Path("/fixture/" + PRIVATE_MARKER + ".png"))
        with patch.object(sender_module.logger, "error") as log:
            report = await self.sender.send_parse_result(event, self.result(media))
        self.assertEqual(report.status, "failed")
        rendered = str(log.call_args_list)
        self.assertIn("RuntimeError", rendered)
        self.assertIn("Image", rendered)
        self.assertNotIn(PRIVATE_MARKER, rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("/fixture/", rendered)

    async def test_cancellation_propagates_from_send_and_media_preparation(self):
        for preview in (False, True):
            self.config.single_heavy_render_card = preview
            event = FakeEvent({1: asyncio.CancelledError()})
            with self.subTest(preview=preview):
                with self.assertRaises(asyncio.CancelledError):
                    await self.sender.send_parse_result(event, self.result(VideoContent(Path("/fixture/video.mp4"))))
                self.assertEqual(len(event.attempts), 1)
        future = asyncio.get_running_loop().create_future()
        future.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.sender.send_parse_result(FakeEvent(), self.result(ImageContent(future)))


class RenderFailureLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_renderer_failure_does_not_log_result_or_exception_text(self):
        renderer = render_module.Renderer.__new__(render_module.Renderer)
        renderer.cfg = SimpleNamespace(cache_dir=Path("/fixture"))
        renderer._create_card_image = AsyncMock(side_effect=RuntimeError(PRIVATE_ERROR))
        result = SenderDeliveryTests.result(TextContent(PRIVATE_MARKER))
        with patch.object(render_module.logger, "error") as log:
            self.assertIsNone(await renderer.render_card(result))
        rendered = str(log.call_args_list)
        self.assertIn("RuntimeError", log.call_args.args[0])
        self.assertNotIn(PRIVATE_MARKER, rendered)
        self.assertNotIn("https://", rendered)


class PublicSendContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_send_returns_real_bool_for_delivery_reports(self):
        from astrbot_plugin_parser.main import ParserPlugin

        for sent_count, expected in ((0, False), (1, True)):
            with self.subTest(sent_count=sent_count):
                result = object()
                event = object()
                report = DeliveryReport(sent_count=sent_count)
                plugin = SimpleNamespace(
                    parse_url=AsyncMock(return_value=result),
                    sender=SimpleNamespace(send_parse_result=AsyncMock(return_value=report)),
                )
                actual = await ParserPlugin.parse_and_send(plugin, event, "https://example.invalid/fixture")
                self.assertIs(actual, expected)
                plugin.sender.send_parse_result.assert_awaited_once_with(event, result)


if __name__ == "__main__":
    unittest.main()

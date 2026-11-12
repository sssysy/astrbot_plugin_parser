import json
import re
from itertools import chain
from typing import Any, ClassVar
from urllib.parse import urlsplit

from aiohttp import ClientError
from bs4 import BeautifulSoup, Tag

from ..config import PluginConfig
from ..constants import COMMON_HEADER
from ..cookie import CookieJar
from ..data import ParseResult, Platform
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class TwitterParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="twitter", display_name="推特")
    MAX_API_BYTES: ClassVar[int] = 1024 * 1024

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.twitter
        self.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://xdown.app",
                "Referer": "https://xdown.app/",
            }
        )
        self.xdown_url = "https://xdown.app/api/ajaxSearch"
        self.cookiejar = CookieJar(config, self.mycfg, domain="xdown.app")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    async def _request_json(
        self, method: str, url: str, *, headers: dict[str, str], data=None
    ) -> dict[str, Any]:
        async with self.session.request(
            method, url, data=data, headers=headers, allow_redirects=False,
        ) as resp:
            if resp.status >= 300:
                raise ParseException(f"上游 HTTP {resp.status}")
            if not (
                resp.content_type == "application/json"
                or resp.content_type.endswith("+json")
            ):
                raise ParseException("上游未返回 JSON")
            body = bytearray()
            async for chunk in resp.content.iter_chunked(32768):
                body.extend(chunk)
                if len(body) > self.MAX_API_BYTES:
                    raise ParseException("上游响应超过大小限制")
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                raise ParseException("上游 JSON 无法解析") from None
            if not isinstance(payload, dict):
                raise ParseException("上游响应结构异常")
            return payload

    async def _req_xdown_api(self, url: str) -> dict[str, Any]:
        return await self._request_json(
            "POST", self.xdown_url, data={"q": url, "lang": "zh-cn"},
            headers=self.headers,
        )

    @staticmethod
    def _xdown_html(resp: dict[str, Any]) -> str:
        code = resp.get("statusCode")
        if code == 404:
            raise ParseException("未找到该帖子的可下载媒体")
        if resp.get("status") != "ok" or resp.get("msg") or (
            code is not None and (type(code) is not int or code != 200)
        ):
            # The upstream message may contain URLs or other untrusted text.
            raise ParseException("上游未能获取帖子内容")
        html = resp.get("data")
        if not isinstance(html, str) or not html.strip():
            raise ParseException("上游未返回有效内容")
        return html

    async def _req_fxtwitter_api(self, tweet_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9]{2,20}", tweet_id):
            raise ParseException("帖子编号无效")
        return await self._request_json(
            "GET", f"https://api.fxtwitter.com/2/status/{tweet_id}",
            headers={**COMMON_HEADER, "Accept": "application/json"},
        )

    @staticmethod
    def _safe_media_url(value: Any, host: str) -> str | None:
        if not isinstance(value, str) or not value or len(value) > 8192:
            return None
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            return None
        try:
            url = urlsplit(value)
            if (
                url.scheme != "https" or url.hostname != host
                or url.username is not None or url.password is not None
                or url.port not in (None, 443) or url.fragment
            ):
                return None
        except ValueError:
            return None
        return value

    def parse_fxtwitter_response(
        self, payload: dict[str, Any], tweet_id: str
    ) -> ParseResult:
        if type(payload.get("code")) is not int or payload["code"] != 200:
            raise ParseException("备用源未能获取帖子内容")
        tweet = payload.get("status")
        if not isinstance(tweet, dict) or tweet.get("type") != "status":
            raise ParseException("备用源返回的帖子不可用")
        if tweet.get("id") != tweet_id:
            raise ParseException("备用源返回的帖子编号不一致")
        text = tweet.get("text", "")
        if not isinstance(text, str) or len(text) > 100000:
            raise ParseException("备用源正文结构异常")
        text = text.strip()
        media = tweet.get("media")
        if media is None:
            media = {}
        if not isinstance(media, dict):
            raise ParseException("备用源媒体结构异常")
        if "all" in media:
            items = media["all"]
        else:
            photos, videos = media.get("photos", []), media.get("videos", [])
            if not isinstance(photos, list) or not isinstance(videos, list):
                raise ParseException("备用源媒体结构异常")
            items = photos + videos
        if not isinstance(items, list) or len(items) > 16:
            raise ParseException("备用源媒体结构异常")

        # Validate the complete list before starting any download tasks.
        checked = []
        for item in items:
            if not isinstance(item, dict) or item.get("type") not in ("photo", "video", "gif"):
                raise ParseException("备用源媒体类型不受支持")
            kind = item["type"]
            host = "pbs.twimg.com" if kind == "photo" else "video.twimg.com"
            url = self._safe_media_url(item.get("url"), host)
            if not url or (kind != "photo" and not urlsplit(url).path.lower().endswith(".mp4")):
                raise ParseException("备用源媒体地址不受支持")
            cover = self._safe_media_url(item.get("thumbnail_url"), "pbs.twimg.com")
            duration = item.get("duration", 0)
            if type(duration) not in (int, float) or not 0 <= duration <= 86400:
                duration = 0
            checked.append((kind, url, cover, duration))
        if not text and not checked:
            raise ParseException("备用源未返回正文或媒体")

        author = tweet.get("author")
        author = author if isinstance(author, dict) else {}
        name = author.get("name") or author.get("screen_name")
        name = name if isinstance(name, str) and name.strip() else "无用户名"
        avatar = self._safe_media_url(author.get("avatar_url"), "pbs.twimg.com")
        # Never pass xdown's Cookie, Origin or Referer to another service/CDN.
        headers = COMMON_HEADER.copy()
        contents = []
        for kind, url, cover, duration in checked:
            if kind == "photo":
                contents.extend(self.create_image_contents([url], headers=headers))
            elif kind == "gif":
                contents.extend(self.create_dynamic_contents([url], headers=headers))
            else:
                contents.append(self.create_video_content(url, cover, duration, headers=headers))
        return self.result(
            text=text or None, url=f"https://x.com/i/status/{tweet_id}",
            author=self.create_author(name, avatar, headers=headers),
            contents=contents,
        )

    @staticmethod
    def _failure_reason(source: str, error: Exception) -> str:
        if isinstance(error, ParseException):
            return f"{source}：{error.message}"
        if isinstance(error, TimeoutError):
            return f"{source} 请求超时"
        return f"{source} 网络请求失败"

    @handle(
        "twitter.com",
        (
            r"(?<![A-Za-z0-9.-])(?:(?:www|mobile)\.)?twitter\.com/"
            r"(?:[A-Za-z0-9_]+/)*status/\d+"
        ),
    )
    @handle(
        "x.com",
        (
            r"(?<![A-Za-z0-9.-])(?:www\.)?x\.com/"
            r"(?:[A-Za-z0-9_]+/)*status/\d+"
        ),
    )
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        # 从匹配对象中获取原始URL
        url = f"https://{searched.group(0)}"
        try:
            resp = await self._req_xdown_api(url)
            return self.parse_twitter_html(self._xdown_html(resp))
        except (ParseException, ClientError, TimeoutError) as error:
            primary_error = self._failure_reason("xdown", error)
        tweet_id = url.rsplit("/", 1)[-1]
        try:
            resp = await self._req_fxtwitter_api(tweet_id)
            return self.parse_fxtwitter_response(resp, tweet_id)
        except (ParseException, ClientError, TimeoutError) as error:
            reason = self._failure_reason("FxTwitter", error)
            raise ParseException(f"X 解析失败：{primary_error}；{reason}") from None

    def parse_twitter_html(self, html_content: str) -> ParseResult:
        """解析 Twitter HTML 内容

        Args:
            html_content (str): Twitter HTML 内容

        Returns:
            ParseResult: 解析结果
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # 初始化数据
        title = None
        cover_url = None
        video_url = None
        images_urls = []
        dynamic_urls = []

        # 1. 提取缩略图链接
        thumb_tag = soup.find("img")
        if isinstance(thumb_tag, Tag):
            if cover := thumb_tag.get("src"):
                cover_url = str(cover)

        # 2. 提取下载链接
        tw_button_tags = soup.find_all("a", class_="tw-button-dl")
        abutton_tags = soup.find_all("a", class_="abutton")
        for tag in chain(tw_button_tags, abutton_tags):
            if not isinstance(tag, Tag):
                continue
            href = tag.get("href")
            if href is None:
                continue

            href = str(href)
            text = tag.get_text(strip=True)
            if "下载 MP4" in text:
                video_url = href
                break
            elif "下载图片" in text:
                images_urls.append(href)
            elif "下载 gif" in text:
                dynamic_urls.append(href)

        # 3. 提取标题
        title_tag = soup.find("h3")
        if title_tag:
            title = title_tag.get_text(strip=True)

        if not title and not (video_url or images_urls or dynamic_urls):
            raise ParseException("上游页面没有正文或媒体")

        # 简洁的构建方式
        contents = []

        # 添加视频内容
        if video_url:
            contents.append(self.create_video_content(video_url, cover_url))

        # 添加图片内容
        if images_urls:
            contents.extend(self.create_image_contents(images_urls))

        # 添加动态内容
        if dynamic_urls:
            contents.extend(self.create_dynamic_contents(dynamic_urls))

        return self.result(
            title=title,
            author=self.create_author("无用户名"),
            contents=contents,
        )
        # # 4. 提取Twitter ID
        # twitter_id_input = soup.find("input", {"id": "TwitterId"})
        # if (
        #     twitter_id_input
        #     and isinstance(twitter_id_input, Tag)
        #     and (value := twitter_id_input.get("value"))
        #     and isinstance(value, str)
        # ):

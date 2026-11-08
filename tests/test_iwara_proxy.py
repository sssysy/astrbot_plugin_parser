"""Synthetic Iwara metadata verifies selected proxy reaches the download."""
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_parser.core.parsers import iwara as iwara_module


class IwaraRepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.proxy = "http://proxy.example.invalid:17891"
        self.video_url = "https://media.example.invalid/fixture.mp4"
        self.cover = Path("/fixtures/cover.jpg")
        self.blurred_cover = Path("/fixtures/cover_blur.jpg")
        self.info = {
            "title": "Fixture video", "body": "Fixture description", "tags": [],
            "user": {"name": "Fixture user", "username": "fixture", "avatar": None},
            "updatedAt": "2026-01-01T00:00:00Z", "rating": "general",
            "file": {"id": "fixture", "duration": 12},
            "fileUrl": "https://file.example.invalid/fixture?expires=1",
        }
        self.api = SimpleNamespace(
            iwaraID_Get_videoInfo=AsyncMock(return_value=self.info),
            videoInfo_Get_Thumbnail=Mock(return_value="https://image.example.invalid/cover"),
            fileURL_get_urlInfo=AsyncMock(return_value=[]),
            urlInfo_Get_videoURL=AsyncMock(return_value=self.video_url),
            _blur=Mock(return_value=self.blurred_cover),
        )
        self.api.auto_blur_video_thumbnail = iwara_module.api.auto_blur_video_thumbnail
        self.parse_video = iwara_module.IwaraParser._parse
        api_patch = patch.object(iwara_module, "api", self.api)
        api_patch.start()
        self.addCleanup(api_patch.stop)
        self.match = re.search(r"(?P<video_id>\w+)", "fixture")

    def harness(self, *, nsfw="blur", proxy=None):
        return SimpleNamespace(
            proxy=proxy,
            mycfg=SimpleNamespace(nsfw=nsfw, video_quality="Source"),
            downloader=SimpleNamespace(
                download_img=AsyncMock(return_value=self.cover),
                download_video=Mock(return_value="fixture video task"),
            ),
            create_author=Mock(return_value="fixture author"),
            result=lambda **kwargs: SimpleNamespace(**kwargs),
        )

    async def test_video_download_follows_selected_proxy_including_direct_mode(self):
        for proxy in (self.proxy, None):
            with self.subTest(proxy=proxy):
                parser = self.harness(proxy=proxy)
                result = await self.parse_video(parser, self.match)
                parser.downloader.download_video.assert_called_once_with(
                    self.video_url, proxy=proxy
                )
                self.assertEqual(result.contents[0].path_task, "fixture video task")
                self.assertEqual(result.contents[0].cover, self.cover)
                self.api._blur.assert_not_called()

    async def test_blur_setting_keeps_existing_cover_and_video_behavior(self):
        # Synthetic rating metadata only; no image or video is requested.
        self.info["rating"] = "ecchi"
        parser = self.harness(proxy=self.proxy)
        result = await self.parse_video(parser, self.match)
        self.api._blur.assert_called_once_with(self.cover, self.blurred_cover)
        self.assertEqual(result.contents[0].cover, self.blurred_cover)
        self.assertEqual(result.contents[0].duration, 12)
        parser.downloader.download_video.assert_called_once_with(
            self.video_url, proxy=self.proxy
        )

    async def test_ignore_setting_still_exits_before_any_media_requests(self):
        self.info["rating"] = "ecchi"
        parser = self.harness(nsfw="ignore", proxy=self.proxy)
        result = await self.parse_video(parser, self.match)
        self.assertIn("已按配置忽略", result.extra["info"])
        parser.downloader.download_img.assert_not_awaited()
        parser.downloader.download_video.assert_not_called()
        self.api.fileURL_get_urlInfo.assert_not_awaited()
        self.api._blur.assert_not_called()

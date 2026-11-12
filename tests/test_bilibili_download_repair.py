"""Verify actual video parsing reaches CDN failover without changing its result."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bilibili_api.video import VideoCodecs, VideoQuality

from astrbot_plugin_parser.core.data import VideoContent
from astrbot_plugin_parser.core.exception import DownloadException, DurationLimitException
from astrbot_plugin_parser.core.parsers import bilibili
from astrbot_plugin_parser.core.parsers.bilibili import candidates


BVID = "BV1234567890"
PRIMARY = "https://primary.bilivideo.com/video.m4s"
BACKUP = "https://backup.bilivideo.com/video.m4s"
AUDIO = "https://primary.bilivideo.com/audio.m4s"


def video_info():
    return {
        "bvid": BVID,
        "title": "Synthetic video",
        "desc": "Original description",
        "duration": 30,
        "owner": {"mid": 1, "name": "Synthetic author", "face": "https://image.example/avatar.png"},
        "stat": {key: 0 for key in ("view", "danmaku", "reply", "favorite", "coin", "share", "like")},
        "pubdate": 1,
        "ctime": 1,
        "pic": "https://image.example/cover.png",
    }


class VideoDownloadRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name)
        self.parser = bilibili.BilibiliParser.__new__(bilibili.BilibiliParser)
        self.parser.cfg = SimpleNamespace(cache_dir=self.cache, max_duration=300, proxy=None)
        self.parser.mycfg = SimpleNamespace(use_proxy=False, block_pcdn=False)
        self.parser.headers = {"Referer": "https://www.bilibili.com/"}
        self.parser.video_quality = VideoQuality._720P
        self.parser.video_codecs = [VideoCodecs.AVC]
        self.parser.login = SimpleNamespace(_credential=None)
        self.parser.downloader = SimpleNamespace(
            streamd=AsyncMock(),
            download_img=Mock(return_value=self.cache / "image.png"),
        )
        self.video = SimpleNamespace(
            get_info=AsyncMock(return_value=video_info()),
            get_download_url=AsyncMock(),
        )
        self.parser._get_video = AsyncMock(return_value=self.video)

    async def test_parse_video_retries_dash_cdn_and_preserves_result(self):
        self.video.get_download_url.return_value = {
            "dash": {
                "video": [{"id": 64, "codecs": "avc1.64001F", "baseUrl": PRIMARY, "backupUrl": [BACKUP]}],
                "audio": [{"id": 30280, "baseUrl": AUDIO}],
            }
        }
        paths = {BACKUP: self.cache / "video.m4s", AUDIO: self.cache / "audio.m4s"}

        async def download(url, **kwargs):
            if url == PRIMARY:
                raise DownloadException()
            return paths[url]

        self.parser.downloader.streamd.side_effect = download
        with patch.object(candidates, "merge_av", AsyncMock()) as merge:
            result = await self.parser.parse_video(bvid=BVID)
            self.assertEqual(len(result.contents), 1)
            self.assertIsInstance(result.contents[0], VideoContent)
            self.assertFalse(result.send_groups)
            self.assertEqual(result.text, "简介: Original description")
            output = await asyncio.wait_for(result.contents[0].get_path(), timeout=2)

        self.assertEqual(output, self.cache / f"{BVID}-1.mp4")
        merge.assert_awaited_once_with(
            v_path=paths[BACKUP], a_path=paths[AUDIO], output_path=output
        )
        calls = self.parser.downloader.streamd.call_args_list
        self.assertEqual([call.args[0] for call in calls].count(PRIMARY), 1)
        self.assertEqual([call.args[0] for call in calls].count(BACKUP), 1)
        self.assertEqual([call.args[0] for call in calls].count(AUDIO), 1)
        self.assertTrue(all(call.kwargs["retry_times"] == 0 for call in calls))
        self.assertTrue(all(call.kwargs["headers"] is self.parser.headers for call in calls))

    async def test_parse_video_retries_combined_mp4_without_merge(self):
        self.video.get_download_url.return_value = {
            "format": "mp4", "durl": [{"url": PRIMARY, "backup_url": [BACKUP]}]
        }
        output = self.cache / f"{BVID}-1.mp4"
        self.parser.downloader.streamd.side_effect = [DownloadException(), output]
        with patch.object(candidates, "merge_av", AsyncMock()) as merge:
            result = await self.parser.parse_video(bvid=BVID)
            self.assertEqual(await result.contents[0].get_path(), output)
        merge.assert_not_awaited()
        calls = self.parser.downloader.streamd.call_args_list
        self.assertEqual([call.args[0] for call in calls], [PRIMARY, BACKUP])
        self.assertTrue(all(call.kwargs["file_name"] == output.name for call in calls))

    async def test_duration_limit_prevents_media_failover(self):
        self.parser.cfg.max_duration = 1
        self.video.get_download_url.return_value = {
            "format": "mp4", "durl": [{"url": PRIMARY, "backup_url": [BACKUP]}]
        }
        result = await self.parser.parse_video(bvid=BVID)
        with self.assertRaises(DurationLimitException):
            await result.contents[0].get_path()
        self.parser.downloader.streamd.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

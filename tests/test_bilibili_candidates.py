"""Bilibili CDN alternatives must retain representation identity and limits."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_parser.core.exception import (
    DownloadException,
    DurationLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from astrbot_plugin_parser.core.parsers.bilibili import BilibiliParser
from astrbot_plugin_parser.core.parsers.bilibili import candidates as module
from bilibili_api.video import VideoCodecs, VideoQuality

PRIMARY = "https://primary.bilivideo.com/video.m4s?token=public-test"
BACKUP = "https://backup.bilivideo.com/video.m4s?token=public-test"
PCDN = "https://node.mcdn.bilivideo.cn/video.m4s"
AUDIO = "https://primary.bilivideo.com/audio.m4s"
AUDIO_BACKUP = "https://backup.bilivideo.com/audio.m4s"


def video_stream(url=PRIMARY, codec=VideoCodecs.AVC):
    return SimpleNamespace(
        url=url, video_quality=VideoQuality._720P, video_codecs=codec
    )


def video_data():
    return {
        "dash": {
            "video": [
                {
                    "id": 64,
                    "codecs": "avc1.64001F",
                    "baseUrl": PRIMARY,
                    "backupUrl": [BACKUP],
                }
            ],
            "audio": [{"id": 30280, "base_url": AUDIO, "backup_url": [AUDIO_BACKUP]}],
        }
    }


class CandidateSelectionTests(unittest.TestCase):
    def test_typed_stream_without_backup_recovers_selected_raw_backups(self):
        self.assertEqual(
            module.stream_candidates(video_stream(), video_data(), False),
            (PRIMARY, BACKUP),
        )

    def test_never_uses_a_different_quality_or_codec(self):
        data = video_data()
        data["dash"]["video"][:0] = [
            {
                "id": 80,
                "codecs": "avc1",
                "baseUrl": PRIMARY,
                "backupUrl": ["https://other.bilivideo.com/1080p.m4s"],
            },
            {
                "id": 64,
                "codecs": "hev1",
                "baseUrl": PRIMARY,
                "backupUrl": ["https://other.bilivideo.com/hevc.m4s"],
            },
            {
                "id": 64,
                "codecs": "avc1",
                "baseUrl": "https://other/primary",
                "backupUrl": ["https://other.bilivideo.com/unselected.m4s"],
            },
        ]
        self.assertEqual(
            module.stream_candidates(video_stream(), data, False), (PRIMARY, BACKUP)
        )

    def test_hvc1_representation_matches_hev_selected_stream(self):
        data = video_data()
        data["dash"]["video"][0]["codecs"] = "hvc1.1.6.L120.90"
        self.assertEqual(
            module.stream_candidates(video_stream(codec=VideoCodecs.HEV), data, False),
            (PRIMARY, BACKUP),
        )

    def test_block_pcdn_uses_clean_backup(self):
        data = {"durl": [{"url": PCDN, "backup_url": [BACKUP]}]}
        self.assertEqual(
            module.stream_candidates(SimpleNamespace(url=PCDN), data, True), (BACKUP,)
        )

    def test_all_pcdn_preserves_original_fallback(self):
        other = "https://other.mcdn.bilivideo.com/video.m4s"
        data = {"durl": [{"url": PCDN, "backup_url": [other]}]}
        self.assertEqual(
            module.stream_candidates(SimpleNamespace(url=PCDN), data, True),
            (PCDN, other),
        )

    def test_without_filter_pcdn_order_is_unchanged(self):
        data = {"durl": [{"url": PCDN, "backup_url": [BACKUP]}]}
        self.assertEqual(
            module.stream_candidates(SimpleNamespace(url=PCDN), data, False),
            (PCDN, BACKUP),
        )

    def test_duplicate_invalid_urls_removed_and_count_bounded(self):
        stream = video_stream()
        stream.backup_url = [
            BACKUP,
            BACKUP,
            None,
            "file:///tmp/private",
            "javascript:x",
        ]
        data = video_data()
        data["dash"]["video"][0]["backup_url"] = [
            f"https://node{i}.bilivideo.com/video" for i in range(20)
        ]
        urls = module.stream_candidates(stream, data, False)
        self.assertEqual(urls[:2], (PRIMARY, BACKUP))
        self.assertEqual(len(urls), module.MAX_CANDIDATES)

    def test_audio_and_special_audio_backups(self):
        for container, raw in (
            ("audio", [{"id": 30280, "baseUrl": AUDIO, "backupUrl": [AUDIO_BACKUP]}]),
            (
                "flac",
                {
                    "audio": {
                        "id": 30280,
                        "base_url": AUDIO,
                        "backup_url": [AUDIO_BACKUP],
                    }
                },
            ),
            (
                "dolby",
                {
                    "audio": [
                        {"id": 30280, "base_url": AUDIO, "backup_url": [AUDIO_BACKUP]}
                    ]
                },
            ),
        ):
            with self.subTest(container=container):
                stream = SimpleNamespace(
                    url=AUDIO, audio_quality=SimpleNamespace(value=30280)
                )
                self.assertEqual(
                    module.stream_candidates(stream, {"dash": {container: raw}}, False),
                    (AUDIO, AUDIO_BACKUP),
                )


class CandidateDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path("/tmp/offline-bili-media")
        self.downloader = SimpleNamespace(streamd=AsyncMock(return_value=self.path))
        self.headers = {"Referer": "https://www.bilibili.com/"}

    async def test_first_success_does_not_contact_backup(self):
        path = await module.download_candidates(
            self.downloader, (PRIMARY, BACKUP), headers=self.headers, proxy=None
        )
        self.assertEqual(path, self.path)
        self.downloader.streamd.assert_awaited_once()
        self.assertEqual(self.downloader.streamd.call_args.args, (PRIMARY,))
        self.assertEqual(self.downloader.streamd.call_args.kwargs["retry_times"], 0)

    async def test_transport_failure_moves_to_backup_with_same_filename_and_proxy(self):
        self.downloader.streamd.side_effect = [DownloadException(), self.path]
        path = await module.download_candidates(
            self.downloader,
            (PRIMARY, BACKUP),
            headers=self.headers,
            proxy="http://proxy:1234",
        )
        self.assertEqual(path, self.path)
        calls = self.downloader.streamd.call_args_list
        self.assertEqual([call.args[0] for call in calls], [PRIMARY, BACKUP])
        self.assertEqual(calls[0].kwargs["file_name"], calls[1].kwargs["file_name"])
        self.assertTrue(all(call.kwargs["headers"] is self.headers for call in calls))
        self.assertTrue(
            all(call.kwargs["proxy"] == "http://proxy:1234" for call in calls)
        )

    async def test_empty_primary_may_use_valid_backup(self):
        self.downloader.streamd.side_effect = [ZeroSizeException(), self.path]
        self.assertEqual(
            await module.download_candidates(
                self.downloader, (PRIMARY, BACKUP), headers=self.headers, proxy=None
            ),
            self.path,
        )

    async def test_limits_do_not_trigger_alternate_downloads(self):
        for error in (SizeLimitException(), DurationLimitException()):
            with self.subTest(error=type(error).__name__):
                self.downloader.streamd.reset_mock()
                self.downloader.streamd.side_effect = error
                with self.assertRaises(type(error)):
                    await module.download_candidates(
                        self.downloader,
                        (PRIMARY, BACKUP),
                        headers=self.headers,
                        proxy=None,
                    )
                self.assertEqual(self.downloader.streamd.await_count, 1)

    async def test_final_failure_is_not_swallowed(self):
        self.downloader.streamd.side_effect = DownloadException()
        with self.assertRaises(DownloadException):
            await module.download_candidates(
                self.downloader, (PRIMARY, BACKUP), headers=self.headers, proxy=None
            )
        self.assertEqual(self.downloader.streamd.await_count, 2)

    async def test_cancellation_never_starts_backup(self):
        self.downloader.streamd.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await module.download_candidates(
                self.downloader, (PRIMARY, BACKUP), headers=self.headers, proxy=None
            )
        self.assertEqual(self.downloader.streamd.await_count, 1)

    async def test_video_and_audio_fail_over_independently_then_merge(self):
        paths = {BACKUP: Path("/tmp/video.m4s"), AUDIO: Path("/tmp/audio.m4s")}

        async def download(url, **kwargs):
            if url == PRIMARY:
                raise DownloadException()
            return paths[url]

        self.downloader.streamd.side_effect = download
        output = Path("/tmp/merged.mp4")
        with patch.object(module, "merge_av", AsyncMock()) as merge:
            result = await module.download_av_candidates(
                self.downloader,
                (PRIMARY, BACKUP),
                (AUDIO, AUDIO_BACKUP),
                output_path=output,
                headers=self.headers,
                proxy=None,
            )
        self.assertEqual(result, output)
        merge.assert_awaited_once_with(
            v_path=paths[BACKUP], a_path=paths[AUDIO], output_path=output
        )

    async def test_failed_video_cancels_audio_download(self):
        audio_started = asyncio.Event()
        audio_cancelled = asyncio.Event()

        async def download(url, **kwargs):
            if url == PRIMARY:
                await audio_started.wait()
                raise DownloadException()
            audio_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                audio_cancelled.set()
                raise

        self.downloader.streamd.side_effect = download
        with self.assertRaises(DownloadException):
            await module.download_av_candidates(
                self.downloader,
                (PRIMARY,),
                (AUDIO,),
                output_path=Path("/tmp/merged.mp4"),
                headers=self.headers,
                proxy=None,
            )
        self.assertTrue(audio_cancelled.is_set())

    async def test_merge_failure_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "partial.mp4"

            async def failed_merge(**kwargs):
                output.write_bytes(b"incomplete")
                raise RuntimeError("synthetic ffmpeg failure")

            with patch.object(module, "merge_av", failed_merge):
                with self.assertRaises(RuntimeError):
                    await module.download_av_candidates(
                        self.downloader,
                        (PRIMARY,),
                        (AUDIO,),
                        output_path=output,
                        headers=self.headers,
                        proxy=None,
                    )
            self.assertFalse(output.exists())


class ParserExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.parser = object.__new__(BilibiliParser)
        self.parser.mycfg = SimpleNamespace(block_pcdn=False)
        self.parser.video_quality = VideoQuality._720P
        self.parser.video_codecs = [VideoCodecs.AVC]
        self.video = SimpleNamespace(
            get_download_url=AsyncMock(side_effect=lambda **_: video_data())
        )

    async def test_native_detector_selection_retains_both_stream_backups(self):
        self.assertEqual(
            await self.parser.extract_download_candidates(self.video),
            ((PRIMARY, BACKUP), (AUDIO, AUDIO_BACKUP)),
        )

    async def test_public_url_api_remains_two_strings(self):
        self.assertEqual(
            await self.parser.extract_download_urls(self.video), (PRIMARY, AUDIO)
        )

    async def test_native_hvc1_normalization_is_preserved(self):
        data = video_data()
        data["dash"]["video"][0]["codecs"] = "hvc1.1.6.L120.90"
        self.parser.video_codecs = [VideoCodecs.HEV]
        self.video.get_download_url = AsyncMock(return_value=data)
        self.assertEqual(
            await self.parser.extract_download_candidates(self.video),
            ((PRIMARY, BACKUP), (AUDIO, AUDIO_BACKUP)),
        )

    async def test_missing_selected_video_raises_download_error(self):
        self.parser.video_codecs = [VideoCodecs.AV1]
        with self.assertRaises(DownloadException):
            await self.parser.extract_download_candidates(self.video)

    async def test_mp4_durl_has_backups_and_no_separate_audio(self):
        data = {"format": "mp4", "durl": [{"url": PRIMARY, "backup_url": [BACKUP]}]}
        self.video.get_download_url = AsyncMock(return_value=data)
        self.assertEqual(
            await self.parser.extract_download_candidates(self.video),
            ((PRIMARY, BACKUP), None),
        )

    async def test_bm_audio_download_uses_candidates(self):
        self.parser.extract_download_candidates = AsyncMock(
            return_value=((PRIMARY,), (AUDIO, AUDIO_BACKUP))
        )
        self.parser.downloader = SimpleNamespace(
            streamd=AsyncMock(side_effect=[DownloadException(), Path("/tmp/audio.m4s")])
        )
        self.parser.headers = {}
        self.parser._proxy = None
        self.parser.cfg = SimpleNamespace(proxy=None)
        self.parser.mycfg = SimpleNamespace(use_proxy=False)
        self.parser.create_audio_content = lambda task: task
        self.parser.result = lambda **kwargs: kwargs
        searched = SimpleNamespace(
            group=lambda key: {"bvid": "BV1234567890", "page_num": None}[key]
        )
        result = await self.parser._parse_bv_bm(searched)
        await result["contents"][0]
        self.assertEqual(
            [call.args[0] for call in self.parser.downloader.streamd.call_args_list],
            [AUDIO, AUDIO_BACKUP],
        )

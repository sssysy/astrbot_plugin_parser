"""Exercise real downloader retry and cancellation handling without networking."""

import asyncio
from contextlib import nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_parser.core import download


class FailedResponse:
    async def __aenter__(self):
        raise download.ClientError("synthetic download connection failure")

    async def __aexit__(self, *args):
        return None


class PartialContent:
    def __init__(self):
        self.after_write = asyncio.Event()

    async def iter_chunked(self, chunk_size):
        yield b"partial video bytes"
        # The caller requests the second chunk after awaiting its first write.
        self.after_write.set()
        await asyncio.Future()


class PartialResponse:
    status = 200
    content_length = None

    def __init__(self):
        self.content = PartialContent()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class DownloadRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = SimpleNamespace(
            source_max_size=1,
            download_timeout=5,
            download_retry_times=2,
            cache_dir=Path(self.temp.name),
        )
        self.downloader = download.Downloader(self.cfg)
        # Constructor and lifecycle are real; HTTP requests use only this fake.
        await self.downloader.client.close()
        self.client = SimpleNamespace(get=Mock(), close=AsyncMock())
        self.downloader.client = self.client
        self.addAsyncCleanup(self.downloader.close)
        self.logger_patch = patch.object(download, "logger", Mock())
        self.logger_patch.start()
        self.addCleanup(self.logger_patch.stop)
        self.sleep = AsyncMock()
        self.sleep_patch = patch.object(download, "sleep", self.sleep)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.progress_patch = patch.object(
            self.downloader, "get_progress_bar",
            return_value=nullcontext(SimpleNamespace(update=Mock())),
        )
        self.progress_patch.start()
        self.addCleanup(self.progress_patch.stop)

    async def test_configured_two_retries_make_three_attempts(self):
        self.client.get.side_effect = lambda *args, **kwargs: FailedResponse()
        with self.assertRaises(download.DownloadException):
            await self.downloader.streamd(
                "https://cdn.example.test/media", file_name="default.mp4"
            )
        self.assertEqual(self.client.get.call_count, 3)
        self.assertEqual(self.sleep.await_count, 2)
        self.assertEqual([call.args for call in self.sleep.await_args_list], [(1,), (2,)])
        self.assertFalse((self.cfg.cache_dir / "default.mp4").exists())

    async def test_zero_retry_override_makes_exactly_one_attempt(self):
        self.client.get.return_value = FailedResponse()
        with self.assertRaises(download.DownloadException):
            await self.downloader.streamd(
                "https://cdn.example.test/media", file_name="once.mp4", retry_times=0
            )
        self.client.get.assert_called_once()
        self.sleep.assert_not_awaited()
        self.assertEqual(self.cfg.download_retry_times, 2)

    async def test_failure_logs_omit_signed_url_and_exception_details(self):
        marker = "synthetic-private-signed-token"
        url = f"https://cdn.example.invalid/media?token={marker}"
        class SignedFailure(FailedResponse):
            async def __aenter__(self):
                raise download.ClientError(url)

        self.client.get.return_value = SignedFailure()
        with self.assertRaises(download.DownloadException):
            await self.downloader.streamd(url, file_name="private.mp4", retry_times=0)
        rendered = str(download.logger.mock_calls)
        self.assertNotIn(marker, rendered)
        self.assertNotIn("https://", rendered)
        self.assertIn("ClientError", rendered)
        download.logger.exception.assert_not_called()

    async def test_cancel_after_first_written_chunk_removes_partial_file(self):
        response = PartialResponse()
        self.client.get.return_value = response
        task = self.downloader.streamd(
            "https://cdn.example.test/media", file_name="partial.mp4"
        )
        self.assertIsInstance(task, asyncio.Task)
        try:
            await asyncio.wait_for(response.content.after_write.wait(), timeout=2)
            self.assertTrue((self.cfg.cache_dir / "partial.mp4").exists())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse((self.cfg.cache_dir / "partial.mp4").exists())
            self.client.get.assert_called_once()
            self.sleep.assert_not_awaited()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()

"""Synthetic API responses for NCM; no real HTTP or media requests."""
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from astrbot_plugin_parser.core.exception import ParseException
from astrbot_plugin_parser.core.parsers.ncm import NCMParser


class FakeResponse:
    status = 200
    reason = "OK"

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.payloads:
            raise AssertionError("Unexpected HTTP request")
        return FakeResponse(self.payloads.pop(0))


class NCMRepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.parse_song = NCMParser._parse_song
        self.song = {
            "name": "Fixture song",
            "alias": [],
            "album": {"name": "Fixture album", "picUrl": "https://example.invalid/cover"},
            "duration": 120000,
            "artists": [{"name": "Fixture artist", "img1v1Url": ""}],
        }
        self.match = re.search(r"(?P<song_id>\d+)", "123")
        self.audio_url = "https://example.invalid/audio.mp3"

    def harness(self, detail, playback=None):
        return SimpleNamespace(
            session=FakeSession(detail, playback),
            headers={},
            create_author=Mock(return_value="fixture author"),
            create_video_content=Mock(return_value="fixture media"),
            result=lambda **kwargs: SimpleNamespace(**kwargs),
        )

    async def test_empty_or_missing_alias_keeps_song_and_existing_media_presentation(self):
        for alias in ([], None, "missing"):
            with self.subTest(alias=alias):
                song = dict(self.song)
                if alias == "missing":
                    song.pop("alias")
                else:
                    song["alias"] = alias
                parser = self.harness(
                    {"songs": [song]}, {"data": [{"url": self.audio_url}]}
                )
                result = await self.parse_song(parser, self.match)
                self.assertEqual(result.title, "Fixture song")
                self.assertEqual(result.contents, ["fixture media"])
                parser.create_video_content.assert_called_once_with(
                    self.audio_url, "https://example.invalid/cover?param=640y640",
                    duration=120,
                )

    async def test_existing_alias_stays_in_title(self):
        parser = self.harness(
            {"songs": [{**self.song, "alias": ["Alias"]}]},
            {"data": [{"url": self.audio_url}]},
        )
        result = await self.parse_song(parser, self.match)
        self.assertEqual(result.title, "Fixture song（Alias）")

    async def test_unavailable_song_has_clear_error_before_media_requests(self):
        for songs in ([], None, {}, [None], [{}]):
            with self.subTest(songs=songs):
                parser = self.harness({"songs": songs})
                with self.assertRaisesRegex(ParseException, "未找到该歌曲"):
                    await self.parse_song(parser, self.match)
                self.assertEqual(len(parser.session.calls), 1)
                parser.create_video_content.assert_not_called()

    async def test_missing_playback_data_has_clear_error_before_media_download(self):
        for data in ([], None, {}, [None]):
            with self.subTest(data=data):
                parser = self.harness({"songs": [self.song]}, {"data": data})
                with self.assertRaisesRegex(ParseException, "未获取到播放地址"):
                    await self.parse_song(parser, self.match)
                parser.create_video_content.assert_not_called()
                parser.create_author.assert_not_called()

    async def test_missing_or_invalid_playback_url_has_clear_error(self):
        for url in (None, "", " ", 0, "file:///fixture.mp3", "https://", "http:///audio.mp3", "https://[", "https://example.invalid:invalid/audio", "https://user:password@example.invalid/audio", "https://example.invalid/a\n.mp3"):
            with self.subTest(url=url):
                parser = self.harness(
                    {"songs": [self.song]}, {"data": [{"url": url}]}
                )
                with self.assertRaisesRegex(ParseException, "未获取到播放地址"):
                    await self.parse_song(parser, self.match)
                parser.create_video_content.assert_not_called()

    async def test_http_failures_become_parse_errors_before_media_creation(self):
        for stage in ("detail", "playback"):
            for status in (403, 404, 500):
                with self.subTest(stage=stage, status=status):
                    parser = self.harness({"songs": [self.song]}, {"data": [{"url": self.audio_url}]})
                    responses = [FakeResponse({"songs": [self.song]}), FakeResponse({"data": [{"url": self.audio_url}]})]
                    responses[stage == "playback"].status = status
                    parser.session.get = Mock(side_effect=responses)
                    with self.assertRaisesRegex(ParseException, f"HTTP {status}"):
                        await self.parse_song(parser, self.match)
                    parser.create_video_content.assert_not_called()

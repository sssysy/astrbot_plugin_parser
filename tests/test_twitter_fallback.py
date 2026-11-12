"""Exercise the actual X parser using synthetic HTTP and media boundaries.

The parser, BaseParser, ParseResult and media types are the candidate modules.
Only configuration/cookie persistence, HTTP and downloads are replaced.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
POST_ID = "1234567890123456789"
POST_URL = f"https://x.com/fixture/status/{POST_ID}"
PHOTO = "https://pbs.twimg.com/media/fixture.jpg"
VIDEO = "https://video.twimg.com/ext_tw_video/fixture/pu/vid/640x360/test.mp4"
COVER = "https://pbs.twimg.com/ext_tw_video_thumb/fixture/pu/img/test.jpg"


@pytest.fixture(scope="module")
def modules():
    """Load real modules without the plugin package startup."""
    prefix = "_twitter_fallback_fixture"
    before = set(sys.modules)
    for name, directory in (
        (prefix, ROOT),
        (f"{prefix}.core", ROOT / "core"),
        (f"{prefix}.core.parsers", ROOT / "core/parsers"),
    ):
        module = types.ModuleType(name)
        module.__path__ = [str(directory)]
        sys.modules[name] = module

    config = types.ModuleType(f"{prefix}.core.config")
    config.PluginConfig = type("PluginConfig", (), {})
    config.ParserItem = type("ParserItem", (), {})
    sys.modules[config.__name__] = config
    cookie = types.ModuleType(f"{prefix}.core.cookie")
    cookie.CookieJar = lambda *_args, **_kwargs: SimpleNamespace(
        cookies_str="fixture_xdown_cookie=synthetic_secret"
    )
    sys.modules[cookie.__name__] = cookie
    download = types.ModuleType(f"{prefix}.core.download")
    download.Downloader = type("Downloader", (), {})
    sys.modules[download.__name__] = download

    loaded = {}
    for name in ("constants", "data", "exception", "parsers.base", "parsers.twitter"):
        qualified = f"{prefix}.core.{name}"
        spec = importlib.util.spec_from_file_location(
            qualified, ROOT / "core" / (name.replace(".", "/") + ".py")
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    yield SimpleNamespace(
        twitter=loaded["parsers.twitter"],
        base=loaded["parsers.base"],
        data=loaded["data"],
        error=loaded["exception"],
    )
    for name in set(sys.modules) - before:
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


@pytest.fixture
def parser(modules):
    config = SimpleNamespace(
        data_dir=Path("/synthetic/no-disk-access"),
        proxy="http://proxy.fixture.invalid:17891",
        common_timeout=15,
        download_retry_times=2,
        parser=SimpleNamespace(twitter=SimpleNamespace(use_proxy=True)),
    )
    downloads = SimpleNamespace(
        download_img=Mock(side_effect=lambda *_a, **_k: Path("/synthetic/photo.jpg")),
        download_video=Mock(side_effect=lambda *_a, **_k: Path("/synthetic/video.mp4")),
        download_audio=Mock(side_effect=lambda *_a, **_k: Path("/synthetic/audio.mp3")),
    )
    return modules.twitter.TwitterParser(config, downloads)


class FakeStream:
    def __init__(self, body):
        self.body = body
        self.offset = 0

    async def read(self, amount=-1):
        if amount is None or amount < 0:
            amount = len(self.body)
        part = self.body[self.offset:self.offset + amount]
        self.offset += len(part)
        return part

    async def iter_chunked(self, amount):
        while part := await self.read(amount):
            yield part


class FakeResponse:
    def __init__(
        self, payload=None, *, status=200, headers=None, body=None, error=None,
        json_error=None,
    ):
        self.payload = payload
        self.status = status
        self.reason = "Synthetic response"
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.content_type = self.headers["Content-Type"].split(";", 1)[0].strip().lower()
        self.body = body if body is not None else json.dumps(payload).encode()
        self.content = FakeStream(self.body)
        self.content_length = len(self.body)
        self.error = error
        self.json_error = json_error
        self.json_calls = 0

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        self.json_calls += 1
        if self.json_error:
            raise self.json_error
        return copy.deepcopy(self.payload)

    async def read(self):
        return await self.content.read()

    async def text(self, **_kwargs):
        return self.body.decode()


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def request(self, method, url=None, **kwargs):
        if url is None:
            url = kwargs.pop("url")
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP request")
        return self.responses.pop(0)

    def get(self, url=None, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url=None, **kwargs):
        return self.request("POST", url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.close()

    async def close(self):
        self.closed = True


def match_url(parser, url=POST_URL):
    return parser.search_url(url)[1]


def download_urls(parser):
    return [
        call.args[0]
        for method in (parser.downloader.download_img, parser.downloader.download_video)
        for call in method.call_args_list
    ]


def assert_no_downloads(parser):
    parser.downloader.download_img.assert_not_called()
    parser.downloader.download_video.assert_not_called()
    parser.downloader.download_audio.assert_not_called()


def fx_payload(*, text="Synthetic post text", media=None, author=None):
    status = {
        "type": "status",
        "id": POST_ID,
        "text": text,
        "author": {"name": "Synthetic author", "screen_name": "fixture"},
    }
    if media is not None:
        status["media"] = copy.deepcopy(media)
    if author is not None:
        status["author"] = copy.deepcopy(author)
    return {"code": 200, "status": status}


def photo(url=PHOTO):
    return {"type": "photo", "url": url, "width": 640, "height": 480}


def video(url=VIDEO, *, kind="video", duration=3.5):
    return {
        "type": kind,
        "url": url,
        "thumbnail_url": COVER,
        "duration": duration,
        "width": 640,
        "height": 360,
    }


@pytest.mark.asyncio
async def test_xdown_success_does_not_contact_fallback(parser, modules):
    html = f'<h3>Synthetic title</h3><a class="abutton" href="{PHOTO}">下载图片</a>'
    session = FakeSession(FakeResponse({"status": "ok", "data": html}))
    parser._session = session

    result = await parser._parse(match_url(parser))

    assert isinstance(result, modules.data.ParseResult)
    assert result.title == "Synthetic title"
    assert isinstance(result.contents[0], modules.data.ImageContent)
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == "https://xdown.app/api/ajaxSearch"
    assert kwargs["data"] == {"q": POST_URL, "lang": "zh-cn"}
    assert kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_observed_xdown_ok_404_missing_data_uses_fixed_fallback(parser, modules):
    primary = {
        "status": "ok", "statusCode": 404,
        "msg": "Synthetic upstream failure: token=synthetic_secret",
    }
    session = FakeSession(
        FakeResponse(primary),
        FakeResponse(fx_payload(media={"all": [photo()]})),
    )
    parser._session = session

    result = await parser._parse(match_url(parser))

    assert isinstance(result, modules.data.ParseResult)
    assert result.text == "Synthetic post text"
    assert result.url == f"https://x.com/i/status/{POST_ID}"
    assert len(session.calls) == 2
    method, url, kwargs = session.calls[1]
    assert method == "GET"
    assert url == f"https://api.fxtwitter.com/2/status/{POST_ID}"
    assert kwargs["allow_redirects"] is False
    safe_headers = {k.lower(): v for k, v in kwargs["headers"].items()}
    assert safe_headers["accept"] == "application/json"
    assert not ({"cookie", "authorization", "origin", "referer"} & safe_headers.keys())
    assert "synthetic_secret" not in str(kwargs)
    for call in parser.downloader.download_img.call_args_list:
        assert call.kwargs["headers"] == modules.base.COMMON_HEADER
        assert call.kwargs["proxy"] == parser.cfg.proxy


@pytest.mark.parametrize("primary", [
    {},
    {"status": "ok"},
    {"status": "ok", "data": None},
    {"status": "ok", "data": 0},
    {"status": "ok", "data": []},
    {"status": "ok", "data": {}},
    {"status": "ok", "data": ""},
    {"status": "ok", "data": "  \n "},
    {"status": "ok", "data": "<div></div>"},
    {"status": "ok", "data": "<h3> </h3>"},
    {"status": "ok", "statusCode": 500, "data": "<h3>not usable</h3>"},
    {"status": "ok", "msg": "Synthetic failure", "data": "<h3>not usable</h3>"},
    {"status": "error", "data": "<h3>not usable</h3>"},
])
@pytest.mark.asyncio
async def test_unusable_primary_response_falls_back_once(parser, primary):
    session = FakeSession(FakeResponse(primary), FakeResponse(fx_payload()))
    parser._session = session
    result = await parser._parse(match_url(parser))
    assert result.text == "Synthetic post text"
    assert len(session.calls) == 2
    assert_no_downloads(parser)


@pytest.mark.parametrize("error_factory", [
    lambda m: TimeoutError(),
    lambda m: m.twitter.ClientError("synthetic network failure"),
])
@pytest.mark.asyncio
async def test_primary_transport_failure_has_one_fallback(parser, modules, error_factory):
    parser._session = FakeSession(
        FakeResponse(error=error_factory(modules)),
        FakeResponse(fx_payload()),
    )
    result = await parser._parse(match_url(parser))
    assert result.text == "Synthetic post text"
    assert len(parser._session.calls) == 2


@pytest.mark.parametrize("fallback_payload", [
    {"code": 404, "message": "synthetic_secret", "status": None},
    {"code": 200, "status": None},
    {"code": 200, "status": []},
    {"code": 200, "status": {"id": POST_ID, "type": "tombstone"}},
])
@pytest.mark.asyncio
async def test_both_sources_failed_is_safe_and_not_retried(parser, modules, fallback_payload):
    primary = {
        "status": "ok", "statusCode": 404,
        "msg": "https://private.invalid/path?token=synthetic_secret",
    }
    parser._session = FakeSession(FakeResponse(primary), FakeResponse(fallback_payload))
    with pytest.raises(modules.error.ParseException) as failure:
        await parser._parse(match_url(parser))
    assert "synthetic_secret" not in str(failure.value)
    assert "private.invalid" not in str(failure.value)
    assert len(parser._session.calls) == 2
    assert_no_downloads(parser)


@pytest.mark.parametrize("cancel_source", ["primary", "fallback"])
@pytest.mark.asyncio
async def test_cancellation_propagates_without_extra_requests(parser, cancel_source):
    responses = [FakeResponse(error=asyncio.CancelledError())]
    if cancel_source == "fallback":
        responses.insert(0, FakeResponse({"status": "ok", "data": None}))
    parser._session = FakeSession(*responses)
    with pytest.raises(asyncio.CancelledError):
        await parser._parse(match_url(parser))
    assert len(parser._session.calls) == (1 if cancel_source == "primary" else 2)
    assert_no_downloads(parser)


@pytest.mark.parametrize("status", [301, 302, 307, 400, 404, 429, 500])
@pytest.mark.asyncio
async def test_json_boundary_rejects_http_errors_and_redirects(parser, modules, status):
    parser._session = FakeSession(FakeResponse(
        {"code": 200, "private": "synthetic_secret"},
        status=status,
        headers={"Location": "https://private.invalid/?token=synthetic_secret"},
    ))
    with pytest.raises(modules.error.ParseException) as failure:
        await parser._request_json("GET", "https://api.fxtwitter.com/2/status/1", headers={})
    assert "synthetic_secret" not in str(failure.value)
    assert len(parser._session.calls) == 1
    assert parser._session.calls[0][2]["allow_redirects"] is False


@pytest.mark.parametrize("content_type", ["text/html", "text/plain", "image/png"])
@pytest.mark.asyncio
async def test_json_boundary_rejects_non_json_without_leaking_body(parser, modules, content_type):
    parser._session = FakeSession(FakeResponse(
        headers={"Content-Type": content_type}, body=b"<html>synthetic_secret</html>",
    ))
    with pytest.raises(modules.error.ParseException) as failure:
        await parser._request_json("GET", "https://api.fxtwitter.com/2/status/1", headers={})
    assert "synthetic_secret" not in str(failure.value)


@pytest.mark.parametrize("content_type", [
    "application/json", "application/json; charset=utf-8", "application/problem+json",
])
@pytest.mark.asyncio
async def test_json_boundary_accepts_bounded_json_objects(parser, content_type):
    parser._session = FakeSession(FakeResponse(
        {"code": 200}, headers={"Content-Type": content_type},
    ))
    assert await parser._request_json("GET", "https://api.fxtwitter.com/2/status/1", headers={}) == {"code": 200}


@pytest.mark.parametrize("payload", [None, [], "a string", 1, True])
@pytest.mark.asyncio
async def test_json_boundary_requires_object(parser, modules, payload):
    parser._session = FakeSession(FakeResponse(payload))
    with pytest.raises(modules.error.ParseException):
        await parser._request_json("GET", "https://api.fxtwitter.com/2/status/1", headers={})


@pytest.mark.parametrize("known_length", [True, False])
@pytest.mark.asyncio
async def test_json_boundary_rejects_oversized_body_including_chunked(parser, modules, known_length):
    response = FakeResponse(body=b" " * (parser.MAX_API_BYTES + 1))
    if not known_length:
        response.content_length = None
    parser._session = FakeSession(response)
    with pytest.raises(modules.error.ParseException):
        await parser._request_json("GET", "https://api.fxtwitter.com/2/status/1", headers={})
    assert response.json_calls == 0


@pytest.mark.asyncio
async def test_json_boundary_rejects_invalid_json_safely(parser, modules):
    parser._session = FakeSession(FakeResponse(body=b'{"secret":"synthetic_secret"'))
    with pytest.raises(modules.error.ParseException) as failure:
        await parser._request_json("GET", "https://api.fxtwitter.com/2/status/1", headers={})
    assert "synthetic_secret" not in str(failure.value)


@pytest.mark.parametrize("post_url", [
    POST_URL,
    f"https://twitter.com/fixture/status/{POST_ID}",
    f"https://mobile.twitter.com/fixture/status/{POST_ID}",
    f"https://www.x.com/fixture/status/{POST_ID}?s=20",
])
@pytest.mark.asyncio
async def test_supported_original_urls_yield_only_id_to_fallback(parser, post_url):
    parser._session = FakeSession(
        FakeResponse({"status": "ok", "data": None}),
        FakeResponse(fx_payload()),
    )
    await parser._parse(match_url(parser, post_url))
    assert parser._session.calls[1][1] == f"https://api.fxtwitter.com/2/status/{POST_ID}"


@pytest.mark.parametrize("media", [None, {}, {"all": []}])
def test_fx_plain_text_is_a_real_result_with_no_media(parser, modules, media):
    payload = fx_payload(media=media)
    if media is None:
        payload["status"]["media"] = None
    result = parser.parse_fxtwitter_response(payload, POST_ID)
    assert isinstance(result, modules.data.ParseResult)
    assert result.text == "Synthetic post text"
    assert result.contents == []
    assert result.url == f"https://x.com/i/status/{POST_ID}"
    assert_no_downloads(parser)


def test_fx_all_keeps_mixed_photo_video_gif_order_and_safe_headers(parser, modules):
    items = [photo(), video(), photo(PHOTO + "?name=small"), video(kind="gif")]
    result = parser.parse_fxtwitter_response(fx_payload(media={"all": items}), POST_ID)
    assert [type(item) for item in result.contents] == [
        modules.data.ImageContent, modules.data.VideoContent,
        modules.data.ImageContent, modules.data.DynamicContent,
    ]
    assert result.contents[1].duration == 3.5
    assert parser.downloader.download_video.call_args_list[0].args[0] == VIDEO
    assert parser.downloader.download_video.call_args_list[1].args[0] == VIDEO
    for method in (parser.downloader.download_img, parser.downloader.download_video):
        for call in method.call_args_list:
            assert call.kwargs["headers"] == modules.base.COMMON_HEADER
            assert call.kwargs["proxy"] == parser.cfg.proxy
            assert "synthetic_secret" not in str(call.kwargs)


def test_fx_photos_and_videos_without_all_are_supported(parser, modules):
    media = {"photos": [photo()], "videos": [video(), video(kind="gif")]}
    result = parser.parse_fxtwitter_response(fx_payload(media=media), POST_ID)
    assert [type(item) for item in result.contents] == [
        modules.data.ImageContent, modules.data.VideoContent, modules.data.DynamicContent,
    ]


def test_fx_media_all_does_not_duplicate_photos_and_videos(parser, modules):
    media = {"all": [photo()], "photos": [photo()], "videos": [video()]}
    result = parser.parse_fxtwitter_response(fx_payload(media=media), POST_ID)
    assert len(result.contents) == 1
    assert isinstance(result.contents[0], modules.data.ImageContent)
    parser.downloader.download_video.assert_not_called()


@pytest.mark.parametrize("author,expected", [
    ({"name": "Visible name", "screen_name": "fixture"}, "Visible name"),
    ({"screen_name": "fixture"}, "fixture"),
    ({}, "无用户名"),
])
def test_fx_author_uses_available_name_without_network(parser, author, expected):
    result = parser.parse_fxtwitter_response(fx_payload(author=author), POST_ID)
    assert result.author.name == expected
    assert_no_downloads(parser)


@pytest.mark.parametrize("avatar", [
    "http://pbs.twimg.com/profile_images/test.jpg",
    "https://private.invalid/avatar.jpg",
    "https://user:synthetic_secret@pbs.twimg.com/profile_images/test.jpg",
])
def test_fx_untrusted_avatar_is_ignored(parser, avatar):
    result = parser.parse_fxtwitter_response(fx_payload(author={"name": "Fixture", "avatar_url": avatar}), POST_ID)
    assert result.author.avatar is None
    assert_no_downloads(parser)


def test_fx_safe_avatar_uses_credential_free_headers(parser, modules):
    avatar = "https://pbs.twimg.com/profile_images/test.jpg"
    result = parser.parse_fxtwitter_response(fx_payload(author={"name": "Fixture", "avatar_url": avatar}), POST_ID)
    assert result.author.avatar == Path("/synthetic/photo.jpg")
    parser.downloader.download_img.assert_called_once_with(
        avatar, headers=modules.base.COMMON_HEADER, proxy=parser.cfg.proxy,
    )


@pytest.mark.parametrize("change", [
    {"code": 403},
    {"code": "200"},
    {"status": None},
    {"status": []},
    {"status": {"type": "tombstone", "id": POST_ID}},
    {"status": {"type": "status", "id": "999", "text": "Synthetic"}},
    {"status": {"type": "status", "id": POST_ID, "text": 123}},
])
def test_fx_invalid_identity_or_payload_fails_before_download(parser, modules, change):
    payload = fx_payload(media={"all": [photo()]})
    payload.update(copy.deepcopy(change))
    with pytest.raises(modules.error.ParseException):
        parser.parse_fxtwitter_response(payload, POST_ID)
    assert_no_downloads(parser)


@pytest.mark.parametrize("url", [
    "http://pbs.twimg.com/media/test.jpg",
    "file:///tmp/test.jpg",
    "https://127.0.0.1/test.jpg",
    "https://pbs.twimg.com.evil.invalid/test.jpg",
    "https://evil.pbs.twimg.com/test.jpg",
    "https://user:synthetic_secret@pbs.twimg.com/test.jpg",
    "https://pbs.twimg.com:444/test.jpg",
    "https://pbs.twimg.com/test.jpg#fragment",
    "https://pbs.twimg.com/test image.jpg",
    "https://pbs.twimg.com/test\n.jpg",
    " https://pbs.twimg.com/test.jpg",
    "https://pbs.twimg.com/test.jpg ",
])
def test_fx_untrusted_photo_is_rejected_before_any_media_task(parser, modules, url):
    media = {"all": [photo(), photo(url)]}
    with pytest.raises(modules.error.ParseException):
        parser.parse_fxtwitter_response(fx_payload(media=media), POST_ID)
    assert_no_downloads(parser)


@pytest.mark.parametrize("url", [
    "https://pbs.twimg.com/test.mp4",
    "https://video.twimg.com.evil.invalid/test.mp4",
    "https://user:synthetic_secret@video.twimg.com/test.mp4",
    "https://video.twimg.com:444/test.mp4",
    "https://video.twimg.com/test.m3u8",
    "https://video.twimg.com/test.mp4.exe",
    "https://video.twimg.com/test.mp4#fragment",
    "http://video.twimg.com/test.mp4",
])
def test_fx_untrusted_video_is_rejected_before_any_media_task(parser, modules, url):
    media = {"all": [photo(), video(url)]}
    with pytest.raises(modules.error.ParseException):
        parser.parse_fxtwitter_response(fx_payload(media=media), POST_ID)
    assert_no_downloads(parser)


def test_fx_https_443_and_signed_media_queries_remain_usable(parser):
    url = "https://video.twimg.com:443/test.mp4?tag=fixture&expires=1"
    parser.parse_fxtwitter_response(fx_payload(media={"all": [video(url)]}), POST_ID)
    assert parser.downloader.download_video.call_args.args[0] == url


@pytest.mark.parametrize("media", [
    [],
    "invalid",
    {"all": "invalid"},
    {"all": {}},
    {"all": [None]},
    {"all": [{"type": "unknown", "url": PHOTO}]},
    {"all": [photo()] * 17},
    {"photos": "invalid"},
    {"videos": "invalid"},
])
def test_fx_malformed_or_excessive_media_fails_before_download(parser, modules, media):
    with pytest.raises(modules.error.ParseException):
        parser.parse_fxtwitter_response(fx_payload(media=media), POST_ID)
    assert_no_downloads(parser)


def test_fx_whole_media_batch_is_validated_before_avatar_download(parser, modules):
    payload = fx_payload(
        media={"all": [photo(), video("https://private.invalid/video.mp4")]},
        author={"name": "Fixture", "avatar_url": "https://pbs.twimg.com/profile_images/test.jpg"},
    )
    with pytest.raises(modules.error.ParseException):
        parser.parse_fxtwitter_response(payload, POST_ID)
    assert_no_downloads(parser)


@pytest.mark.parametrize("text", ["", "  \n\t"])
def test_fx_no_text_and_no_media_is_not_success(parser, modules, text):
    with pytest.raises(modules.error.ParseException):
        parser.parse_fxtwitter_response(fx_payload(text=text), POST_ID)
    assert_no_downloads(parser)


def test_fx_quoted_or_thread_content_is_not_downloaded(parser):
    payload = fx_payload()
    payload["status"]["quote"] = fx_payload(media={"all": [photo()]})["status"]
    payload["status"]["thread"] = [fx_payload(media={"all": [video()]})["status"]]
    result = parser.parse_fxtwitter_response(payload, POST_ID)
    assert result.text == "Synthetic post text"
    assert result.contents == []
    assert_no_downloads(parser)


@pytest.mark.asyncio
async def test_primary_title_without_media_keeps_existing_result(parser):
    parser._session = FakeSession(FakeResponse({
        "status": "ok", "statusCode": 200, "msg": "", "data": "<h3>Only a title</h3>",
    }))
    result = await parser._parse(match_url(parser))
    assert result.title == "Only a title"
    assert len(parser._session.calls) == 1
    assert_no_downloads(parser)


@pytest.mark.asyncio
async def test_fallback_transport_failure_is_not_retried(parser, modules):
    parser._session = FakeSession(
        FakeResponse({"status": "ok", "data": None}),
        FakeResponse(error=modules.twitter.ClientError("synthetic_secret")),
    )
    with pytest.raises(modules.error.ParseException) as failure:
        await parser._parse(match_url(parser))
    assert "synthetic_secret" not in str(failure.value)
    assert len(parser._session.calls) == 2
    assert_no_downloads(parser)


@pytest.mark.parametrize("tweet_id", [
    "1", "", "123/status/456", "123?token=synthetic_secret", " 123", "123\n", "9" * 21,
])
@pytest.mark.asyncio
async def test_fallback_endpoint_rejects_noncanonical_id_before_http(parser, modules, tweet_id):
    parser._request_json = AsyncMock()
    with pytest.raises(modules.error.ParseException):
        await parser._req_fxtwitter_api(tweet_id)
    parser._request_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_http_wrapper_uses_only_fixed_endpoint_and_clean_headers(parser, modules):
    parser._request_json = AsyncMock(return_value=fx_payload())
    await parser._req_fxtwitter_api(POST_ID)
    parser._request_json.assert_awaited_once_with(
        "GET", f"https://api.fxtwitter.com/2/status/{POST_ID}",
        headers={**modules.base.COMMON_HEADER, "Accept": "application/json"},
    )


def test_fx_missing_media_field_is_supported(parser):
    result = parser.parse_fxtwitter_response(fx_payload(), POST_ID)
    assert result.text == "Synthetic post text"
    assert_no_downloads(parser)


@pytest.mark.parametrize("duration", [-1, float("nan"), float("inf"), True, "3", None, 86401])
def test_fx_invalid_video_duration_is_normalized_to_zero(parser, duration):
    result = parser.parse_fxtwitter_response(
        fx_payload(media={"all": [video(duration=duration)]}), POST_ID,
    )
    assert result.contents[0].duration == 0


@pytest.mark.parametrize("thumbnail", [
    "https://private.invalid/test.jpg",
    "http://pbs.twimg.com/test.jpg",
    "https://pbs.twimg.com:444/test.jpg",
    "https://pbs.twimg.com/test.jpg#fragment",
])
def test_fx_invalid_optional_video_cover_does_not_block_media(parser, thumbnail):
    item = video()
    item["thumbnail_url"] = thumbnail
    result = parser.parse_fxtwitter_response(fx_payload(media={"all": [item]}), POST_ID)
    assert result.contents[0].cover is None
    parser.downloader.download_video.assert_called_once()
    parser.downloader.download_img.assert_not_called()


def test_fx_sixteen_media_items_are_supported(parser):
    result = parser.parse_fxtwitter_response(fx_payload(media={"all": [photo()] * 16}), POST_ID)
    assert len(result.contents) == 16
    assert parser.downloader.download_img.call_count == 16


def test_fx_direct_mode_preserves_network_setting_for_media(parser, modules):
    parser.cfg.parser.twitter.use_proxy = False
    parser.parse_fxtwitter_response(fx_payload(media={"all": [photo(), video()]}), POST_ID)
    for method in (parser.downloader.download_img, parser.downloader.download_video):
        for call in method.call_args_list:
            assert call.kwargs["proxy"] is None
            assert call.kwargs["headers"] == modules.base.COMMON_HEADER


@pytest.mark.asyncio
async def test_text_only_result_reaches_native_sender_without_media_or_rendering():
    from astrbot.core.message.components import Plain
    from astrbot_plugin_parser.core.parsers.twitter import TwitterParser
    from astrbot_plugin_parser.core.sender import MessageSender

    parser = object.__new__(TwitterParser)
    result = parser.parse_fxtwitter_response(fx_payload(), POST_ID)
    event = SimpleNamespace(send=AsyncMock(), chain_result=lambda segments: segments)
    renderer = SimpleNamespace(render_card=AsyncMock())
    config = SimpleNamespace(
        single_heavy_render_card=False, forward_threshold=10,
        show_download_fail_tip=False, audio_to_file=True,
    )

    await MessageSender(config, renderer).send_parse_result(event, result)

    event.send.assert_awaited_once()
    segments = event.send.await_args.args[0]
    assert all(isinstance(segment, Plain) for segment in segments)
    assert "Synthetic post text" in "".join(segment.text for segment in segments)
    renderer.render_card.assert_not_awaited()

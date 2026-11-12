"""Incomplete metadata or downloads must not produce a partial PDF."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from astrbot_plugin_parser.core.exception import ParseException
from astrbot_plugin_parser.core.parsers.pixiv import PixivHelper, PixivParser


def test_page_order_is_preserved():
    pages = [
        {"urls": {"original": f"https://images.example.invalid/{n}.jpg"}}
        for n in range(3)
    ]
    assert PixivParser._page_image_urls(pages, 3) == [
        f"https://images.example.invalid/{n}.jpg" for n in range(3)
    ]


@pytest.mark.parametrize("pages,count", [
    ([], 0), ([], 1),
    ([{"urls": {"original": "https://images.example.invalid/1.jpg"}}], 2),
    ([{"urls": {}}], 1), ([{"urls": {"original": None}}], 1),
    ([None], 1), ({"error": "synthetic"}, 1),
])
def test_incomplete_page_metadata_is_rejected(pages, count):
    with pytest.raises(ParseException):
        PixivParser._page_image_urls(pages, count)


def test_failed_page_preserves_previous_pdf(tmp_path):
    existing = tmp_path / "pixiv_123.pdf"
    existing.write_bytes(b"previous complete document")
    parser = PixivParser.__new__(PixivParser)
    parser.cfg = SimpleNamespace(cache_dir=tmp_path)

    async def run():
        async def downloaded():
            return [tmp_path / "page1.jpg", tmp_path / "page3.jpg"]
        with pytest.raises(ParseException):
            await parser._build_pdf(asyncio.create_task(downloaded()), "123", 3)

    with patch.object(PixivHelper, "imgs_to_pdf") as convert:
        asyncio.run(run())
    convert.assert_not_called()
    assert existing.read_bytes() == b"previous complete document"


def test_complete_pages_are_forwarded_in_order(tmp_path):
    paths = [tmp_path / "page1.jpg", tmp_path / "page2.jpg"]
    for path in paths:
        Image.new("RGB", (2, 2)).save(path)
    parser = PixivParser.__new__(PixivParser)
    parser.cfg = SimpleNamespace(cache_dir=tmp_path)

    async def run():
        async def downloaded():
            return paths
        return await parser._build_pdf(asyncio.create_task(downloaded()), "123", 2)

    def convert_pages(actual_paths, output):
        assert actual_paths == paths
        output.write_bytes(b"complete synthetic PDF")

    with patch.object(PixivHelper, "imgs_to_pdf", side_effect=convert_pages) as convert:
        output = asyncio.run(run())
    assert output == tmp_path / "pixiv_123.pdf"
    convert.assert_called_once()
    assert convert.call_args.args[1] != output
    assert output.read_bytes() == b"complete synthetic PDF"


def test_partial_cached_image_is_removed_and_old_pdf_preserved(tmp_path):
    path = tmp_path / "partial.jpg"
    path.write_bytes(b"incomplete image")
    output = tmp_path / "pixiv_123.pdf"
    output.write_bytes(b"previous complete PDF")
    parser = PixivParser.__new__(PixivParser)
    parser.cfg = SimpleNamespace(cache_dir=tmp_path)

    async def run():
        async def downloaded():
            return [path]
        with pytest.raises(ParseException, match="缓存不完整"):
            await parser._build_pdf(asyncio.create_task(downloaded()), "123", 1)

    with patch.object(PixivHelper, "imgs_to_pdf") as convert:
        asyncio.run(run())
    convert.assert_not_called()
    assert not path.exists()
    assert output.read_bytes() == b"previous complete PDF"


def test_concurrent_conversion_publishes_only_complete_unique_outputs(tmp_path):
    path = tmp_path / "page.jpg"
    Image.new("RGB", (2, 2)).save(path)
    output = tmp_path / "pixiv_123.pdf"
    output.write_bytes(b"previous complete PDF")
    parser = PixivParser.__new__(PixivParser)
    parser.cfg = SimpleNamespace(cache_dir=tmp_path)
    barrier = threading.Barrier(2)
    targets = []

    def convert(_paths, target):
        targets.append(target)
        target.write_bytes(b"partial")
        barrier.wait(timeout=5)
        assert output.read_bytes() in (b"previous complete PDF", b"complete synthetic PDF")
        target.write_bytes(b"complete synthetic PDF")

    async def run():
        async def downloaded():
            return [path]
        return await asyncio.gather(*(
            parser._build_pdf(asyncio.create_task(downloaded()), "123", 1)
            for _ in range(2)
        ))

    with patch.object(PixivHelper, "imgs_to_pdf", side_effect=convert):
        assert asyncio.run(run()) == [output, output]
    assert len(set(targets)) == 2
    assert output.read_bytes() == b"complete synthetic PDF"
    assert not list(tmp_path.glob(".pixiv_123-*"))


def test_cancel_waits_for_conversion_worker_and_cleans_temporary_files(tmp_path):
    path = tmp_path / "page.jpg"
    Image.new("RGB", (2, 2)).save(path)
    parser = PixivParser.__new__(PixivParser)
    parser.cfg = SimpleNamespace(cache_dir=tmp_path)
    started, release = threading.Event(), threading.Event()

    def convert(_paths, target):
        target.write_bytes(b"partial")
        started.set()
        assert release.wait(timeout=5)
        target.write_bytes(b"complete synthetic PDF")

    async def run():
        async def downloaded():
            return [path]
        task = asyncio.create_task(parser._build_pdf(asyncio.create_task(downloaded()), "123", 1))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    with patch.object(PixivHelper, "imgs_to_pdf", side_effect=convert):
        asyncio.run(run())
    assert (tmp_path / "pixiv_123.pdf").read_bytes() == b"complete synthetic PDF"
    assert not list(tmp_path.glob(".pixiv_123-*"))


def test_conversion_failure_does_not_replace_previous_pdf(tmp_path):
    path = tmp_path / "page.jpg"
    Image.new("RGB", (2, 2)).save(path)
    output = tmp_path / "pixiv_123.pdf"
    output.write_bytes(b"previous complete PDF")
    parser = PixivParser.__new__(PixivParser)
    parser.cfg = SimpleNamespace(cache_dir=tmp_path)

    def convert(_paths, target):
        target.write_bytes(b"partial")
        raise OSError("synthetic conversion failure")

    async def run():
        async def downloaded():
            return [path]
        with pytest.raises(OSError, match="synthetic conversion failure"):
            await parser._build_pdf(asyncio.create_task(downloaded()), "123", 1)

    with patch.object(PixivHelper, "imgs_to_pdf", side_effect=convert):
        asyncio.run(run())
    assert output.read_bytes() == b"previous complete PDF"
    assert not list(tmp_path.glob(".pixiv_123-*"))

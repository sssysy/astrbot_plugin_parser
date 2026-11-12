"""Keep backup URLs for the selected Bilibili representation."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from astrbot.api import logger

from ...exception import DownloadException, DownloadLimitException
from ...utils import generate_file_name, merge_av, safe_unlink

if TYPE_CHECKING:
    from ...download import Downloader


BLOCKED_PCDN_DOMAINS = (
    "mcdn.bilivideo.com",
    "mcdn.bilivideo.cn",
    "szbdyd.com",
    "mountaintoys.cn",
    "nexusedgeio.com",
    "ahdohpiechei.com",
)
MAX_CANDIDATES = 6


def _is_pcdn_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == domain or host.endswith("." + domain) for domain in BLOCKED_PCDN_DOMAINS
    )


def _urls(record: Mapping[str, Any]) -> list[str]:
    urls = [record.get(key) for key in ("baseUrl", "base_url", "url")]
    for key in ("backupUrl", "backup_url"):
        backup = record.get(key)
        if isinstance(backup, str):
            urls.append(backup)
        elif isinstance(backup, (list, tuple)):
            urls.extend(backup)
    return [url for url in urls if isinstance(url, str)]


def _same_representation(stream: Any, record: Mapping[str, Any]) -> bool:
    # The detector discards backups. Match its exact primary URL so other
    # qualities/codecs cannot accidentally become CDN alternatives.
    if stream.url not in (
        record.get("baseUrl"),
        record.get("base_url"),
        record.get("url"),
    ):
        return False
    quality = getattr(stream, "video_quality", getattr(stream, "audio_quality", None))
    if quality is not None and record.get("id") is not None:
        if str(record["id"]) != str(quality.value):
            return False
    codec = getattr(stream, "video_codecs", None)
    raw_codec = record.get("codecs")
    if codec is not None and isinstance(raw_codec, str):
        normalized = raw_codec.replace("hvc1", "hev")
        if codec.value not in normalized:
            return False
    return True


def stream_candidates(
    stream: Any, download_data: Mapping[str, Any], filter_pcdn: bool
) -> tuple[str, ...]:
    """Return primary and backup URLs without changing stream selection."""
    urls = [stream.url]
    # Keep compatibility with detector versions which retain backups themselves.
    urls.extend(
        _urls({key: getattr(stream, key, None) for key in ("backupUrl", "backup_url")})
    )
    dash = download_data.get("dash") or {}
    records: list[Any] = list(download_data.get("durl") or [])
    if isinstance(dash, Mapping):
        for key in ("video", "audio"):
            records.extend(dash.get(key) or [])
        for key in ("flac", "dolby"):
            special = dash.get(key) or {}
            if isinstance(special, Mapping):
                audio = special.get("audio")
                if isinstance(audio, Mapping):
                    records.append(audio)
                elif isinstance(audio, list):
                    records.extend(audio)
    for record in records:
        if isinstance(record, Mapping) and _same_representation(stream, record):
            urls.extend(_urls(record))
    unique: list[str] = []
    for url in urls:
        if not isinstance(url, str) or url in unique:
            continue
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                continue
        except ValueError:
            continue
        unique.append(url)
    if filter_pcdn:
        clean = [url for url in unique if not _is_pcdn_url(url)]
        # Preserve the previous all-PCDN fallback, but do not reintroduce blocked
        # PCDN hosts when the API supplied ordinary CDN alternatives.
        unique = clean or unique
    return tuple(unique[:MAX_CANDIDATES])


async def download_candidates(
    downloader: "Downloader",
    candidates: tuple[str, ...],
    *,
    headers: dict[str, str],
    proxy: str | None,
    file_name: str | None = None,
    default_suffix: str = "",
) -> Path:
    """Try each CDN once; size and duration limits must remain final."""
    if not candidates:
        raise DownloadException("未找到可下载的媒体链接")
    if file_name is None:
        file_name = generate_file_name(candidates[0], default_suffix)
    for index, url in enumerate(candidates):
        try:
            return await downloader.streamd(
                url, file_name=file_name, headers=headers, proxy=proxy, retry_times=0
            )
        except DownloadLimitException:
            raise
        except DownloadException:
            if index == len(candidates) - 1:
                raise
            # Log only the host: signed download URLs contain temporary tokens.
            logger.warning(
                f"B站媒体 CDN {urlparse(url).hostname} 下载失败，尝试备用 CDN "
                f"({index + 2}/{len(candidates)})"
            )
    raise DownloadException("媒体下载失败")


async def download_av_candidates(
    downloader: "Downloader",
    video_urls: tuple[str, ...],
    audio_urls: tuple[str, ...],
    *,
    output_path: Path,
    headers: dict[str, str],
    proxy: str | None,
) -> Path:
    tasks = [
        asyncio.create_task(
            download_candidates(
                downloader, urls, headers=headers, proxy=proxy, default_suffix=suffix
            )
        )
        for urls, suffix in ((video_urls, ".mp4"), (audio_urls, ".mp3"))
    ]
    try:
        video_path, audio_path = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    try:
        await merge_av(v_path=video_path, a_path=audio_path, output_path=output_path)
    except BaseException:
        await safe_unlink(output_path)
        raise
    return output_path

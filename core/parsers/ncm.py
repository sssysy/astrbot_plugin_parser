from re import Match
from typing import ClassVar
from urllib.parse import urlsplit

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Platform
from ..download import Downloader
from .base import BaseParser, ParseException, handle


class NCMParser(BaseParser):
    """网易云音乐解析器"""

    platform: ClassVar[Platform] = Platform(name="ncm", display_name="网易云")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": "https://music.163.com"})
        self.mycfg = config.parser.ncm
        self.cookiejar = CookieJar(config, self.mycfg, domain="music.163.com")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    @handle("163cn.tv", r"163cn\.tv/(?P<short_key>\w+)")
    async def _parse_short(self, searched: Match[str]):
        short_url = f"https://163cn.tv/{searched.group('short_key')}"
        # 让框架跟随 302 后再走通用解析
        return await self.parse_with_redirect(short_url)

    @handle("y.music.163.com", r"y\.music\.163\.com/m/song\?.*id=(?P<song_id>\d+)")
    @handle("music.163.com", r"music\.163\.com/#/song\?.*id=(?P<song_id>\d+)")
    async def _parse_song(self, searched: Match[str]):
        song_id = searched.group("song_id")
        detail_url = (
            f"https://music.163.com/api/song/detail/?id={song_id}&ids=[{song_id}]"
        )
        play_url = f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=320000"

        # 1. 取歌曲元数据
        async with self.session.get(detail_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ParseException(f"[NCM] 获取歌曲信息失败 HTTP {resp.status}")
            detail_json = await resp.json()

        songs = detail_json.get("songs") if isinstance(detail_json, dict) else None
        if (
            not isinstance(songs, list)
            or not songs
            or not isinstance(songs[0], dict)
            or not songs[0]
        ):
            raise ParseException("[NCM] 未找到该歌曲，可能已下架或无权访问")
        song = songs[0]

        title = song.get("name", "")
        sub_title = (song.get("alias") or [""])[0]  # 没有别名时 API 返回空列表
        album_name = song.get("album", {}).get("name", "")
        cover_url = song.get("album", {}).get("picUrl", "") + "?param=640y640"
        duration_ms = song.get("duration", 0)

        # 作者信息
        ar_list = song.get("artists", [])
        author_name = " / ".join(ar.get("name", "") for ar in ar_list)
        author_avatar = ar_list[0].get("img1v1Url", "") if ar_list else ""

        # 2. 取播放地址
        async with self.session.get(play_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ParseException(f"[NCM] 获取播放地址失败 HTTP {resp.status}")
            play_json = await resp.json()
        play_data = play_json.get("data") if isinstance(play_json, dict) else None
        if (
            not isinstance(play_data, list)
            or not play_data
            or not isinstance(play_data[0], dict)
        ):
            raise ParseException("[NCM] 未获取到播放地址，歌曲可能已下架或需要登录/会员权限")
        audio_url = play_data[0].get("url")
        try:
            parsed_url = urlsplit(audio_url) if isinstance(audio_url, str) else None
            valid_url = bool(
                parsed_url and parsed_url.scheme in ("http", "https")
                and parsed_url.hostname and parsed_url.port != 0
                and parsed_url.username is None and parsed_url.password is None
                and not any(char.isspace() or ord(char) < 32 for char in audio_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ParseException("[NCM] 未获取到播放地址，歌曲可能已下架或需要登录/会员权限")

        # 3. 组装结果
        author = self.create_author(author_name, author_avatar)
        audio = self.create_video_content(
            audio_url, cover_url, duration=duration_ms // 1000
        )

        # 4. 返回
        return self.result(
            title=f"{title}{'（' + sub_title + '）' if sub_title else ''}",
            text=f"专辑：{album_name}",
            author=author,
            contents=[audio],
            timestamp=None,
            url=f"https://music.163.com/#/song?id={song_id}",
        )

    # 3. 直链 mp3 —— 直接下载
    @handle("music.126.net", r"https?://[^/]*music\.126\.net/.*\.mp3(?:\?.*)?$")
    async def _parse_direct_mp3(self, searched: Match[str]):
        url = searched.group(0)  # 整条 url
        audio = self.create_audio_content(url)
        return self.result(
            title="网易云音乐",
            text="直链音频",
            contents=[audio],
            url=url,
        )

    @handle(
        "music.163.com/song/media/outer/url",
        r"(https?://music\.163\.com/song/media/outer/url\?[^>\s]+)",
    )
    async def _parse_private_outer(self, searched: Match[str]):
        # 整条原始 URL 就是直链
        private_url = searched.group(0)
        print(private_url)
        audio = self.create_audio_content(private_url)
        return self.result(
            title="网易云音乐（私人直链）",
            text="直链音频",
            contents=[audio],
            url=private_url,
        )

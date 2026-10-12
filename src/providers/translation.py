"""Fetch published original/Chinese lyric pairs from NetEase and QQ Music."""
import base64
import html
import logging
import re

import httpx

from ..scanner import MusicFile

logger = logging.getLogger(__name__)


def normalize(text: str) -> str:
    return "".join(char for char in text.casefold() if char.isalnum())


class TranslationProvider:
    def __init__(self):
        self.client = httpx.Client(timeout=15.0, follow_redirects=True)

    def get_lyrics(self, source: str, music_file: MusicFile) -> tuple[str, str] | None:
        """Match title and artist before fetching the platform's lyric pair."""
        if not music_file.title or not music_file.artist:
            return None
        try:
            songs = self._search(source, f"{music_file.artist} {music_file.title}")
            artists = {normalize(part) for part in re.split(r"[/,;&、]", music_file.artist) if part.strip()}
            matches = [song for song in songs
                       if normalize(song["title"]) == normalize(music_file.title)
                       and artists.intersection(normalize(artist) for artist in song["artists"])]
            matches.sort(key=lambda song: normalize(song["album"]) != normalize(music_file.album))
            # shortcut: try at most three matching releases, increase if alternate releases are commonly missed.
            for song in matches[:3]:
                pair = self._lyrics(source, song["id"])
                if pair and all(isinstance(lyrics, str) and lyrics for lyrics in pair):
                    return pair
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("%s translation unavailable for %s: %s", source, music_file.title, exc)
        return None

    def _search(self, source: str, keyword: str) -> list[dict]:
        if source == "netease":
            response = self.client.post(
                "https://music.163.com/api/search/get/web",
                data={"s": keyword, "type": 1, "limit": 20, "offset": 0},
                headers={"Referer": "https://music.163.com/"},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 200:
                return []
            return [{"id": str(song["id"]), "title": song["name"],
                     "artists": [artist["name"] for artist in song["artists"]],
                     "album": song["album"]["name"]}
                    for song in data.get("result", {}).get("songs", [])]
        response = self.client.post(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            json={"comm": {"ct": 24, "cv": 0}, "request": {
                "module": "music.search.SearchCgiService", "method": "DoSearchForQQMusicDesktop",
                "param": {"query": keyword, "search_type": 0, "num_per_page": 20, "page_num": 1},
            }},
            headers={"Referer": "https://y.qq.com/"},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") == 0 and data.get("request", {}).get("code") == 0:
            songs = data["request"].get("data", {}).get("body", {}).get("song", {}).get("list", [])
            if songs:
                return [{"id": song["mid"], "title": html.unescape(song["title"]),
                         "artists": [html.unescape(artist["name"]) for artist in song["singer"]],
                         "album": html.unescape(song["album"]["name"])} for song in songs]
        response = self.client.get(
            "https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg",
            params={"key": keyword, "format": "json"},
            headers={"Referer": "https://y.qq.com/"},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            return []
        return [{"id": song["mid"], "title": html.unescape(song["name"]),
                 "artists": html.unescape(song["singer"]).split("/"), "album": ""}
                for song in data.get("data", {}).get("song", {}).get("itemlist", [])]

    def _lyrics(self, source: str, song_id: str) -> tuple[str, str] | None:
        if source == "netease":
            response = self.client.get(
                "https://music.163.com/api/song/lyric",
                params={"id": song_id, "lv": -1, "tv": -1, "rv": -1},
                headers={"Referer": "https://music.163.com/"},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 200:
                return None
            return (data.get("lrc", {}).get("lyric", ""),
                    data.get("tlyric", {}).get("lyric", ""))
        response = self.client.get(
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            params={"songmid": song_id, "format": "json", "nobase64": 0},
            headers={"Referer": "https://y.qq.com/"},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("retcode", data.get("code")) != 0:
            return None
        return tuple(html.unescape(base64.b64decode(data.get(key, ""), validate=True).decode("utf-8"))
                     for key in ("lyric", "trans"))

    def close(self):
        self.client.close()

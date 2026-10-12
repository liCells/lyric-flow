"""Bilingual merging, provider boundaries, and safe existing-file upgrades."""
import base64
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest

from src.bilingual import BilingualProcessor, parse_lrc
from src.config import Config
from src.main import LyricFlow
from src.metadata_handler import MetadataHandler
from src.providers.translation import TranslationProvider
from src.scanner import MusicFile

ORIGINAL = "[ar:Artist]\n[00:01.00]Hello\n[00:02.000]Goodbye\n"
TRANSLATION = "[00:01.050]你好\n[00:02]再见\n"


@pytest.fixture
def music(tmp_path):
    return MusicFile(tmp_path / "Song.strm", "Artist", "Song", "Album", is_strm=True)


@pytest.fixture
def config():
    return Config(bilingual_enabled=True, upgrade_existing_lrc=True, download_cover=False)


def test_config_env(monkeypatch):
    for key, value in {"BILINGUAL_ENABLED": "true", "BILINGUAL_LANGUAGE": "zh-CN",
                       "BILINGUAL_SOURCE": "qq", "UPGRADE_EXISTING_LRC": "true",
                       "OVERWRITE_LYRICS": "false"}.items():
        monkeypatch.setenv(key, value)
    config = Config.from_env()
    assert config.bilingual_enabled and config.upgrade_existing_lrc
    assert config.bilingual_language == "zh-CN" and config.bilingual_source == "qq"
    assert not config.overwrite_lyrics
    assert not Config().bilingual_enabled and not Config().upgrade_existing_lrc


@pytest.mark.parametrize("kwargs", [{"bilingual_language": "en"}, {"bilingual_source": "invalid"}])
def test_config_rejects_unsupported_values(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


def test_parser_multitag_offset_and_precision():
    entries = parse_lrc("\ufeff[offset:+100]\n[00:01.2][01:02:345]Hello\n[00:61.00]Invalid\n[ar:中文]\n")
    assert entries == [(1, 1300, "[00:01.2]", "Hello"), (1, 62445, "[01:02:345]", "Hello")]


def test_merge_preserves_tags_timestamps_and_original_lines():
    result = BilingualProcessor.merge(ORIGINAL, ORIGINAL, TRANSLATION)
    assert result == "[ar:Artist]\n[00:01.00]Hello\n[00:01.00]你好\n[00:02.000]Goodbye\n[00:02.000]再见\n"
    assert BilingualProcessor(Config()).needs_translation(result) is False


def test_merge_uses_remote_text_alignment_with_local_timing():
    local = "[offset:-200]\r\n[00:10.0][00:20.000]Hello"
    remote = "[offset:100]\n[00:01.00]Hello\n[00:02.00]Hello\n"
    translated = "[offset:100]\n[00:01.00]你好\n[00:02.00]你好\n"
    assert BilingualProcessor.merge(local, remote, translated) == (
        local + "\n[00:10.0]你好\n[00:20.000]你好\n"
    )


@pytest.mark.parametrize("remote,translated", [
    ("[00:01.00]Different\n[00:02.00]Goodbye\n", TRANSLATION),
    (ORIGINAL, "[00:03.00]错位\n"),
    (ORIGINAL, "[00:01.00]English only\n"),
    (ORIGINAL, "plain text 你好"),
    ("[00:01.00]Hello\n[00:01.05]Goodbye\n", "[00:01.04]歧义\n"),
])
def test_merge_rejects_wrong_versions_invalid_or_ambiguous_translations(remote, translated):
    assert BilingualProcessor.merge(ORIGINAL, remote, translated) == ORIGINAL


@pytest.mark.parametrize("lyrics", [
    "[00:01.00]中文歌", "[00:01.00]Hello\n[00:01.00]你好", "[00:01.00]こんにちは",
    "[00:01.00]한국어", "plain lyrics", "[00:01.00]", "[ar:English]\n[00:01.00]123",
])
def test_ineligible_lyrics_skip_network(config, music, lyrics):
    processor = BilingualProcessor(config)
    with patch("src.bilingual.TranslationProvider") as provider:
        assert processor.process(lyrics, music) == lyrics
        provider.assert_not_called()


def test_chinese_credits_do_not_block_english(config, music):
    processor = BilingualProcessor(config)
    assert processor.needs_translation("[ar:中文歌手]\n[00:00]作词：作者\n" + ORIGINAL)


def test_disabled_skips_network(music):
    with patch("src.bilingual.TranslationProvider") as provider:
        assert BilingualProcessor(Config()).process(ORIGINAL, music) == ORIGINAL
        provider.assert_not_called()


def test_sources_fall_back_when_first_translation_does_not_align(config, music):
    processor = BilingualProcessor(config)
    processor.provider = Mock()
    processor.provider.get_lyrics.side_effect = [(ORIGINAL, "[00:30]错误"), (ORIGINAL, TRANSLATION)]
    assert "你好" in processor.process(ORIGINAL, music)
    assert [call.args[0] for call in processor.provider.get_lyrics.call_args_list] == ["netease", "qq"]
    processor.close()
    assert processor.provider is None


def test_explicit_source_and_no_translation(config, music):
    config.bilingual_source = "qq"
    processor = BilingualProcessor(config)
    processor.provider = Mock()
    processor.provider.get_lyrics.return_value = None
    assert processor.process(ORIGINAL, music) == ORIGINAL
    processor.provider.get_lyrics.assert_called_once_with("qq", music)


@pytest.mark.parametrize("source", ["netease", "qq"])
def test_provider_fetches_and_decodes_published_translation(source, music):
    def respond(request):
        if "search/get" in request.url.path:
            return httpx.Response(200, json={"code": 200, "result": {"songs": [
                {"id": 123, "name": "Song", "artists": [{"name": "Artist"}], "album": {"name": "Album"}}
            ]}})
        if "musicu" in request.url.path:
            return httpx.Response(200, json={"code": 0, "request": {"code": 0, "data": {"body": {"song": {"list": [
                {"mid": "abc", "title": "Song", "singer": [{"name": "Artist"}], "album": {"name": "Album"}}
            ]}}}}})
        if "music.163.com" == request.url.host:
            assert request.url.params["id"] == "123"
            return httpx.Response(200, json={"code": 200, "lrc": {"lyric": ORIGINAL}, "tlyric": {"lyric": TRANSLATION}})
        assert request.url.params["songmid"] == "abc"
        assert request.headers["Referer"] == "https://y.qq.com/"
        return httpx.Response(200, json={"retcode": 0, "lyric": base64.b64encode(ORIGINAL.encode()).decode(),
                                         "trans": base64.b64encode(TRANSLATION.encode()).decode()})
    provider = TranslationProvider()
    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        assert provider.get_lyrics(source, music) == (ORIGINAL, TRANSLATION)
    finally:
        provider.close()


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("offline"), ValueError("invalid JSON"), TypeError("malformed response"),
])
def test_provider_failures_return_none(failure, music):
    provider = TranslationProvider()
    try:
        with patch.object(provider, "_search", side_effect=failure):
            assert provider.get_lyrics("netease", music) is None
    finally:
        provider.close()


@pytest.mark.parametrize("payload", [
    {"code": 200, "lrc": {"lyric": None}, "tlyric": {"lyric": TRANSLATION}},
    {"retcode": 0, "lyric": "invalid base64!", "trans": "invalid"},
])
def test_malformed_lyric_payload_falls_back(payload, music):
    provider = TranslationProvider()
    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    song = {"id": "123", "title": "Song", "artists": ["Artist"], "album": "Album"}
    try:
        with patch.object(provider, "_search", return_value=[song]):
            source = "netease" if payload.get("code") == 200 else "qq"
            assert provider.get_lyrics(source, music) is None
    finally:
        provider.close()


def test_qq_empty_search_falls_back_to_suggestions(music):
    def respond(request):
        if "musicu" in request.url.path:
            return httpx.Response(200, json={"code": 0, "request": {"code": 0, "data": {"body": {"song": {"list": []}}}}})
        if "smartbox" in request.url.path:
            return httpx.Response(200, json={"code": 0, "data": {"song": {"itemlist": [
                {"mid": "abc", "name": "Song", "singer": "Artist/Guest"}
            ]}}})
        return httpx.Response(200, json={"retcode": 0, "lyric": base64.b64encode(ORIGINAL.encode()).decode(),
                                         "trans": base64.b64encode(TRANSLATION.encode()).decode()})
    provider = TranslationProvider()
    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        assert provider.get_lyrics("qq", music) == (ORIGINAL, TRANSLATION)
    finally:
        provider.close()


def test_provider_rejects_wrong_title_or_artist_and_prioritizes_album(music):
    provider = TranslationProvider()
    songs = [
        {"id": "wrong-title", "title": "Song Live", "artists": ["Artist"], "album": "Album"},
        {"id": "wrong-artist", "title": "Song", "artists": ["Other"], "album": "Album"},
        {"id": "alternate", "title": "Song", "artists": ["Artist"], "album": "Other"},
        {"id": "exact", "title": "Song", "artists": ["Artist"], "album": "Album"},
    ]
    try:
        with patch.object(provider, "_search", return_value=songs), patch.object(provider, "_lyrics", return_value=(ORIGINAL, TRANSLATION)) as fetch:
            assert provider.get_lyrics("netease", music) == (ORIGINAL, TRANSLATION)
            fetch.assert_called_once_with("netease", "exact")
    finally:
        provider.close()


def test_upgrade_is_separate_from_overwrite_and_preserves_permissions(config, music):
    handler = MetadataHandler(config)
    music.lyrics_path.write_text(ORIGINAL)
    music.lyrics_path.chmod(0o640)
    merged = BilingualProcessor.merge(ORIGINAL, ORIGINAL, TRANSLATION)
    assert handler.needs_processing(music) == (False, False)
    assert handler.save_lyrics(music, merged) is False
    assert handler.upgrade_lyrics(music, ORIGINAL, merged) is True
    assert music.lyrics_path.read_text() == merged
    assert music.lyrics_path.stat().st_mode & 0o777 == 0o640
    assert handler.upgrade_lyrics(music, ORIGINAL, merged) is False
    assert list(music.path.parent.glob(".Song.lrc.*")) == []


@pytest.mark.parametrize("replacement", [ORIGINAL, "[00:01]你好\n", ORIGINAL.replace("Hello", "Changed") + "[00:01]你好\n"])
def test_upgrade_rejects_unchanged_or_replaced_original(config, music, replacement):
    music.lyrics_path.write_text(ORIGINAL)
    assert MetadataHandler(config).upgrade_lyrics(music, ORIGINAL, replacement) is False
    assert music.lyrics_path.read_text() == ORIGINAL


def test_upgrade_write_failure_leaves_original_and_cleans_temp(config, music):
    music.lyrics_path.write_text(ORIGINAL)
    merged = BilingualProcessor.merge(ORIGINAL, ORIGINAL, TRANSLATION)
    with patch("src.metadata_handler.os.replace", side_effect=OSError("disk error")):
        assert MetadataHandler(config).upgrade_lyrics(music, ORIGINAL, merged) is False
    assert music.lyrics_path.read_text() == ORIGINAL
    assert list(music.path.parent.glob(".Song.lrc.*")) == []


def test_upgrade_detects_edit_during_write(config, music):
    music.lyrics_path.write_text(ORIGINAL)
    merged = BilingualProcessor.merge(ORIGINAL, ORIGINAL, TRANSLATION)
    with patch("src.metadata_handler.os.fsync", side_effect=lambda _: music.lyrics_path.write_text("user edit")):
        assert MetadataHandler(config).upgrade_lyrics(music, ORIGINAL, merged) is False
    assert music.lyrics_path.read_text() == "user edit"
    assert list(music.path.parent.glob(".Song.lrc.*")) == []


def test_invalid_encoding_and_symlinks_are_not_overwritten(config, music):
    handler = MetadataHandler(config)
    music.lyrics_path.write_bytes(b"\xff")
    assert handler.read_lyrics(music) is None
    music.lyrics_path.unlink()
    target = music.path.parent / "target.lrc"
    target.write_text(ORIGINAL)
    music.lyrics_path.symlink_to(target)
    assert handler.upgrade_lyrics(music, ORIGINAL, BilingualProcessor.merge(ORIGINAL, ORIGINAL, TRANSLATION)) is False
    assert target.read_text() == ORIGINAL


@pytest.mark.parametrize("option", ["bilingual_enabled", "upgrade_existing_lrc", "download_lyrics"])
def test_upgrade_requires_flags(config, music, option):
    setattr(config, option, False)
    music.lyrics_path.write_text(ORIGINAL)
    assert MetadataHandler(config).upgrade_lyrics(music, ORIGINAL, BilingualProcessor.merge(ORIGINAL, ORIGINAL, TRANSLATION)) is False
    assert music.lyrics_path.read_text() == ORIGINAL


def make_flow(config, music):
    with patch("src.main.get_provider") as factory:
        flow = LyricFlow(config)
    flow.scanner.scan = Mock(return_value=iter([music]))
    flow.bilingual.provider = Mock()
    flow.bilingual.provider.get_lyrics.return_value = (ORIGINAL, TRANSLATION)
    return flow


def test_flow_upgrades_local_without_original_search_or_download(config, music):
    music.lyrics_path.write_text(ORIGINAL)
    flow = make_flow(config, music)
    flow.run()
    assert "你好" in music.lyrics_path.read_text()
    assert flow.stats["lyrics_upgraded"] == 1
    flow.client.search.assert_not_called()
    flow.client.get_lyrics.assert_not_called()


def test_flow_download_merges_before_save_and_embed(config, music):
    config.update_lyrics = True
    music.is_strm = False
    flow = make_flow(config, music)
    flow.client.get_lyrics.return_value = ORIGINAL
    flow.embedder.embed_lyrics = Mock(return_value=True)
    with patch("src.main.time.sleep"):
        flow.run()
    saved = music.lyrics_path.read_text()
    assert "你好" in saved
    assert flow.stats["lyrics_saved"] == 1 and flow.stats["lyrics_updated"] == 1
    flow.embedder.embed_lyrics.assert_called_once_with(music, saved)


def test_flow_no_translation_leaves_existing_bytes_unchanged(config, music):
    content = ORIGINAL.replace("\n", "\r\n").encode()
    music.lyrics_path.write_bytes(content)
    flow = make_flow(config, music)
    flow.bilingual.provider.get_lyrics.return_value = None
    flow.run()
    assert music.lyrics_path.read_bytes() == content
    assert flow.stats["lyrics_upgraded"] == 0
    flow.client.get_lyrics.assert_not_called()


def test_flow_existing_bilingual_skips_translation_and_download(config, music):
    merged = BilingualProcessor.merge(ORIGINAL, ORIGINAL, TRANSLATION)
    music.lyrics_path.write_text(merged)
    flow = make_flow(config, music)
    provider = flow.bilingual.provider
    flow.run()
    assert music.lyrics_path.read_text() == merged
    provider.get_lyrics.assert_not_called()
    assert flow.bilingual.provider is None
    flow.client.get_lyrics.assert_not_called()

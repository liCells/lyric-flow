"""Add published Chinese translations while preserving local LRC lines."""
import logging
import re

from .config import Config
from .providers.translation import TranslationProvider, normalize
from .scanner import MusicFile

logger = logging.getLogger(__name__)
TIMESTAMP = re.compile(r"\[(\d+):([0-5]\d)(?:[.:](\d{1,3}))?\]")
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
CHINESE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
CREDIT = re.compile(r"^(?:作词|作曲|编曲|译者|翻译|词|曲|lyrics(?: by)?|music(?: by)?|written by|composer|arranger|translated by)\s*[:：]", re.I)


def parse_lrc(lyrics: str) -> list[tuple[int, int, str, str]]:
    """Return (line index, milliseconds including offset, original tag, text)."""
    offset_match = re.search(r"\[offset:([+-]?\d+)\]", lyrics, re.I)
    offset = int(offset_match.group(1)) if offset_match else 0
    entries = []
    for index, line in enumerate(lyrics.splitlines()):
        line = line.lstrip("\ufeff")
        tags = list(TIMESTAMP.finditer(line))
        if not tags or tags[0].start() != 0:
            continue
        text = line[tags[-1].end():].strip()
        if not text or CREDIT.match(text):
            continue
        for tag in tags:
            fraction = (tag.group(3) or "").ljust(3, "0")
            milliseconds = (int(tag.group(1)) * 60 + int(tag.group(2))) * 1000 + int(fraction)
            entries.append((index, milliseconds + offset, tag.group(), text))
    return sorted(entries, key=lambda entry: entry[1])


class BilingualProcessor:
    def __init__(self, config: Config):
        self.config = config
        self.provider = None

    def needs_translation(self, lyrics: str) -> bool:
        entries = parse_lrc(lyrics)
        # shortcut: only English LRC without CJK lyric lines is eligible, add language detection for mixed-language support.
        return bool(entries) and any(re.search(r"[a-zA-Z]", entry[3]) for entry in entries) and not any(
            CJK.search(entry[3]) for entry in entries
        )

    def process(self, lyrics: str, music_file: MusicFile) -> str:
        if not self.config.bilingual_enabled or not self.needs_translation(lyrics):
            return lyrics
        if self.provider is None:
            self.provider = TranslationProvider()
        sources = ("netease", "qq") if self.config.bilingual_source == "auto" else (self.config.bilingual_source,)
        for source in sources:
            pair = self.provider.get_lyrics(source, music_file)
            if pair:
                merged = self.merge(lyrics, *pair)
                if merged != lyrics:
                    logger.info("Added %s Chinese translation: %s", source, music_file.title)
                    return merged
        return lyrics

    @staticmethod
    def merge(lyrics: str, original: str, translation: str) -> str:
        local = parse_lrc(lyrics)
        remote = parse_lrc(original)
        if not local or [normalize(entry[3]) for entry in local] != [normalize(entry[3]) for entry in remote]:
            return lyrics
        additions = {}
        for _, timestamp, _, text in parse_lrc(translation):
            if not CHINESE.search(text):
                continue
            # shortcut: accept only unique timestamps within 100 ms, add explicit alignment for retimed translations.
            matches = [index for index, entry in enumerate(remote) if abs(entry[1] - timestamp) <= 100]
            if len(matches) != 1:
                continue
            index = matches[0]
            line_index, _, tag, _ = local[index]
            addition = f"{tag}{text}"
            if addition not in additions.setdefault(line_index, []):
                additions[line_index].append(addition)
        if not additions:
            return lyrics
        lines = lyrics.splitlines(keepends=True)
        output = []
        for index, line in enumerate(lines):
            output.append(line)
            if index in additions:
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                if not line.endswith(("\r", "\n")):
                    output.append(newline)
                output.extend(addition + newline for addition in additions[index])
        return "".join(output)

    def close(self):
        if self.provider is not None:
            self.provider.close()
            self.provider = None

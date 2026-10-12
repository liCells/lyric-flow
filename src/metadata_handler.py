# Metadata handler - saves lyrics and covers
import logging
import os
import tempfile
from pathlib import Path
from .bilingual import CHINESE, TIMESTAMP
from .config import Config
from .scanner import MusicFile

logger = logging.getLogger(__name__)


class MetadataHandler:
    """Handles saving lyrics and cover files."""
    
    def __init__(self, config: Config):
        self.config = config
    
    def save_lyrics(self, music_file: MusicFile, lyrics_content: str) -> bool:
        """
        Save lyrics content to .lrc file.
        Returns True if successful.
        """
        if not lyrics_content:
            return False
        
        lyrics_path = music_file.lyrics_path
        
        # Check if already exists
        if lyrics_path.exists() and not self.config.overwrite_lyrics:
            logger.debug(f"Lyrics already exists, skipping: {lyrics_path}")
            return False
        
        try:
            # Ensure content ends with newline
            if not lyrics_content.endswith("\n"):
                lyrics_content += "\n"
            
            lyrics_path.write_text(lyrics_content, encoding="utf-8")
            logger.info(f"✓ Saved lyrics: {lyrics_path.name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save lyrics {lyrics_path}: {e}")
            return False
    
    def read_lyrics(self, music_file: MusicFile) -> str | None:
        try:
            return music_file.lyrics_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            logger.warning("Cannot read existing lyrics %s: %s", music_file.lyrics_path, exc)
            return None

    def upgrade_lyrics(self, music_file: MusicFile, original: str, lyrics: str) -> bool:
        """Atomically add translations only if the original file is unchanged."""
        if not (self.config.download_lyrics and self.config.bilingual_enabled and self.config.upgrade_existing_lrc):
            return False
        if not original or lyrics == original:
            return False
        original_lines = iter(original.splitlines())
        expected = next(original_lines, None)
        added = False
        for line in lyrics.splitlines():
            if expected is not None and line == expected:
                expected = next(original_lines, None)
            elif TIMESTAMP.match(line) and CHINESE.search(line):
                added = True
            else:
                return False
        if expected is not None or not added:
            return False
        path = music_file.lyrics_path
        temporary = None
        try:
            if path.is_symlink() or path.read_bytes() != original.encode("utf-8"):
                logger.warning("Existing lyrics changed, skipping upgrade: %s", path)
                return False
            mode = path.stat().st_mode & 0o777
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(lyrics.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(mode)
            if path.is_symlink() or path.read_bytes() != original.encode("utf-8"):
                logger.warning("Existing lyrics changed, skipping upgrade: %s", path)
                return False
            # shortcut: the final check and replace are not locked, coordinate writers if other tools edit LRC concurrently.
            os.replace(temporary, path)
            logger.info("✓ Upgraded bilingual lyrics: %s", path.name)
            return True
        except OSError as exc:
            logger.error("Failed to upgrade lyrics %s: %s", path, exc)
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Cannot remove temporary lyrics %s: %s", temporary, exc)

    def save_cover(self, music_file: MusicFile, cover_data: bytes) -> bool:
        """
        Save cover image to cover.jpg in the album directory.
        Returns True if successful.
        """
        if not cover_data:
            return False
        
        cover_path = music_file.cover_path
        
        # Check if already exists
        if cover_path.exists() and not self.config.overwrite_cover:
            logger.debug(f"Cover already exists, skipping: {cover_path}")
            return False
        
        try:
            cover_path.write_bytes(cover_data)
            logger.info(f"✓ Saved cover: {cover_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save cover {cover_path}: {e}")
            return False
    
    def needs_processing(self, music_file: MusicFile) -> tuple[bool, bool]:
        """
        Check if a music file needs lyrics and/or cover download.
        Returns (needs_lyrics, needs_cover).
        """
        needs_lyrics = False
        needs_cover = False
        
        if self.config.download_lyrics:
            if not music_file.has_lyrics() or self.config.overwrite_lyrics:
                needs_lyrics = True
        
        if self.config.download_cover:
            if not music_file.has_cover() or self.config.overwrite_cover:
                needs_cover = True
        
        return needs_lyrics, needs_cover

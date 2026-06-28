import io
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pyexiv2
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# IPTC field lengths are spec-defined maximums
_MAX_IPTC_TITLE_LEN = 64
_MAX_IPTC_CAPTION_LEN = 2000


class WriteResult:
    """
    Result of a write_metadata() call.

    Deliberately usable as a plain bool (truthy iff ok) so existing code
    written against the old `bool`-returning write_metadata keeps working
    unchanged (`if self.meta.write_metadata(...):` still does the right
    thing), while new code can inspect `.reason` and `.rolled_back` for a
    meaningful error message (item #6 / item #7) instead of a generic
    failure.
    """

    __slots__ = ("ok", "reason", "rolled_back")

    def __init__(self, ok: bool, reason: str = "", rolled_back: bool = False):
        self.ok = ok
        self.reason = reason
        self.rolled_back = rolled_back

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f"WriteResult(ok={self.ok!r}, reason={self.reason!r}, rolled_back={self.rolled_back!r})"


class MetadataEngine:
    def __init__(self, config_manager):
        self.config = config_manager
        self.backup_enabled: bool = bool(self.config.get("image_engine", "backup_before_write"))
        self.thumb_size: int = int(self.config.get("image_engine", "thumbnail_max_size") or 256)
        self.supported_exts: list = self.config.get_supported_extensions()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_supported(self, image_path: str) -> bool:
        return Path(image_path).suffix.lower() in self.supported_exts

    def read_metadata(self, image_path: str) -> Dict[str, object]:
        """Read XMP/IPTC metadata; returns dict with title, description, keywords."""
        self._assert_exists(image_path)

        metadata: Dict[str, object] = {"title": "", "description": "", "keywords": []}

        try:
            with pyexiv2.Image(image_path) as img:
                xmp = img.read_xmp()
                iptc = img.read_iptc()

            # Prefer XMP, fallback to IPTC.
            # pyexiv2 returns lang-alt XMP fields (dc.title, dc.description) as
            # dicts like {"x-default": "value"} — extract the string value.
            raw_title = xmp.get("Xmp.dc.title") or iptc.get("Iptc.Application2.ObjectName") or ""
            raw_desc  = xmp.get("Xmp.dc.description") or iptc.get("Iptc.Application2.Caption") or ""
            metadata["title"]       = self._extract_lang_alt(raw_title)
            metadata["description"] = self._extract_lang_alt(raw_desc)

            raw_kw = xmp.get("Xmp.dc.subject") or iptc.get("Iptc.Application2.Keywords") or []
            metadata["keywords"] = self._normalise_keywords(raw_kw)

        except Exception as exc:
            logger.warning("Could not read metadata from %s: %s", image_path, exc)

        return metadata

    def write_metadata(
        self,
        image_path: str,
        title: str,
        description: str,
        keywords: List[str],
    ) -> "WriteResult":
        """
        Write metadata, then read it back and verify every field matches
        what was requested (item #6). If verification fails, the file is
        automatically rolled back to its pre-write backup so a partially-
        or incorrectly-written file is never left in place.

        Returns a WriteResult, which is also usable as a plain bool in
        existing `if success:` checks (truthy iff ok is True), so callers
        written against the old boolean-returning version keep working.
        """
        self._assert_exists(image_path)

        if not self.is_supported(image_path):
            msg = f"Unsupported file type: {Path(image_path).suffix}"
            logger.warning(msg)
            return WriteResult(False, msg)

        # Truncate to IPTC spec limits before writing
        title = title[:_MAX_IPTC_TITLE_LEN]
        description = description[:_MAX_IPTC_CAPTION_LEN]
        keywords = [str(k).strip() for k in keywords if k]

        backup_path: Optional[str] = None
        if self.backup_enabled:
            try:
                backup_path = self._create_backup(image_path)
            except OSError as exc:
                logger.error("Could not create backup for %s: %s", image_path, exc)
                return WriteResult(False, f"Could not create backup before writing: {exc}")

        try:
            with pyexiv2.Image(image_path) as img:
                # pyexiv2 accepts plain strings for lang-alt XMP fields.
                # On readback it prefixes the value with "x-default "
                # (handled by _extract_lang_alt). Do NOT pass dicts here.
                img.modify_xmp({
                    "Xmp.dc.title":       title,
                    "Xmp.dc.description": description,
                    "Xmp.dc.subject":     keywords,
                })
                img.modify_iptc({
                    "Iptc.Application2.ObjectName": title,
                    "Iptc.Application2.Caption":    description,
                    "Iptc.Application2.Keywords":   keywords,
                })
        except Exception as exc:
            logger.error("Write failed for %s: %s — rolling back.", image_path, exc)
            if backup_path:
                self._restore_backup(image_path, backup_path)
            return WriteResult(False, f"Write failed: {exc}")

        # --- Item #6: read back and verify ---
        mismatch = self._verify_written_metadata(image_path, title, description, keywords)
        if mismatch:
            logger.error(
                "Verification failed for %s (%s) — rolling back.", image_path, mismatch
            )
            if backup_path:
                self._restore_backup(image_path, backup_path)
            return WriteResult(False, f"Metadata verification failed: {mismatch}",
                                rolled_back=bool(backup_path))

        # Clean up backup only once write + verification both succeed
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError as exc:
                logger.warning("Could not remove backup %s: %s", backup_path, exc)

        logger.info("Metadata written and verified: %s", image_path)
        return WriteResult(True, "")

    def _verify_written_metadata(
        self, image_path: str, expected_title: str,
        expected_description: str, expected_keywords: List[str],
    ) -> str:
        """
        Read metadata back from disk and compare against what was meant to
        be written. Returns "" if everything matches, otherwise a short
        description of the first mismatch found.
        """
        try:
            actual = self.read_metadata(image_path)
        except Exception as exc:
            return f"could not read back metadata: {exc}"

        if (actual.get("title") or "") != expected_title:
            return (
                f"title mismatch (wrote {len(expected_title)} chars, "
                f"read back {len(actual.get('title') or '')} chars)"
            )
        if (actual.get("description") or "") != expected_description:
            return (
                f"description mismatch (wrote {len(expected_description)} chars, "
                f"read back {len(actual.get('description') or '')} chars)"
            )

        actual_keywords = actual.get("keywords") or []
        if list(actual_keywords) != list(expected_keywords):
            return (
                f"keywords mismatch (wrote {len(expected_keywords)} keywords, "
                f"read back {len(actual_keywords)} keywords)"
            )

        return ""

    def get_image_info(self, image_path: str) -> Dict[str, object]:
        """
        NEW: Return image dimensions, mode, and file size without loading full pixels.
        Useful for populating the Resolution column in the batch table.
        """
        self._assert_exists(image_path)
        info: Dict[str, object] = {"width": 0, "height": 0, "mode": "", "file_size_kb": 0}
        try:
            with Image.open(image_path) as img:
                info["width"], info["height"] = img.size
                info["mode"] = img.mode
            info["file_size_kb"] = round(os.path.getsize(image_path) / 1024, 1)
        except (UnidentifiedImageError, OSError) as exc:
            logger.warning("Cannot read image info for %s: %s", image_path, exc)
        return info

    def get_ui_thumbnail(self, image_path: str) -> Optional[bytes]:
        """
        Generate a JPEG thumbnail as raw bytes for UI display.
        Returns None if the image cannot be opened.
        """
        if not os.path.exists(image_path):
            return None

        try:
            with Image.open(image_path) as img:
                # BUG FIX: handle palette/CMYK/RGBA modes properly
                if img.mode == "P":
                    img = img.convert("RGBA")
                if img.mode in ("RGBA", "LA"):
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[-1])
                    img = background
                elif img.mode != "RGB":
                    img = img.convert("RGB")

                img.thumbnail((self.thumb_size, self.thumb_size), Image.Resampling.LANCZOS)

                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                return buf.getvalue()

        except (UnidentifiedImageError, OSError) as exc:
            logger.warning("Cannot generate thumbnail for %s: %s", image_path, exc)
            return None

    def validate_image(self, image_path: str) -> Tuple[bool, str]:
        """
        Item #4 — Image validation before AI generation.

        Returns (is_valid, reason). reason is "" when valid, otherwise a
        short, user-facing explanation of why the image was rejected:
        missing file, unsupported format, empty file, or corrupted/
        unreadable image data.
        """
        if not os.path.exists(image_path):
            return False, "File not found"

        if not self.is_supported(image_path):
            ext = Path(image_path).suffix or "(no extension)"
            return False, f"Unsupported format: {ext}"

        try:
            size = os.path.getsize(image_path)
        except OSError as exc:
            return False, f"Cannot read file: {exc}"

        if size == 0:
            return False, "Empty file (0 bytes)"

        try:
            with Image.open(image_path) as img:
                img.verify()  # cheap structural check, doesn't decode pixels
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            return False, f"Corrupted or unreadable image: {exc}"
        except Exception as exc:  # noqa: BLE001 — be defensive; bad images
            # can raise all sorts of decoder-specific errors we don't want
            # to let crash a batch run.
            return False, f"Corrupted or unreadable image: {exc}"

        # img.verify() invalidates the file handle for further operations,
        # so re-open for a true full-decode pass — this catches truncated
        # files that pass verify() but fail on actual pixel access.
        try:
            with Image.open(image_path) as img:
                img.load()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            return False, f"Corrupted or unreadable image: {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Corrupted or unreadable image: {exc}"

        return True, ""

    def batch_validate(self, paths: List[str]) -> Tuple[List[str], List[str]]:
        """
        Split a list of paths into (valid, invalid).
        Invalid means non-existent, unsupported extension, empty file, or
        corrupted/unreadable image data (see validate_image for details).
        """
        valid, invalid = [], []
        for p in paths:
            ok, _reason = self.validate_image(p)
            (valid if ok else invalid).append(p)
        return valid, invalid

    def batch_validate_with_reasons(self, paths: List[str]) -> Tuple[List[str], Dict[str, str]]:
        """
        Same as batch_validate, but returns invalid paths mapped to their
        rejection reason so the UI/worker can show a meaningful error
        instead of a generic "skipped" status.
        """
        valid: List[str] = []
        invalid: Dict[str, str] = {}
        for p in paths:
            ok, reason = self.validate_image(p)
            if ok:
                valid.append(p)
            else:
                invalid[p] = reason
        return valid, invalid

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_lang_alt(value: object) -> str:
        """
        Normalise a pyexiv2 XMP lang-alt value to a plain string.

        pyexiv2 behaviour (observed across versions):
          - Writing a plain string then reading it back returns
            "x-default <original_value>" as a plain string (10-char prefix).
          - Some builds return a dict {"x-default": "<value>"}.
          - Empty / unset fields may be None, "", or {}.

        This strips whichever wrapper is present so callers always
        get the bare text that was originally written.
        """
        if not value:
            return ""
        if isinstance(value, dict):
            text = str(value.get("x-default") or next(iter(value.values()), ""))
        else:
            text = str(value)
        # Strip leading language tag that pyexiv2 prepends on readback.
        for prefix in ('lang="x-default" ', 'x-default '):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        return text.strip()

    @staticmethod
    def _assert_exists(image_path: str) -> None:
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image not found: {image_path}")

    @staticmethod
    def _normalise_keywords(raw: object) -> List[str]:
        if isinstance(raw, str):
            return [k.strip() for k in raw.split(",") if k.strip()]
        if isinstance(raw, list):
            return [str(k).strip() for k in raw if k]
        return []

    def _create_backup(self, original_path: str) -> str:
        p = Path(original_path)
        backup = p.with_suffix(p.suffix + ".bak")
        shutil.copy2(original_path, backup)
        return str(backup)

    def _restore_backup(self, original_path: str, backup_path: str) -> None:
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, original_path)
            try:
                os.remove(backup_path)
            except OSError:
                pass

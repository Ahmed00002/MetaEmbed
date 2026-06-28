import csv
import logging
import datetime
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
HISTORY_FILE = DATA_DIR / "history.csv"

# Item #7 — structured logging. Every row now carries enough context that
# an error never has to be reported as a bare "Unknown Error": which image,
# what operation, which stage of processing, which AI provider (if any),
# and the specific reason.
CSV_HEADERS = [
    "timestamp", "action", "status", "image_name", "processing_stage",
    "ai_provider", "error_reason", "details",
]
# Older history.csv files (pre item-7) only have these 4 columns.
_LEGACY_HEADERS = ["timestamp", "action", "status", "details"]


class HistoryManager:
    def __init__(self, max_entries: int = 1000):
        self.max_entries = max_entries
        self._ensure_data_dir()
        self._ensure_csv_initialized()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_data_dir(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _ensure_csv_initialized(self) -> None:
        if not HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, mode="w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(CSV_HEADERS)
            except OSError as exc:
                logger.error("Cannot initialise history file: %s", exc)
            return

        # Migrate an old 4-column history file to the new structured schema
        # by reading it once and rewriting with empty values for the new
        # columns — existing history entries are preserved, just without
        # the extra structured fields they predate.
        try:
            with open(HISTORY_FILE, mode="r", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                rows = list(reader)
        except OSError as exc:
            logger.error("Cannot read history file for migration check: %s", exc)
            return

        if not rows:
            self._write_all_rows([])
            return

        header = rows[0]
        if header == CSV_HEADERS:
            return  # already current schema

        if header == _LEGACY_HEADERS:
            logger.info("Migrating history.csv to the structured logging schema.")
            migrated = []
            for row in rows[1:]:
                row = row + [""] * (len(_LEGACY_HEADERS) - len(row))
                timestamp, action, status, details = row[:4]
                # image_name, processing_stage, ai_provider, error_reason, details
                migrated.append([timestamp, action, status, "", "", "", "", details])
            self._write_all_rows(migrated)
        else:
            # Unknown/corrupted header — back up and start fresh rather
            # than silently losing or misreading data.
            logger.warning("Unrecognised history.csv header; starting a new file.")
            backup = HISTORY_FILE.with_suffix(".csv.bak")
            try:
                HISTORY_FILE.replace(backup)
            except OSError:
                pass
            with open(HISTORY_FILE, mode="w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(CSV_HEADERS)

    def _read_all_rows(self) -> List[List[str]]:
        """Return all data rows (excluding header), padded to the current schema width."""
        if not HISTORY_FILE.exists():
            return []
        try:
            with open(HISTORY_FILE, mode="r", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                rows = list(reader)
        except OSError as exc:
            logger.error("Cannot read history file: %s", exc)
            return []
        if len(rows) <= 1:
            return []
        width = len(CSV_HEADERS)
        return [r + [""] * (width - len(r)) for r in rows[1:]]

    def _write_all_rows(self, rows: List[List[str]]) -> None:
        try:
            with open(HISTORY_FILE, mode="w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_HEADERS)
                writer.writerows(rows)
        except OSError as exc:
            logger.error("Cannot write history file: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_action(
        self,
        action: str,
        status: str,
        details: str = "",
        image_name: str = "",
        processing_stage: str = "",
        ai_provider: str = "",
        error_reason: str = "",
    ) -> None:
        """
        Append one structured log entry and trim the file if it exceeds
        max_entries.

        `details` remains a free-text field for backward compatibility with
        existing call sites; the new structured fields (image_name,
        processing_stage, ai_provider, error_reason) let the UI and any
        future export show exactly what failed and why, instead of a
        generic message.
        """
        timestamp = datetime.datetime.now().isoformat(timespec="seconds")
        row = [
            timestamp, action, status, image_name, processing_stage,
            ai_provider, error_reason, details,
        ]
        try:
            with open(HISTORY_FILE, mode="a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(row)
        except OSError as exc:
            logger.error("Cannot append to history: %s", exc)
            return

        rows = self._read_all_rows()
        if len(rows) > self.max_entries:
            self._write_all_rows(rows[-self.max_entries:])

    def get_recent_history(self, limit: int = 50) -> List[Dict[str, str]]:
        """Return the most-recent `limit` entries, newest first."""
        rows = self._read_all_rows()
        recent = rows[-limit:][::-1]  # last N rows reversed
        return [dict(zip(CSV_HEADERS, row)) for row in recent]

    def get_stats(self) -> Dict[str, int]:
        """Return simple aggregate counts."""
        rows = self._read_all_rows()
        stats: Dict[str, int] = {"total": len(rows), "success": 0, "error": 0}
        status_idx = CSV_HEADERS.index("status")
        for row in rows:
            if len(row) > status_idx:
                status = row[status_idx].lower()
                if status == "success":
                    stats["success"] += 1
                elif status == "error":
                    stats["error"] += 1
        return stats

    def clear_history(self) -> None:
        self._write_all_rows([])

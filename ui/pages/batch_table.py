"""
ui/pages/batch_table.py  —  BatchTableWidget page widget.
Auto-extracted from ui_main.py. Edit here; do not edit the old monolith.
"""
import os
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QSize, QTimer, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPixmap, QFont, QIcon, QColor, QGuiApplication, QDesktopServices, QImageReader
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QTableWidget, QTableWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QProgressBar, QFormLayout, QFrame,
    QHeaderView, QComboBox, QMessageBox, QFileDialog,
    QSpinBox, QCheckBox, QScrollArea, QSizePolicy, QGroupBox,
    QAbstractItemView, QListWidget, QInputDialog, QSplitter, QPlainTextEdit,
)
import sys

from core.stock_markets import MARKETS, get_all_market_names, MARKET_DISPLAY_NAMES
from core.keyword_tools import compute_quality_score, check_metadata_quality

logger = logging.getLogger(__name__)

PROVIDER_MAP = {
    "Google GenAI":  "google",
    "OpenAI":        "openai",
    "OpenRouter":    "openrouter",
    "Groq":          "groq",
}
PROVIDER_KEY_LABELS = {
    "google":     "Google API Key",
    "openai":     "OpenAI API Key",
    "openrouter": "OpenRouter API Key",
    "groq":       "Groq API Key",
}
PROVIDER_MODELS = {
    "google":     ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash", "gemini-1.5-pro"],
    "openai":     ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5", "gpt-4o-mini"],
    "openrouter": ["google/gemini-2.5-flash", "openai/gpt-5.4-mini", "anthropic/claude-3-haiku",
                   "meta-llama/llama-4-scout"],
    "groq":       ["meta-llama/llama-4-scout-17b-16e-instruct",
                   "meta-llama/llama-4-maverick-17b-128e-instruct"],
}
PROVIDER_DOCS = {
    "google":     "https://aistudio.google.com/apikey",
    "openai":     "https://platform.openai.com/api-keys",
    "openrouter": "https://openrouter.ai/keys",
    "groq":       "https://console.groq.com/keys",
}
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"}

class BatchTableWidget(QTableWidget):
    """Accepts drag-and-drop images; stores full paths in a hidden column."""

    def __init__(self):
        super().__init__()
        self._paths: list[str] = []

        self.setAcceptDrops(True)
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["Filename", "Status", "Resolution", "Path"])
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.setColumnHidden(3, True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.verticalHeader().setVisible(False)

    def dragEnterEvent(self, e: QDragEnterEvent):
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dragMoveEvent(self, e):
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dropEvent(self, e: QDropEvent):
        for url in e.mimeData().urls():
            if not url.isLocalFile():
                continue
            local_path = url.toLocalFile()
            if os.path.isdir(local_path):
                # Item #14 — folders: recurse and add every supported image found.
                for root_dir, _dirs, filenames in os.walk(local_path):
                    for fname in sorted(filenames):
                        self.add_file(os.path.join(root_dir, fname))
            else:
                self.add_file(local_path)
        e.acceptProposedAction()

    def add_file(self, file_path: str):
        file_path = os.path.normpath(file_path)
        if Path(file_path).suffix.lower() not in SUPPORTED_EXTENSIONS:
            return
        if file_path in self._paths:
            return
        self._paths.append(file_path)

        row = self.rowCount()
        self.insertRow(row)
        self.setRowHeight(row, 38)
        self.setItem(row, 0, QTableWidgetItem(Path(file_path).name))
        status = QTableWidgetItem("Ready")
        status.setForeground(QColor("#64748b"))
        self.setItem(row, 1, status)
        self.setItem(row, 2, QTableWidgetItem(self._describe_image(file_path)))
        self.setItem(row, 3, QTableWidgetItem(file_path))

    @staticmethod
    def _describe_image(file_path: str) -> str:
        """Item #15 — show resolution + file size as soon as the file is added."""
        size_str = "—"
        try:
            size_bytes = os.path.getsize(file_path)
            if size_bytes >= 1_048_576:
                size_str = f"{size_bytes / 1_048_576:.1f} MB"
            elif size_bytes >= 1024:
                size_str = f"{size_bytes / 1024:.0f} KB"
            else:
                size_str = f"{size_bytes} B"
        except OSError:
            pass

        res_str = "?×?"
        try:
            from PIL import Image
            with Image.open(file_path) as img:
                res_str = f"{img.width}×{img.height}"
        except Exception:
            pass

        return f"{res_str}  •  {size_str}"

    def update_row_status(self, row: int, status: str):
        if row < 0 or row >= self.rowCount():
            return
        item = QTableWidgetItem(status)
        # Exact matches first, then prefix-based for dynamic labels like
        # "Done (via Groq)" or "Generated (via OpenRouter)".
        exact_colors = {
            "Done":                       "#22c55e",
            "Generated":                  "#22c55e",
            "Ready":                      "#64748b",
            "Error":                      "#ef4444",
            "Write Failed":               "#ef4444",
            "Write Failed (rolled back)": "#ef4444",
            "Skipped (invalid)":          "#f59e0b",
            "Duplicate (skipped)":        "#f59e0b",
            "Validating…":                "#94a3b8",
            "Generating…":                "#f59e0b",
            "Writing…":                   "#818cf8",
            "Processing…":                "#f59e0b",
        }
        if status in exact_colors:
            color = exact_colors[status]
        elif status.startswith("Done"):
            color = "#22c55e"   # green for any "Done (via X)"
        elif status.startswith("Generated"):
            color = "#22c55e"
        elif status.startswith("Write Failed"):
            color = "#ef4444"
        else:
            color = "#64748b"
        item.setForeground(QColor(color))
        self.setItem(row, 1, item)

    # Status texts that mean "already successfully generated"
    _DONE_STATUSES = {"Done", "Generated", "Writing…"}

    def get_row_status(self, row: int) -> str:
        """Return the current status text of a row."""
        if row < 0 or row >= self.rowCount():
            return ""
        item = self.item(row, 1)
        return item.text() if item else ""

    def is_row_done(self, row: int) -> bool:
        """Return True if this row already has successful metadata."""
        status = self.get_row_status(row)
        if status in self._DONE_STATUSES:
            return True
        # Also cover "Done (via X)" and "Generated (via X)"
        return status.startswith("Done") or status.startswith("Generated")

    def is_row_failed(self, row: int) -> bool:
        """Return True if this row has a failure status."""
        status = self.get_row_status(row)
        return status in ("Error", "Write Failed", "Write Failed (rolled back)")

    def get_pending_paths(self) -> list[str]:
        """Paths that are not yet successfully generated (Ready + Error + others)."""
        result = []
        for i, path in enumerate(self._paths):
            if not self.is_row_done(i):
                result.append(path)
        return result

    def get_failed_paths(self) -> list[str]:
        """Paths with an error/write-failed status."""
        result = []
        for i, path in enumerate(self._paths):
            if self.is_row_failed(i):
                result.append(path)
        return result

    def has_failed_rows(self) -> bool:
        """True if at least one row has a failure status."""
        return any(self.is_row_failed(i) for i in range(len(self._paths)))

    def get_all_paths(self) -> list[str]:
        return list(self._paths)

    def get_path_at_row(self, row: int) -> Optional[str]:
        return self._paths[row] if 0 <= row < len(self._paths) else None

    def get_row_for_path(self, path: str) -> Optional[int]:
        """Inverse of get_path_at_row — needed for item #12 (regenerate single)."""
        norm = os.path.normpath(path)
        for i, p in enumerate(self._paths):
            if os.path.normpath(p) == norm:
                return i
        return None

    def clear_queue(self):
        self.setRowCount(0)
        self._paths.clear()


# ============================================================================
# Page widgets
# ============================================================================


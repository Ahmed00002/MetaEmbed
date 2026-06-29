"""
ui/pages/history_page.py  —  HistoryPage page widget.
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

class HistoryPage(QWidget):
    """Item #7 — surfaces the structured action history stored in history.csv."""

    clear_history_requested = Signal()   # emitted when user clicks Clear

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Header row
        hdr_row = QHBoxLayout()
        title = QLabel("History")
        title.setObjectName("PageTitle")
        hdr_row.addWidget(title)
        hdr_row.addStretch()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setObjectName("SecBtn")
        self.btn_refresh.setFixedHeight(32)
        self.btn_clear = QPushButton("Clear History")
        self.btn_clear.setObjectName("SecBtn")
        self.btn_clear.setFixedHeight(32)
        hdr_row.addWidget(self.btn_refresh)
        hdr_row.addWidget(self.btn_clear)
        layout.addLayout(hdr_row)

        subtitle = QLabel(
            "All metadata generation actions, errors, and validations are logged here."
        )
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # Stats strip
        stats_card = QFrame()
        stats_card.setObjectName("Card")
        sc_layout = QHBoxLayout(stats_card)
        sc_layout.setContentsMargins(16, 12, 16, 12)
        sc_layout.setSpacing(32)
        self.lbl_total   = self._stat_widget("Total", "0")
        self.lbl_success = self._stat_widget("Success", "0")
        self.lbl_error   = self._stat_widget("Errors", "0")
        sc_layout.addWidget(self.lbl_total[0])
        sc_layout.addWidget(self.lbl_success[0])
        sc_layout.addWidget(self.lbl_error[0])
        sc_layout.addStretch()
        layout.addWidget(stats_card)

        # History table
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Time", "Action", "Status", "Image", "Stage", "Detail"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 200)
        self.table.setColumnWidth(4, 130)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        layout.addWidget(self.table)

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_clear.clicked.connect(self._confirm_clear)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _stat_widget(self, label: str, value: str):
        """Return a (container, value_label) pair for the stats strip."""
        w = QWidget()
        wl = QVBoxLayout(w)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(2)
        lbl_val = QLabel(value)
        lbl_val.setObjectName("CardTitle")
        lbl_key = QLabel(label)
        lbl_key.setObjectName("CardNote")
        wl.addWidget(lbl_val)
        wl.addWidget(lbl_key)
        return w, lbl_val

    def refresh(self, entries: list = None, stats: dict = None):
        """Populate table and stats. Call with pre-fetched data or without args
        to show the last data that was set (for pure repaint)."""
        if entries is not None:
            self._last_entries = entries
        if stats is not None:
            self._last_stats = stats

        entries = getattr(self, "_last_entries", [])
        stats   = getattr(self, "_last_stats", {"total": 0, "success": 0, "error": 0})

        # Update stat labels
        self.lbl_total[1].setText(str(stats.get("total", 0)))
        self.lbl_success[1].setText(str(stats.get("success", 0)))
        self.lbl_error[1].setText(str(stats.get("error", 0)))

        self.table.setRowCount(0)
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setRowHeight(row, 34)

            timestamp = entry.get("timestamp", "")
            action    = entry.get("action", "")
            status    = entry.get("status", "")
            image     = entry.get("image_name", "") or entry.get("details", "")
            stage     = entry.get("processing_stage", "")
            detail    = entry.get("error_reason", "") or entry.get("details", "")

            # Shorten image path to filename only
            if image and ("/" in image or "\\" in image):
                from pathlib import Path as _Path
                image = _Path(image).name

            cells = [timestamp, action, status, image, stage, detail]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col == 2:  # status column — colour coding
                    color_map = {
                        "success": "#22c55e",
                        "error":   "#ef4444",
                        "warning": "#f59e0b",
                        "skipped": "#f59e0b",
                    }
                    item.setForeground(QColor(color_map.get(status.lower(), "#94a3b8")))
                self.table.setItem(row, col, item)

    def _confirm_clear(self):
        reply = QMessageBox.question(
            self, "Clear History",
            "This will permanently delete all history entries.\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.clear_history_requested.emit()



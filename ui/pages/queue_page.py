"""
ui/pages/queue_page.py  —  QueuePage page widget.
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

from ui.pages.batch_table import BatchTableWidget

class QueuePage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Drop zone header
        drop_hint = QLabel("Drop images or folders here  ·  JPG, PNG, TIFF, WEBP")
        drop_hint.setObjectName("DropHint")
        drop_hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(drop_hint)

        # ── Vertical splitter: table (top) + console (bottom) ──
        self._splitter = QSplitter(Qt.Vertical)
        self._splitter.setHandleWidth(4)
        self._splitter.setChildrenCollapsible(True)

        # Table container (upper pane)
        self.batch_table = BatchTableWidget()
        self._splitter.addWidget(self.batch_table)

        # Console pane (lower pane) — built as a named frame so it can be
        # styled independently and collapsed/expanded via the toggle button.
        self._console_pane = QFrame()
        self._console_pane.setObjectName("ConsolePane")
        console_outer = QVBoxLayout(self._console_pane)
        console_outer.setContentsMargins(0, 0, 0, 0)
        console_outer.setSpacing(0)

        # Console header bar
        console_header = QWidget()
        console_header.setObjectName("ConsoleHeader")
        console_header.setFixedHeight(30)
        ch_layout = QHBoxLayout(console_header)
        ch_layout.setContentsMargins(10, 0, 8, 0)
        ch_layout.setSpacing(8)

        console_title = QLabel("Console")
        console_title.setObjectName("ConsoleTitleLbl")
        ch_layout.addWidget(console_title)
        ch_layout.addStretch()

        self.btn_clear_console = QPushButton("Clear")
        self.btn_clear_console.setObjectName("ChipBtn")
        self.btn_clear_console.setFixedHeight(22)
        ch_layout.addWidget(self.btn_clear_console)

        self.btn_toggle_console = QPushButton("▲ Hide")
        self.btn_toggle_console.setObjectName("ChipBtn")
        self.btn_toggle_console.setFixedHeight(22)
        ch_layout.addWidget(self.btn_toggle_console)

        console_outer.addWidget(console_header)

        # The actual log output widget
        self.console_output = QPlainTextEdit()
        self.console_output.setObjectName("ConsoleOutput")
        self.console_output.setReadOnly(True)
        self.console_output.setMaximumBlockCount(2000)   # cap at 2000 lines
        self.console_output.setPlaceholderText("Generation progress will appear here…")
        console_outer.addWidget(self.console_output, stretch=1)

        self._splitter.addWidget(self._console_pane)

        # Default split: ~70 % table, ~30 % console
        self._splitter.setSizes([600, 220])
        self._console_expanded = True

        # Wire console toggle
        self.btn_toggle_console.clicked.connect(self._toggle_console)
        self.btn_clear_console.clicked.connect(self.console_output.clear)

        layout.addWidget(self._splitter, stretch=1)

        # Controls bar
        bar = QWidget()
        bar.setObjectName("ControlBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(14, 10, 14, 10)
        bar_layout.setSpacing(6)

        self.btn_add_files = QPushButton("Add Files")
        self.btn_add_files.setObjectName("SecBtn")
        self.btn_add_folder = QPushButton("Add Folder")
        self.btn_add_folder.setObjectName("SecBtn")
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setObjectName("SecBtn")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("SecBtn")
        self.btn_cancel.setEnabled(False)
        self.btn_save_all = QPushButton("Save All to Files")
        self.btn_save_all.setObjectName("GreenBtn")
        self.btn_save_all.setMinimumHeight(36)
        self.btn_save_all.setEnabled(False)
        self.btn_retry_failed = QPushButton("Generate Failed")
        self.btn_retry_failed.setObjectName("WarnBtn")
        self.btn_retry_failed.setMinimumHeight(36)
        self.btn_retry_failed.setEnabled(False)
        self.btn_retry_failed.setToolTip("Re-run generation only for images that failed")
        self.btn_process = QPushButton("Generate Metadata")
        self.btn_process.setObjectName("PrimaryBtn")
        self.btn_process.setMinimumHeight(36)

        bar_layout.addWidget(self.btn_add_files)
        bar_layout.addWidget(self.btn_add_folder)
        bar_layout.addWidget(self.btn_clear)
        bar_layout.addWidget(self.btn_cancel)
        bar_layout.addStretch()
        bar_layout.addWidget(self.btn_save_all)
        bar_layout.addWidget(self.btn_retry_failed)
        bar_layout.addWidget(self.btn_process)
        layout.addWidget(bar)

    def _toggle_console(self):
        """Collapse or expand the console pane."""
        if self._console_expanded:
            # Remember current sizes before collapsing
            self._sizes_before_collapse = self._splitter.sizes()
            # Give the top pane all the space; console pane collapses to header-only
            total = sum(self._splitter.sizes())
            self._splitter.setSizes([total - 30, 30])
            self.console_output.setVisible(False)
            self.btn_toggle_console.setText("▼ Show")
            self._console_expanded = False
        else:
            self.console_output.setVisible(True)
            sizes = getattr(self, "_sizes_before_collapse", None)
            if sizes:
                self._splitter.setSizes(sizes)
            else:
                total = sum(self._splitter.sizes())
                self._splitter.setSizes([int(total * 0.7), int(total * 0.3)])
            self.btn_toggle_console.setText("▲ Hide")
            self._console_expanded = True

    def log_console(self, msg: str):
        """Append a timestamped line to the console output."""
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.console_output.appendPlainText(f"[{ts}]  {msg}")
        # Auto-scroll to bottom
        sb = self.console_output.verticalScrollBar()
        sb.setValue(sb.maximum())



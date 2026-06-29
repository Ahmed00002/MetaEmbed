"""
ui/pages/market_page.py  —  MarketPage page widget.
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

class MarketPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("Target Market")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        subtitle = QLabel("Select which stock platform you are submitting to. Rules are applied automatically.")
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # Market selector card
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)

        card_layout.addWidget(QLabel("Active Market:"))
        self.market_combo = QComboBox()
        self.market_combo.addItems(get_all_market_names())
        self.market_combo.setMinimumHeight(36)
        card_layout.addWidget(self.market_combo)
        layout.addWidget(card)

        # Rules display card
        rules_card = QFrame()
        rules_card.setObjectName("Card")
        rc_layout = QVBoxLayout(rules_card)
        rc_layout.setContentsMargins(16, 16, 16, 16)
        rc_layout.setSpacing(10)

        self.market_name_lbl = QLabel("")
        self.market_name_lbl.setObjectName("CardTitle")
        rc_layout.addWidget(self.market_name_lbl)

        self.market_notes = QLabel("")
        self.market_notes.setWordWrap(True)
        self.market_notes.setObjectName("CardNote")
        rc_layout.addWidget(self.market_notes)

        # Rule grid
        grid = QWidget()
        grid_layout = QHBoxLayout(grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(12)

        self.rule_title_lbl  = self._make_rule_chip("Title", "—")
        self.rule_kw_lbl     = self._make_rule_chip("Keywords", "—")
        self.rule_desc_lbl   = self._make_rule_chip("Description", "—")
        grid_layout.addWidget(self.rule_title_lbl)
        grid_layout.addWidget(self.rule_kw_lbl)
        grid_layout.addWidget(self.rule_desc_lbl)
        grid_layout.addStretch()
        rc_layout.addWidget(grid)

        lbl_cols = QLabel("Export Columns:")
        lbl_cols.setObjectName("FieldLabel")
        rc_layout.addWidget(lbl_cols)
        self.market_cols_lbl = QLabel("")
        self.market_cols_lbl.setWordWrap(True)
        self.market_cols_lbl.setObjectName("CardNote")
        rc_layout.addWidget(self.market_cols_lbl)

        layout.addWidget(rules_card)

        # Export
        exp_card = QFrame()
        exp_card.setObjectName("Card")
        exp_layout = QHBoxLayout(exp_card)
        exp_layout.setContentsMargins(16, 14, 16, 14)

        exp_info = QVBoxLayout()
        exp_title = QLabel("Export Metadata CSV")
        exp_title.setObjectName("CardTitle")
        exp_sub = QLabel("Exports generated metadata in the format required by the selected market.")
        exp_sub.setObjectName("CardNote")
        exp_sub.setWordWrap(True)
        exp_info.addWidget(exp_title)
        exp_info.addWidget(exp_sub)
        exp_layout.addLayout(exp_info)
        exp_layout.addStretch()
        self.btn_export = QPushButton("⬇  Export CSV")
        self.btn_export.setObjectName("GreenBtn")
        self.btn_export.setMinimumHeight(36)
        self.btn_export.setMinimumWidth(130)
        exp_layout.addWidget(self.btn_export)
        layout.addWidget(exp_card)

        layout.addStretch()

        self.market_combo.currentTextChanged.connect(self._on_market_changed)
        if self.market_combo.count():
            self._on_market_changed(self.market_combo.currentText())

    def _make_rule_chip(self, label_text: str, value_text: str) -> QFrame:
        chip = QFrame()
        chip.setObjectName("RuleChip")
        cl = QVBoxLayout(chip)
        cl.setContentsMargins(12, 8, 12, 8)
        cl.setSpacing(2)
        lbl = QLabel(label_text)
        lbl.setObjectName("ChipLabel")
        val = QLabel(value_text)
        val.setObjectName("ChipValue")
        cl.addWidget(lbl)
        cl.addWidget(val)
        chip._value_label = val
        return chip

    def _on_market_changed(self, display_name: str):
        key = MARKET_DISPLAY_NAMES.get(display_name)
        if not key:
            return
        from core.stock_markets import get_market
        m = get_market(key)
        if not m:
            return
        self.market_name_lbl.setText(m.name)
        self.market_notes.setText(m.notes)
        self.rule_title_lbl._value_label.setText(f"{m.title_min}–{m.title_max} chars")
        self.rule_kw_lbl._value_label.setText(f"{m.keyword_min}–{m.keyword_max} keywords")
        self.rule_desc_lbl._value_label.setText(f"max {m.description_max} chars")
        self.market_cols_lbl.setText(", ".join(m.csv_columns))

    def get_selected_market(self) -> str:
        display = self.market_combo.currentText()
        return MARKET_DISPLAY_NAMES.get(display, "adobe")



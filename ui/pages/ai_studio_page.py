"""
ui/pages/ai_studio_page.py  —  AIStudioPage page widget.
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

class AIStudioPage(QWidget):
    def __init__(self):
        super().__init__()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("AI Studio")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        subtitle = QLabel("Manage your AI provider, models, and API keys. Keys are stored locally in your config file.")
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # Active provider card
        prov_card = QFrame()
        prov_card.setObjectName("Card")
        pc_layout = QVBoxLayout(prov_card)
        pc_layout.setContentsMargins(16, 16, 16, 16)
        pc_layout.setSpacing(12)

        pt = QLabel("Active Provider")
        pt.setObjectName("CardTitle")
        pc_layout.addWidget(pt)

        pn = QLabel("All metadata generation will use this provider and its configured model.")
        pn.setObjectName("CardNote")
        pn.setWordWrap(True)
        pc_layout.addWidget(pn)

        prow = QHBoxLayout()
        prow.addWidget(QLabel("Provider:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(list(PROVIDER_MAP.keys()))
        self.provider_combo.setMinimumHeight(36)
        self.provider_combo.setMinimumWidth(200)
        prow.addWidget(self.provider_combo)
        prow.addSpacing(20)

        prow.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setMinimumHeight(36)
        self.model_combo.setMinimumWidth(260)
        prow.addWidget(self.model_combo)
        prow.addStretch()
        pc_layout.addLayout(prow)
        layout.addWidget(prov_card)

        # Status row
        self.status_card = QFrame()
        self.status_card.setObjectName("StatusCard")
        sc_layout = QHBoxLayout(self.status_card)
        sc_layout.setContentsMargins(16, 12, 16, 12)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("StatusDotOff")
        self.status_text = QLabel("No API key configured for this provider.")
        self.status_text.setObjectName("CardNote")
        sc_layout.addWidget(self.status_dot)
        sc_layout.addWidget(self.status_text)
        sc_layout.addStretch()
        layout.addWidget(self.status_card)

        # API Keys card
        keys_card = QFrame()
        keys_card.setObjectName("Card")
        kc_layout = QVBoxLayout(keys_card)
        kc_layout.setContentsMargins(16, 16, 16, 16)
        kc_layout.setSpacing(14)

        kt = QLabel("API Keys")
        kt.setObjectName("CardTitle")
        kc_layout.addWidget(kt)

        kn = QLabel("Enter your API keys below. Click the eye to reveal/hide. Keys are saved to your local config.json.")
        kn.setObjectName("CardNote")
        kn.setWordWrap(True)
        kc_layout.addWidget(kn)

        self.api_inputs: dict[str, QLineEdit] = {}
        for provider_key, label_text in PROVIDER_KEY_LABELS.items():
            row_widget = QWidget()
            row_lyt = QVBoxLayout(row_widget)
            row_lyt.setContentsMargins(0, 0, 0, 0)
            row_lyt.setSpacing(4)

            row_header = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setObjectName("FieldLabel")
            docs_link = QLabel(f'<a href="{PROVIDER_DOCS[provider_key]}" style="color:#3b82f6;">Get key ↗</a>')
            docs_link.setOpenExternalLinks(True)
            docs_link.setObjectName("LinkLabel")
            row_header.addWidget(lbl)
            row_header.addStretch()
            row_header.addWidget(docs_link)
            row_lyt.addLayout(row_header)

            inp_row = QHBoxLayout()
            inp_row.setSpacing(6)
            inp = QLineEdit()
            inp.setEchoMode(QLineEdit.Password)
            inp.setPlaceholderText("Paste API key…")
            inp.setMinimumHeight(36)
            inp.textChanged.connect(self._update_status)
            self.api_inputs[provider_key] = inp
            inp_row.addWidget(inp)

            toggle = QPushButton("👁")
            toggle.setObjectName("EyeBtn")
            toggle.setFixedSize(36, 36)
            toggle.setCheckable(True)
            toggle.toggled.connect(
                lambda checked, i=inp: i.setEchoMode(
                    QLineEdit.Normal if checked else QLineEdit.Password
                )
            )
            inp_row.addWidget(toggle)
            row_lyt.addLayout(inp_row)

            sep = QFrame()
            sep.setObjectName("HRule")
            sep.setFrameShape(QFrame.HLine)

            kc_layout.addWidget(row_widget)
            kc_layout.addWidget(sep)

        # Remove last separator
        layout.addWidget(keys_card)

        layout.addStretch()

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(inner)
        outer_layout.addWidget(scroll)

        # Wire combos
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        self._on_provider_changed(self.provider_combo.currentText())

    def _on_provider_changed(self, display_name: str):
        provider_key = PROVIDER_MAP.get(display_name, "openai")
        models = PROVIDER_MODELS.get(provider_key, [])
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self._update_status()

    def _update_status(self):
        display = self.provider_combo.currentText()
        provider_key = PROVIDER_MAP.get(display, "openai")
        key_val = self.api_inputs.get(provider_key, QLineEdit()).text().strip()
        if key_val:
            self.status_dot.setObjectName("StatusDotOn")
            self.status_text.setText(f"API key configured for {display}.")
        else:
            self.status_dot.setObjectName("StatusDotOff")
            self.status_text.setText(f"No API key configured for {display}.")
        # Force style refresh
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)

    def get_selected_provider(self) -> str:
        return PROVIDER_MAP.get(self.provider_combo.currentText(), "openai")

    def get_selected_model(self) -> str:
        return self.model_combo.currentText()


# ============================================================================
# Main Window
# ============================================================================


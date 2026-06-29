"""
ui/pages/settings_page.py  —  SettingsPage page widget.
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

class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        subtitle = QLabel("Configure metadata generation rules and processing behaviour.")
        subtitle.setObjectName("PageSubtitle")
        layout.addWidget(subtitle)

        # --- Batch size ---
        batch_card = QFrame()
        batch_card.setObjectName("Card")
        bc_layout = QVBoxLayout(batch_card)
        bc_layout.setContentsMargins(16, 16, 16, 16)
        bc_layout.setSpacing(12)

        bt = QLabel("Batch Processing")
        bt.setObjectName("CardTitle")
        bc_layout.addWidget(bt)

        bs_note = QLabel("Number of images to process concurrently. Higher values are faster but may hit API rate limits.")
        bs_note.setObjectName("CardNote")
        bs_note.setWordWrap(True)
        bc_layout.addWidget(bs_note)

        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("Batch Size:"))
        self.spin_batch_size = QSpinBox()
        self.spin_batch_size.setRange(1, 10)
        self.spin_batch_size.setValue(3)
        self.spin_batch_size.setMinimumWidth(80)
        self.spin_batch_size.setMinimumHeight(34)
        batch_row.addWidget(self.spin_batch_size)

        # Quick presets
        for n in [1, 3, 5]:
            b = QPushButton(str(n))
            b.setObjectName("ChipBtn")
            b.setFixedSize(36, 28)
            b.clicked.connect(lambda checked, v=n: self.spin_batch_size.setValue(v))
            batch_row.addWidget(b)

        batch_row.addStretch()
        bc_layout.addLayout(batch_row)
        layout.addWidget(batch_card)

        # --- Title rules ---
        title_card = QFrame()
        title_card.setObjectName("Card")
        tc_layout = QVBoxLayout(title_card)
        tc_layout.setContentsMargins(16, 16, 16, 16)
        tc_layout.setSpacing(12)

        tc_layout.addWidget(self._section_label("Title Length"))
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Min:"))
        self.spin_title_min = QSpinBox()
        self.spin_title_min.setRange(1, 50)
        self.spin_title_min.setValue(5)
        self.spin_title_min.setMinimumHeight(34)
        row1.addWidget(self.spin_title_min)
        row1.addSpacing(20)
        row1.addWidget(QLabel("Max:"))
        self.spin_title_max = QSpinBox()
        self.spin_title_max.setRange(10, 200)
        self.spin_title_max.setValue(70)
        self.spin_title_max.setMinimumHeight(34)
        row1.addWidget(self.spin_title_max)
        row1.addStretch()
        tc_layout.addLayout(row1)
        layout.addWidget(title_card)

        # --- Keyword rules ---
        kw_card = QFrame()
        kw_card.setObjectName("Card")
        kc_layout = QVBoxLayout(kw_card)
        kc_layout.setContentsMargins(16, 16, 16, 16)
        kc_layout.setSpacing(12)

        kc_layout.addWidget(self._section_label("Keyword Count"))
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Min:"))
        self.spin_kw_min = QSpinBox()
        self.spin_kw_min.setRange(1, 499)   # upper bound enforced by cross-validation
        self.spin_kw_min.setValue(7)
        self.spin_kw_min.setMinimumHeight(34)
        row2.addWidget(self.spin_kw_min)
        row2.addSpacing(20)
        row2.addWidget(QLabel("Max:"))
        self.spin_kw_max = QSpinBox()
        self.spin_kw_max.setRange(2, 500)
        self.spin_kw_max.setValue(49)
        self.spin_kw_max.setMinimumHeight(34)
        row2.addWidget(self.spin_kw_max)
        row2.addStretch()
        kc_layout.addLayout(row2)

        kw_note = QLabel("Min must be less than Max. Both values are passed directly to the AI — no hard cap.")
        kw_note.setObjectName("CardNote")
        kw_note.setWordWrap(True)
        kc_layout.addWidget(kw_note)

        # Cross-validate: keep min < max at all times
        self.spin_kw_min.valueChanged.connect(
            lambda v: self.spin_kw_max.setMinimum(v + 1)
        )
        self.spin_kw_max.valueChanged.connect(
            lambda v: self.spin_kw_min.setMaximum(v - 1)
        )

        layout.addWidget(kw_card)

        # --- Custom keywords ---
        ckw_card = QFrame()
        ckw_card.setObjectName("Card")
        ck_layout = QVBoxLayout(ckw_card)
        ck_layout.setContentsMargins(16, 16, 16, 16)
        ck_layout.setSpacing(10)

        ck_layout.addWidget(self._section_label("Custom Keywords"))
        self.chk_custom_kw = QCheckBox("Prepend these keywords to every image")
        self.chk_custom_kw.setChecked(True)
        ck_layout.addWidget(self.chk_custom_kw)

        ck_layout.addWidget(QLabel("Keywords (comma-separated):"))
        self.custom_kw_input = QTextEdit()
        self.custom_kw_input.setPlaceholderText("e.g.  nature, outdoor, spring")
        self.custom_kw_input.setFixedHeight(76)
        ck_layout.addWidget(self.custom_kw_input)

        note = QLabel("These keywords are always added before AI-generated keywords for every image.")
        note.setWordWrap(True)
        note.setObjectName("CardNote")
        ck_layout.addWidget(note)
        layout.addWidget(ckw_card)

        # --- Item #21: Optional marketplace rule validation ---
        mv_card = QFrame()
        mv_card.setObjectName("Card")
        mv_layout = QVBoxLayout(mv_card)
        mv_layout.setContentsMargins(16, 16, 16, 16)
        mv_layout.setSpacing(10)

        mv_layout.addWidget(self._section_label("Metadata Validation"))
        self.chk_marketplace_validation = QCheckBox("Enable Marketplace Rule Validation")
        self.chk_marketplace_validation.setChecked(False)  # off by default
        mv_layout.addWidget(self.chk_marketplace_validation)

        mv_note = QLabel(
            "When enabled, marketplace-specific character and keyword-count limits "
            "are applied and metadata is automatically trimmed to fit, with a note "
            "on which fields were changed. When disabled (default), the AI-generated "
            "metadata is written exactly as generated — nothing is trimmed or modified."
        )
        mv_note.setWordWrap(True)
        mv_note.setObjectName("CardNote")
        mv_layout.addWidget(mv_note)
        layout.addWidget(mv_card)

        # --- Auto-embed toggle ---
        ae_card = QFrame()
        ae_card.setObjectName("Card")
        ae_layout = QVBoxLayout(ae_card)
        ae_layout.setContentsMargins(16, 16, 16, 16)
        ae_layout.setSpacing(10)

        ae_layout.addWidget(self._section_label("Auto Embed Metadata"))
        self.chk_auto_embed = QCheckBox("Automatically write metadata to file after generation")
        self.chk_auto_embed.setChecked(True)
        ae_layout.addWidget(self.chk_auto_embed)

        ae_note = QLabel(
            "When ON (default), metadata is embedded into each image file immediately "
            "after the AI generates it. When OFF, metadata is shown in the Inspector "
            "only — use 'Write to File' or 'Save All to Files' to embed it manually."
        )
        ae_note.setWordWrap(True)
        ae_note.setObjectName("CardNote")
        ae_layout.addWidget(ae_note)
        layout.addWidget(ae_card)

        # --- Auto-provider toggle ---
        ap_card = QFrame()
        ap_card.setObjectName("Card")
        ap_layout = QVBoxLayout(ap_card)
        ap_layout.setContentsMargins(16, 16, 16, 16)
        ap_layout.setSpacing(10)

        ap_layout.addWidget(self._section_label("Auto Provider Selection & Fallback"))
        self.chk_auto_provider = QCheckBox("Automatically select provider and fall back on failure")
        self.chk_auto_provider.setChecked(True)
        ap_layout.addWidget(self.chk_auto_provider)

        ap_note = QLabel(
            "When ON (default), the app tries your chosen provider first. If it fails "
            "(network error, rate limit, bad key), it automatically retries with any "
            "other provider that has an API key configured. When OFF, only the selected "
            "provider is used and failures are reported immediately without fallback."
        )
        ap_note.setWordWrap(True)
        ap_note.setObjectName("CardNote")
        ap_layout.addWidget(ap_note)
        layout.addWidget(ap_card)

        # --- Fallback Provider Order (drag-to-reorder, only relevant in Auto mode) ---
        fo_card = QFrame()
        fo_card.setObjectName("Card")
        fo_layout = QVBoxLayout(fo_card)
        fo_layout.setContentsMargins(16, 16, 16, 16)
        fo_layout.setSpacing(10)

        fo_layout.addWidget(self._section_label("Fallback Provider Order"))
        fo_note = QLabel(
            "When Auto mode is ON, providers are tried in this order. "
            "Drag rows to reorder. Only providers with API keys are actually used."
        )
        fo_note.setWordWrap(True)
        fo_note.setObjectName("CardNote")
        fo_layout.addWidget(fo_note)

        self.fallback_order_list = QListWidget()
        self.fallback_order_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.fallback_order_list.setDefaultDropAction(Qt.MoveAction)
        self.fallback_order_list.setFixedHeight(130)
        self.fallback_order_list.setObjectName("FallbackOrderList")
        # Populate with default order — load_config will override this
        for p in ["Google GenAI", "OpenAI", "OpenRouter", "Groq"]:
            self.fallback_order_list.addItem(p)
        fo_layout.addWidget(self.fallback_order_list)

        layout.addWidget(fo_card)

        # --- Item #19: Metadata templates (optional, reusable) ---
        tpl_card = QFrame()
        tpl_card.setObjectName("Card")
        tpl_layout = QVBoxLayout(tpl_card)
        tpl_layout.setContentsMargins(16, 16, 16, 16)
        tpl_layout.setSpacing(10)

        tpl_layout.addWidget(self._section_label("Metadata Templates"))
        tpl_note = QLabel(
            "Optional. A template can prepend/append fixed text to every title and "
            "description and add fixed keywords to every image."
        )
        tpl_note.setWordWrap(True)
        tpl_note.setObjectName("CardNote")
        tpl_layout.addWidget(tpl_note)

        tpl_select_row = QHBoxLayout()
        tpl_select_row.addWidget(QLabel("Active template:"))
        self.template_combo = QComboBox()
        self.template_combo.addItem("None")
        self.template_combo.setMinimumHeight(32)
        tpl_select_row.addWidget(self.template_combo, stretch=1)
        tpl_layout.addLayout(tpl_select_row)

        form = QFormLayout()
        self.tpl_name_input = QLineEdit()
        self.tpl_name_input.setPlaceholderText("e.g. Nature Pack")
        form.addRow("Template Name:", self.tpl_name_input)
        self.tpl_title_prefix = QLineEdit()
        form.addRow("Title Prefix:", self.tpl_title_prefix)
        self.tpl_title_suffix = QLineEdit()
        form.addRow("Title Suffix:", self.tpl_title_suffix)
        self.tpl_desc_prefix = QLineEdit()
        form.addRow("Description Prefix:", self.tpl_desc_prefix)
        self.tpl_desc_suffix = QLineEdit()
        form.addRow("Description Suffix:", self.tpl_desc_suffix)
        self.tpl_fixed_keywords = QLineEdit()
        self.tpl_fixed_keywords.setPlaceholderText("comma, separated, keywords")
        form.addRow("Fixed Keywords:", self.tpl_fixed_keywords)
        tpl_layout.addLayout(form)

        tpl_btn_row = QHBoxLayout()
        self.btn_save_template = QPushButton("Save Template")
        self.btn_save_template.setObjectName("SecBtn")
        self.btn_delete_template = QPushButton("Delete Template")
        self.btn_delete_template.setObjectName("SecBtn")
        tpl_btn_row.addWidget(self.btn_save_template)
        tpl_btn_row.addWidget(self.btn_delete_template)
        tpl_btn_row.addStretch()
        tpl_layout.addLayout(tpl_btn_row)

        layout.addWidget(tpl_card)

        layout.addStretch()

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(inner)
        outer_layout.addWidget(scroll)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("CardTitle")
        return lbl



"""
ui/pages/settings_page.py  —  SettingsPage with tabbed layout.

Tabs:
  AI Settings    — provider fallback order, auto-provider, batch timing
  Metadata       — title/keyword rules, custom keywords, validation, templates
  Processing     — batch size, delay, auto-embed, image resolution
"""
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTextEdit, QFormLayout, QFrame,
    QComboBox, QSpinBox, QCheckBox, QScrollArea, QSizePolicy,
    QAbstractItemView, QListWidget, QTabWidget, QGroupBox,
)

from core.stock_markets import MARKETS, get_all_market_names, MARKET_DISPLAY_NAMES

logger = logging.getLogger(__name__)

PROVIDER_MAP = {
    "Google GenAI":  "google",
    "OpenAI":        "openai",
    "OpenRouter":    "openrouter",
    "Groq":          "groq",
    "Mistral":       "mistral",
}
PROVIDER_KEY_LABELS = {
    "google":     "Google API Key",
    "openai":     "OpenAI API Key",
    "openrouter": "OpenRouter API Key",
    "groq":       "Groq API Key",
    "mistral":    "Mistral API Key",
}
PROVIDER_MODELS = {
    "google":     ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash",
                   "gemini-1.5-flash", "gemini-1.5-pro"],
    "openai":     ["gpt-4o-mini", "gpt-4o", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5"],
    "openrouter": ["google/gemini-2.5-flash", "openai/gpt-4o-mini", "openai/gpt-5.4-mini",
                   "meta-llama/llama-4-scout:free", "meta-llama/llama-4-maverick:free",
                   "anthropic/claude-3-haiku", "mistralai/pixtral-12b"],
    "groq":       ["meta-llama/llama-4-scout-17b-16e-instruct",
                   "meta-llama/llama-4-maverick-17b-128e-instruct"],
    "mistral":    ["pixtral-12b-2409", "pixtral-large-2411", "mistral-small-latest"],
}
PROVIDER_DOCS = {
    "google":     "https://aistudio.google.com/apikey",
    "openai":     "https://platform.openai.com/api-keys",
    "openrouter": "https://openrouter.ai/keys",
    "groq":       "https://console.groq.com/keys",
    "mistral":    "https://console.mistral.ai/api-keys",
}
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"}


def _card(title: str = "") -> tuple:
    """Return (QFrame card, QVBoxLayout inner_layout)."""
    card = QFrame()
    card.setObjectName("Card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    if title:
        lbl = QLabel(title)
        lbl.setObjectName("CardTitle")
        lay.addWidget(lbl)
    return card, lay


def _note(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("CardNote")
    lbl.setWordWrap(True)
    return lbl


def _scrollable_tab(content_widget: QWidget) -> QWidget:
    """Wrap a widget in a QScrollArea for a tab page."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidget(content_widget)
    outer = QWidget()
    lay = QVBoxLayout(outer)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(scroll)
    return outer


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 16)
        outer.setSpacing(10)

        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        outer.addWidget(title)

        sub = QLabel("Configure AI providers, metadata rules, and processing behaviour.")
        sub.setObjectName("PageSubtitle")
        sub.setWordWrap(True)
        outer.addWidget(sub)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("SettingsTabs")
        outer.addWidget(self._tabs, stretch=1)

        self._build_ai_tab()
        self._build_metadata_tab()
        self._build_processing_tab()

    # ──────────────────────────────────────────────────────────────────
    # Tab 1 — AI Settings
    # ──────────────────────────────────────────────────────────────────

    def _build_ai_tab(self):
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        # Auto-provider toggle
        ap_card, ap_lay = _card("Auto Provider & Fallback")
        self.chk_auto_provider = QCheckBox(
            "Automatically select provider and fall back on failure")
        self.chk_auto_provider.setChecked(True)
        ap_lay.addWidget(self.chk_auto_provider)
        ap_lay.addWidget(_note(
            "When ON, the app tries your chosen provider first. If it fails "
            "(network error, rate limit, bad key) it automatically retries "
            "with any other provider that has an API key configured. "
            "When OFF, only the selected provider is used."
        ))
        lay.addWidget(ap_card)

        # Fallback provider order
        fo_card, fo_lay = _card("Fallback Provider Order")
        fo_lay.addWidget(_note(
            "Drag rows to set priority. Providers without an API key are "
            "automatically skipped. Mistral is now included."
        ))
        self.fallback_order_list = QListWidget()
        self.fallback_order_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.fallback_order_list.setDefaultDropAction(Qt.MoveAction)
        self.fallback_order_list.setFixedHeight(148)
        self.fallback_order_list.setObjectName("FallbackOrderList")
        for p in ["Google GenAI", "OpenAI", "OpenRouter", "Groq", "Mistral"]:
            self.fallback_order_list.addItem(p)
        fo_lay.addWidget(self.fallback_order_list)
        lay.addWidget(fo_card)

        lay.addStretch()
        self._tabs.addTab(_scrollable_tab(content), "  AI Settings  ")

    # ──────────────────────────────────────────────────────────────────
    # Tab 2 — Metadata
    # ──────────────────────────────────────────────────────────────────

    def _build_metadata_tab(self):
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        # Title length
        tc_card, tc_lay = _card("Title Length")
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Min chars:"))
        self.spin_title_min = QSpinBox()
        self.spin_title_min.setRange(1, 50)
        self.spin_title_min.setValue(5)
        self.spin_title_min.setMinimumHeight(32)
        row1.addWidget(self.spin_title_min)
        row1.addSpacing(24)
        row1.addWidget(QLabel("Max chars:"))
        self.spin_title_max = QSpinBox()
        self.spin_title_max.setRange(10, 200)
        self.spin_title_max.setValue(70)
        self.spin_title_max.setMinimumHeight(32)
        row1.addWidget(self.spin_title_max)
        row1.addStretch()
        tc_lay.addLayout(row1)
        tc_lay.addWidget(_note("Adobe Stock recommends under 70 characters for best SEO."))
        lay.addWidget(tc_card)

        # Keyword count
        kw_card, kc_lay = _card("Keyword Count")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Min:"))
        self.spin_kw_min = QSpinBox()
        self.spin_kw_min.setRange(1, 499)
        self.spin_kw_min.setValue(7)
        self.spin_kw_min.setMinimumHeight(32)
        row2.addWidget(self.spin_kw_min)
        row2.addSpacing(24)
        row2.addWidget(QLabel("Max:"))
        self.spin_kw_max = QSpinBox()
        self.spin_kw_max.setRange(2, 500)
        self.spin_kw_max.setValue(49)
        self.spin_kw_max.setMinimumHeight(32)
        row2.addWidget(self.spin_kw_max)
        row2.addStretch()
        kc_lay.addLayout(row2)
        kc_lay.addWidget(_note(
            "Min must be less than Max. Both are passed directly to the AI."))
        self.spin_kw_min.valueChanged.connect(
            lambda v: self.spin_kw_max.setMinimum(v + 1))
        self.spin_kw_max.valueChanged.connect(
            lambda v: self.spin_kw_min.setMaximum(v - 1))
        lay.addWidget(kw_card)

        # Custom keywords
        ckw_card, ck_lay = _card("Custom Keywords")
        self.chk_custom_kw = QCheckBox("Prepend these keywords to every image")
        self.chk_custom_kw.setChecked(True)
        ck_lay.addWidget(self.chk_custom_kw)
        ck_lay.addWidget(QLabel("Keywords (comma-separated):"))
        self.custom_kw_input = QTextEdit()
        self.custom_kw_input.setPlaceholderText("e.g.  nature, outdoor, spring")
        self.custom_kw_input.setFixedHeight(72)
        ck_lay.addWidget(self.custom_kw_input)
        ck_lay.addWidget(_note(
            "These are always prepended before AI-generated keywords on every image."))
        lay.addWidget(ckw_card)

        # Marketplace validation
        mv_card, mv_lay = _card("Marketplace Validation")
        self.chk_marketplace_validation = QCheckBox(
            "Enable Marketplace Rule Validation (trims to platform limits)")
        self.chk_marketplace_validation.setChecked(False)
        mv_lay.addWidget(self.chk_marketplace_validation)
        mv_lay.addWidget(_note(
            "When enabled, character and keyword-count limits for the selected "
            "market are enforced and metadata is automatically trimmed. "
            "Default OFF — AI output is written exactly as generated."
        ))
        lay.addWidget(mv_card)

        # Templates
        tpl_card, tpl_lay = _card("Metadata Templates")
        tpl_lay.addWidget(_note(
            "Optional. A template prepends/appends fixed text to every title and "
            "description and adds fixed keywords to every image."
        ))
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Active template:"))
        self.template_combo = QComboBox()
        self.template_combo.addItem("None")
        self.template_combo.setMinimumHeight(32)
        sel_row.addWidget(self.template_combo, stretch=1)
        tpl_lay.addLayout(sel_row)

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
        tpl_lay.addLayout(form)

        btn_row = QHBoxLayout()
        self.btn_save_template = QPushButton("Save Template")
        self.btn_save_template.setObjectName("SecBtn")
        self.btn_delete_template = QPushButton("Delete Template")
        self.btn_delete_template.setObjectName("SecBtn")
        btn_row.addWidget(self.btn_save_template)
        btn_row.addWidget(self.btn_delete_template)
        btn_row.addStretch()
        tpl_lay.addLayout(btn_row)
        lay.addWidget(tpl_card)

        lay.addStretch()
        self._tabs.addTab(_scrollable_tab(content), "  Metadata  ")

    # ──────────────────────────────────────────────────────────────────
    # Tab 3 — Processing
    # ──────────────────────────────────────────────────────────────────

    def _build_processing_tab(self):
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        # Batch size + delay (combined card)
        bp_card, bp_lay = _card("Batch Processing")
        bp_lay.addWidget(_note(
            "Batch Size controls how many images are sent to the AI in parallel. "
            "Higher values are faster but may hit API rate limits."))

        bs_row = QHBoxLayout()
        bs_row.addWidget(QLabel("Batch Size:"))
        self.spin_batch_size = QSpinBox()
        self.spin_batch_size.setRange(1, 10)
        self.spin_batch_size.setValue(3)
        self.spin_batch_size.setMinimumWidth(70)
        self.spin_batch_size.setMinimumHeight(32)
        bs_row.addWidget(self.spin_batch_size)
        for n in [1, 3, 5]:
            b = QPushButton(str(n))
            b.setObjectName("ChipBtn")
            b.setFixedSize(36, 28)
            b.clicked.connect(lambda checked, v=n: self.spin_batch_size.setValue(v))
            bs_row.addWidget(b)
        bs_row.addStretch()
        bp_lay.addLayout(bs_row)

        bp_lay.addSpacing(6)
        bp_lay.addWidget(_note(
            "Delay Between Batches: seconds to wait after completing each batch "
            "before starting the next. Use this to avoid hitting per-minute "
            "rate limits. Set to 0 to disable (process immediately)."))

        bd_row = QHBoxLayout()
        bd_row.addWidget(QLabel("Delay (seconds):"))
        self.spin_batch_delay = QSpinBox()
        self.spin_batch_delay.setRange(0, 300)
        self.spin_batch_delay.setValue(0)
        self.spin_batch_delay.setSuffix(" s")
        self.spin_batch_delay.setMinimumWidth(80)
        self.spin_batch_delay.setMinimumHeight(32)
        self.spin_batch_delay.setToolTip(
            "0 = no delay. e.g. 10 = wait 10 seconds after each batch of images.")
        bd_row.addWidget(self.spin_batch_delay)
        for n in [0, 5, 10, 30]:
            b = QPushButton(str(n) + "s")
            b.setObjectName("ChipBtn")
            b.setFixedSize(40, 28)
            b.clicked.connect(lambda checked, v=n: self.spin_batch_delay.setValue(v))
            bd_row.addWidget(b)
        bd_row.addStretch()
        bp_lay.addLayout(bd_row)
        lay.addWidget(bp_card)

        # Auto-embed
        ae_card, ae_lay = _card("Auto Embed Metadata")
        self.chk_auto_embed = QCheckBox(
            "Automatically write metadata to file after generation")
        self.chk_auto_embed.setChecked(True)
        ae_lay.addWidget(self.chk_auto_embed)
        ae_lay.addWidget(_note(
            "When ON (default), metadata is embedded into each image file "
            "immediately after the AI generates it. When OFF, use "
            "'Write to File' or 'Save All to Files' to embed manually."
        ))
        lay.addWidget(ae_card)

        # Image resolution
        ir_card, ir_lay = _card("Image Resolution for AI")
        ir_lay.addWidget(_note(
            "Images are resized before being sent to the AI to reduce token "
            "usage. 512 px (recommended) saves the most tokens with no quality "
            "loss for metadata generation. Higher resolutions send more detail "
            "but burn tokens much faster."
        ))
        ir_row = QHBoxLayout()
        ir_row.addWidget(QLabel("Max dimension:"))
        self.combo_image_res = QComboBox()
        self.combo_image_res.addItems([
            "512  — Max saving (recommended)",
            "768  — Balanced",
            "1024 — High detail",
            "1536 — Maximum detail",
        ])
        self.combo_image_res.setCurrentIndex(0)
        self.combo_image_res.setMinimumHeight(32)
        ir_row.addWidget(self.combo_image_res)
        ir_row.addStretch()
        ir_lay.addLayout(ir_row)

        # Token-cost indicator — updates live as the user changes the combo
        self.res_cost_lbl = QLabel("~85 input tokens/image  •  ~20–25× cheaper than maximum")
        self.res_cost_lbl.setObjectName("CardNote")
        ir_lay.addWidget(self.res_cost_lbl)
        self.combo_image_res.currentIndexChanged.connect(self._update_res_cost_label)

        lay.addWidget(ir_card)

        lay.addStretch()
        self._tabs.addTab(_scrollable_tab(content), "  Processing  ")

    # ──────────────────────────────────────────────────────────────────
    # Helpers (kept for external callers in ui_main.py)
    # ──────────────────────────────────────────────────────────────────

    def get_image_resolution(self) -> int:
        idx = self.combo_image_res.currentIndex()
        return [512, 768, 1024, 1536][idx]

    def _update_res_cost_label(self, index: int) -> None:
        """Update the token-cost hint label when image resolution changes."""
        labels = [
            "~85 input tokens/image  •  ~20–25× cheaper than maximum (recommended)",
            "~190 input tokens/image  •  ~10× cheaper than maximum",
            "~340 input tokens/image  •  ~5× cheaper than maximum",
            "~850 input tokens/image  •  maximum token usage",
        ]
        self.res_cost_lbl.setText(labels[index])

    def _section_label(self, text: str) -> QLabel:
        """Kept for backward compat; use _card() internally now."""
        lbl = QLabel(text)
        lbl.setObjectName("CardTitle")
        return lbl

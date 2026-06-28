
import os
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPixmap, QFont, QIcon, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QTableWidget, QTableWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QProgressBar, QFormLayout, QFrame,
    QHeaderView, QComboBox, QMessageBox, QFileDialog,
    QSpinBox, QCheckBox, QScrollArea, QSizePolicy, QGroupBox,
    QAbstractItemView, QListWidget, QInputDialog,
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

NAV_ITEMS = [
    ("queue",    "Queue"),
    ("market",   "Market"),
    ("settings", "Settings"),
    ("ai",       "AI Studio"),
    ("history",  "History"),
]


# ============================================================================
# Batch table
# ============================================================================

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

        self.batch_table = BatchTableWidget()
        layout.addWidget(self.batch_table, stretch=1)

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
        self.btn_process = QPushButton("Generate Metadata")
        self.btn_process.setObjectName("PrimaryBtn")
        self.btn_process.setMinimumHeight(36)

        bar_layout.addWidget(self.btn_add_files)
        bar_layout.addWidget(self.btn_add_folder)
        bar_layout.addWidget(self.btn_clear)
        bar_layout.addWidget(self.btn_cancel)
        bar_layout.addStretch()
        bar_layout.addWidget(self.btn_save_all)   # NEW
        bar_layout.addWidget(self.btn_process)
        layout.addWidget(bar)


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


class MetaEmbedMainWindow(QMainWindow):
    request_processing          = Signal(list, int)   # [file_path, ...], batch_size
    save_config_requested       = Signal(dict)
    cancel_requested             = Signal()
    write_single_requested      = Signal(str, str, str, list)
    save_all_requested           = Signal()            # write all results at once
    export_requested             = Signal(str)
    regenerate_single_requested  = Signal(str)         # Item #12 — path to regenerate
    clear_history_requested      = Signal()            # Item #7 — wired to HistoryManager
    refresh_history_requested    = Signal()            # triggers Controller to fetch & push history data

    def __init__(self, config_manager=None):
        super().__init__()
        self._config = config_manager
        self._current_preview_path: Optional[str] = None
        self._row_results: dict[int, dict] = {}

        self.setWindowTitle("MetaEmbed AI — Commercial Metadata Engine")
        self.resize(1440, 880)
        self._apply_stylesheet()
        self._setup_ui()
        self._connect_internal_signals()

    # ------------------------------------------------------------------
    # Controller API
    # ------------------------------------------------------------------

    def load_config(self, config_manager):
        self._config = config_manager

        # API keys in AI Studio
        for provider, inp in self.ai_page.api_inputs.items():
            inp.setText(config_manager.get("api_keys", provider) or "")

        # Provider combo
        saved = config_manager.get_active_provider()
        for display, key in PROVIDER_MAP.items():
            if key == saved:
                self.ai_page.provider_combo.setCurrentIndex(
                    self.ai_page.provider_combo.findText(display))
                break

        # Model combo
        saved_model = config_manager.get("default_models", saved) or ""
        idx = self.ai_page.model_combo.findText(saved_model)
        if idx >= 0:
            self.ai_page.model_combo.setCurrentIndex(idx)

        # Market
        market_key = config_manager.get_active_market()
        for m in MARKETS.values():
            if m.key == market_key:
                self.market_page.market_combo.setCurrentIndex(
                    self.market_page.market_combo.findText(m.name))
                self.market_page._on_market_changed(m.name)
                break

        # Metadata rules
        rules = config_manager.get_metadata_rules()
        self.settings_page.spin_title_min.setValue(int(rules.get("title_min_length", 5)))
        self.settings_page.spin_title_max.setValue(int(rules.get("title_max_length", 70)))
        self.settings_page.spin_kw_min.setValue(int(rules.get("keyword_min_count", 7)))
        self.settings_page.spin_kw_max.setValue(int(rules.get("keyword_max_count", 49)))
        self.settings_page.chk_custom_kw.setChecked(bool(rules.get("custom_keywords_enabled", True)))
        custom_kw = config_manager.get_custom_keywords()
        self.settings_page.custom_kw_input.setPlainText(", ".join(custom_kw))

        # Item #21 — marketplace validation toggle (off by default), saved/restored
        self.settings_page.chk_marketplace_validation.setChecked(
            bool(rules.get("marketplace_validation_enabled", False))
        )
        self.settings_page.chk_auto_embed.setChecked(
            bool(rules.get("auto_embed", True))
        )
        self.settings_page.chk_auto_provider.setChecked(
            bool(rules.get("auto_provider", True))
        )

        # Fallback provider order — map internal keys to display names
        _key_to_display = {v: k for k, v in PROVIDER_MAP.items()}
        saved_order = config_manager.get_fallback_provider_order()
        self.settings_page.fallback_order_list.clear()
        for key in saved_order:
            display = _key_to_display.get(key, key.title())
            self.settings_page.fallback_order_list.addItem(display)
        # Append any provider not in saved order (future-proofing)
        saved_keys = set(saved_order)
        for key in ["google", "openai", "openrouter", "groq"]:
            if key not in saved_keys:
                display = _key_to_display.get(key, key.title())
                self.settings_page.fallback_order_list.addItem(display)

        # Batch size
        batch_size = config_manager.get("system", "batch_size") or 3
        self.settings_page.spin_batch_size.setValue(int(batch_size))

        # Item #19 — metadata templates
        self._reload_templates_combo()
        active_tpl_name = config_manager.get_active_template_name()
        if active_tpl_name:
            idx = self.settings_page.template_combo.findText(active_tpl_name)
            if idx >= 0:
                self.settings_page.template_combo.setCurrentIndex(idx)

        # Update AI studio status
        self.ai_page._update_status()

    def get_selected_provider(self) -> str:
        return self.ai_page.get_selected_provider()

    def get_selected_model(self) -> str:
        return self.ai_page.get_selected_model()

    def get_selected_market(self) -> str:
        return self.market_page.get_selected_market()

    def get_row_for_path(self, path: str) -> Optional[int]:
        """Item #12 — needed by Controller._regenerate_single to know which
        table row to target."""
        return self.queue_page.batch_table.get_row_for_path(path)

    def show_batch_summary(self, summary) -> None:
        """Item #2/#9 — always show a final summary, whether the batch
        completed normally or was cancelled partway through."""
        title = "Batch Cancelled" if summary.cancelled else "Batch Complete"
        QMessageBox.information(self, title, summary.to_message())

    def update_progress(self, info) -> None:
        """Item #8 — rich progress display: current image, remaining,
        live success/failed counts, and an ETA, while staying responsive
        (this slot just updates labels; all real work happens off-thread
        in the Worker)."""
        self.progress_bar.setValue(info.percent)
        eta_text = info.eta_text()
        self.progress_bar.setFormat(
            f"{info.percent}%  —  {info.current_index}/{info.total}  "
            f"({info.images_remaining} remaining)  —  "
            f"OK:{info.success}  Fail:{info.failed}  Skip:{info.skipped}  —  "
            f"ETA {eta_text}"
        )
        if info.current_image:
            self.progress_detail_lbl.setText(f"Processing: {info.current_image}")
        else:
            self.progress_detail_lbl.setText("")

    def update_row_status(self, row: int, status: str):
        self.queue_page.batch_table.update_row_status(row, status)

    def on_result_ready(self, row: int, result: dict):
        self._row_results[row] = result
        if self.queue_page.batch_table.currentRow() == row:
            self._populate_inspector_from_dict(result)
        # Enable Save All the moment we have at least one result
        self.queue_page.btn_save_all.setEnabled(True)

    def get_all_results(self) -> list:
        """
        Bug fix: previously, "Save All to Files" and CSV export read from
        Controller._batch_results, which the Controller clears and
        repopulates from scratch on every single worker run (full batch,
        regenerate-one, etc.). That meant only the MOST RECENT run's
        results were ever available to save/export — if you generated
        metadata for the whole queue, then regenerated just one image
        afterward, "Save All" would only see that one regenerated image
        and report "Generate metadata first" even though every image
        actually had metadata.

        self._row_results, by contrast, is keyed by table row and
        accumulates across every run for the lifetime of the queue (it's
        only cleared when the queue itself is cleared) — it's exactly
        the cumulative, always-correct source the UI already uses to
        decide whether Save All should be enabled at all. This method
        is the single source of truth both that enable/disable check and
        the actual save/export actions should read from, so the two can
        never disagree again.
        """
        records = []
        for row, result in self._row_results.items():
            path = self.queue_page.batch_table.get_path_at_row(row)
            if not path:
                continue
            record = dict(result)
            record["filename"] = path
            records.append(record)
        return records

    def set_processing_state(self, running: bool):
        self.queue_page.btn_process.setEnabled(not running)
        self.queue_page.btn_cancel.setEnabled(running)
        self.queue_page.btn_clear.setEnabled(not running)
        self.queue_page.btn_add_files.setEnabled(not running)
        self.queue_page.btn_add_folder.setEnabled(not running)
        # Save All becomes available only after a completed batch
        if running:
            self.queue_page.btn_save_all.setEnabled(False)
        else:
            # Enable if we have any results stored (checked via row_results)
            has_results = bool(self._row_results)
            self.queue_page.btn_save_all.setEnabled(has_results)
            self.progress_detail_lbl.setText("")

    def show_warning(self, title: str, msg: str):
        QMessageBox.warning(self, title, msg)

    def show_error(self, msg: str):
        QMessageBox.critical(self, "Error", msg)

    def show_info(self, title: str, msg: str):
        QMessageBox.information(self, title, msg)

    def refresh_history(self, entries: list, stats: dict) -> None:
        """Called by Controller with fresh history data to display."""
        self.history_page.refresh(entries, stats)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Sidebar
        self.sidebar = self._build_sidebar()
        root.addWidget(self.sidebar)

        # Content area (stack + inspector + progress)
        content_wrapper = QWidget()
        content_wrapper.setObjectName("ContentWrapper")
        cw_layout = QVBoxLayout(content_wrapper)
        cw_layout.setContentsMargins(0, 0, 0, 0)
        cw_layout.setSpacing(0)

        # Main content (pages + inspector side by side)
        main_area = QWidget()
        main_layout = QHBoxLayout(main_area)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Page stack
        self.stack = QStackedWidget()
        self.queue_page    = QueuePage()
        self.market_page   = MarketPage()
        self.settings_page = SettingsPage()
        self.ai_page       = AIStudioPage()
        self.history_page  = HistoryPage()

        self.stack.addWidget(self.queue_page)
        self.stack.addWidget(self.market_page)
        self.stack.addWidget(self.settings_page)
        self.stack.addWidget(self.ai_page)
        self.stack.addWidget(self.history_page)

        main_layout.addWidget(self.stack, stretch=1)
        main_layout.addWidget(self._build_inspector())

        cw_layout.addWidget(main_area, stretch=1)

        # Progress bar + current-image detail at bottom (item #8)
        self.progress_detail_lbl = QLabel("")
        self.progress_detail_lbl.setObjectName("CardNote")
        self.progress_detail_lbl.setContentsMargins(8, 2, 8, 0)
        cw_layout.addWidget(self.progress_detail_lbl)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("MainProgress")
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Ready  —  0 / 0 images")
        self.progress_bar.setFixedHeight(28)
        cw_layout.addWidget(self.progress_bar)

        root.addWidget(content_wrapper, stretch=1)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Logo area
        logo_area = QWidget()
        logo_area.setObjectName("LogoArea")
        logo_layout = QVBoxLayout(logo_area)
        logo_layout.setContentsMargins(20, 22, 20, 18)
        app_name = QLabel("MetaEmbed")
        app_name.setObjectName("AppName")
        app_sub  = QLabel("AI · METADATA ENGINE")
        app_sub.setObjectName("AppSub")
        logo_layout.addWidget(app_name)
        logo_layout.addWidget(app_sub)
        layout.addWidget(logo_area)

        # Nav separator
        sep = QFrame()
        sep.setObjectName("SidebarSep")
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        # Nav buttons
        nav_area = QWidget()
        nav_layout = QVBoxLayout(nav_area)
        nav_layout.setContentsMargins(12, 12, 12, 12)
        nav_layout.setSpacing(4)

        self._nav_buttons: list[QPushButton] = []
        page_indices = {"queue": 0, "market": 1, "settings": 2, "ai": 3, "history": 4}

        # Map page IDs to QStyle standard pixel-map icons (no emoji needed)
        from PySide6.QtWidgets import QStyle
        _style = self.style()
        _nav_icons = {
            "queue":    _style.standardIcon(QStyle.SP_MediaPlay),
            "market":   _style.standardIcon(QStyle.SP_DriveNetIcon),
            "settings": _style.standardIcon(QStyle.SP_FileDialogDetailedView),
            "ai":       _style.standardIcon(QStyle.SP_ComputerIcon),
            "history":  _style.standardIcon(QStyle.SP_FileDialogInfoView),
        }

        for page_id, label in NAV_ITEMS:
            btn = QPushButton(f"  {label}")
            btn.setIcon(_nav_icons.get(page_id, _style.standardIcon(QStyle.SP_FileIcon)))
            btn.setIconSize(QSize(16, 16))
            btn.setObjectName("NavBtn")
            btn.setCheckable(True)
            btn.setMinimumHeight(40)
            idx = page_indices[page_id]
            btn.clicked.connect(lambda checked, i=idx: self._navigate(i))
            self._nav_buttons.append(btn)
            nav_layout.addWidget(btn)

        nav_layout.addStretch()
        layout.addWidget(nav_area, stretch=1)

        # Save button at bottom
        bottom = QWidget()
        bottom.setObjectName("SidebarBottom")
        bot_layout = QVBoxLayout(bottom)
        bot_layout.setContentsMargins(12, 12, 12, 16)
        self.btn_save_config = QPushButton("Save Configuration")
        self.btn_save_config.setObjectName("SaveBtn")
        self.btn_save_config.setMinimumHeight(38)
        bot_layout.addWidget(self.btn_save_config)
        layout.addWidget(bottom)

        # Set first item active
        self._nav_buttons[0].setChecked(True)
        return sidebar

    def _navigate(self, index: int):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self._nav_buttons):
            btn.setChecked(i == index)
        # Auto-refresh history when the user switches to the history page
        if index == 4:
            self._request_history_refresh()

    def _request_history_refresh(self):
        """Tell the Controller to fetch history data and call refresh_history."""
        self.refresh_history_requested.emit()

    def _build_inspector(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Inspector")
        frame.setFixedWidth(360)
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        hdr = QLabel("Inspector")
        hdr.setObjectName("PageTitle")
        hdr.setContentsMargins(0, 0, 0, 4)
        layout.addWidget(hdr)

        self.img_preview = QLabel("No image selected")
        self.img_preview.setObjectName("ImagePreview")
        self.img_preview.setAlignment(Qt.AlignCenter)
        self.img_preview.setFixedHeight(200)
        self.img_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.img_preview)

        self.path_label = QLabel("")
        self.path_label.setObjectName("CardNote")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)

        # Item #12 — regenerate metadata for just this one image
        self.btn_regenerate = QPushButton("↺  Regenerate")
        self.btn_regenerate.setObjectName("SecBtn")
        layout.addWidget(self.btn_regenerate)

        # --- Title ---
        title_row = QHBoxLayout()
        title_row.addWidget(self._field_label("Title"))
        title_row.addStretch()
        self.btn_copy_title = QPushButton("Copy")
        self.btn_copy_title.setObjectName("ChipBtn")
        self.btn_copy_title.setFixedHeight(24)
        title_row.addWidget(self.btn_copy_title)
        layout.addLayout(title_row)

        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Generated title…")
        self.title_input.setMinimumHeight(34)
        layout.addWidget(self.title_input)

        self.title_char_lbl = QLabel("0 chars")
        self.title_char_lbl.setObjectName("CardNote")
        self.title_input.textChanged.connect(
            lambda t: self.title_char_lbl.setText(f"{len(t)} chars"))
        layout.addWidget(self.title_char_lbl)

        # --- Description ---
        desc_row = QHBoxLayout()
        desc_row.addWidget(self._field_label("Description"))
        desc_row.addStretch()
        self.btn_copy_desc = QPushButton("Copy")
        self.btn_copy_desc.setObjectName("ChipBtn")
        self.btn_copy_desc.setFixedHeight(24)
        desc_row.addWidget(self.btn_copy_desc)
        layout.addLayout(desc_row)

        self.desc_input = QTextEdit()
        self.desc_input.setFixedHeight(72)
        self.desc_input.setPlaceholderText("Generated description…")
        layout.addWidget(self.desc_input)

        # --- Keywords ---
        kw_row = QHBoxLayout()
        kw_row.addWidget(self._field_label("Keywords"))
        kw_row.addStretch()
        self.btn_copy_keywords = QPushButton("Copy")
        self.btn_copy_keywords.setObjectName("ChipBtn")
        self.btn_copy_keywords.setFixedHeight(24)
        kw_row.addWidget(self.btn_copy_keywords)
        layout.addLayout(kw_row)

        self.keywords_input = QTextEdit()
        self.keywords_input.setFixedHeight(84)
        self.keywords_input.setPlaceholderText("keyword1, keyword2, …")
        self.keywords_input.textChanged.connect(self._update_kw_count)
        layout.addWidget(self.keywords_input)

        self.kw_count_lbl = QLabel("0 keywords")
        self.kw_count_lbl.setObjectName("CardNote")
        layout.addWidget(self.kw_count_lbl)

        # Item #20 — overall metadata quality score, updated live as fields change
        score_card = QFrame()
        score_card.setObjectName("Card")
        score_layout = QVBoxLayout(score_card)
        score_layout.setContentsMargins(12, 10, 12, 10)
        score_layout.setSpacing(6)

        score_top = QHBoxLayout()
        self.quality_score_lbl = QLabel("Quality Score: —")
        self.quality_score_lbl.setObjectName("CardTitle")
        score_top.addWidget(self.quality_score_lbl)
        score_top.addStretch()
        self.quality_label_lbl = QLabel("")
        self.quality_label_lbl.setObjectName("CardNote")
        score_top.addWidget(self.quality_label_lbl)
        score_layout.addLayout(score_top)

        self.quality_bar = QProgressBar()
        self.quality_bar.setRange(0, 100)
        self.quality_bar.setValue(0)
        self.quality_bar.setTextVisible(False)
        self.quality_bar.setFixedHeight(8)
        score_layout.addWidget(self.quality_bar)

        self.quality_detail_lbl = QLabel("")
        self.quality_detail_lbl.setObjectName("CardNote")
        self.quality_detail_lbl.setWordWrap(True)
        score_layout.addWidget(self.quality_detail_lbl)

        layout.addWidget(score_card)

        # --- Copy all + action buttons ---
        self.btn_copy_all = QPushButton("Copy All Metadata")
        self.btn_copy_all.setObjectName("SecBtn")
        layout.addWidget(self.btn_copy_all)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_revert = QPushButton("Revert")
        self.btn_revert.setObjectName("SecBtn")
        self.btn_save_meta = QPushButton("Write to File")
        self.btn_save_meta.setObjectName("PrimaryBtn")
        self.btn_save_meta.setMinimumHeight(36)
        btn_row.addWidget(self.btn_revert)
        btn_row.addWidget(self.btn_save_meta)
        layout.addLayout(btn_row)

        layout.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        return frame

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("FieldLabel")
        return lbl

    # ------------------------------------------------------------------
    # Internal signal wiring
    # ------------------------------------------------------------------

    def _connect_internal_signals(self):
        self.queue_page.btn_process.clicked.connect(self._trigger_processing)
        self.queue_page.btn_clear.clicked.connect(self._clear_queue)
        self.queue_page.btn_cancel.clicked.connect(self.cancel_requested)
        self.queue_page.btn_add_files.clicked.connect(self._open_file_dialog)
        self.queue_page.btn_add_folder.clicked.connect(self._open_folder_dialog)
        self.queue_page.btn_save_all.clicked.connect(self.save_all_requested)   # NEW
        self.btn_save_config.clicked.connect(self._emit_save_config)
        self.btn_save_meta.clicked.connect(self._emit_write_single)
        self.btn_revert.clicked.connect(self._revert_inspector)
        self.btn_regenerate.clicked.connect(self._emit_regenerate_single)
        self.btn_copy_title.clicked.connect(lambda: self._copy_to_clipboard(self.title_input.text()))
        self.btn_copy_desc.clicked.connect(lambda: self._copy_to_clipboard(self.desc_input.toPlainText()))
        self.btn_copy_keywords.clicked.connect(lambda: self._copy_to_clipboard(self.keywords_input.toPlainText()))
        self.btn_copy_all.clicked.connect(self._copy_all_metadata)
        self.market_page.btn_export.clicked.connect(
            lambda: self.export_requested.emit(self.get_selected_market()))
        self.queue_page.batch_table.itemSelectionChanged.connect(
            self._on_table_selection_changed)
        self.title_input.textChanged.connect(self._refresh_quality_score)
        self.desc_input.textChanged.connect(self._refresh_quality_score)
        self.keywords_input.textChanged.connect(self._refresh_quality_score)
        self.settings_page.template_combo.currentTextChanged.connect(self._on_template_selected)
        self.settings_page.btn_save_template.clicked.connect(self._save_template)
        self.settings_page.btn_delete_template.clicked.connect(self._delete_template)
        self.history_page.clear_history_requested.connect(self.clear_history_requested)
        self.history_page.btn_refresh.clicked.connect(self._request_history_refresh)

    # ------------------------------------------------------------------
    # Slot implementations
    # ------------------------------------------------------------------

    def _trigger_processing(self):
        files = self.queue_page.batch_table.get_all_paths()
        if not files:
            self.show_warning("Empty Queue", "Add images before processing.")
            return
        batch_size = self.settings_page.spin_batch_size.value()
        self.request_processing.emit(files, batch_size)

    def _clear_queue(self):
        self.queue_page.batch_table.clear_queue()
        self._row_results.clear()
        self.queue_page.btn_save_all.setEnabled(False)   # reset when queue is cleared
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Ready  —  0 / 0 images")
        self.progress_detail_lbl.setText("")
        self._clear_inspector()

    def _open_file_dialog(self):
        start_dir = self._config.get_last_opened_folder() if self._config else ""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add Image Files", start_dir or "",
            "Images (*.jpg *.jpeg *.png *.tiff *.tif *.webp)"
        )
        for p in paths:
            self.queue_page.batch_table.add_file(p)
        if paths and self._config:
            self._config.set_last_opened_folder(str(Path(paths[0]).parent))

    def _open_folder_dialog(self):
        """Item #14 — add every supported image found in a chosen folder (recursive)."""
        start_dir = self._config.get_last_opened_folder() if self._config else ""
        folder = QFileDialog.getExistingDirectory(self, "Add Folder of Images", start_dir or "")
        if not folder:
            return
        added = 0
        before = self.queue_page.batch_table.rowCount()
        for root_dir, _dirs, filenames in os.walk(folder):
            for fname in sorted(filenames):
                self.queue_page.batch_table.add_file(os.path.join(root_dir, fname))
        added = self.queue_page.batch_table.rowCount() - before
        if self._config:
            self._config.set_last_opened_folder(folder)
        if added == 0:
            self.show_warning("No Images Found", f"No supported images found in:\n{folder}")

    def _emit_save_config(self):
        raw_kw = self.settings_page.custom_kw_input.toPlainText()
        custom_keywords = [k.strip() for k in raw_kw.split(",") if k.strip()]

        provider_key = self.ai_page.get_selected_provider()
        model = self.ai_page.get_selected_model()

        # Collect fallback order from drag-to-reorder list (convert display → key)
        fo_list = self.settings_page.fallback_order_list
        fallback_order = [
            PROVIDER_MAP.get(fo_list.item(i).text(), fo_list.item(i).text().lower())
            for i in range(fo_list.count())
        ]

        self.save_config_requested.emit({
            "api_keys": {p: inp.text().strip() for p, inp in self.ai_page.api_inputs.items()},
            "active_provider": provider_key,
            "active_model": model,
            "batch_size": self.settings_page.spin_batch_size.value(),
            "metadata_rules": {
                "title_min_length":        self.settings_page.spin_title_min.value(),
                "title_max_length":        self.settings_page.spin_title_max.value(),
                "keyword_min_count":       self.settings_page.spin_kw_min.value(),
                "keyword_max_count":       self.settings_page.spin_kw_max.value(),
                "custom_keywords":         custom_keywords,
                "custom_keywords_enabled": self.settings_page.chk_custom_kw.isChecked(),
                "marketplace_validation_enabled": self.settings_page.chk_marketplace_validation.isChecked(),
                "auto_embed":    self.settings_page.chk_auto_embed.isChecked(),
                "auto_provider": self.settings_page.chk_auto_provider.isChecked(),
                "fallback_provider_order": fallback_order,
            },
            "active_market": self.get_selected_market(),
        })

    def _emit_write_single(self):
        path = self._current_preview_path
        if not path:
            self.show_warning("No File Selected", "Select a file in the queue first.")
            return
        title    = self.title_input.text().strip()
        desc     = self.desc_input.toPlainText().strip()
        keywords = [k.strip() for k in self.keywords_input.toPlainText().split(",") if k.strip()]

        # Item #17 — warn before embedding if something looks off.
        warnings = check_metadata_quality(title, desc, keywords)
        if warnings:
            proceed = QMessageBox.warning(
                self, "Metadata Quality Warning",
                "Before writing, please note:\n\n" + "\n".join(f"• {w}" for w in warnings) +
                "\n\nWrite anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if proceed != QMessageBox.Yes:
                return

        self.write_single_requested.emit(path, title, desc, keywords)

    def _emit_regenerate_single(self):
        """Item #12 — regenerate metadata for only the currently-selected image."""
        path = self._current_preview_path
        if not path:
            self.show_warning("No File Selected", "Select a file in the queue first.")
            return
        self.regenerate_single_requested.emit(path)

    # ------------------------------------------------------------------
    # Item #19 — metadata templates
    # ------------------------------------------------------------------

    def _reload_templates_combo(self):
        if not self._config:
            return
        templates = self._config.get_templates()
        combo = self.settings_page.template_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("None")
        for t in templates:
            combo.addItem(t.get("name", "Unnamed"))
        combo.blockSignals(False)

    def _on_template_selected(self, name: str):
        if not self._config:
            return
        if name == "None" or not name:
            self.settings_page.tpl_name_input.clear()
            self.settings_page.tpl_title_prefix.clear()
            self.settings_page.tpl_title_suffix.clear()
            self.settings_page.tpl_desc_prefix.clear()
            self.settings_page.tpl_desc_suffix.clear()
            self.settings_page.tpl_fixed_keywords.clear()
            self._config.set_active_template_name("")
            return
        for t in self._config.get_templates():
            if t.get("name") == name:
                self.settings_page.tpl_name_input.setText(t.get("name", ""))
                self.settings_page.tpl_title_prefix.setText(t.get("title_prefix", ""))
                self.settings_page.tpl_title_suffix.setText(t.get("title_suffix", ""))
                self.settings_page.tpl_desc_prefix.setText(t.get("description_prefix", ""))
                self.settings_page.tpl_desc_suffix.setText(t.get("description_suffix", ""))
                self.settings_page.tpl_fixed_keywords.setText(
                    ", ".join(t.get("fixed_keywords", []))
                )
                self._config.set_active_template_name(name)
                return

    def _save_template(self):
        if not self._config:
            return
        name = self.settings_page.tpl_name_input.text().strip()
        if not name:
            self.show_warning("Template Name Required", "Enter a name for this template before saving.")
            return
        fixed_kw = [k.strip() for k in self.settings_page.tpl_fixed_keywords.text().split(",") if k.strip()]
        new_template = {
            "name": name,
            "title_prefix": self.settings_page.tpl_title_prefix.text().strip(),
            "title_suffix": self.settings_page.tpl_title_suffix.text().strip(),
            "description_prefix": self.settings_page.tpl_desc_prefix.text().strip(),
            "description_suffix": self.settings_page.tpl_desc_suffix.text().strip(),
            "fixed_keywords": fixed_kw,
        }
        templates = self._config.get_templates()
        templates = [t for t in templates if t.get("name") != name]
        templates.append(new_template)
        self._config.set_templates(templates)
        self._config.set_active_template_name(name)
        self._reload_templates_combo()
        idx = self.settings_page.template_combo.findText(name)
        if idx >= 0:
            self.settings_page.template_combo.setCurrentIndex(idx)
        self.show_info("Template Saved", f"Template '{name}' saved.")

    def _delete_template(self):
        if not self._config:
            return
        name = self.settings_page.template_combo.currentText()
        if name == "None" or not name:
            self.show_warning("No Template Selected", "Select a template to delete first.")
            return
        templates = [t for t in self._config.get_templates() if t.get("name") != name]
        self._config.set_templates(templates)
        self._config.set_active_template_name("")
        self._reload_templates_combo()
        self.settings_page.template_combo.setCurrentIndex(0)
        self.show_info("Template Deleted", f"Template '{name}' deleted.")

    def _copy_to_clipboard(self, text: str):
        """Item #13 — one-click copy."""
        if not text:
            self.show_warning("Nothing to Copy", "This field is empty.")
            return
        QGuiApplication.clipboard().setText(text)

    def _copy_all_metadata(self):
        """Item #13 — copy Title + Description + Keywords as one block."""
        title    = self.title_input.text().strip()
        desc     = self.desc_input.toPlainText().strip()
        keywords = self.keywords_input.toPlainText().strip()
        if not (title or desc or keywords):
            self.show_warning("Nothing to Copy", "No metadata to copy yet.")
            return
        combined = f"Title: {title}\n\nDescription: {desc}\n\nKeywords: {keywords}"
        QGuiApplication.clipboard().setText(combined)

    def _refresh_quality_score(self):
        """Item #20 — recompute and display the quality score live as the
        user edits Title/Description/Keywords in the inspector."""
        title = self.title_input.text()
        desc = self.desc_input.toPlainText()
        keywords = [k.strip() for k in self.keywords_input.toPlainText().split(",") if k.strip()]

        if not (title or desc or keywords):
            self.quality_score_lbl.setText("Quality Score: —")
            self.quality_label_lbl.setText("")
            self.quality_bar.setValue(0)
            self.quality_detail_lbl.setText("")
            return

        rules = self._config.get_metadata_rules() if self._config else {}
        result = compute_quality_score(
            title, desc, keywords,
            title_min=int(rules.get("title_min_length", 5)),
            title_max=int(rules.get("title_max_length", 70)),
            keyword_min=int(rules.get("keyword_min_count", 7)),
            keyword_max=int(rules.get("keyword_max_count", 49)),
        )
        self.quality_score_lbl.setText(f"Quality Score: {result['overall']}")
        self.quality_label_lbl.setText(result["label"])
        self.quality_bar.setValue(result["overall"])

        dims = result["dimensions"]
        self.quality_detail_lbl.setText(
            f"Title {int(dims['title_quality']*100)}%  •  "
            f"Description {int(dims['description_quality']*100)}%  •  "
            f"Keywords {int(dims['keyword_relevance']*100)}% relevant, "
            f"{int(dims['keyword_uniqueness']*100)}% unique"
        )

    def _revert_inspector(self):
        if self._current_preview_path:
            for row, result in self._row_results.items():
                if os.path.normpath(result.get("filename", "")) == \
                   os.path.normpath(self._current_preview_path):
                    self._populate_inspector_from_dict(result)
                    return
            self._load_inspector_from_file(self._current_preview_path)

    # ------------------------------------------------------------------
    # Table selection → inspector
    # ------------------------------------------------------------------

    def _on_table_selection_changed(self):
        row = self.queue_page.batch_table.currentRow()
        if row < 0:
            return
        path = self.queue_page.batch_table.get_path_at_row(row)
        if not path:
            return
        self._current_preview_path = path
        self._load_preview(path)
        self.path_label.setText(Path(path).name)
        if row in self._row_results:
            self._populate_inspector_from_dict(self._row_results[row])
        else:
            self._load_inspector_from_file(path)

    def _load_preview(self, path: str):
        px = QPixmap(path)
        if px.isNull():
            self.img_preview.setText("Preview unavailable")
        else:
            self.img_preview.setPixmap(
                px.scaled(
                    self.img_preview.width() or 308,
                    self.img_preview.height(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )

    def _load_inspector_from_file(self, path: str):
        if self._config is None:
            return
        from core.metadata_engine import MetadataEngine
        engine = MetadataEngine(self._config)
        try:
            meta = engine.read_metadata(path)
            self._populate_inspector_from_dict(meta)
        except Exception as exc:
            logger.warning("Inspector read failed for %s: %s", path, exc)
            self._clear_inspector_fields()

    def _populate_inspector_from_dict(self, meta: dict):
        self.title_input.setText(meta.get("title", ""))
        self.desc_input.setPlainText(meta.get("description", ""))
        kw = meta.get("keywords", [])
        if isinstance(kw, list):
            self.keywords_input.setPlainText(", ".join(kw))
        else:
            self.keywords_input.setPlainText(str(kw))
        self._refresh_quality_score()

    def _clear_inspector(self):
        self._current_preview_path = None
        self.img_preview.setText("No image selected")
        self.path_label.setText("")
        self._clear_inspector_fields()

    def _clear_inspector_fields(self):
        self.title_input.clear()
        self.desc_input.clear()
        self.keywords_input.clear()
        self._refresh_quality_score()

    def _update_kw_count(self):
        raw = self.keywords_input.toPlainText()
        count = len([k for k in raw.split(",") if k.strip()])
        self.kw_count_lbl.setText(f"{count} keywords")

    # ------------------------------------------------------------------
    # Stylesheet
    # ------------------------------------------------------------------

    def _apply_stylesheet(self):
        # ── Design tokens ──────────────────────────────────────────────────────
        # Base:        #07090f   (near-black, deep space)
        # Surface-1:   #0d1117   (sidebar, inspector)
        # Surface-2:   #111520   (cards, control bars)
        # Surface-3:   #161b27   (input backgrounds)
        # Border:      #1e2433   (subtle dividers)
        # Border-hi:   #252e40   (card borders on hover)
        # Accent:      #6366f1   (indigo — primary CTA, active nav)
        # Accent-dim:  #3730a3   (pressed/disabled accent)
        # Accent-glow: #818cf8   (accent text on dark)
        # Success:     #10b981   (emerald green)
        # Success-dim: #064e3b   (success bg)
        # Warning:     #f59e0b
        # Danger:      #ef4444
        # Text-hi:     #f0f4ff   (headings)
        # Text-mid:    #8b95a8   (body / labels)
        # Text-lo:     #3d4758   (muted / disabled)
        # ──────────────────────────────────────────────────────────────────────
        self.setStyleSheet("""

        /* ══════════════════════════════════════════════
           BASE
        ══════════════════════════════════════════════ */
        QMainWindow, QWidget {
            background-color: #07090f;
            color: #c9d1e0;
            font-family: 'Segoe UI', 'Inter', system-ui, sans-serif;
            font-size: 13px;
        }

        /* ══════════════════════════════════════════════
           SIDEBAR  — darkest surface, premium feel
        ══════════════════════════════════════════════ */
        QFrame#Sidebar {
            background-color: #0d1117;
            border-right: 1px solid #1e2433;
        }
        QWidget#LogoArea {
            background-color: #0d1117;
        }
        QLabel#AppName {
            font-size: 17px;
            font-weight: 700;
            color: #f0f4ff;
            letter-spacing: -0.3px;
            background-color: none;
        }
        QLabel#AppSub {
            font-size: 10px;
            font-weight: 500;
            color: #3d4758;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            background-color: none;
        }
        QFrame#SidebarSep {
            color: #1e2433;
            background: #1e2433;
            max-height: 1px;
        }

        /* Nav buttons — signature glowing left-bar active indicator */
        QPushButton#NavBtn {
            background: transparent;
            color: #4b5875;
            border: none;
            border-radius: 7px;
            padding: 0 14px;
            font-size: 13px;
            font-weight: 500;
            text-align: left;
        }
        QPushButton#NavBtn:hover {
            background-color: #111825;
            color: #8b95a8;
        }
        QPushButton#NavBtn:checked {
            background-color: rgba(99, 102, 241, 0.12);
            color: #818cf8;
            font-weight: 600;
            border-left: 3px solid #6366f1;
            padding-left: 11px;
        }

        QWidget#SidebarBottom {
            background: #0d1117;
            border-top: 1px solid #1e2433;
        }
        QPushButton#SaveBtn {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #4338ca, stop:1 #6366f1);
            color: #f0f4ff;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            font-size: 13px;
            letter-spacing: 0.1px;
        }
        QPushButton#SaveBtn:hover {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #4f46e5, stop:1 #818cf8);
        }
        QPushButton#SaveBtn:pressed {
            background-color: #3730a3;
        }

        /* ══════════════════════════════════════════════
           CONTENT WRAPPER
        ══════════════════════════════════════════════ */
        QWidget#ContentWrapper {
            background-color: #07090f;
        }
        QLabel {
            background-color: none;
        }

        /* ══════════════════════════════════════════════
           TYPOGRAPHY
        ══════════════════════════════════════════════ */
        QLabel#PageTitle {
            font-size: 19px;
            font-weight: 700;
            color: #f0f4ff;
            letter-spacing: -0.4px;
            background-color: none;

        }
        QLabel#PageSubtitle {
            font-size: 13px;
            color: #4b5875;
            line-height: 1.5;
            background-color: none;
        }
        QLabel#FieldLabel {
            font-size: 11px;
            font-weight: 600;
            color: #4b5875;
            letter-spacing: 0.6px;
            text-transform: uppercase;
            background-color: none;
        }
        QLabel#CardTitle {
            font-size: 14px;
            font-weight: 600;
            color: #dde3f0;
            background-color: none;
        }
        QLabel#CardNote {
            font-size: 12px;
            color: #4b5875;
            line-height: 1.5;
            background-color: none;
        }
        QLabel#LinkLabel {
            font-size: 12px;
            background-color: none;
        }

        /* ══════════════════════════════════════════════
           CARDS  — layered surface system
        ══════════════════════════════════════════════ */
        QFrame#Card {
            background-color: #0d1117;
            border: 1px solid #1e2433;
            border-radius: 10px;
        }
        QFrame#StatusCard {
            background-color: #0a1120;
            border: 1px solid #1e3356;
            border-radius: 8px;
        }
        QFrame#HRule {
            color: #1e2433;
            background: #1e2433;
            max-height: 1px;
            background-color: none;
        }
        QFrame#RuleChip {
            background-color: #07090f;
            border: 1px solid #1e2433;
            border-radius: 8px;
        }
        QLabel#ChipLabel {
            font-size: 10px;
            font-weight: 600;
            color: #3d4758;
            letter-spacing: 0.7px;
            text-transform: uppercase;
            background-color: none;
        }
        QLabel#ChipValue {
            font-size: 14px;
            font-weight: 700;
            color: #818cf8;
        }

        /* ══════════════════════════════════════════════
           INSPECTOR PANEL
        ══════════════════════════════════════════════ */
        QFrame#Inspector {
            background-color: #0d1117;
            border-left: 1px solid #1e2433;
        }
        QLabel#ImagePreview {
            background-color: #07090f;
            border: 1px dashed #252e40;
            border-radius: 10px;
            color: #252e40;
            font-size: 12px;
        }

        /* ══════════════════════════════════════════════
           FORM INPUTS
        ══════════════════════════════════════════════ */
        QLineEdit, QTextEdit, QComboBox, QSpinBox {
            background-color: #111520;
            border: 1px solid #1e2433;
            border-radius: 7px;
            padding: 6px 10px;
            color: #c9d1e0;
            selection-background-color: #3730a3;
        }
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {
            border: 1px solid #6366f1;
            background-color: #0f1421;
        }
        QLineEdit:hover, QTextEdit:hover, QComboBox:hover, QSpinBox:hover {
            border-color: #252e40;
        }
        QComboBox::drop-down {
            border: none;
            padding-right: 10px;
            subcontrol-position: right center;
        }
        QComboBox::down-arrow { color: #4b5875; }
        QComboBox QAbstractItemView {
            background-color: #111520;
            border: 1px solid #252e40;
            border-radius: 7px;
            selection-background-color: rgba(99, 102, 241, 0.2);
            color: #c9d1e0;
            padding: 4px;
        }
        QSpinBox::up-button, QSpinBox::down-button {
            width: 20px;
            background: #161b27;
            border: none;
        }
        QSpinBox::up-button:hover, QSpinBox::down-button:hover {
            background: #1e2433;
        }
        QCheckBox {
            color: #8b95a8;
            spacing: 10px;
            font-size: 13px;
            background-color: none;
        }
        QCheckBox::indicator {
            width: 17px;
            height: 17px;
            border: 1.5px solid #252e40;
            border-radius: 5px;
            background: #111520;
        }
        QCheckBox::indicator:hover {
            border-color: #6366f1;
        }
        QCheckBox::indicator:checked {
            background: #6366f1;
            border-color: #6366f1;
        }

        /* ══════════════════════════════════════════════
           BUTTONS — clear 3-tier hierarchy
        ══════════════════════════════════════════════ */

        /* Primary — indigo gradient CTA */
        QPushButton#PrimaryBtn {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #4338ca, stop:1 #6366f1);
            color: #f0f4ff;
            border: none;
            border-radius: 7px;
            padding: 8px 18px;
            font-weight: 600;
            font-size: 13px;
            letter-spacing: 0.1px;
        }
        QPushButton#PrimaryBtn:hover {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #4f46e5, stop:1 #818cf8);
        }
        QPushButton#PrimaryBtn:pressed { background-color: #3730a3; }
        QPushButton#PrimaryBtn:disabled {
            background: #1a1f2e;
            color: #3d4758;
        }

        /* Secondary — ghost button */
        QPushButton#SecBtn {
            background-color: transparent;
            color: #6b778f;
            border: 1px solid #1e2433;
            border-radius: 7px;
            padding: 7px 14px;
            font-weight: 500;
            font-size: 13px;
        }
        QPushButton#SecBtn:hover {
            background-color: #111520;
            border-color: #252e40;
            color: #c9d1e0;
        }
        QPushButton#SecBtn:pressed { background-color: #0d1117; }
        QPushButton#SecBtn:disabled { color: #252e40; border-color: #111520; }

        /* Success — emerald */
        QPushButton#GreenBtn {
            background-color: #064e3b;
            color: #6ee7b7;
            border: 1px solid #065f46;
            border-radius: 7px;
            padding: 8px 18px;
            font-weight: 600;
            font-size: 13px;
        }
        QPushButton#GreenBtn:hover {
            background-color: #065f46;
            color: #a7f3d0;
            border-color: #10b981;
        }
        QPushButton#GreenBtn:disabled {
            background-color: #0a1a12;
            color: #1a3326;
            border-color: #0a1a12;
        }

        /* Chip / inline action */
        QPushButton#ChipBtn {
            background-color: #111520;
            color: #818cf8;
            border: 1px solid #252e40;
            border-radius: 5px;
            font-size: 11px;
            font-weight: 600;
            padding: 2px 8px;
        }
        QPushButton#ChipBtn:hover {
            background-color: rgba(99, 102, 241, 0.15);
            border-color: #6366f1;
            color: #a5b4fc;
        }

        /* API key show/hide toggle */
        QPushButton#EyeBtn {
            background-color: #111520;
            color: #4b5875;
            border: 1px solid #1e2433;
            border-radius: 7px;
            font-size: 12px;
            font-weight: 500;
            padding: 0 8px;
        }
        QPushButton#EyeBtn:checked {
            color: #818cf8;
            border-color: #4338ca;
            background-color: rgba(99, 102, 241, 0.1);
        }
        QPushButton#EyeBtn:hover { background-color: #161b27; }

        /* ══════════════════════════════════════════════
           STATUS INDICATORS
        ══════════════════════════════════════════════ */
        QLabel#StatusDotOn {
            color: #10b981;
            font-size: 14px;
        }
        QLabel#StatusDotOff {
            color: #252e40;
            font-size: 14px;
        }

        /* ══════════════════════════════════════════════
           QUEUE PAGE
        ══════════════════════════════════════════════ */
        QLabel#DropHint {
            background-color: #07090f;
            color: #252e40;
            font-size: 12px;
            font-weight: 500;
            letter-spacing: 0.2px;
            padding: 11px 16px;
            border-bottom: 1px solid #1e2433;
        }
        QWidget#ControlBar {
            background-color: #0d1117;
            border-top: 1px solid #1e2433;
        }

        /* ══════════════════════════════════════════════
           TABLE  — file-manager feel
        ══════════════════════════════════════════════ */
        QTableWidget {
            background-color: #07090f;
            alternate-background-color: #0a0d14;
            gridline-color: transparent;
            border: none;
            selection-background-color: rgba(99, 102, 241, 0.14);
            outline: none;
        }
        QHeaderView::section {
            background-color: #0d1117;
            color: #3d4758;
            padding: 9px 12px;
            border: none;
            border-bottom: 1px solid #1e2433;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.8px;
        }
        QTableWidget::item {
            padding: 6px 12px;
            color: #8b95a8;
            border-bottom: 1px solid #0d1117;
        }
        QTableWidget::item:selected {
            background-color: rgba(99, 102, 241, 0.14);
            color: #a5b4fc;
        }
        QTableWidget::item:hover {
            background-color: rgba(99, 102, 241, 0.07);
        }

        /* ══════════════════════════════════════════════
           PROGRESS BAR
        ══════════════════════════════════════════════ */
        QProgressBar#MainProgress {
            border: none;
            border-top: 1px solid #1e2433;
            background-color: #0d1117;
            text-align: center;
            color: #3d4758;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.2px;
        }
        QProgressBar#MainProgress::chunk {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #4338ca, stop:1 #818cf8);
        }

        /* Quality score mini-bar */
        QProgressBar {
            border: 1px solid #1e2433;
            border-radius: 4px;
            background: #111520;
            text-align: center;
            color: transparent;
        }
        QProgressBar::chunk {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #4338ca, stop:1 #10b981);
            border-radius: 4px;
        }

        /* ══════════════════════════════════════════════
           SCROLLBARS  — minimal, refined
        ══════════════════════════════════════════════ */
        QScrollBar:vertical {
            background: transparent;
            width: 5px;
            border-radius: 3px;
            margin: 0;
        }
        QScrollBar::handle:vertical {
            background: #1e2433;
            border-radius: 3px;
            min-height: 30px;
        }
        QScrollBar::handle:vertical:hover { background: #252e40; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar:horizontal {
            background: transparent;
            height: 5px;
            border-radius: 3px;
        }
        QScrollBar::handle:horizontal {
            background: #1e2433;
            border-radius: 3px;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        QScrollArea { border: none; background: transparent; }

        /* ══════════════════════════════════════════════
           LIST WIDGET  (history, templates)
        ══════════════════════════════════════════════ */
        QListWidget {
            background-color: #0d1117;
            border: 1px solid #1e2433;
            border-radius: 7px;
            color: #8b95a8;
            outline: none;
        }
        QListWidget::item { padding: 6px 10px; border-radius: 5px; }
        QListWidget::item:selected {
            background-color: rgba(99, 102, 241, 0.14);
            color: #a5b4fc;
        }
        QListWidget::item:hover { background-color: #111520; }

        /* ══════════════════════════════════════════════
           FORM LAYOUT LABELS  (Settings QFormLayout)
        ══════════════════════════════════════════════ */
        QFormLayout QLabel {
            color: #6b778f;
            font-size: 12px;
        }

        /* ══════════════════════════════════════════════
           FALLBACK ORDER DRAG-TO-REORDER LIST
        ══════════════════════════════════════════════ */
        QListWidget#FallbackOrderList {
            background-color: #0d1117;
            border: 1px solid #1e2433;
            border-radius: 7px;
            color: #8b95a8;
            outline: none;
        }
        QListWidget#FallbackOrderList::item {
            padding: 8px 12px;
            border-radius: 5px;
            border-bottom: 1px solid #111520;
            color: #c9d1e0;
        }
        QListWidget#FallbackOrderList::item:selected {
            background-color: rgba(99, 102, 241, 0.18);
            color: #a5b4fc;
        }
        QListWidget#FallbackOrderList::item:hover {
            background-color: #111520;
            cursor: grab;
        }

        /* ══════════════════════════════════════════════
           TOOLTIPS
        ══════════════════════════════════════════════ */
        QToolTip {
            background-color: #111520;
            color: #c9d1e0;
            border: 1px solid #252e40;
            border-radius: 6px;
            padding: 5px 8px;
            font-size: 12px;
        }

        """)


if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    window = MetaEmbedMainWindow()
    window.show()
    sys.exit(app.exec())

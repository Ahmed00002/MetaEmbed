
import os
import logging
from pathlib import Path
from typing import Optional

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
from ui.pages.dashboard_page import DashboardPage

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
    "google": [
        "gemini-2.5-flash",          # ✅ Free — 1,500 req/day
        "gemini-2.5-pro",            # ✅ Free — 50 req/day (higher quality)
        "gemini-1.5-flash",
        "gemini-1.5-pro",
    ],
    "openai": [
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.5",
        "gpt-4o-mini",
    ],
    "openrouter": [
        # ✅ Free vision models (append :free for zero-cost)
        "meta-llama/llama-4-maverick:free",   # ✅ Free — vision, 128K ctx
        "meta-llama/llama-4-scout:free",      # ✅ Free — vision, fast
        "qwen/qwen3-vl-30b-a3b:free",        # ✅ Free — vision, 262K ctx
        "qwen/qwen3.6-plus:free",            # ✅ Free — vision, 1M ctx
        # Paid / standard
        "google/gemini-2.5-flash",
        "openai/gpt-5.4-mini",
        "anthropic/claude-3-haiku",
        "meta-llama/llama-4-scout",
        "meta-llama/llama-4-maverick",
    ],
    "groq": [
        "meta-llama/llama-4-scout-17b-16e-instruct",    # ✅ Free — vision
        "meta-llama/llama-4-maverick-17b-128e-instruct", # ✅ Free — vision
    ],
    "mistral": [
        "pixtral-12b-2409",     # ✅ Free tier — vision, 128K ctx
        "mistral-large-latest", # Paid — vision via Mistral Large
    ],
}
PROVIDER_DOCS = {
    "google":     "https://aistudio.google.com/apikey",
    "openai":     "https://platform.openai.com/api-keys",
    "openrouter": "https://openrouter.ai/keys",
    "groq":       "https://console.groq.com/keys",
    "mistral":    "https://console.mistral.ai/api-keys",
}
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"}

NAV_ITEMS = [
    ("queue",     "Metadata"),
    ("market",    "Export"),
    ("settings",  "Settings"),
    ("ai",        "AI Studio"),
    ("history",   "History"),
    ("dashboard", "Dashboard"),
    ("about",     "About"),
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

        # --- Image Resolution for API ---
        res_card = QFrame()
        res_card.setObjectName("Card")
        rc_layout = QVBoxLayout(res_card)
        rc_layout.setContentsMargins(16, 16, 16, 16)
        rc_layout.setSpacing(12)

        rc_layout.addWidget(self._section_label("Image Resolution Sent to AI"))
        res_note = QLabel(
            "Lower = fewer tokens consumed per image = more images per API quota. "
            "The AI understands imagery equally well at 512 px and 1536 px — "
            "resolution does NOT improve metadata quality, only token cost."
        )
        res_note.setObjectName("CardNote")
        res_note.setWordWrap(True)
        rc_layout.addWidget(res_note)

        res_row = QHBoxLayout()
        res_row.addWidget(QLabel("Max side (px):"))
        self.combo_image_res = QComboBox()
        self.combo_image_res.addItems(["512  — Max saving (recommended)", "768  — Balanced", "1024 — High detail", "1536 — Maximum detail"])
        self.combo_image_res.setCurrentIndex(0)
        self.combo_image_res.setMinimumHeight(34)
        res_row.addWidget(self.combo_image_res)
        res_row.addStretch()
        rc_layout.addLayout(res_row)

        # Token cost indicator, updates with combo
        self.res_cost_lbl = QLabel("~85 input tokens/image  •  ~20–25× cheaper than maximum")
        self.res_cost_lbl.setObjectName("CardNote")
        rc_layout.addWidget(self.res_cost_lbl)

        self.combo_image_res.currentIndexChanged.connect(self._update_res_cost_label)
        layout.addWidget(res_card)
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

    def _update_res_cost_label(self, index: int):
        labels = [
            "~85 input tokens/image  •  ~20–25× cheaper than maximum (recommended)",
            "~190 input tokens/image  •  ~10× cheaper than maximum",
            "~340 input tokens/image  •  ~5× cheaper than maximum",
            "~850 input tokens/image  •  maximum token usage",
        ]
        self.res_cost_lbl.setText(labels[index])

    def get_image_resolution(self) -> int:
        """Return the selected max image dimension in pixels."""
        return [512, 768, 1024, 1536][self.combo_image_res.currentIndex()]


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


class AboutPage(QWidget):
    """About page — developer credit and bKash support section."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 32, 32, 32)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 24, 32)
        layout.setSpacing(24)
        layout.setAlignment(Qt.AlignTop)

        # ── Page header ───────────────────────────────────────────────
        title = QLabel("About")
        title.setObjectName("PageTitle")
        sub = QLabel("MetaEmbed AI — open-source micro-stock metadata generator")
        sub.setObjectName("PageSubtitle")
        layout.addWidget(title)
        layout.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("HRule")
        layout.addWidget(sep)

        # ── Developer card ────────────────────────────────────────────
        dev_card = QFrame()
        dev_card.setObjectName("Card")
        dev_layout = QVBoxLayout(dev_card)
        dev_layout.setContentsMargins(24, 22, 24, 22)
        dev_layout.setSpacing(14)

        dev_title = QLabel("Developer")
        dev_title.setObjectName("FieldLabel")
        dev_layout.addWidget(dev_title)

        # Avatar circle + name row
        name_row = QHBoxLayout()
        name_row.setSpacing(16)

        avatar = QLabel("LA")
        avatar.setFixedSize(52, 52)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setStyleSheet("""
            QLabel {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #4338ca, stop:1 #818cf8);
                color: #f0f4ff;
                font-size: 18px;
                font-weight: 700;
                border-radius: 26px;
            }
        """)
        name_row.addWidget(avatar)

        name_text = QVBoxLayout()
        name_text.setSpacing(3)
        name_lbl = QLabel("Layek Ahmed Numan")
        name_lbl.setStyleSheet("font-size: 17px; font-weight: 700; color: #f0f4ff;")
        role_lbl = QLabel("Software Developer · Bangladesh")
        role_lbl.setStyleSheet("font-size: 13px; color: #4b5875;")
        name_text.addWidget(name_lbl)
        name_text.addWidget(role_lbl)
        name_row.addLayout(name_text)
        name_row.addStretch()
        dev_layout.addLayout(name_row)

        # Description
        desc = QLabel(
            "Built MetaEmbed AI to help stock contributors automate metadata "
            "generation with AI — saving hours of manual work per batch. "
            "This project is free and open-source for everyone."
        )
        desc.setStyleSheet("font-size: 13px; color: #6b778f; line-height: 1.6;")
        desc.setWordWrap(True)
        dev_layout.addWidget(desc)

        # GitHub button
        gh_btn = QPushButton("⎋  github.com/Ahmed00002")
        gh_btn.setObjectName("SecBtn")
        gh_btn.setFixedHeight(34)
        gh_btn.setCursor(Qt.PointingHandCursor)
        gh_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/Ahmed00002"))
        )
        dev_layout.addWidget(gh_btn)

        layout.addWidget(dev_card)

        # ── App info card ─────────────────────────────────────────────
        app_card = QFrame()
        app_card.setObjectName("Card")
        app_layout = QVBoxLayout(app_card)
        app_layout.setContentsMargins(24, 22, 24, 22)
        app_layout.setSpacing(10)

        app_title = QLabel("Application")
        app_title.setObjectName("FieldLabel")
        app_layout.addWidget(app_title)

        for label, value in [
            ("Name",      "MetaEmbed AI"),
            ("Version",   "v1.0"),
            ("License",   "MIT — Free to use and distribute"),
            ("Platform",  "Windows"),
        ]:
            row = QHBoxLayout()
            k = QLabel(label)
            k.setStyleSheet("font-size: 12px; color: #3d4758; min-width: 80px;")
            v = QLabel(value)
            v.setStyleSheet("font-size: 13px; color: #8b95a8;")
            row.addWidget(k)
            row.addWidget(v)
            row.addStretch()
            app_layout.addLayout(row)

        layout.addWidget(app_card)

        # ── bKash support card ────────────────────────────────────────
        bkash_card = QFrame()
        bkash_card.setObjectName("Card")
        bkash_card.setStyleSheet("""
            QFrame#Card {
                border: 1px solid #2d1f3d;
                background: #0e0a18;
            }
        """)
        bkash_layout = QVBoxLayout(bkash_card)
        bkash_layout.setContentsMargins(24, 22, 24, 24)
        bkash_layout.setSpacing(14)

        support_hdr = QHBoxLayout()
        support_hdr.setSpacing(10)
        coffee_icon = QLabel("☕")
        coffee_icon.setStyleSheet("font-size: 22px;")
        support_heading = QLabel("Support the Developer")
        support_heading.setStyleSheet(
            "font-size: 15px; font-weight: 700; color: #f0f4ff;"
        )
        support_hdr.addWidget(coffee_icon)
        support_hdr.addWidget(support_heading)
        support_hdr.addStretch()
        bkash_layout.addLayout(support_hdr)

        support_desc = QLabel(
            "MetaEmbed AI is completely free. If it saves you time and helps "
            "your stock photography business, consider sending a small thank-you "
            "via bKash — any amount is deeply appreciated and keeps this project alive."
        )
        support_desc.setStyleSheet("font-size: 13px; color: #6b778f; line-height: 1.6;")
        support_desc.setWordWrap(True)
        bkash_layout.addWidget(support_desc)

        # bKash number display box
        bkash_box = QFrame()
        bkash_box.setStyleSheet("""
            QFrame {
                background: #160d24;
                border: 1px solid #3d1f5c;
                border-radius: 10px;
            }
        """)
        bkash_box_layout = QVBoxLayout(bkash_box)
        bkash_box_layout.setContentsMargins(20, 16, 20, 16)
        bkash_box_layout.setSpacing(6)

        bkash_label_row = QHBoxLayout()
        bkash_logo = QLabel("bKash")
        bkash_logo.setStyleSheet(
            "font-size: 11px; font-weight: 800; color: #e2136e; letter-spacing: 0.5px;"
        )
        bkash_type = QLabel("Personal")
        bkash_type.setStyleSheet(
            "font-size: 11px; color: #4b2d6b; font-weight: 500;"
        )
        bkash_label_row.addWidget(bkash_logo)
        bkash_label_row.addStretch()
        bkash_label_row.addWidget(bkash_type)
        bkash_box_layout.addLayout(bkash_label_row)

        number_row = QHBoxLayout()
        number_row.setSpacing(12)
        number_lbl = QLabel("01859-737677")
        number_lbl.setStyleSheet(
            "font-size: 24px; font-weight: 700; color: #f0f4ff; letter-spacing: 1px;"
        )
        copy_btn = QPushButton("Copy")
        copy_btn.setObjectName("ChipBtn")
        copy_btn.setFixedHeight(28)
        copy_btn.setFixedWidth(58)
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.clicked.connect(self._copy_bkash)
        number_row.addWidget(number_lbl)
        number_row.addWidget(copy_btn)
        number_row.addStretch()
        bkash_box_layout.addLayout(number_row)

        hint = QLabel("Send to this bKash Personal number · any amount welcome")
        hint.setStyleSheet("font-size: 11px; color: #4b2d6b;")
        bkash_box_layout.addWidget(hint)

        bkash_layout.addWidget(bkash_box)

        # Steps
        steps_lbl = QLabel(
            "How to send:  Open bKash app → Send Money → enter number above → any amount → confirm"
        )
        steps_lbl.setStyleSheet("font-size: 12px; color: #3d4758; font-style: italic;")
        steps_lbl.setWordWrap(True)
        bkash_layout.addWidget(steps_lbl)

        layout.addWidget(bkash_card)

        # ── Thank-you note ────────────────────────────────────────────
        thanks = QLabel("Thank you for using MetaEmbed AI  🙏")
        thanks.setStyleSheet(
            "font-size: 13px; color: #3d4758; font-style: italic;"
        )
        thanks.setAlignment(Qt.AlignCenter)
        layout.addWidget(thanks)

        scroll.setWidget(container)
        root.addWidget(scroll)

    def _copy_bkash(self):
        QGuiApplication.clipboard().setText("01859737677")
        # Brief visual feedback on the button
        btn = self.sender()
        if btn:
            btn.setText("✓")
            btn.setStyleSheet(btn.styleSheet() +
                              "QPushButton { color: #10b981; border-color: #10b981; }")
            QTimer.singleShot(1800, lambda: (
                btn.setText("Copy"),
                btn.setStyleSheet(""),
            ))


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

    def __init__(self, config_manager=None, token_tracker=None):
        super().__init__()
        self._config = config_manager
        self._token_tracker = token_tracker
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

        # Image resolution setting
        saved_res = int(config_manager.get("system", "image_resolution") or 512)
        res_map = {512: 0, 768: 1, 1024: 2, 1536: 3}
        self.settings_page.combo_image_res.setCurrentIndex(res_map.get(saved_res, 0))

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
        # Mirror to console
        if info.current_image:
            self.queue_page.log_console(
                f"Processing  [{info.current_index}/{info.total}]  {info.current_image}"
                f"  —  OK:{info.success}  Fail:{info.failed}  Skip:{info.skipped}"
            )

    def update_row_status(self, row: int, status: str):
        self.queue_page.batch_table.update_row_status(row, status)
        # Log meaningful state transitions to the console (skip noisy
        # intermediate states like Validating… and Generating… to reduce noise)
        _console_statuses = {
            "Done", "Generated", "Error", "Write Failed",
            "Write Failed (rolled back)", "Skipped (invalid)",
            "Duplicate (skipped)", "Writing…",
        }
        if any(status.startswith(s) for s in _console_statuses):
            path = self.queue_page.batch_table.get_path_at_row(row)
            name = Path(path).name if path else f"row {row}"
            # Pick a prefix symbol to make scanning easier
            if status.startswith("Done") or status.startswith("Generated"):
                prefix = "✓"
            elif "Error" in status or "Failed" in status:
                prefix = "✗"
            elif "Skipped" in status or "Duplicate" in status:
                prefix = "⚠"
            else:
                prefix = "→"
            self.queue_page.log_console(f"{prefix}  {name}  —  {status}")

    def on_result_ready(self, row: int, result: dict):
        self._row_results[row] = result
        if self.queue_page.batch_table.currentRow() == row:
            self._populate_inspector_from_dict(result)
        # Enable Save All the moment we have at least one result
        self.queue_page.btn_save_all.setEnabled(True)
        # Log result details to console
        if not result.get("error"):
            name = Path(result.get("filename", "")).name or f"row {row}"
            kw_count = len(result.get("keywords", []))
            title_len = len(result.get("title", ""))
            self.queue_page.log_console(
                f"  Metadata ready  —  title: {title_len} chars  keywords: {kw_count}  "
                f"→  {result.get('title', '')[:60]}"
            )

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
        # While running: only Cancel is active
        # When stopped: all buttons enabled EXCEPT Cancel — Generate Failed
        # follows its own independent check (has failed rows or not)
        self.queue_page.btn_process.setEnabled(not running)
        self.queue_page.btn_cancel.setEnabled(running)
        self.queue_page.btn_clear.setEnabled(not running)
        self.queue_page.btn_add_files.setEnabled(not running)
        self.queue_page.btn_add_folder.setEnabled(not running)
        # Save All: enabled when stopped AND we have at least one result
        has_results = bool(self._row_results)
        self.queue_page.btn_save_all.setEnabled(not running and has_results)

        if running:
            # Disable Generate Failed while a batch is in progress
            self.queue_page.btn_retry_failed.setEnabled(False)
            total = self.queue_page.batch_table.rowCount()
            self.queue_page.log_console(
                f"━━━  Batch started  —  {total} image(s) queued  ━━━"
            )
        else:
            # Generate Failed enabled only if there are actually failed rows
            self.queue_page.btn_retry_failed.setEnabled(
                self.queue_page.batch_table.has_failed_rows()
            )
            self.progress_detail_lbl.setText("")
            self.queue_page.log_console("━━━  Batch finished  ━━━")

    def show_warning(self, title: str, msg: str):
        QMessageBox.warning(self, title, msg)

    def show_error(self, msg: str):
        # Log to console (first line only, to avoid huge dialogs in console)
        first_line = msg.split("\n")[0][:120]
        self.queue_page.log_console(f"✗  ERROR  {first_line}")
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
        central.setObjectName("AppShell")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        # Redesign: give the whole window a structural "shell" margin so the
        # workspace (table/cards/inspector) visibly floats as its own raised
        # surface, separated from the dark sidebar shell by real negative
        # space — not just a 1px border between two near-identical greys.
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        # Sidebar
        self.sidebar = self._build_sidebar()
        root.addWidget(self.sidebar)

        # Content area (stack + inspector + progress) — this whole block is
        # the "workspace": a distinctly lighter surface than the dark
        # sidebar shell, so it reads as its own raised panel rather than a
        # continuation of the same background. (No border-radius here on
        # purpose — Qt stylesheets don't clip child widgets to a parent's
        # rounded corners, and the table/inspector sit flush against this
        # widget's edges, so rounding it would show square corners poking
        # past the curve. The sidebar opposite it CAN be rounded because
        # its child sections — logo area, nav list, bottom bar — are all
        # transparent and only their hairline borders/text show, so the
        # rounded frame background shows through cleanly with nothing
        # square-edged sitting on top of it.)
        content_wrapper = QWidget()
        content_wrapper.setObjectName("WorkspaceSurface")
        cw_layout = QVBoxLayout(content_wrapper)
        cw_layout.setContentsMargins(0, 0, 0, 0)
        cw_layout.setSpacing(0)

        # Main content (pages + inspector side by side) — use a QSplitter
        # so Qt properly redistributes space when the inspector is shown or
        # hidden. A plain QHBoxLayout caches widget size hints at layout time
        # and doesn't correctly reclaim/give back space when max-width changes
        # after the window is already shown — the splitter fixes that entirely.
        main_area = QSplitter(Qt.Horizontal)
        main_area.setObjectName("WorkspaceInner")
        main_area.setHandleWidth(0)        # invisible handle — inspector has its own border
        main_area.setChildrenCollapsible(True)

        # Page stack
        self.stack = QStackedWidget()
        self.queue_page    = QueuePage()
        self.market_page   = MarketPage()
        self.settings_page = SettingsPage()
        self.ai_page       = AIStudioPage()
        self.history_page  = HistoryPage()
        self.dashboard_page = DashboardPage(token_tracker=self._token_tracker)
        self.about_page    = AboutPage()

        self.stack.addWidget(self.queue_page)
        self.stack.addWidget(self.market_page)
        self.stack.addWidget(self.settings_page)
        self.stack.addWidget(self.ai_page)
        self.stack.addWidget(self.history_page)
        self.stack.addWidget(self.dashboard_page)
        self.stack.addWidget(self.about_page)

        main_area.addWidget(self.stack)
        self.inspector_panel = self._build_inspector()
        main_area.addWidget(self.inspector_panel)

        # Stack gets all stretch; inspector is fixed at 360 px when open
        main_area.setStretchFactor(0, 1)
        main_area.setStretchFactor(1, 0)

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

        # Inspector starts collapsed. With a QSplitter we collapse it by
        # setting its sizes so the inspector pane gets 0 width.
        self._inspector_visible = False
        self._main_splitter = main_area
        # Give all space to the stack, zero to the inspector
        QTimer.singleShot(0, lambda: self._main_splitter.setSizes([self._main_splitter.width(), 0]))

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
        page_indices = {"queue": 0, "market": 1, "settings": 2, "ai": 3, "history": 4, "dashboard": 5, "about": 6}

        # Map page IDs to QStyle standard pixel-map icons (no emoji needed)
        from PySide6.QtWidgets import QStyle
        _style = self.style()
        _nav_icons = {
            "queue":     _style.standardIcon(QStyle.SP_MediaPlay),
            "market":    _style.standardIcon(QStyle.SP_DriveNetIcon),
            "settings":  _style.standardIcon(QStyle.SP_FileDialogDetailedView),
            "ai":        _style.standardIcon(QStyle.SP_ComputerIcon),
            "history":   _style.standardIcon(QStyle.SP_FileDialogInfoView),
            "dashboard": _style.standardIcon(QStyle.SP_FileDialogContentsView),
            "about":     _style.standardIcon(QStyle.SP_MessageBoxInformation),
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

    def _show_inspector(self):
        """Expand the inspector panel into view using splitter sizing."""
        if self._inspector_visible:
            return
        self._inspector_visible = True
        total = self._main_splitter.width()
        inspector_w = 360
        self._main_splitter.setSizes([max(0, total - inspector_w), inspector_w])

    def _hide_inspector(self):
        """Collapse the inspector panel to zero width via splitter."""
        if not self._inspector_visible:
            return
        self._inspector_visible = False
        total = self._main_splitter.width()
        self._main_splitter.setSizes([total, 0])

    def _navigate(self, index: int):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self._nav_buttons):
            btn.setChecked(i == index)
        # Inspector is only relevant on the Queue page (index 0), and
        # even there it stays hidden until the user explicitly clicks an
        # image row — so navigating away from Queue always hides it, but
        # navigating back to Queue does NOT auto-show it.
        if index != 0:
            self._hide_inspector()
        # Auto-refresh history when the user switches to the history page
        if index == 4:
            self._request_history_refresh()
        # Auto-refresh dashboard when switching to dashboard page
        if index == 5:
            self.dashboard_page.refresh()

    def _request_history_refresh(self):
        """Tell the Controller to fetch history data and call refresh_history."""
        self.refresh_history_requested.emit()

    def _build_inspector(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Inspector")
        frame.setMaximumWidth(360)
        frame.setMinimumWidth(0)
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

        # Collapse button — visible at the top of the inspector
        collapse_row = QHBoxLayout()
        collapse_row.setContentsMargins(0, 0, 0, 0)
        collapse_row.addStretch()
        self.btn_collapse_inspector = QPushButton("✕  Close Inspector")
        self.btn_collapse_inspector.setObjectName("ChipBtn")
        self.btn_collapse_inspector.setFixedHeight(24)
        self.btn_collapse_inspector.clicked.connect(self._hide_inspector)
        collapse_row.addWidget(self.btn_collapse_inspector)
        layout.addLayout(collapse_row)

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
        self.queue_page.btn_retry_failed.clicked.connect(self._trigger_retry_failed)
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
        files = self.queue_page.batch_table.get_pending_paths()
        if not files:
            all_files = self.queue_page.batch_table.get_all_paths()
            if not all_files:
                self.show_warning("Empty Queue", "Add images before processing.")
            else:
                self.show_warning("All Done", "All images have already been generated successfully.")
            return
        batch_size = self.settings_page.spin_batch_size.value()
        self.request_processing.emit(files, batch_size)

    def _trigger_retry_failed(self):
        files = self.queue_page.batch_table.get_failed_paths()
        if not files:
            self.show_warning("No Failed Images", "There are no failed images to retry.")
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
            "image_resolution": self.settings_page.get_image_resolution(),
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
            # Nothing selected — close the inspector
            self._hide_inspector()
            self._clear_inspector()
            return
        path = self.queue_page.batch_table.get_path_at_row(row)
        if not path:
            return
        # Show the inspector panel the first time a row is clicked
        self._show_inspector()
        self._current_preview_path = path
        self._load_preview(path)
        self.path_label.setText(Path(path).name)
        if row in self._row_results:
            self._populate_inspector_from_dict(self._row_results[row])
        else:
            self._load_inspector_from_file(path)

    def _load_preview(self, path: str):
        # Remove the 256 MB allocation cap so large/high-resolution images are
        # never rejected with "QImageIOHandler: Rejecting image as it exceeds
        # the current allocation limit of 256 megabytes". 0 = no limit.
        QImageReader.setAllocationLimit(0)
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
        # ── Design tokens (redesign) ─────────────────────────────────────
        # The old palette had the sidebar (#0d1117) and the main content
        # (#07090f) one hex step apart — nearly imperceptible, so the
        # sidebar and workspace read as the same surface. This redesign
        # makes the split deliberate and structural instead of a stylistic
        # tweak: a dark, recessed "shell" for navigation/chrome, and a
        # distinctly lighter "workspace" surface for the actual canvas
        # (table, cards, inspector) — the same shell/canvas split used by
        # Linear, Raycast, and most modern AI-tool UIs, so the eye always
        # knows which region is structural vs. where the work happens.
        #
        # Shell (chrome):     #05060a  sidebar, outer window margin
        # Shell-raised:       #0a0d14  sidebar bottom bar, separators
        # Workspace:          #11151f  the canvas — stack, inspector, table
        # Workspace-card:      #171c29  cards/inputs sitting ON the workspace
        # Workspace-card-hi:   #1f2535  hovered/elevated card state
        # Border:              #232a3b  subtle dividers on the workspace
        # Border-hi:           #2d3548  hover borders
        # Accent:              #6366f1  indigo — primary CTA, active nav
        # Accent-dim:          #3730a3  pressed/disabled accent
        # Accent-glow:         #818cf8  accent text on dark
        # Success:             #10b981  emerald
        # Success-dim:         #064e3b
        # Warning:             #f59e0b
        # Danger:              #ef4444
        # Text-hi:             #f5f7ff  headings
        # Text-mid:            #9aa3b8  body / labels
        # Text-lo:             #4b5468  muted / disabled
        # ──────────────────────────────────────────────────────────────────
        self.setStyleSheet("""

        /* ══════════════════════════════════════════════
           BASE — recessed shell behind everything
        ══════════════════════════════════════════════ */
        QMainWindow, QWidget#AppShell {
            background-color: #05060a;
        }
        QWidget {
            color: #c9d1e0;
            font-family: 'Segoe UI', 'Inter', system-ui, sans-serif;
            font-size: 13px;
        }

        /* ══════════════════════════════════════════════
           WORKSPACE — the raised canvas: table, pages, inspector
           This is the single biggest visual change: a distinctly
           lighter surface than the sidebar shell, so the two regions
           never get confused for the same plane.
        ══════════════════════════════════════════════ */
        QWidget#WorkspaceSurface {
            background-color: #11151f;
            border: 1px solid #232a3b;
            border-top: 1px solid #2d3548;
        }
        QScrollArea { 
            border: none; 
            background-color: transparent; 
        }

        /* Force the hidden viewport to be transparent */
        QScrollArea > QWidget#qt_scrollarea_viewport {
            background-color: transparent;
        }

        /* Force the inner container widget to be transparent */
        QScrollArea > QWidget#qt_scrollarea_viewport > QWidget {
            background-color: transparent;
        }

        /* ══════════════════════════════════════════════
           SIDEBAR  — dark, recessed control rail
        ══════════════════════════════════════════════ */
        QFrame#Sidebar {
            background-color: #05060a;
            border: 1px solid #181c27;
            border-radius: 14px;
        }
        QWidget#LogoArea {
            background-color: transparent;
        }
        QLabel#AppName {
            font-size: 17px;
            font-weight: 700;
            color: #f5f7ff;
            letter-spacing: -0.3px;
            background-color: none;
        }
        QLabel#AppSub {
            font-size: 10px;
            font-weight: 500;
            color: #4b5468;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            background-color: none;
        }
        QFrame#SidebarSep {
            color: #181c27;
            background: #181c27;
            max-height: 1px;
        }

        /* Nav buttons — signature: filled glow pill on the active item,
           not just a thin left-bar tick, so the sidebar itself feels
           like a deck of selectable tool-cards rather than a menu. */
        QPushButton#NavBtn {
            background: transparent;
            color: #5b6580;
            border: 1px solid transparent;
            border-radius: 9px;
            padding: 0 14px;
            font-size: 13px;
            font-weight: 500;
            text-align: left;
        }
        QPushButton#NavBtn:hover {
            background-color: #0d1018;
            color: #9aa3b8;
            border: 1px solid #181c27;
        }
        QPushButton#NavBtn:checked {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(99, 102, 241, 0.22), stop:1 rgba(99, 102, 241, 0.10));
            color: #c7ceff;
            font-weight: 600;
            border: 1px solid rgba(99, 102, 241, 0.45);
        }

        QWidget#SidebarBottom {
            background: transparent;
            border-top: 1px solid #181c27;
        }
        QPushButton#SaveBtn {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #4338ca, stop:1 #6366f1);
            color: #f5f7ff;
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
           TYPOGRAPHY
        ══════════════════════════════════════════════ */
        QLabel {
            background-color: none;
        }
        QLabel#PageTitle {
            font-size: 19px;
            font-weight: 700;
            color: #f5f7ff;
            letter-spacing: -0.4px;
            background-color: none;

        }
        QLabel#PageSubtitle {
            font-size: 13px;
            color: #5b6580;
            line-height: 1.5;
            background-color: none;
        }
        QLabel#FieldLabel {
            font-size: 11px;
            font-weight: 600;
            color: #5b6580;
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
            color: #5b6580;
            line-height: 1.5;
            background-color: none;
        }
        QLabel#LinkLabel {
            font-size: 12px;
            background-color: none;
        }

        /* ══════════════════════════════════════════════
           CARDS  — sit ON the lighter workspace surface, so they need
           their own further step up to stay legible against it.
        ══════════════════════════════════════════════ */
        QFrame#Card {
            background-color: #171c29;
            border: 1px solid #232a3b;
            border-radius: 10px;
        }
        QFrame#StatusCard {
            background-color: #11192e;
            border: 1px solid #233563;
            border-radius: 8px;
        }
        QFrame#HRule {
            color: #232a3b;
            background: #232a3b;
            max-height: 1px;
            background-color: none;
        }
        QFrame#RuleChip {
            background-color: #0d1018;
            border: 1px solid #232a3b;
            border-radius: 8px;
        }
        QLabel#ChipLabel {
            font-size: 10px;
            font-weight: 600;
            color: #4b5468;
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
           INSPECTOR PANEL — same workspace surface, separated by a
           hairline only (it's part of the same canvas as the table).
        ══════════════════════════════════════════════ */
        QFrame#Inspector {
            background-color: #11151f;
            border-left: 1px solid #232a3b;
        }
        QLabel#ImagePreview {
            background-color: #0d1018;
            border: 1px dashed #2d3548;
            border-radius: 10px;
            color: #2d3548;
            font-size: 12px;
        }

        /* ══════════════════════════════════════════════
           FORM INPUTS
        ══════════════════════════════════════════════ */
        QLineEdit, QTextEdit, QComboBox, QSpinBox {
            background-color: #171c29;
            border: 1px solid #232a3b;
            border-radius: 7px;
            padding: 6px 10px;
            color: #c9d1e0;
            selection-background-color: #3730a3;
        }
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {
            border: 1px solid #6366f1;
            background-color: #1a2030;
        }
        QLineEdit:hover, QTextEdit:hover, QComboBox:hover, QSpinBox:hover {
            border-color: #2d3548;
        }
        QComboBox::drop-down {
            border: none;
            padding-right: 10px;
            subcontrol-position: right center;
        }
        QComboBox::down-arrow { color: #5b6580; }
        QComboBox QAbstractItemView {
            background-color: #171c29;
            border: 1px solid #2d3548;
            border-radius: 7px;
            selection-background-color: rgba(99, 102, 241, 0.2);
            color: #c9d1e0;
            padding: 4px;
        }
        QSpinBox::up-button, QSpinBox::down-button {
            width: 20px;
            background: #1f2535;
            border: none;
        }
        QSpinBox::up-button:hover, QSpinBox::down-button:hover {
            background: #232a3b;
        }
        QCheckBox {
            color: #9aa3b8;
            spacing: 10px;
            font-size: 13px;
            background-color: none;
        }
        QCheckBox::indicator {
            width: 17px;
            height: 17px;
            border: 1.5px solid #2d3548;
            border-radius: 5px;
            background: #171c29;
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
            color: #f5f7ff;
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
            background: #1c2231;
            color: #4b5468;
        }

        /* Secondary — ghost button */
        QPushButton#SecBtn {
            background-color: transparent;
            color: #7b86a0;
            border: 1px solid #232a3b;
            border-radius: 7px;
            padding: 7px 14px;
            font-weight: 500;
            font-size: 13px;
        }
        QPushButton#SecBtn:hover {
            background-color: #171c29;
            border-color: #2d3548;
            color: #c9d1e0;
        }
        QPushButton#SecBtn:pressed { background-color: #0d1018; }
        QPushButton#SecBtn:disabled { color: #2d3548; border-color: #171c29; }

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
            background-color: #0e1a16;
            color: #234136;
            border-color: #0e1a16;
        }

        /* Warning — amber, for "Generate Failed" */
        QPushButton#WarnBtn {
            background-color: #451a03;
            color: #fbbf24;
            border: 1px solid #78350f;
            border-radius: 7px;
            padding: 8px 18px;
            font-weight: 600;
            font-size: 13px;
        }
        QPushButton#WarnBtn:hover {
            background-color: #78350f;
            color: #fde68a;
            border-color: #f59e0b;
        }
        QPushButton#WarnBtn:disabled {
            background-color: #1a1208;
            color: #3d2e10;
            border-color: #1a1208;
        }

        /* Chip / inline action */
        QPushButton#ChipBtn {
            background-color: #171c29;
            color: #818cf8;
            border: 1px solid #2d3548;
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
            background-color: #171c29;
            color: #5b6580;
            border: 1px solid #232a3b;
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
        QPushButton#EyeBtn:hover { background-color: #1f2535; }

        /* ══════════════════════════════════════════════
           STATUS INDICATORS
        ══════════════════════════════════════════════ */
        QLabel#StatusDotOn {
            color: #10b981;
            font-size: 14px;
        }
        QLabel#StatusDotOff {
            color: #2d3548;
            font-size: 14px;
        }

        /* ══════════════════════════════════════════════
           QUEUE PAGE
        ══════════════════════════════════════════════ */
        QLabel#DropHint {
            background-color: #0d1018;
            color: #2d3548;
            font-size: 12px;
            font-weight: 500;
            letter-spacing: 0.2px;
            padding: 11px 16px;
            border-bottom: 1px solid #232a3b;
        }
        QWidget#ControlBar {
            background-color: #0d1018;
            border-top: 1px solid #232a3b;
        }

        /* ══════════════════════════════════════════════
           TABLE  — file-manager feel, on the workspace surface
        ══════════════════════════════════════════════ */
        QTableWidget {
            background-color: #11151f;
            alternate-background-color: #0d1018;
            gridline-color: transparent;
            border: none;
            selection-background-color: rgba(99, 102, 241, 0.14);
            outline: none;
        }
        QHeaderView::section {
            background-color: #0d1018;
            color: #4b5468;
            padding: 9px 12px;
            border: none;
            border-bottom: 1px solid #232a3b;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.8px;
        }
        QTableWidget::item {
            padding: 6px 12px;
            color: #9aa3b8;
            border-bottom: 1px solid #171c29;
        }
        QTableWidget::item:selected {
            background-color: rgba(99, 102, 241, 0.16);
            color: #a5b4fc;
        }
        QTableWidget::item:hover {
            background-color: rgba(99, 102, 241, 0.08);
        }

        /* ══════════════════════════════════════════════
           PROGRESS BAR
        ══════════════════════════════════════════════ */
        QProgressBar#MainProgress {
            border: none;
            border-top: 1px solid #232a3b;
            background-color: #0d1018;
            text-align: center;
            color: #4b5468;
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
            border: 1px solid #232a3b;
            border-radius: 4px;
            background: #171c29;
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
            background: #232a3b;
            border-radius: 3px;
            min-height: 30px;
        }
        QScrollBar::handle:vertical:hover { background: #2d3548; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar:horizontal {
            background: transparent;
            height: 5px;
            border-radius: 3px;
        }
        QScrollBar::handle:horizontal {
            background: #232a3b;
            border-radius: 3px;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        QScrollArea { border: none; background: transparent; }

        /* ══════════════════════════════════════════════
           LIST WIDGET  (history, templates)
        ══════════════════════════════════════════════ */
        QListWidget {
            background-color: #171c29;
            border: 1px solid #232a3b;
            border-radius: 7px;
            color: #9aa3b8;
            outline: none;
        }
        QListWidget::item { padding: 6px 10px; border-radius: 5px; }
        QListWidget::item:selected {
            background-color: rgba(99, 102, 241, 0.14);
            color: #a5b4fc;
        }
        QListWidget::item:hover { background-color: #1f2535; }

        /* ══════════════════════════════════════════════
           FORM LAYOUT LABELS  (Settings QFormLayout)
        ══════════════════════════════════════════════ */
        QFormLayout QLabel {
            color: #7b86a0;
            font-size: 12px;
        }

        /* ══════════════════════════════════════════════
           FALLBACK ORDER DRAG-TO-REORDER LIST
        ══════════════════════════════════════════════ */
        QListWidget#FallbackOrderList {
            background-color: #171c29;
            border: 1px solid #232a3b;
            border-radius: 7px;
            color: #9aa3b8;
            outline: none;
        }
        QListWidget#FallbackOrderList::item {
            padding: 8px 12px;
            border-radius: 5px;
            border-bottom: 1px solid #1f2535;
            color: #c9d1e0;
        }
        QListWidget#FallbackOrderList::item:selected {
            background-color: rgba(99, 102, 241, 0.18);
            color: #a5b4fc;
        }
        QListWidget#FallbackOrderList::item:hover {
            background-color: #1f2535;
            cursor: grab;
        }

        /* ══════════════════════════════════════════════
           TOOLTIPS
        ══════════════════════════════════════════════ */
        QToolTip {
            background-color: #171c29;
            color: #c9d1e0;
            border: 1px solid #2d3548;
            border-radius: 6px;
            padding: 5px 8px;
            font-size: 12px;
        }

        /* ══════════════════════════════════════════════
           CONSOLE PANE
        ══════════════════════════════════════════════ */
        QFrame#ConsolePane {
            background-color: #080b11;
            border-top: 1px solid #1e2535;
        }
        QWidget#ConsoleHeader {
            background-color: #0a0e18;
            border-bottom: 1px solid #1a2030;
        }
        QLabel#ConsoleTitleLbl {
            font-size: 10px;
            font-weight: 700;
            color: #3d4f6b;
            letter-spacing: 1.2px;
            text-transform: uppercase;
            background: transparent;
        }
        QPlainTextEdit#ConsoleOutput {
            background-color: #080b11;
            color: #4a9eff;
            border: none;
            border-radius: 0;
            font-family: 'Cascadia Code', 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
            font-size: 11px;
            line-height: 1.6;
            padding: 6px 10px;
            selection-background-color: #1e3a5f;
        }
        QSplitter::handle {
            background-color: #1a2030;
            height: 4px;
        }
        QSplitter::handle:hover {
            background-color: #6366f1;
        }


        """)


if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    window = MetaEmbedMainWindow()
    window.show()
    sys.exit(app.exec())

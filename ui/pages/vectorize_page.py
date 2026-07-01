"""
ui/pages/vectorize_page.py  —  Image-to-Vector conversion page.

Lets users pick logos/icons/line-art, choose a tuned preset, preview the
traced SVG against the source raster, and export single files or whole
batches. Conversion runs on VectorEngine (core/vector_engine.py) — fully
local/classical tracing, no AI call, so it's near-instant even in bulk.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QObject, QThread, Signal, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QFileDialog, QComboBox, QCheckBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QSizePolicy, QStyle, QAbstractItemView,
)

try:
    from PySide6.QtSvgWidgets import QSvgWidget
    SVG_WIDGET_AVAILABLE = True
except ImportError:  # pragma: no cover - older PySide6 without QtSvgWidgets
    QSvgWidget = None
    SVG_WIDGET_AVAILABLE = False

from core.vector_engine import VectorEngine, VectorizeOptions, VectorizeResult
from core.vector_presets import (
    PRESETS, DEFAULT_PRESET_KEY, get_preset,
    FIDELITY_LEVELS, DEFAULT_FIDELITY_KEY, get_fidelity,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff"}

_STATUS_PENDING = "Pending"
_STATUS_DONE = "Done"
_STATUS_FAILED = "Failed"


# ---------------------------------------------------------------------------
# Background worker — converts a queue of files without blocking the UI
# ---------------------------------------------------------------------------

class VectorizeWorker(QObject):
    """Runs VectorEngine.convert() for each queued file on a worker thread."""

    row_started  = Signal(int)
    row_finished = Signal(int, object)   # (row, VectorizeResult)
    all_finished = Signal()

    def __init__(self, engine: VectorEngine, jobs: list, options: VectorizeOptions):
        super().__init__()
        self.engine = engine
        # jobs: list of (row_index, input_path, output_path)
        self.jobs = jobs
        self.options = options
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        for row, in_path, out_path in self.jobs:
            if self._cancelled:
                break
            self.row_started.emit(row)
            result = self.engine.convert(in_path, out_path, self.options)
            self.row_finished.emit(row, result)
        self.all_finished.emit()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

class VectorizePage(QWidget):
    """Image-to-Vector conversion workspace."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = VectorEngine()
        self._queue: list = []          # list of dicts: {path, output, status, result}
        self._thread: Optional[QThread] = None
        self._worker: Optional[VectorizeWorker] = None
        self._current_preview_svg: Optional[Path] = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        title = QLabel("Image to Vector")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Convert logos, icons, and line art into clean SVG vectors — "
            "fast, local, algorithmic tracing (no AI call required)."
        )
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        # --- Controls row ---------------------------------------------------
        controls = QFrame()
        controls.setObjectName("Card")
        c_layout = QHBoxLayout(controls)
        c_layout.setContentsMargins(16, 14, 16, 14)
        c_layout.setSpacing(10)

        self.btn_add_files = QPushButton("  Add Images")
        self.btn_add_files.setIcon(self.style().standardIcon(QStyle.SP_FileDialogStart))
        self.btn_add_files.clicked.connect(self._on_add_files)

        self.btn_add_folder = QPushButton("  Add Folder")
        self.btn_add_folder.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.btn_add_folder.clicked.connect(self._on_add_folder)

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self._on_clear)

        c_layout.addWidget(self.btn_add_files)
        c_layout.addWidget(self.btn_add_folder)
        c_layout.addWidget(self.btn_clear)
        c_layout.addStretch()

        c_layout.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        for key, preset in PRESETS.items():
            self.preset_combo.addItem(preset.label, userData=key)
        idx = self.preset_combo.findData(DEFAULT_PRESET_KEY)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        c_layout.addWidget(self.preset_combo)

        c_layout.addWidget(QLabel("Fidelity:"))
        self.fidelity_combo = QComboBox()
        for key, fid in FIDELITY_LEVELS.items():
            self.fidelity_combo.addItem(fid.label, userData=key)
        fidx = self.fidelity_combo.findData(DEFAULT_FIDELITY_KEY)
        if fidx >= 0:
            self.fidelity_combo.setCurrentIndex(fidx)
        self.fidelity_combo.currentIndexChanged.connect(self._on_preset_changed)
        c_layout.addWidget(self.fidelity_combo)

        self.chk_remove_bg = QCheckBox("Remove background")
        self.chk_remove_bg.setChecked(True)
        c_layout.addWidget(self.chk_remove_bg)

        self.chk_upscale = QCheckBox("Upscale small images")
        self.chk_upscale.setChecked(True)
        c_layout.addWidget(self.chk_upscale)

        root.addWidget(controls)

        self.preset_desc_lbl = QLabel(get_preset(DEFAULT_PRESET_KEY).description)
        self.preset_desc_lbl.setObjectName("CardNote")
        self.preset_desc_lbl.setWordWrap(True)
        root.addWidget(self.preset_desc_lbl)

        # --- Main split: queue table (left) + preview (right) --------------
        mid = QHBoxLayout()
        mid.setSpacing(14)

        # Queue table
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["File", "Status", "Paths"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        mid.addWidget(self.table, stretch=1)

        # Preview pane (source vs traced SVG, side by side)
        preview = QFrame()
        preview.setObjectName("Card")
        preview.setMinimumWidth(420)
        p_layout = QVBoxLayout(preview)
        p_layout.setContentsMargins(16, 14, 16, 14)

        p_layout.addWidget(QLabel("Preview"))
        compare_row = QHBoxLayout()

        src_col = QVBoxLayout()
        src_col.addWidget(self._small_label("Source"))
        self.src_preview = QLabel("No image selected")
        self.src_preview.setAlignment(Qt.AlignCenter)
        self.src_preview.setMinimumSize(180, 180)
        self.src_preview.setObjectName("PreviewBox")
        src_col.addWidget(self.src_preview)
        compare_row.addLayout(src_col)

        svg_col = QVBoxLayout()
        svg_col.addWidget(self._small_label("Vector Result"))
        if SVG_WIDGET_AVAILABLE:
            self.svg_preview = QSvgWidget()
            self.svg_preview.setMinimumSize(180, 180)
        else:
            self.svg_preview = QLabel("SVG preview unavailable\n(install PySide6-Addons)")
            self.svg_preview.setAlignment(Qt.AlignCenter)
        self.svg_preview.setObjectName("PreviewBox")
        svg_col.addWidget(self.svg_preview)
        compare_row.addLayout(svg_col)

        p_layout.addLayout(compare_row)

        self.stats_lbl = QLabel("")
        self.stats_lbl.setObjectName("CardNote")
        self.stats_lbl.setWordWrap(True)
        p_layout.addWidget(self.stats_lbl)

        p_layout.addStretch()

        self.btn_save_svg = QPushButton("Save SVG As…")
        self.btn_save_svg.setEnabled(False)
        self.btn_save_svg.clicked.connect(self._on_save_current_svg)
        p_layout.addWidget(self.btn_save_svg)

        mid.addWidget(preview)
        root.addLayout(mid, stretch=1)

        # --- Bottom action bar -----------------------------------------------
        bottom = QHBoxLayout()
        self.queue_count_lbl = QLabel("0 images queued")
        self.queue_count_lbl.setObjectName("CardNote")
        bottom.addWidget(self.queue_count_lbl)
        bottom.addStretch()

        self.btn_export_all = QPushButton("Export All to Folder…")
        self.btn_export_all.clicked.connect(self._on_export_all)
        bottom.addWidget(self.btn_export_all)

        self.btn_convert = QPushButton("  Convert All")
        self.btn_convert.setObjectName("SaveBtn")
        self.btn_convert.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.btn_convert.clicked.connect(self._on_convert_all)
        bottom.addWidget(self.btn_convert)

        root.addLayout(bottom)

    def _small_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("CardNote")
        return lbl

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def _on_add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select images", "",
            "Images (*.jpg *.jpeg *.png *.bmp *.webp *.gif *.tif *.tiff)",
        )
        if files:
            self._add_to_queue(files)

    def _on_add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder")
        if not folder:
            return
        files = [
            str(p) for p in sorted(Path(folder).iterdir())
            if p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not files:
            QMessageBox.information(self, "No images found",
                                     "That folder has no supported image files.")
            return
        self._add_to_queue(files)

    def _add_to_queue(self, paths: list):
        for path in paths:
            self._queue.append({
                "path": path,
                "output": str(Path(path).with_suffix(".svg")),
                "status": _STATUS_PENDING,
                "result": None,
            })
        self._refresh_table()

    def _on_clear(self):
        self._queue.clear()
        self._refresh_table()
        self._clear_preview()

    def _refresh_table(self):
        self.table.setRowCount(len(self._queue))
        for row, item in enumerate(self._queue):
            self.table.setItem(row, 0, QTableWidgetItem(Path(item["path"]).name))
            self.table.setItem(row, 1, QTableWidgetItem(item["status"]))
            paths = item["result"].path_count if item["result"] else ""
            self.table.setItem(row, 2, QTableWidgetItem(str(paths)))
        self.queue_count_lbl.setText(f"{len(self._queue)} images queued")

    # ------------------------------------------------------------------
    # Preset handling
    # ------------------------------------------------------------------

    def _on_preset_changed(self):
        key = self.preset_combo.currentData()
        fkey = self.fidelity_combo.currentData()
        preset = get_preset(key)
        fid = get_fidelity(fkey)
        parts = []
        if preset:
            parts.append(preset.description)
        if fid:
            parts.append(f"<b>Fidelity:</b> {fid.description}")
        self.preset_desc_lbl.setText("  ·  ".join(parts) if parts else "")

    def _current_options(self) -> VectorizeOptions:
        key = self.preset_combo.currentData()
        fkey = self.fidelity_combo.currentData()
        preset = get_preset(key) or get_preset(DEFAULT_PRESET_KEY)
        return VectorizeOptions(
            preset=preset,
            fidelity=fkey or DEFAULT_FIDELITY_KEY,
            remove_background=self.chk_remove_bg.isChecked(),
            upscale_small_input=self.chk_upscale.isChecked(),
        )

    # ------------------------------------------------------------------
    # Conversion (single row, used for live preview on selection)
    # ------------------------------------------------------------------

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self._clear_preview()
            return
        row = rows[0].row()
        if row >= len(self._queue):
            return
        item = self._queue[row]
        self._show_source_preview(item["path"])

        if item["result"] and item["result"].ok:
            self._show_svg_preview(item["result"].svg_path, item["result"])
        else:
            self._clear_svg_preview()

    def _show_source_preview(self, path: str):
        pix = QPixmap(path)
        if pix.isNull():
            self.src_preview.setText("Preview unavailable")
            return
        scaled = pix.scaled(180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.src_preview.setPixmap(scaled)

    def _show_svg_preview(self, svg_path: Path, result: VectorizeResult):
        self._current_preview_svg = svg_path
        if SVG_WIDGET_AVAILABLE:
            self.svg_preview.load(str(svg_path))
        self.btn_save_svg.setEnabled(True)
        src = result.source_size or ("?", "?")
        self.stats_lbl.setText(
            f"{result.path_count} paths  ·  preset: {result.preset_key}  ·  "
            f"fidelity: {result.fidelity_key}  ·  source {src[0]}×{src[1]}px"
        )

    def _clear_svg_preview(self):
        self._current_preview_svg = None
        self.btn_save_svg.setEnabled(False)
        self.stats_lbl.setText("Not converted yet — click Convert All.")
        if SVG_WIDGET_AVAILABLE:
            self.svg_preview.load(b"")

    def _clear_preview(self):
        self.src_preview.setText("No image selected")
        self.src_preview.setPixmap(QPixmap())
        self._clear_svg_preview()

    # ------------------------------------------------------------------
    # Batch conversion (background thread — UI never blocks)
    # ------------------------------------------------------------------

    def _on_convert_all(self):
        if not self._queue:
            QMessageBox.information(self, "Nothing to convert", "Add some images first.")
            return
        if self._thread is not None:
            QMessageBox.information(self, "Already running",
                                     "A conversion is already in progress.")
            return

        jobs = [
            (row, item["path"], item["output"])
            for row, item in enumerate(self._queue)
        ]
        options = self._current_options()

        self.btn_convert.setEnabled(False)
        self._thread = QThread()
        self._worker = VectorizeWorker(self.engine, jobs, options)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.row_started.connect(self._on_row_started, Qt.QueuedConnection)
        self._worker.row_finished.connect(self._on_row_finished, Qt.QueuedConnection)
        self._worker.all_finished.connect(self._on_all_finished, Qt.QueuedConnection)
        self._worker.all_finished.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)

        self._thread.start()

    def _on_row_started(self, row: int):
        if row < len(self._queue):
            self._queue[row]["status"] = "Converting…"
            self._refresh_table()

    def _on_row_finished(self, row: int, result: VectorizeResult):
        if row >= len(self._queue):
            return
        self._queue[row]["result"] = result
        self._queue[row]["status"] = _STATUS_DONE if result.ok else _STATUS_FAILED
        if not result.ok:
            logger.warning("Vectorize failed for row %d: %s", row, result.reason)
        self._refresh_table()

    def _on_all_finished(self):
        self.btn_convert.setEnabled(True)
        failed = sum(1 for i in self._queue if i["status"] == _STATUS_FAILED)
        done = sum(1 for i in self._queue if i["status"] == _STATUS_DONE)
        if failed:
            QMessageBox.warning(
                self, "Conversion complete",
                f"{done} converted, {failed} failed. Check the Status column "
                "for details — hover a failed row's source file for the reason.",
            )
        self._on_selection_changed()  # refresh preview if current row finished

    def _cleanup_thread(self):
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_save_current_svg(self):
        if not self._current_preview_svg or not self._current_preview_svg.exists():
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save SVG As", self._current_preview_svg.name, "SVG files (*.svg)",
        )
        if dest:
            try:
                Path(dest).write_bytes(self._current_preview_svg.read_bytes())
            except OSError as exc:
                QMessageBox.critical(self, "Save failed", str(exc))

    def _on_export_all(self):
        done_items = [i for i in self._queue if i["status"] == _STATUS_DONE and i["result"]]
        if not done_items:
            QMessageBox.information(
                self, "Nothing to export",
                "Convert images first (Convert All), then export the results.",
            )
            return
        folder = QFileDialog.getExistingDirectory(self, "Export SVGs to folder")
        if not folder:
            return

        errors = []
        for item in done_items:
            svg_path = item["result"].svg_path
            dest = Path(folder) / svg_path.name
            try:
                dest.write_bytes(Path(svg_path).read_bytes())
            except OSError as exc:
                errors.append(f"{svg_path.name}: {exc}")

        if errors:
            QMessageBox.warning(self, "Export finished with errors", "\n".join(errors))
        else:
            QMessageBox.information(self, "Export complete",
                                     f"{len(done_items)} SVG files exported to {folder}.")

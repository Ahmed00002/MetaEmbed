"""
ui/pages/dashboard_page.py  —  Token & Usage Dashboard page.

Shows live stats for every AI call made: token burn, cost estimates,
provider/model breakdown, success rate, image payload sizes, and a
call-timeline sparkline.  Reads from TokenTracker (core/token_tracker.py).
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QGridLayout, QSizePolicy, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tiny sparkline widget (no external charting library required)
# ---------------------------------------------------------------------------

class SparklineWidget(QWidget):
    """Draws a simple line chart of recent per-call token counts."""

    def __init__(self, color: str = "#6366f1", parent=None):
        super().__init__(parent)
        self._data: list[float] = []
        self._color = QColor(color)
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, values: list[float]):
        self._data = list(values[-120:])   # keep last 120 points
        self.update()

    def paintEvent(self, event):
        if len(self._data) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        pad = 4
        data = self._data
        mn, mx = min(data), max(data)
        span = mx - mn or 1

        def _y(v):
            return h - pad - int((v - mn) / span * (h - 2 * pad))

        def _x(i):
            return pad + int(i / (len(data) - 1) * (w - 2 * pad))

        # Fill area under line
        fill = QColor(self._color)
        fill.setAlpha(30)
        p.setBrush(QBrush(fill))
        p.setPen(Qt.NoPen)
        from PySide6.QtGui import QPolygon
        from PySide6.QtCore import QPoint
        pts = [QPoint(_x(i), _y(v)) for i, v in enumerate(data)]
        pts += [QPoint(_x(len(data) - 1), h), QPoint(_x(0), h)]
        p.drawPolygon(QPolygon(pts))

        # Line
        pen = QPen(self._color, 1.5)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        for i in range(len(data) - 1):
            p.drawLine(_x(i), _y(data[i]), _x(i + 1), _y(data[i + 1]))

        p.end()


# ---------------------------------------------------------------------------
# Helper: stat card (small KPI tile)
# ---------------------------------------------------------------------------

def _stat_card(label: str, value: str = "—", accent: str = "#818cf8") -> tuple:
    """Return (card_frame, value_label) so callers can update the value."""
    card = QFrame()
    card.setObjectName("Card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(4)

    lbl = QLabel(label.upper())
    lbl.setObjectName("FieldLabel")
    lay.addWidget(lbl)

    val = QLabel(value)
    val.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {accent};")
    lay.addWidget(val)

    return card, val


# ---------------------------------------------------------------------------
# Main dashboard page
# ---------------------------------------------------------------------------

class DashboardPage(QWidget):
    """Token burn & AI usage dashboard."""

    def __init__(self, token_tracker=None):
        super().__init__()
        self._tracker = token_tracker
        self._build_ui()

        # Auto-refresh every 5 s while the page is visible
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 16)
        outer.setSpacing(16)

        # ── Header ────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("Usage Dashboard")
        title.setObjectName("PageTitle")
        hdr.addWidget(title)
        hdr.addStretch()

        self._last_updated_lbl = QLabel("Never updated")
        self._last_updated_lbl.setObjectName("CardNote")
        hdr.addWidget(self._last_updated_lbl)

        self._btn_refresh = QPushButton("↻ Refresh")
        self._btn_refresh.setObjectName("SecBtn")
        self._btn_refresh.setFixedHeight(32)
        self._btn_refresh.clicked.connect(self.refresh)
        hdr.addWidget(self._btn_refresh)

        self._btn_reset = QPushButton("Reset Stats")
        self._btn_reset.setObjectName("WarnBtn")
        self._btn_reset.setFixedHeight(32)
        self._btn_reset.clicked.connect(self._on_reset)
        hdr.addWidget(self._btn_reset)

        outer.addLayout(hdr)

        sub = QLabel("Live token consumption, cost estimates, and model breakdown for every AI call.")
        sub.setObjectName("PageSubtitle")
        sub.setWordWrap(True)
        outer.addWidget(sub)

        # ── Scroll area (everything below the header) ─────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(16)
        scroll.setWidget(container)
        outer.addWidget(scroll, stretch=1)

        # ── KPI row ───────────────────────────────────────────────────
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)

        card, self._kpi_calls       = _stat_card("Total Calls",       "0",   "#818cf8")
        kpi_row.addWidget(card)
        card, self._kpi_success     = _stat_card("Success Rate",      "—",   "#10b981")
        kpi_row.addWidget(card)
        card, self._kpi_in_tok      = _stat_card("Input Tokens",      "0",   "#6366f1")
        kpi_row.addWidget(card)
        card, self._kpi_out_tok     = _stat_card("Output Tokens",     "0",   "#a78bfa")
        kpi_row.addWidget(card)
        card, self._kpi_cost        = _stat_card("Est. Cost (USD)",   "$0.000", "#f59e0b")
        kpi_row.addWidget(card)
        card, self._kpi_img_mb      = _stat_card("Image Data Sent",   "0 MB", "#38bdf8")
        kpi_row.addWidget(card)
        lay.addLayout(kpi_row)

        # ── Token ratio bar ───────────────────────────────────────────
        ratio_card = QFrame()
        ratio_card.setObjectName("Card")
        rc_lay = QVBoxLayout(ratio_card)
        rc_lay.setContentsMargins(16, 14, 16, 14)
        rc_lay.setSpacing(8)

        ratio_hdr = QHBoxLayout()
        ratio_title = QLabel("Input vs Output Token Split")
        ratio_title.setObjectName("CardTitle")
        ratio_hdr.addWidget(ratio_title)
        ratio_hdr.addStretch()
        self._ratio_lbl = QLabel("—")
        self._ratio_lbl.setObjectName("CardNote")
        ratio_hdr.addWidget(self._ratio_lbl)
        rc_lay.addLayout(ratio_hdr)

        self._ratio_bar = QProgressBar()
        self._ratio_bar.setRange(0, 100)
        self._ratio_bar.setValue(0)
        self._ratio_bar.setTextVisible(False)
        self._ratio_bar.setFixedHeight(10)
        self._ratio_bar.setStyleSheet("""
            QProgressBar { background: #232a3b; border-radius: 5px; border: none; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #6366f1, stop:1 #a78bfa); border-radius: 5px; }
        """)
        rc_lay.addWidget(self._ratio_bar)

        ratio_footnote = QLabel("Tip: Input tokens cost less than output tokens on most providers.")
        ratio_footnote.setObjectName("CardNote")
        rc_lay.addWidget(ratio_footnote)
        lay.addWidget(ratio_card)

        # ── Sparkline ─────────────────────────────────────────────────
        spark_card = QFrame()
        spark_card.setObjectName("Card")
        sk_lay = QVBoxLayout(spark_card)
        sk_lay.setContentsMargins(16, 14, 16, 14)
        sk_lay.setSpacing(8)

        spark_title = QLabel("Tokens Per Call — Recent History")
        spark_title.setObjectName("CardTitle")
        sk_lay.addWidget(spark_title)

        self._sparkline = SparklineWidget("#6366f1")
        sk_lay.addWidget(self._sparkline)

        spark_note = QLabel("Each bar = one AI call (input + output tokens combined). Last 120 calls shown.")
        spark_note.setObjectName("CardNote")
        sk_lay.addWidget(spark_note)
        lay.addWidget(spark_card)

        # ── Provider breakdown table ──────────────────────────────────
        prov_card = QFrame()
        prov_card.setObjectName("Card")
        pv_lay = QVBoxLayout(prov_card)
        pv_lay.setContentsMargins(16, 14, 16, 14)
        pv_lay.setSpacing(10)

        prov_title = QLabel("Provider Breakdown")
        prov_title.setObjectName("CardTitle")
        pv_lay.addWidget(prov_title)

        self._prov_table = QTableWidget(0, 7)
        self._prov_table.setHorizontalHeaderLabels([
            "Provider", "Calls", "✓ Success", "✗ Errors",
            "Input Tokens", "Output Tokens", "Est. Cost",
        ])
        self._prov_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._prov_table.verticalHeader().setVisible(False)
        self._prov_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._prov_table.setAlternatingRowColors(True)
        self._prov_table.setShowGrid(False)
        self._prov_table.setFixedHeight(180)
        pv_lay.addWidget(self._prov_table)
        lay.addWidget(prov_card)

        # ── Model breakdown table ─────────────────────────────────────
        mdl_card = QFrame()
        mdl_card.setObjectName("Card")
        md_lay = QVBoxLayout(mdl_card)
        md_lay.setContentsMargins(16, 14, 16, 14)
        md_lay.setSpacing(10)

        mdl_title = QLabel("Model Breakdown")
        mdl_title.setObjectName("CardTitle")
        md_lay.addWidget(mdl_title)

        self._mdl_table = QTableWidget(0, 6)
        self._mdl_table.setHorizontalHeaderLabels([
            "Model", "Calls", "Input Tok", "Output Tok", "Est. Cost", "Avg Tok/Call",
        ])
        self._mdl_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._mdl_table.verticalHeader().setVisible(False)
        self._mdl_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._mdl_table.setAlternatingRowColors(True)
        self._mdl_table.setShowGrid(False)
        self._mdl_table.setFixedHeight(180)
        md_lay.addWidget(self._mdl_table)
        lay.addWidget(mdl_card)

        # ── Efficiency tips card ──────────────────────────────────────
        tips_card = QFrame()
        tips_card.setObjectName("Card")
        tp_lay = QVBoxLayout(tips_card)
        tp_lay.setContentsMargins(16, 14, 16, 14)
        tp_lay.setSpacing(6)

        tips_title = QLabel("Efficiency Notes")
        tips_title.setObjectName("CardTitle")
        tp_lay.addWidget(tips_title)

        self._tips_lbl = QLabel("Run some images to see personalized efficiency tips.")
        self._tips_lbl.setObjectName("CardNote")
        self._tips_lbl.setWordWrap(True)
        tp_lay.addWidget(self._tips_lbl)
        lay.addWidget(tips_card)

        # ── Session info ──────────────────────────────────────────────
        info_card = QFrame()
        info_card.setObjectName("Card")
        inf_lay = QGridLayout(info_card)
        inf_lay.setContentsMargins(16, 14, 16, 14)
        inf_lay.setSpacing(8)

        def _info_pair(label, row, col):
            lbl = QLabel(label)
            lbl.setObjectName("FieldLabel")
            val = QLabel("—")
            val.setObjectName("CardNote")
            inf_lay.addWidget(lbl, row, col * 2)
            inf_lay.addWidget(val, row, col * 2 + 1)
            return val

        info_title = QLabel("Session Info")
        info_title.setObjectName("CardTitle")
        inf_lay.addWidget(info_title, 0, 0, 1, 4)

        self._info_first   = _info_pair("First Call",  1, 0)
        self._info_last    = _info_pair("Last Call",   1, 1)
        self._info_avg_in  = _info_pair("Avg Input Tok/Call",  2, 0)
        self._info_avg_out = _info_pair("Avg Output Tok/Call", 2, 1)
        self._info_img_avg = _info_pair("Avg Image Size",      3, 0)
        self._info_err_rate = _info_pair("Error Rate",          3, 1)

        lay.addWidget(info_card)
        lay.addStretch()

        # Initial load
        self.refresh()

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def set_tracker(self, tracker):
        self._tracker = tracker
        self.refresh()

    def refresh(self):
        if self._tracker is None:
            return
        stats = self._tracker.get_stats()
        self._populate(stats)
        now = datetime.now().strftime("%H:%M:%S")
        self._last_updated_lbl.setText(f"Updated {now}")

    def _populate(self, stats: Dict[str, Any]):
        t = stats.get("totals", {})
        calls      = t.get("calls", 0)
        success    = t.get("success", 0)
        errors     = t.get("errors", 0)
        in_tok     = t.get("input_tokens", 0)
        out_tok    = t.get("output_tokens", 0)
        cost       = t.get("estimated_cost_usd", 0.0)
        img_bytes  = t.get("image_bytes", 0)

        # KPI cards
        self._kpi_calls.setText(f"{calls:,}")
        if calls:
            pct = int(success / calls * 100)
            self._kpi_success.setText(f"{pct}%")
        else:
            self._kpi_success.setText("—")
        self._kpi_in_tok.setText(f"{in_tok:,}")
        self._kpi_out_tok.setText(f"{out_tok:,}")
        self._kpi_cost.setText(f"${cost:.4f}")
        mb = img_bytes / 1_048_576
        self._kpi_img_mb.setText(f"{mb:.1f} MB")

        # Token ratio bar
        total_tok = in_tok + out_tok
        if total_tok:
            ratio_pct = int(in_tok / total_tok * 100)
            self._ratio_bar.setValue(ratio_pct)
            self._ratio_lbl.setText(
                f"Input {ratio_pct}%  ·  Output {100 - ratio_pct}%  "
                f"({in_tok:,} / {out_tok:,} tokens)"
            )

        # Sparkline — total tokens per call
        recent = stats.get("recent_calls", [])
        spark_vals = [r.get("input", 0) + r.get("output", 0) for r in recent]
        self._sparkline.set_data(spark_vals)

        # Provider table
        providers = stats.get("providers", {})
        self._prov_table.setRowCount(0)
        for prov, pd in sorted(providers.items(), key=lambda x: -x[1]["calls"]):
            row = self._prov_table.rowCount()
            self._prov_table.insertRow(row)
            items = [
                prov.title(),
                f"{pd['calls']:,}",
                f"{pd['success']:,}",
                f"{pd['errors']:,}",
                f"{pd['input_tokens']:,}",
                f"{pd['output_tokens']:,}",
                f"${pd['estimated_cost_usd']:.4f}",
            ]
            for col, txt in enumerate(items):
                item = QTableWidgetItem(txt)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 3 and pd["errors"] > 0:
                    item.setForeground(QColor("#ef4444"))
                self._prov_table.setItem(row, col, item)

        # Model table
        models = stats.get("models", {})
        self._mdl_table.setRowCount(0)
        for mkey, md in sorted(models.items(), key=lambda x: -x[1]["calls"]):
            row = self._mdl_table.rowCount()
            self._mdl_table.insertRow(row)
            avg = (md["input_tokens"] + md["output_tokens"]) // max(md["calls"], 1)
            items = [
                mkey,
                f"{md['calls']:,}",
                f"{md['input_tokens']:,}",
                f"{md['output_tokens']:,}",
                f"${md['estimated_cost_usd']:.4f}",
                f"{avg:,}",
            ]
            for col, txt in enumerate(items):
                item = QTableWidgetItem(txt)
                item.setTextAlignment(Qt.AlignCenter)
                self._mdl_table.setItem(row, col, item)

        # Session info
        def _fmt_ts(iso):
            if not iso:
                return "—"
            try:
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                return dt.astimezone().strftime("%b %d  %H:%M")
            except Exception:
                return iso

        self._info_first.setText(_fmt_ts(stats.get("first_call_ts")))
        self._info_last.setText(_fmt_ts(stats.get("last_call_ts")))
        if calls:
            self._info_avg_in.setText(f"{in_tok // calls:,}")
            self._info_avg_out.setText(f"{out_tok // calls:,}")
            avg_img_kb = (img_bytes / calls) / 1024
            self._info_img_avg.setText(f"{avg_img_kb:.0f} KB")
            err_pct = errors / calls * 100
            self._info_err_rate.setText(f"{err_pct:.1f}%")
        else:
            for lbl in (self._info_avg_in, self._info_avg_out,
                        self._info_img_avg, self._info_err_rate):
                lbl.setText("—")

        # Efficiency tips
        self._tips_lbl.setText(self._compute_tips(t, calls))

    # ------------------------------------------------------------------
    # Efficiency tips generator
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_tips(t: dict, calls: int) -> str:
        if calls == 0:
            return "Run some images to see personalized efficiency tips."
        tips = []
        in_tok  = t.get("input_tokens", 0)
        out_tok = t.get("output_tokens", 0)
        errors  = t.get("errors", 0)
        img_bytes = t.get("image_bytes", 0)

        avg_in = in_tok // calls
        if avg_in > 1500:
            tips.append(
                f"⚠  Average input tokens ({avg_in:,}) is high — your system prompt or image "
                f"payloads may be larger than needed. Images are already capped at 768 px."
            )
        elif avg_in < 800:
            tips.append("✓  Input token usage looks efficient.")

        avg_img_kb = (img_bytes / calls) / 1024
        if avg_img_kb > 200:
            tips.append(
                f"📷  Average image payload is {avg_img_kb:.0f} KB — consider dropping "
                f"JPEG quality to 65 in main.py for further savings."
            )
        else:
            tips.append(f"✓  Average image payload is {avg_img_kb:.0f} KB — well optimised.")

        if errors / calls > 0.1:
            tips.append(
                f"🔴  Error rate is {errors/calls*100:.0f}% — check your API keys and "
                f"rate limits, or enable Auto-Provider fallback in Settings."
            )

        total_tok = in_tok + out_tok
        if total_tok > 0 and out_tok / total_tok > 0.4:
            tips.append(
                "💡  Output tokens are a large share of your total — "
                "max_tokens is already set to 1024 (good); the model is "
                "generating long responses. This is normal for detailed metadata."
            )

        return "\n".join(tips) if tips else "✓  All efficiency metrics look healthy."

    # ------------------------------------------------------------------

    def _on_reset(self):
        if self._tracker is None:
            return
        reply = QMessageBox.question(
            self, "Reset Usage Stats",
            "This will permanently delete all recorded usage statistics.\n\nAre you sure?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._tracker.reset()
            self.refresh()

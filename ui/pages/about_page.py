"""
ui/pages/about_page.py  —  AboutPage page widget.
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



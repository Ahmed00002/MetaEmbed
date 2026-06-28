import os
import sys
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from typing import Optional

from PySide6.QtCore import QThread, Signal, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtGui import QImageReader

from ui.ui_main import MetaEmbedMainWindow
from core.ai_service import AIService
from core.metadata_engine import MetadataEngine
from core.config_manager import ConfigManager
from core.history_manager import HistoryManager
from core.stock_markets import MARKETS, get_market, apply_rules
from core.exporter import MetadataExporter
from core.keyword_tools import apply_template, check_metadata_quality, enforce_range_limits

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class ProgressInfo:
    """Item #8 — rich progress payload emitted on every step so the UI can
    show current image, remaining count, live success/fail counts, and an
    estimated time remaining, instead of just a bare percentage."""
    percent: int = 0
    current_image: str = ""
    current_index: int = 0      # 1-based index of the image just processed
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    duplicates: int = 0
    elapsed_seconds: float = 0.0
    eta_seconds: Optional[float] = None

    @property
    def images_remaining(self) -> int:
        return max(0, self.total - self.current_index)

    def eta_text(self) -> str:
        if self.eta_seconds is None:
            return "estimating…"
        mins, secs = divmod(int(self.eta_seconds), 60)
        return f"{mins}m {secs}s" if mins else f"{secs}s"


@dataclass
class BatchSummary:
    """Item #2 — final batch summary shown after a run finishes or is cancelled."""
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    duplicates: int = 0
    cancelled: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    failed_files: list = field(default_factory=list)   # [(path, reason), ...]
    skipped_files: list = field(default_factory=list)  # [(path, reason), ...]

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def to_message(self) -> str:
        mins, secs = divmod(int(self.elapsed_seconds), 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        lines = [
            "Batch cancelled — partial summary:" if self.cancelled else "Batch complete.",
            "",
            f"Total images:   {self.total}",
            f"Succeeded:      {self.success}",
            f"Failed:         {self.failed}",
            f"Skipped:        {self.skipped}",
            f"Duplicates:     {self.duplicates}",
            f"Processing time: {time_str}",
        ]
        if self.failed_files:
            lines.append("")
            lines.append("Failed files:")
            for path, reason in self.failed_files[:8]:
                lines.append(f"  • {Path(path).name} — {reason}")
            if len(self.failed_files) > 8:
                lines.append(f"  …and {len(self.failed_files) - 8} more")
        return "\n".join(lines)


def _resize_for_api(image_bytes: bytes, max_px: int = 1536) -> bytes:
    """
    Downscale image bytes so neither dimension exceeds `max_px` pixels,
    then re-encode as JPEG. Returns the original bytes unchanged if:
      - Pillow is not available
      - the image is already within the size limit
      - re-encoding would produce a larger payload (rare for very small images)

    This is a pure speed optimisation: vision models understand imagery at
    1024-1536 px just as well as at 4000+ px, but the base64 payload sent
    over the network can be 10-50× smaller, cutting upload time dramatically
    for large TIFFs, PNGs, and high-res JPEGs.
    """
    try:
        from PIL import Image
        import io
    except ImportError:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        if w <= max_px and h <= max_px:
            return image_bytes          # already small enough

        # Preserve aspect ratio
        ratio = min(max_px / w, max_px / h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Convert palette/RGBA to RGB for JPEG compatibility
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90, optimize=True)
        resized = buf.getvalue()

        # Only use the resized version if it's actually smaller
        if len(resized) < len(image_bytes):
            logger.debug(
                "Resized image %dx%d → %dx%d  (%.0f KB → %.0f KB)",
                w, h, new_w, new_h,
                len(image_bytes) / 1024, len(resized) / 1024,
            )
            return resized
        return image_bytes

    except Exception as exc:  # noqa: BLE001
        logger.debug("Image resize skipped (%s); sending original.", exc)
        return image_bytes


class Worker(QObject):
    """
    Batch metadata generation in a background thread.

    Processes files in groups of `batch_size`, one file at a time within
    each batch, allowing fine-grained progress and cancel support.

    Item #2 / #9: a failure on one image never aborts the batch — the
    worker always continues to the next image, and the queue is processed
    exactly once through (never restarted) even across cancel/resume.
    Cancelling lets the in-flight image finish its current operation safely
    before stopping the remaining queue.
    """
    progress      = Signal(object)     # ProgressInfo
    status_update = Signal(int, str)   # (row_index, status_text)
    result_ready  = Signal(int, dict)  # (row_index, metadata_dict)
    error         = Signal(str)
    finished      = Signal(object)     # BatchSummary

    def __init__(self, files: list, ai: AIService, meta: MetadataEngine,
                 history: HistoryManager, provider: str, model: str,
                 market_key: str, custom_keywords: list,
                 marketplace_validation_enabled: bool,
                 batch_size: int = 3, row_offset: int = 0,
                 active_template: Optional[dict] = None,
                 auto_embed: bool = True,
                 auto_provider: bool = True,
                 metadata_rule_overrides: Optional[dict] = None,
                 row_map: Optional[dict] = None):
        super().__init__()
        self.files          = files
        self.ai             = ai
        self.meta           = meta
        self.history        = history
        self.provider       = provider
        self.model          = model
        self.market_key     = market_key
        self.custom_keywords = custom_keywords
        self.marketplace_validation_enabled = marketplace_validation_enabled
        self.active_template = active_template
        self.batch_size     = max(1, batch_size)
        self.row_offset     = row_offset
        self.auto_embed     = auto_embed
        self.auto_provider  = auto_provider
        # Bug fix: the Settings page's Title Length / Keyword Count fields
        # used to do nothing except feed the cosmetic quality-score widget
        # in the Inspector — the actual AI call always used the selected
        # market's own fixed defaults instead, so the AI would routinely
        # generate outside the range the user explicitly configured. This
        # dict (title_min_length/title_max_length/keyword_min_count/
        # keyword_max_count) is now applied on top of the market's rules
        # before every generation call — see run() below.
        self.metadata_rule_overrides = metadata_rule_overrides or {}
        self._cancelled     = False
        self._results: list[dict] = []
        self.summary        = BatchSummary()
        # row_map: {path -> row_index} for precise cross-queue targeting.
        # When present, overrides the simple row_offset+i calculation so
        # partial runs (retry-failed, pending-only) update the correct rows.
        self._row_map: dict[str, int] = row_map or {}

    def cancel(self):
        """Item #9 — request a safe stop. The image currently being
        processed is allowed to finish (write + verify) before the worker
        stops picking up new images."""
        self._cancelled = True

    def get_results(self) -> list:
        return list(self._results)

    def _apply_rule_overrides(self, market):
        """
        Bug fix: apply the user's Settings-page title/keyword range
        (title_min_length, title_max_length, keyword_min_count,
        keyword_max_count) on top of the selected market's built-in
        defaults, producing the actual MarketRules used for this run.

        Before this fix, these Settings fields were read ONLY by the
        Inspector's live quality-score widget — the real generation call
        always used the selected market's hardcoded numbers regardless
        of what the user configured, which is why the AI appeared to
        "ignore" the Settings entirely. Every other market field
        (keyword_max_len, description_max, sentence_case_title,
        csv_columns, notes) is left untouched — only the four numeric
        range fields the user can actually edit in Settings are
        overridden here.

        Defensive validation, since these are free-typed QSpinBox values
        that can disagree with each other or be physically impossible:
          - min is clamped to be < max for both title and keywords
            (falls back to the market's own pairing if the user's values
            are inverted/equal, rather than silently producing a
            zero-or-negative-width range the AI could never satisfy)
          - title_min can't go below 1, keyword_min can't go below 1
          - if no override dict was supplied at all (e.g. an older
            caller), the market is returned completely unchanged
        """
        if not self.metadata_rule_overrides:
            return market

        rules = self.metadata_rule_overrides
        title_min = rules.get("title_min_length")
        title_max = rules.get("title_max_length")
        kw_min    = rules.get("keyword_min_count")
        kw_max    = rules.get("keyword_max_count")

        updates = {}

        if title_min is not None and title_max is not None:
            title_min, title_max = int(title_min), int(title_max)
            if title_min >= 1 and title_max > title_min:
                updates["title_min"] = title_min
                updates["title_max"] = title_max
            else:
                logger.warning(
                    "Ignoring invalid title length override (%s-%s); "
                    "using market default (%s-%s).",
                    title_min, title_max, market.title_min, market.title_max,
                )

        if kw_min is not None and kw_max is not None:
            kw_min, kw_max = int(kw_min), int(kw_max)
            if kw_min >= 1 and kw_max > kw_min:
                updates["keyword_min"] = kw_min
                updates["keyword_max"] = kw_max
            else:
                logger.warning(
                    "Ignoring invalid keyword count override (%s-%s); "
                    "using market default (%s-%s).",
                    kw_min, kw_max, market.keyword_min, market.keyword_max,
                )

        if not updates:
            return market
        return replace(market, **updates)

    def _emit_progress(self, current_index: int, total: int, current_image_path: str) -> None:
        """Item #8 — build and emit the rich progress payload. ETA is a
        simple average-time-per-image projection: reliable once a handful
        of images have completed, intentionally not shown ("estimating…")
        before that, rather than guessing wildly from a single sample."""
        elapsed = time.time() - self.summary.started_at
        eta = None
        if current_index > 0:
            avg_per_image = elapsed / current_index
            remaining = total - current_index
            if current_index >= 2 or total <= 3:
                eta = max(0.0, avg_per_image * remaining)

        info = ProgressInfo(
            percent=int((current_index / total) * 100) if total else 0,
            current_image=Path(current_image_path).name,
            current_index=current_index,
            total=total,
            success=self.summary.success,
            failed=self.summary.failed,
            skipped=self.summary.skipped,
            duplicates=self.summary.duplicates,
            elapsed_seconds=elapsed,
            eta_seconds=eta,
        )
        self.progress.emit(info)

    def run(self):
        total = len(self.files)
        self.summary = BatchSummary(total=total, started_at=time.time())

        if total == 0:
            self.finished.emit(self.summary)
            return

        market = get_market(self.market_key)
        market = self._apply_rule_overrides(market)

        # Thread-safety: summary counters are mutated from multiple worker
        # threads when batch_size > 1; guard all writes with this lock.
        self._summary_lock = Lock()

        # ----------------------------------------------------------------
        # Pre-pass: mark duplicates immediately so they are never submitted
        # to the thread pool. Item #10 behaviour is preserved — every row
        # still gets a visible status update.
        # ----------------------------------------------------------------
        seen_paths: dict = {}   # normalized path -> first row index
        work_items = []         # (i, row, path) tuples for unique files
        for i, path in enumerate(self.files):
            # Use the explicit row_map when provided (partial runs like
            # retry-failed or pending-only), fall back to sequential offset.
            if self._row_map:
                row = self._row_map.get(os.path.normpath(path),
                                        self._row_map.get(path, self.row_offset + i))
            else:
                row = self.row_offset + i
            norm = os.path.normpath(path)
            if norm in seen_paths:
                with self._summary_lock:
                    self.summary.duplicates += 1
                self.status_update.emit(row, "Duplicate (skipped)")
                self.history.log_action(
                    action="duplicate_detected", status="skipped",
                    image_name=Path(path).name, processing_stage="queue_dedup",
                    error_reason="Duplicate of an already-processed file in this batch",
                )
            else:
                seen_paths[norm] = row
                work_items.append((i, row, path))

        # ----------------------------------------------------------------
        # Concurrent processing: batch_size controls how many AI requests
        # run simultaneously. Each image is read and sent to the AI in its
        # own thread — network I/O dominates, so true parallelism gives an
        # ~(batch_size)x throughput improvement over the old serial loop.
        # ----------------------------------------------------------------
        completed_count = 0

        def _process_item(item):
            i, row, path = item
            if self._cancelled:
                return i, path
            self._process_one(path, row, market)
            return i, path

        with ThreadPoolExecutor(max_workers=self.batch_size) as pool:
            futures = {pool.submit(_process_item, item): item for item in work_items}
            for future in as_completed(futures):
                completed_count += 1
                i, path = future.result()
                self._emit_progress(completed_count, total, path)
                if self._cancelled:
                    # Cancel pending futures; already-running ones finish safely.
                    for f in futures:
                        f.cancel()
                    break

        self.summary.cancelled = self._cancelled
        self.summary.finished_at = time.time()
        self.history.log_action(
            action="batch_complete",
            status="cancelled" if self._cancelled else "success",
            details=(
                f"total={self.summary.total} success={self.summary.success} "
                f"failed={self.summary.failed} skipped={self.summary.skipped} "
                f"duplicates={self.summary.duplicates} "
                f"elapsed={self.summary.elapsed_seconds:.1f}s"
            ),
            processing_stage="batch_complete",
        )
        self.finished.emit(self.summary)

    # ------------------------------------------------------------------
    # Per-image pipeline (also reused by regenerate-single-image)
    # ------------------------------------------------------------------

    def _process_one(self, path: str, row: int, market) -> None:
        image_name = Path(path).name
        # Shorthand so every summary mutation is one line with the lock.
        _lock = getattr(self, "_summary_lock", None)

        def _inc(attr, amount=1):
            if _lock:
                with _lock:
                    setattr(self.summary, attr, getattr(self.summary, attr) + amount)
            else:
                setattr(self.summary, attr, getattr(self.summary, attr) + amount)

        def _append(attr, value):
            if _lock:
                with _lock:
                    getattr(self.summary, attr).append(value)
            else:
                getattr(self.summary, attr).append(value)

        # --- Item #4: validate before calling the AI at all ---
        self.status_update.emit(row, "Validating…")
        is_valid, reason = self.meta.validate_image(path)
        if not is_valid:
            _inc("skipped")
            _append("skipped_files", (path, reason))
            self.status_update.emit(row, "Skipped (invalid)")
            self.history.log_action(
                action="validate_image", status="skipped",
                image_name=image_name, processing_stage="validation",
                error_reason=reason,
            )
            self.error.emit(f"Skipped (invalid image):\n{path}\n\n{reason}")
            return

        self.status_update.emit(row, "Generating…")
        try:
            with open(path, "rb") as fh:
                image_bytes = fh.read()
        except OSError as exc:
            _inc("failed")
            _append("failed_files", (path, str(exc)))
            self.status_update.emit(row, "Error")
            self.history.log_action(
                action="read_file", status="error",
                image_name=image_name, processing_stage="file_read",
                error_reason=str(exc),
            )
            self.error.emit(f"Could not read file:\n{path}\n\n{exc}")
            return

        # --- Speed optimisation: downscale large images before upload ---
        # AI vision models need enough detail to understand the image but
        # don't benefit from multi-megapixel resolution. Resizing to
        # ≤1024×1024 (≈1 MP) before base64-encoding reduces upload payload
        # by 90 %+ for large TIFFs/PNGs, which is usually the biggest
        # single source of per-image latency.
        image_bytes = _resize_for_api(image_bytes, max_px=1536)

        # --- AI generation: with auto-provider fallback or fixed provider ---
        # Bug fix: OpenAI's API hard-rejects any request using
        # response_format/text.format type "json_object" unless the
        # word "json" literally appears in the user-role input message
        # (not just the system/instructions message) — see
        # https://platform.openai.com/docs/guides/text-generation#json-mode
        # The previous fallback prompt ("Analyze this stock image and
        # generate commercial metadata.") never said "json" anywhere, so
        # OpenAI's Responses API returned a 400 on every single call:
        # "Response input messages must contain the word 'json'...".
        # The system prompt (_build_system_prompt) already says "JSON"
        # many times, but OpenAI's Responses API only scans the `input`
        # array for this check, not `instructions` — so the requirement
        # has to be satisfied here too, in the user-role text itself.
        # This also defensively covers OpenRouter/Groq's Chat Completions
        # -style json_object mode, which has the same literal-word rule.
        if self.auto_provider:
            result = self.ai.generate_metadata_with_fallback(
                preferred_provider=self.provider,
                image_bytes=image_bytes,
                text_fallback_prompt=(
                    "Analyze this stock image and generate commercial "
                    "metadata. Respond with the result as JSON."
                ),
                market_rules=market,
            )
        else:
            result = self.ai.generate_metadata(
                provider=self.provider,
                image_bytes=image_bytes,
                text_fallback_prompt=(
                    "Analyze this stock image and generate commercial "
                    "metadata. Respond with the result as JSON."
                ),
                market_rules=market,
            )

        # Strip internal bookkeeping key before passing result around
        provider_used = result.pop("_provider_used", self.provider) or self.provider

        if result.get("error"):
            reason = result.get("description", "AI generation failed for an unspecified reason.")
            _inc("failed")
            _append("failed_files", (path, reason))
            self.status_update.emit(row, "Error")
            self.history.log_action(
                action="generate_metadata", status="error",
                image_name=image_name, processing_stage="ai_generation",
                ai_provider=provider_used, error_reason=reason,
            )
            self.error.emit(f"AI generation failed for:\n{path}\n\n{reason}")
            return

        # --- Item #19: optional reusable template (prefix/suffix + fixed keywords) ---
        if self.active_template:
            result = apply_template(
                result["title"], result["description"], result["keywords"],
                self.active_template,
            )

        # --- Item #21: marketplace trimming ---
        modified_fields: list = []
        if self.marketplace_validation_enabled and market:
            result = apply_rules(
                title=result["title"],
                description=result["description"],
                keywords=result["keywords"],
                rules=market,
                custom_keywords=self.custom_keywords,
            )
            modified_fields = result.get("modified_fields", [])
        else:
            # SEO fix: custom keywords are appended AFTER the AI's
            # commercially-ranked keywords, not prepended — prepending
            # would bump the AI's #1 (primary-subject) keyword out of
            # pole position every time custom keywords are enabled.
            merged = list(dict.fromkeys(result["keywords"] + self.custom_keywords))
            result["keywords"] = merged

        # --- Bug fix: hard-enforce the user's configured title/keyword
        # range unconditionally, regardless of whether "Marketplace Rule
        # Validation" (apply_rules, above) is enabled. The system prompt
        # already asks the AI for this range, but a prompt is a request
        # the model can still drift from — this is the actual guarantee.
        # `market` here already has the Settings overrides applied (see
        # Worker._apply_rule_overrides), so these are the true effective
        # limits, not just the selected platform's raw defaults. ---
        range_notes: list = []
        if market:
            result["title"], result["keywords"], range_notes = enforce_range_limits(
                result["title"], result["keywords"],
                title_min=market.title_min, title_max=market.title_max,
                keyword_min=market.keyword_min, keyword_max=market.keyword_max,
            )

        # --- Item #17: quality checks ---
        quality_warnings = check_metadata_quality(
            result["title"], result["description"], result["keywords"],
        )
        quality_warnings = range_notes + quality_warnings
        if quality_warnings:
            self.history.log_action(
                action="quality_check", status="warning",
                image_name=image_name, processing_stage="pre_write_validation",
                ai_provider=provider_used,
                error_reason="; ".join(quality_warnings),
            )

        record = {
            "filename":         path,
            "title":            result["title"],
            "description":      result["description"],
            "keywords":         result["keywords"],
            "modified_fields":  modified_fields,
            "quality_warnings": quality_warnings,
        }
        self._results.append(record)
        self.result_ready.emit(row, record)

        # --- Auto-embed: write to file only if enabled ---
        if not self.auto_embed:
            _inc("success")
            if self.auto_provider and provider_used != self.provider:
                gen_label = f"Generated (via {provider_used.title()})"
            else:
                gen_label = "Generated"
            self.status_update.emit(row, gen_label)
            self.history.log_action(
                action="generate_metadata", status="success",
                image_name=image_name, processing_stage="ai_generation",
                ai_provider=provider_used,
                details="auto_embed=off; metadata not written to file",
            )
            return

        # --- Item #6: write + read-back verification + auto-rollback ---
        self.status_update.emit(row, "Writing…")
        write_result = self.meta.write_metadata(
            path, result["title"], result["description"], result["keywords"],
        )

        if write_result:
            _inc("success")
            # Show which provider was actually used when fallback kicked in
            if self.auto_provider and provider_used != self.provider:
                done_label = f"Done (via {provider_used.title()})"
            else:
                done_label = "Done"
            self.status_update.emit(row, done_label)
            self.history.log_action(
                action="write_metadata", status="success",
                image_name=image_name, processing_stage="write_verify",
                ai_provider=provider_used,
            )
        else:
            _inc("failed")
            _append("failed_files", (path, write_result.reason))
            status_label = "Write Failed (rolled back)" if write_result.rolled_back else "Write Failed"
            self.status_update.emit(row, status_label)
            self.history.log_action(
                action="write_metadata", status="error",
                image_name=image_name, processing_stage="write_verify",
                ai_provider=provider_used, error_reason=write_result.reason,
            )


class Controller:
    def __init__(self):
        self.config   = ConfigManager()
        self.history  = HistoryManager()
        self.ai       = AIService(self.config)
        self.meta     = MetadataEngine(self.config)
        self.exporter = MetadataExporter()
        self.window   = MetaEmbedMainWindow(self.config)

        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker]  = None
        # NOTE: kept for backward compatibility / potential future use, but
        # no longer read by _write_all or _export_csv (see the bug-fix
        # notes on those methods and on MetaEmbedMainWindow.get_all_results)
        # — it only ever reflects the MOST RECENT worker run, not the
        # cumulative set of generated results, so it's unsuitable as a
        # source of truth for "save/export everything generated so far."
        self._batch_results: list       = []

        # Signal wiring
        w = self.window
        w.request_processing.connect(self.start_batch_thread)
        w.save_config_requested.connect(self._save_config)
        w.cancel_requested.connect(self._cancel_batch)
        w.write_single_requested.connect(self._write_single)
        w.save_all_requested.connect(self._write_all)
        w.export_requested.connect(self._export_csv)
        w.regenerate_single_requested.connect(self._regenerate_single)   # Item #12
        w.clear_history_requested.connect(self._clear_history)           # Item #7
        w.refresh_history_requested.connect(self._push_history)          # Item #7

        self.window.load_config(self.config)

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def start_batch_thread(self, files: list, batch_size: int):
        if not files:
            self.window.show_warning("No Files", "Add images to the queue first.")
            return
        if self._is_thread_running():
            self.window.show_warning("Busy", "A batch job is already running.")
            return

        provider   = self.window.get_selected_provider()
        model      = self.window.get_selected_model()
        market_key = self.window.get_selected_market()
        api_key    = self.config.get("api_keys", provider)

        if not api_key:
            self.window.show_warning(
                "Missing API Key",
                f"No API key configured for '{provider}'.\n"
                "Go to AI Studio and enter your key, then click Save Configuration.",
            )
            return

        custom_keywords = []
        if self.config.get("metadata_rules", "custom_keywords_enabled"):
            custom_keywords = self.config.get_custom_keywords()

        marketplace_validation_enabled = self.config.is_marketplace_validation_enabled()
        active_template = self.config.get_active_template()
        auto_embed      = self.config.is_auto_embed_enabled()
        auto_provider   = self.config.is_auto_provider_enabled()
        # Bug fix: forward the Settings page's title/keyword range to the
        # worker so the AI is actually constrained to it (see
        # Worker._apply_rule_overrides for the full explanation).
        metadata_rule_overrides = self.config.get_metadata_rules()

        # Build a (path -> row_index) map from the actual table so the
        # worker emits status_update on the correct rows even when we are
        # running only a subset of the queue (pending-only or failed-only).
        row_map: dict[str, int] = {}
        for path in files:
            row = self.window.get_row_for_path(path)
            if row is not None:
                row_map[path] = row

        # Clear previous results so Save All only applies to this run.
        self._batch_results.clear()

        self._run_worker(
            files, provider, model, market_key, custom_keywords,
            marketplace_validation_enabled, batch_size, row_offset=0,
            active_template=active_template,
            auto_embed=auto_embed,
            auto_provider=auto_provider,
            metadata_rule_overrides=metadata_rule_overrides,
            row_map=row_map,
        )
        logger.info("Started batch: %d files, batch_size=%d, provider=%s, model=%s",
                    len(files), batch_size, provider, model)

    def _run_worker(self, files, provider, model, market_key, custom_keywords,
                     marketplace_validation_enabled, batch_size, row_offset,
                     active_template=None, auto_embed=True, auto_provider=True,
                     metadata_rule_overrides=None, row_map=None):
        self._thread = QThread()
        self._worker = Worker(
            files, self.ai, self.meta, self.history,
            provider, model, market_key, custom_keywords,
            marketplace_validation_enabled, batch_size, row_offset,
            active_template=active_template,
            auto_embed=auto_embed,
            auto_provider=auto_provider,
            metadata_rule_overrides=metadata_rule_overrides,
            row_map=row_map,
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        # Use QueuedConnection for cross-thread signals so Qt dispatches them
        # on the main thread's event loop — this keeps dialogs responsive and
        # lets the cancel button process clicks while the worker is running.
        self._worker.progress.connect(
            self.window.update_progress, Qt.QueuedConnection)
        self._worker.status_update.connect(
            self.window.update_row_status, Qt.QueuedConnection)
        self._worker.result_ready.connect(
            self.window.on_result_ready, Qt.QueuedConnection)
        self._worker.error.connect(
            self.window.show_error, Qt.QueuedConnection)
        self._worker.finished.connect(
            self._on_batch_finished, Qt.QueuedConnection)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        # Null out the Python references AFTER Qt has destroyed the C++
        # objects, so subsequent isRunning() calls never hit a deleted
        # C++ object and raise RuntimeError.
        self._thread.finished.connect(self._clear_thread_refs)

        self.window.set_processing_state(True)
        self._thread.start()

    def _clear_thread_refs(self):
        """Null out stale Qt object references after the thread finishes.
        deleteLater() destroys the C++ object but leaves the Python wrapper
        alive; calling any method on it (e.g. isRunning()) raises
        RuntimeError.  Zeroing the refs here prevents that entirely."""
        self._thread = None
        self._worker = None

    def _is_thread_running(self) -> bool:
        """Safe replacement for self._thread.isRunning() that never raises
        RuntimeError on a deleted C++ QThread object."""
        if self._thread is None:
            return False
        try:
            return self._thread.isRunning()
        except RuntimeError:
            self._thread = None
            self._worker = None
            return False

    def _cancel_batch(self):
        if self._worker:
            try:
                self._worker.cancel()
            except RuntimeError:
                self._worker = None

    def _on_batch_finished(self, summary: BatchSummary):
        # Collect results before nulling refs (deleteLater fires asynchronously
        # so the worker Python object is still alive at this point).
        if self._worker:
            try:
                self._batch_results.extend(self._worker.get_results())
            except RuntimeError:
                pass
        self.window.set_processing_state(False)
        self._push_history()
        logger.info(
            "Batch finished. total=%d success=%d failed=%d skipped=%d "
            "duplicates=%d cancelled=%s elapsed=%.1fs",
            summary.total, summary.success, summary.failed, summary.skipped,
            summary.duplicates, summary.cancelled, summary.elapsed_seconds,
        )
        # Defer the summary dialog by one event-loop tick so the thread
        # cleanup (deleteLater) has time to complete before Qt processes
        # the modal dialog — this is what makes the dialog fully responsive.
        QTimer.singleShot(0, lambda: self.window.show_batch_summary(summary))

    # ------------------------------------------------------------------
    # Item #12 — regenerate metadata for a single selected image
    # ------------------------------------------------------------------

    def _regenerate_single(self, path: str):
        if self._is_thread_running():
            self.window.show_warning("Busy", "A batch job is already running.")
            return
        if not path:
            return

        provider   = self.window.get_selected_provider()
        model      = self.window.get_selected_model()
        market_key = self.window.get_selected_market()
        api_key    = self.config.get("api_keys", provider)

        if not api_key:
            self.window.show_warning(
                "Missing API Key",
                f"No API key configured for '{provider}'.\n"
                "Go to AI Studio and enter your key, then click Save Configuration.",
            )
            return

        custom_keywords = []
        if self.config.get("metadata_rules", "custom_keywords_enabled"):
            custom_keywords = self.config.get_custom_keywords()
        marketplace_validation_enabled = self.config.is_marketplace_validation_enabled()
        active_template = self.config.get_active_template()
        auto_embed      = self.config.is_auto_embed_enabled()
        auto_provider   = self.config.is_auto_provider_enabled()
        # Bug fix: same override as the full-batch path — regenerating a
        # single image must respect the configured range too, otherwise
        # a regenerated image could silently fall back to the market
        # default and disagree with the rest of the batch.
        metadata_rule_overrides = self.config.get_metadata_rules()

        row = self.window.get_row_for_path(path)
        if row is None:
            self.window.show_warning("Not Found", "That file is no longer in the queue.")
            return

        self._run_worker(
            [path], provider, model, market_key, custom_keywords,
            marketplace_validation_enabled, batch_size=1, row_offset=row,
            active_template=active_template,
            auto_embed=auto_embed,
            auto_provider=auto_provider,
            metadata_rule_overrides=metadata_rule_overrides,
            row_map={os.path.normpath(path): row},
        )

    # ------------------------------------------------------------------
    # Config save
    # ------------------------------------------------------------------

    def _save_config(self, data: dict):
        # API keys
        for provider, key in data.get("api_keys", {}).items():
            self.config.set("api_keys", provider, key)

        # Active provider + model
        if "active_provider" in data:
            self.config.set("ui", "active_provider", data["active_provider"])
        if "active_model" in data:
            provider_key = data.get("active_provider", self.config.get_active_provider())
            self.config.set("default_models", provider_key, data["active_model"])

        # Batch size
        if "batch_size" in data:
            self.config.set("system", "batch_size", data["batch_size"])

        # Metadata rules (includes marketplace_validation_enabled — item #21)
        rules = data.get("metadata_rules", {})
        if rules:
            self.config.set_metadata_rules(rules)
        # Active market
        market = data.get("active_market")
        if market:
            self.config.set_active_market(market)

        # Templates (item #19)
        if "templates" in data:
            self.config.set_templates(data["templates"])
        if "active_template" in data:
            self.config.set_active_template_name(data["active_template"])

        self.window.show_info("Saved", "Configuration saved successfully.")

    # ------------------------------------------------------------------
    # Manual single-file write
    # ------------------------------------------------------------------

    def _write_single(self, path: str, title: str, description: str, keywords: list):
        result = self.meta.write_metadata(path, title, description, keywords)
        if result:
            self.window.show_info("Success", "Metadata written to file.")
            self.history.log_action(
                action="manual_write", status="success",
                image_name=Path(path).name, processing_stage="write_verify",
            )
        else:
            label = "rolled back to the original file" if result.rolled_back else "no changes made"
            self.window.show_error(
                f"Failed to write metadata to:\n{path}\n\n{result.reason}\n({label})"
            )
            self.history.log_action(
                action="manual_write", status="error",
                image_name=Path(path).name, processing_stage="write_verify",
                error_reason=result.reason,
            )

    # ------------------------------------------------------------------
    # Save all results to their files at once
    # ------------------------------------------------------------------

    def _write_all(self):
        """
        Write every generated result back to its source image file.

        Bug fix: reads from self.window.get_all_results() — the UI's
        cumulative, row-keyed results — instead of self._batch_results,
        which only ever holds the most recent worker run's output and
        gets cleared at the start of every new run (see
        MetaEmbedMainWindow.get_all_results for the full explanation).
        """
        all_results = self.window.get_all_results()
        if not all_results:
            self.window.show_warning(
                "Nothing to Save",
                "Generate metadata first before saving all files.",
            )
            return

        success_count = 0
        fail_paths = []
        for record in all_results:
            path = record.get("filename", "")
            if not path:
                continue
            result = self.meta.write_metadata(
                path, record["title"], record["description"], record["keywords"],
            )
            if result:
                success_count += 1
                self.history.log_action(
                    action="write_metadata", status="success",
                    image_name=Path(path).name, processing_stage="write_verify",
                )
            else:
                fail_paths.append((path, result.reason))
                self.history.log_action(
                    action="write_metadata", status="error",
                    image_name=Path(path).name, processing_stage="write_verify",
                    error_reason=result.reason,
                )

        if fail_paths:
            failed_lines = "\n".join(f"{Path(p).name} — {r}" for p, r in fail_paths[:5])
            extra = f"\n…and {len(fail_paths) - 5} more" if len(fail_paths) > 5 else ""
            self.window.show_error(
                f"Saved {success_count} files. Failed to write {len(fail_paths)}:\n"
                f"{failed_lines}{extra}"
            )
        else:
            self.window.show_info(
                "All Saved",
                f"Metadata written to all {success_count} file(s) successfully.",
            )
        logger.info("Save-all: %d success, %d failed.", success_count, len(fail_paths))

    # ------------------------------------------------------------------
    # CSV Export
    # ------------------------------------------------------------------

    def _export_csv(self, market_key: str):
        """
        Bug fix: same root cause as _write_all above — reads from the
        UI's cumulative self.window.get_all_results() instead of the
        controller's last-run-only self._batch_results.
        """
        all_results = self.window.get_all_results()
        if not all_results:
            self.window.show_warning(
                "Nothing to Export",
                "Run a batch job first to generate metadata, then export.",
            )
            return

        market = get_market(market_key)
        if not market:
            self.window.show_warning("Unknown Market", f"No rules for market: {market_key}")
            return

        start_dir = self.config.get_last_export_folder() or ""
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            f"Export for {market.name}",
            os.path.join(start_dir, f"metadata_{market.key}.csv") if start_dir else f"metadata_{market.key}.csv",
            "CSV Files (*.csv)",
        )
        if not path:
            return

        # Item #16 — remember the export folder for next time.
        self.config.set_last_export_folder(str(Path(path).parent))

        ok = self.exporter.export_csv(all_results, market, path)
        if ok:
            self.window.show_info(
                "Export Complete",
                f"Exported {len(all_results)} records to:\n{path}",
            )
        else:
            self.window.show_error("Export failed. Check logs for details.")

    # ------------------------------------------------------------------
    # Item #7 — History page: fetch and push data to the UI
    # ------------------------------------------------------------------

    def _push_history(self):
        """Fetch recent history from HistoryManager and update the history page."""
        entries = self.history.get_recent_history(limit=200)
        stats   = self.history.get_stats()
        self.window.refresh_history(entries, stats)

    def _clear_history(self):
        """Clear all history and immediately refresh the history page."""
        self.history.clear_history()
        self._push_history()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    # Remove the default 256 MB allocation limit so large images load without
    # triggering: "QImageIOHandler: Rejecting image as it exceeds the current
    # allocation limit of 256 megabytes". A value of 0 means no limit.
    QImageReader.setAllocationLimit(0)
    ctrl = Controller()
    ctrl.window.show()
    sys.exit(app.exec())

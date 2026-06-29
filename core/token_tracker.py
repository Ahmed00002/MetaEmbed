"""
token_tracker.py
~~~~~~~~~~~~~~~~
Persistent token / call tracking for the AI Dashboard page.

All usage is stored in  data/token_stats.json  so the dashboard survives
app restarts. The tracker is intentionally simple: it never calls the AI —
it only records what main.py / ai_service.py tells it happened.

Public API
----------
TokenTracker(data_dir)          – instantiate (cheap)
.record(provider, model,
        input_tokens, output_tokens,
        image_bytes_sent, success)  – call after every AI request
.get_stats()                    – returns full stats dict for the dashboard
.reset()                        – wipe all stored stats
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

_STATS_FILE_NAME = "token_stats.json"

# Approximate cost tables (USD per 1 000 tokens, as of mid-2025).
# These are best-effort estimates used only for the "estimated cost" widget.
# Users can see exact costs on their provider dashboards.
_COST_PER_1K: Dict[str, Dict[str, float]] = {
    # provider -> {input, output}
    "google":     {"input": 0.00015,  "output": 0.0006},    # Gemini 2.5 Flash
    "openai":     {"input": 0.00015,  "output": 0.0006},    # gpt-5.4-mini
    "openrouter": {"input": 0.0002,   "output": 0.0008},    # conservative average
    "groq":       {"input": 0.00011,  "output": 0.00011},   # Llama 4 Scout
    "mistral":    {"input": 0.00015,  "output": 0.00046},   # Pixtral 12B free tier
}
_DEFAULT_COST = {"input": 0.0002, "output": 0.0008}

# Rough token-per-byte estimate for vision payloads.
# 1 image token ≈ 0.75 bytes of base64 decoded  → 1 token per ~0.75 bytes
# In practice providers charge image tokens differently; we use a flat rate.
_IMAGE_TOKENS_PER_KB = 0.85   # ~870 tokens per MB — conservative


def _cost(provider: str, input_tok: int, output_tok: int) -> float:
    rates = _COST_PER_1K.get(provider, _DEFAULT_COST)
    return (input_tok / 1000) * rates["input"] + (output_tok / 1000) * rates["output"]


class TokenTracker:
    def __init__(self, data_dir: Path):
        self._file = Path(data_dir) / _STATS_FILE_NAME
        self._stats = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                # Migrate older files that may be missing new keys
                return self._migrate(data)
            except Exception as exc:
                logger.warning("Could not load token_stats.json: %s — starting fresh.", exc)
        return self._blank_stats()

    def _save(self):
        try:
            with open(self._file, "w", encoding="utf-8") as fh:
                json.dump(self._stats, fh, indent=2)
        except OSError as exc:
            logger.error("Cannot save token_stats.json: %s", exc)

    @staticmethod
    def _blank_stats() -> Dict[str, Any]:
        return {
            "version": 2,
            "first_call_ts": None,
            "last_call_ts": None,
            "totals": {
                "calls": 0,
                "success": 0,
                "errors": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "image_bytes": 0,
                "estimated_cost_usd": 0.0,
            },
            # per-provider breakdown
            "providers": {},
            # per-model breakdown (flat key = "provider/model")
            "models": {},
            # last 200 call records for the timeline chart
            "recent_calls": [],
        }

    @staticmethod
    def _blank_provider() -> Dict[str, Any]:
        return {
            "calls": 0, "success": 0, "errors": 0,
            "input_tokens": 0, "output_tokens": 0,
            "image_bytes": 0, "estimated_cost_usd": 0.0,
        }

    def _migrate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        blank = self._blank_stats()
        # Add any keys present in blank but absent from saved file
        for k, v in blank.items():
            if k not in data:
                data[k] = v
        if "totals" in data:
            for k, v in blank["totals"].items():
                if k not in data["totals"]:
                    data["totals"][k] = v
        return data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        image_bytes_sent: int = 0,
        success: bool = True,
    ):
        """Record one AI call. Call this after every generate_metadata()."""
        now_ts = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        if self._stats["first_call_ts"] is None:
            self._stats["first_call_ts"] = now_iso
        self._stats["last_call_ts"] = now_iso

        est_cost = _cost(provider, input_tokens, output_tokens)

        # Totals
        t = self._stats["totals"]
        t["calls"] += 1
        t["success"] += int(success)
        t["errors"] += int(not success)
        t["input_tokens"] += input_tokens
        t["output_tokens"] += output_tokens
        t["image_bytes"] += image_bytes_sent
        t["estimated_cost_usd"] = round(t["estimated_cost_usd"] + est_cost, 6)

        # Per-provider
        if provider not in self._stats["providers"]:
            self._stats["providers"][provider] = self._blank_provider()
        p = self._stats["providers"][provider]
        p["calls"] += 1
        p["success"] += int(success)
        p["errors"] += int(not success)
        p["input_tokens"] += input_tokens
        p["output_tokens"] += output_tokens
        p["image_bytes"] += image_bytes_sent
        p["estimated_cost_usd"] = round(p["estimated_cost_usd"] + est_cost, 6)

        # Per-model
        model_key = f"{provider}/{model}"
        if model_key not in self._stats["models"]:
            self._stats["models"][model_key] = self._blank_provider()
        m = self._stats["models"][model_key]
        m["calls"] += 1
        m["success"] += int(success)
        m["errors"] += int(not success)
        m["input_tokens"] += input_tokens
        m["output_tokens"] += output_tokens
        m["image_bytes"] += image_bytes_sent
        m["estimated_cost_usd"] = round(m["estimated_cost_usd"] + est_cost, 6)

        # Timeline (cap at 500)
        self._stats["recent_calls"].append({
            "ts": now_iso,
            "provider": provider,
            "model": model,
            "input": input_tokens,
            "output": output_tokens,
            "cost": round(est_cost, 6),
            "ok": success,
        })
        if len(self._stats["recent_calls"]) > 500:
            self._stats["recent_calls"] = self._stats["recent_calls"][-500:]

        self._save()

    def get_stats(self) -> Dict[str, Any]:
        """Return a deep copy of the full stats dict."""
        import copy
        return copy.deepcopy(self._stats)

    def reset(self):
        """Wipe all stored stats."""
        self._stats = self._blank_stats()
        self._save()

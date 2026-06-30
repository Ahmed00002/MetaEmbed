import json
import logging
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_keys": {
        "openai":     "",
        "google":     "",
        "openrouter": "",
        "groq":       "",
        "mistral":    "",
    },
    "default_models": {
        "openai":     "gpt-5.4-mini",
        "google":     "gemini-2.5-flash",
        "openrouter": "google/gemini-2.5-flash",
        "groq":       "meta-llama/llama-4-scout-17b-16e-instruct",
        "mistral":    "pixtral-12b-2409",
    },
    "ui": {
        "theme": "dark",
        "font_size": 12,
        "active_provider": "openai",
        "active_market": "adobe",
        # Item #16: remembered across sessions
        "last_opened_folder": "",
        "last_export_folder": "",
    },
    "system": {
        "debug_mode": False,
        "max_history_entries": 1000,
        "request_timeout_seconds": 60,
        "batch_size": 3,
        "batch_delay_seconds": 0,   # seconds to sleep between each batch of images (0 = off)
    },
    "image_engine": {
        "backup_before_write": True,
        "thumbnail_max_size": 256,
        "supported_extensions": [".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"],
    },
    # New: per-market metadata generation rules
    "metadata_rules": {
        "title_min_length": 5,
        "title_max_length": 70,
        "keyword_min_count": 7,
        "keyword_max_count": 49,
        "custom_keywords": [],          # Always prepended to AI keywords
        "custom_keywords_enabled": True,
        # Item #21: marketplace-specific trimming is OPT-IN. When False
        # (the default), AI-generated metadata is written exactly as
        # generated — no character/keyword-count trimming is applied.
        "marketplace_validation_enabled": False,
        # Auto-embed: when True, metadata is written to the file immediately
        # after AI generation. When False, results are shown in the inspector
        # only and the user must click "Write to File" or "Save All" manually.
        "auto_embed": True,
        # Auto-provider: when True, the app picks the best available provider
        # automatically and falls back to others on failure.
        "auto_provider": True,
        # Drag-to-reorder fallback priority (used when auto_provider=True).
        # Lists provider keys in the order they should be tried.
        "fallback_provider_order": ["google", "openai", "openrouter", "groq", "mistral"],
    },
    # Item #19: reusable metadata templates (optional; empty by default).
    # Each template: {name, title_prefix, title_suffix, description_prefix,
    #                 description_suffix, fixed_keywords: [...]}
    "metadata_templates": {
        "templates": [],
        "active_template": "",   # "" = no template applied
    },
}

VALID_PROVIDERS = list(DEFAULT_CONFIG["api_keys"].keys())


class ConfigManager:
    def __init__(self):
        self._ensure_data_dir()
        self._config = self._load_config()

    def _ensure_data_dir(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> Dict[str, Any]:
        if not CONFIG_FILE.exists():
            return self._create_default_config()
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            merged = self._merge_configs(DEFAULT_CONFIG, data)
            return self._migrate_legacy_providers(merged)
        except json.JSONDecodeError:
            logger.warning("config.json is malformed — resetting to defaults.")
            return self._create_default_config()
        except OSError as exc:
            logger.error("Cannot read config.json: %s", exc)
            return copy.deepcopy(DEFAULT_CONFIG)

    def _migrate_legacy_providers(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Drop config entries for providers that no longer exist (currently:
        'deepseek', removed because its public API has no image-input
        support). Falls back the active provider to 'openai' if it was
        pointed at a removed provider, so old configs never silently try
        to use a dead provider on launch.
        """
        removed_providers = {"deepseek"}
        for section in ("api_keys", "default_models"):
            for provider in list(config.get(section, {}).keys()):
                if provider in removed_providers:
                    del config[section][provider]

        if config.get("ui", {}).get("active_provider") in removed_providers:
            logger.warning(
                "Saved active_provider was a removed provider; falling back to 'openai'."
            )
            config["ui"]["active_provider"] = "openai"

        return config

    def _create_default_config(self) -> Dict[str, Any]:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, indent=4)
        except OSError as exc:
            logger.error("Cannot write default config: %s", exc)
        return copy.deepcopy(DEFAULT_CONFIG)

    def _merge_configs(self, default: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
        merged = copy.deepcopy(default)
        for key, value in user.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                merged[key] = self._merge_configs(merged[key], value)
            else:
                merged[key] = value
        return merged

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, section: str, key: Optional[str] = None) -> Any:
        if key is None:
            return self._config.get(section)
        return self._config.get(section, {}).get(key)

    def set(self, section: str, key: str, value: Any) -> None:
        if section not in self._config:
            self._config[section] = {}
        self._config[section][key] = value
        self.save()

    def save(self) -> bool:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._config, fh, indent=4)
            return True
        except OSError as exc:
            logger.error("Failed to save config: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def get_supported_extensions(self) -> list:
        return self.get("image_engine", "supported_extensions") or \
               DEFAULT_CONFIG["image_engine"]["supported_extensions"]

    def get_active_provider(self) -> str:
        return self.get("ui", "active_provider") or "openai"

    def set_active_provider(self, provider: str) -> None:
        if provider in VALID_PROVIDERS:
            self.set("ui", "active_provider", provider)

    def get_active_market(self) -> str:
        return self.get("ui", "active_market") or "adobe"

    def set_active_market(self, market_key: str) -> None:
        self.set("ui", "active_market", market_key)

    def has_api_key(self, provider: str) -> bool:
        return bool(self.get("api_keys", provider))

    def get_all_api_keys(self) -> Dict[str, str]:
        return dict(self._config.get("api_keys", {}))

    # ------------------------------------------------------------------
    # Metadata rules helpers
    # ------------------------------------------------------------------

    def get_custom_keywords(self) -> List[str]:
        raw = self.get("metadata_rules", "custom_keywords") or []
        return [k.strip() for k in raw if k.strip()]

    def set_custom_keywords(self, keywords: List[str]) -> None:
        self.set("metadata_rules", "custom_keywords", keywords)

    def get_metadata_rules(self) -> Dict[str, Any]:
        return dict(self._config.get("metadata_rules", DEFAULT_CONFIG["metadata_rules"]))

    def set_metadata_rules(self, rules: Dict[str, Any]) -> None:
        """Bulk-update metadata_rules section and persist."""
        if "metadata_rules" not in self._config:
            self._config["metadata_rules"] = {}
        self._config["metadata_rules"].update(rules)
        self.save()

    def is_marketplace_validation_enabled(self) -> bool:
        """Item #21 — off by default; metadata is written exactly as the AI generated it."""
        return bool(self.get("metadata_rules", "marketplace_validation_enabled"))

    def is_auto_embed_enabled(self) -> bool:
        """When True (default), metadata is written to file immediately after generation."""
        val = self.get("metadata_rules", "auto_embed")
        return val if val is not None else True

    def is_auto_provider_enabled(self) -> bool:
        """When True (default), app picks provider automatically and falls back on failure."""
        val = self.get("metadata_rules", "auto_provider")
        return val if val is not None else True

    def get_fallback_provider_order(self) -> list:
        """Return user-defined fallback provider order list."""
        order = self.get("metadata_rules", "fallback_provider_order")
        default = ["google", "openai", "openrouter", "groq", "mistral"]
        if not order or not isinstance(order, list):
            return default
        return order

    def set_fallback_provider_order(self, order: list) -> None:
        self.set("metadata_rules", "fallback_provider_order", order)

    # ------------------------------------------------------------------
    # Item #16 — remembered user preferences
    # ------------------------------------------------------------------

    def get_last_opened_folder(self) -> str:
        return self.get("ui", "last_opened_folder") or ""

    def set_last_opened_folder(self, folder: str) -> None:
        self.set("ui", "last_opened_folder", folder)

    def get_last_export_folder(self) -> str:
        return self.get("ui", "last_export_folder") or ""

    def set_last_export_folder(self, folder: str) -> None:
        self.set("ui", "last_export_folder", folder)

    # ------------------------------------------------------------------
    # Item #19 — metadata templates
    # ------------------------------------------------------------------

    def get_templates(self) -> List[Dict[str, Any]]:
        return list(self.get("metadata_templates", "templates") or [])

    def set_templates(self, templates: List[Dict[str, Any]]) -> None:
        self.set("metadata_templates", "templates", templates)

    def get_active_template_name(self) -> str:
        return self.get("metadata_templates", "active_template") or ""

    def set_active_template_name(self, name: str) -> None:
        self.set("metadata_templates", "active_template", name)

    def get_active_template(self) -> Optional[Dict[str, Any]]:
        name = self.get_active_template_name()
        if not name:
            return None
        for t in self.get_templates():
            if t.get("name") == name:
                return t
        return None
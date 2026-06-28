"""
ai_service.py
~~~~~~~~~~~~~~
AI metadata generation backend.

Rewritten to call providers through their official Python SDKs instead of
hand-rolled `requests` calls:

  - OpenAI       -> `openai` SDK (Responses API), GPT-5.4-mini / GPT-5.4-nano
  - Google       -> `google-genai` SDK (the GA replacement for the
                    deprecated `google-generativeai` package)
  - OpenRouter   -> `openai` SDK pointed at OpenRouter's OpenAI-compatible
                    base_url (this is OpenRouter's own documented approach)
  - Groq         -> `openai` SDK pointed at Groq's OpenAI-compatible
                    base_url, using a vision-capable Llama 4 model so the
                    image is actually analyzed (the previous default,
                    llama-3.3-70b-versatile, is text-only and never saw
                    the image at all)

DeepSeek has been removed as a provider: its public API does not yet
expose the vision endpoint used by chat.deepseek.com, so metadata
generated "from DeepSeek" was never actually based on the image.

Every provider call still funnels through the same retry / JSON-repair /
sanitization / error-handling pipeline as before, so the rest of the app
(`main.py`, `ui_main.py`) does not need to change at all — the public
surface is still `AIService(config_manager).generate_metadata(...)`.
"""

import json
import re
import time
import logging
from typing import Dict, Any, Optional

from core.keyword_tools import clean_keywords

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional SDK imports.
# Each provider's SDK is imported lazily/defensively so that a missing
# package degrades to a clear, actionable error message instead of an
# ImportError crash at app startup.
# ---------------------------------------------------------------------------

try:
    from openai import OpenAI as _OpenAIClient
    import openai as _openai_errors  # same module, used for exception classes
    _HAVE_OPENAI_SDK = True
except ImportError:
    _HAVE_OPENAI_SDK = False

try:
    from google import genai as _genai
    from google.genai import types as _genai_types
    from google.genai import errors as _genai_errors
    _HAVE_GOOGLE_SDK = True
except ImportError:
    _HAVE_GOOGLE_SDK = False


SUPPORTED_IMAGE_TYPES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG":      "image/png",
    b"GIF8":         "image/gif",
    b"RIFF":         "image/webp",
}

# Providers retained after removing DeepSeek (no API-level vision support).
SUPPORTED_PROVIDERS = ("google", "openai", "openrouter", "groq")

# Vision-capable model used for Groq. Kept separate from whatever the user
# has saved as their "default model" string so that older configs saved
# before this change (e.g. llama-3.3-70b-versatile, which is text-only)
# don't silently keep producing image-blind metadata.
GROQ_VISION_FALLBACK_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


def _detect_mime(image_bytes: bytes) -> str:
    for magic, mime in SUPPORTED_IMAGE_TYPES.items():
        if image_bytes[:len(magic)] == magic:
            return mime
    return "image/jpeg"


def _build_system_prompt(market_name: str, title_min: int, title_max: int,
                          kw_min: int, kw_max: int, kw_max_len: int,
                          description_max: int) -> str:
    """
    Build a market-specific system prompt embedding all hard constraints
    PLUS an explicit SEO/ranking framework.

    Item #SEO-1: this prompt no longer just hands the model a character
    limit and the vague instruction "most relevant first" — it walks the
    model through the same five-tier keyword taxonomy buyers actually
    search with (subject -> action/context -> concept/emotion ->
    descriptive attribute -> technical/category), and requires the
    keywords array to be emitted strictly in that tier order. This is
    what makes keyword #1 the single highest-commercial-value term
    instead of whatever the model happened to notice first.
    """
    return (
        f"You are a senior micro-stock SEO strategist and metadata specialist "
        f"targeting {market_name}. Your job is not to describe the image — it is "
        f"to make this asset rank #1 in {market_name} buyer search results. "
        f"Think like a buyer typing a search query, not like a captioner.\n\n"

        f"=== STEP 1: ANALYZE LIKE A BUYER ===\n"
        f"Before writing anything, silently identify:\n"
        f"  (a) The single PRIMARY SUBJECT — the one noun phrase a buyer would "
        f"type first to find this exact image.\n"
        f"  (b) The ACTION or USE-CASE CONTEXT — what is happening, and what "
        f"commercial use-case this image would be licensed for (e.g. "
        f"\"healthcare,\" \"remote work,\" \"e-commerce,\" \"wellness campaign\").\n"
        f"  (c) The CONCEPT or EMOTION the image conveys (e.g. \"freedom,\" "
        f"\"teamwork,\" \"solitude,\" \"growth\") — buyers search concepts as "
        f"often as literal objects.\n"
        f"  (d) Visually distinctive ATTRIBUTES — dominant colors, composition, "
        f"lighting, season, or style, each as its OWN single keyword (never "
        f"combine two ideas into one phrase, e.g. write \"red\" and \"dress\" "
        f"separately, never \"red dress\").\n"
        f"  (e) TECHNICAL/CATEGORY terms a buyer filters by — orientation "
        f"(horizontal/vertical/square), \"copy space,\" \"isolated,\" "
        f"\"background,\" \"close-up,\" \"top view,\" etc., only if genuinely "
        f"present in the image.\n\n"

        f"=== STEP 2: KEYWORDS — STRICT COMMERCIAL-RANK ORDER ===\n"
        f"Output {kw_min}–{kw_max} keywords, each a single word or short "
        f"natural phrase, max {kw_max_len} characters, with ZERO duplicates "
        f"(including near-duplicates like \"coffee\"/\"coffee cup\"/\"cup of "
        f"coffee\" — pick the ONE strongest form and drop the rest). "
        f"Keywords MUST be ordered in exactly this tier sequence, most "
        f"commercially valuable first:\n"
        f"  1. Primary subject keyword(s) from Step 1a — these go FIRST, "
        f"always.\n"
        f"  2. Action / use-case / context keywords from Step 1b.\n"
        f"  3. Concept / emotion keywords from Step 1c.\n"
        f"  4. Descriptive attribute keywords from Step 1d (color, style, "
        f"composition, season, lighting).\n"
        f"  5. Technical / category / orientation keywords from Step 1e — "
        f"these go LAST, always.\n"
        f"Never let a generic technical term (e.g. \"background,\" "
        f"\"isolated,\" \"horizontal\") outrank a specific subject or concept "
        f"term. Never include the literal words \"stock,\" \"photo,\" "
        f"\"image,\" or \"picture\" as keywords — they have zero search "
        f"value and buyers never search them.\n\n"

        f"=== STEP 3: TITLE — WRITE FOR SEARCH, NOT FOR CAPTIONING ===\n"
        f"Length: {title_min}–{title_max} characters. Write ONE natural-"
        f"language sentence or sentence fragment — never a comma-separated "
        f"keyword list (a title like \"woman, laptop, office, coffee\" reads "
        f"as spam and is penalized by {market_name}'s search ranking). "
        f"Front-load your #1 ranked keyword from Step 2 within the first few "
        f"words of the title — that is the single highest-leverage SEO "
        f"placement you control. Weave in 1-2 secondary keywords naturally "
        f"if they fit without forcing it. Describe what is literally "
        f"visible — never claim a use-case, brand, or context that is not "
        f"actually depicted.\n\n"

        f"=== STEP 4: DESCRIPTION — REINFORCE, DON'T REPEAT ===\n"
        f"Length: max {description_max} characters, one clear sentence. "
        f"The description must NOT simply restate the title in different "
        f"words — it exists to capture buyer-intent and use-case keywords "
        f"that did not fit in the title (concept, context, or use-case "
        f"terms from Step 1b/1c). Treat it as a second, complementary SEO "
        f"surface, not a duplicate of the title.\n\n"

        f"Return ONLY a valid JSON object. No markdown, no explanation, no "
        f"extra text, no commentary about your reasoning process.\n"
        "JSON format:\n"
        "{\n"
        '  "title": "...",\n'
        '  "description": "...",\n'
        '  "keywords": ["primary_subject_kw1", "primary_subject_kw2", '
        '"action_or_context_kw", "concept_kw", "attribute_kw", '
        '"technical_kw", ...]\n'
        "}"
    )


def _repair_json(raw: str) -> str:
    """
    Best-effort repair of a truncated JSON string from the AI.
    Handles the most common truncation: unterminated string in keywords array
    or in a field value. Returns a repaired string or raises ValueError.
    """
    s = raw.strip()

    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass

    open_braces   = s.count("{") - s.count("}")
    open_brackets = s.count("[") - s.count("]")

    trimmed = s.rstrip()
    if trimmed.endswith(","):
        trimmed = trimmed[:-1].rstrip()

    closing = ""
    if open_brackets > 0:
        closing += "]" * open_brackets
    if open_braces > 0:
        closing += "}" * open_braces

    candidate = trimmed + closing
    try:
        json.loads(candidate)
        logger.warning("Repaired truncated JSON by closing open structures.")
        return candidate
    except json.JSONDecodeError:
        pass

    bracket_pos = trimmed.rfind(",")
    if bracket_pos > 0:
        shorter = trimmed[:bracket_pos].rstrip()
        candidate2 = shorter + closing
        try:
            json.loads(candidate2)
            logger.warning("Repaired truncated JSON by dropping last partial keyword.")
            return candidate2
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Unable to repair JSON: {raw[:120]!r}…")


class AIService:
    """
    SDK-backed metadata generator.

    Public interface is unchanged from the previous `requests`-based
    implementation: `generate_metadata(provider, image_bytes,
    text_fallback_prompt, market_rules)` -> dict with
    {title, description, keywords[, error]}.
    """

    def __init__(self, config_manager):
        self.config = config_manager
        # Per-provider SDK clients are created lazily and cached, since
        # building a client is cheap but we still don't want to repeat it
        # per-image inside a batch.
        self._openai_client = None       # api.openai.com
        self._openrouter_client = None   # openrouter.ai
        self._groq_client = None         # api.groq.com
        self._google_client = None       # generativelanguage.googleapis.com
        self._cached_keys = {}           # provider -> api_key used to build the cached client

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _get_system_prompt(self, market_rules=None) -> str:
        # Always read the user's configured keyword/title limits from config.
        # These take priority over market defaults so that when a user sets
        # "min 35 / max 45" in Settings the AI is prompted with those bounds
        # regardless of which market is selected.
        user_rules = self.config.get_metadata_rules()
        user_kw_min = user_rules.get("keyword_min_count")
        user_kw_max = user_rules.get("keyword_max_count")
        user_title_min = user_rules.get("title_min_length")
        user_title_max = user_rules.get("title_max_length")

        if market_rules is None:
            return _build_system_prompt(
                market_name="micro-stock platforms",
                title_min=user_title_min if user_title_min is not None else 5,
                title_max=user_title_max if user_title_max is not None else 70,
                kw_min=user_kw_min if user_kw_min is not None else 7,
                kw_max=user_kw_max if user_kw_max is not None else 49,
                kw_max_len=50,
                description_max=200,
            )

        # Market rules provide structural defaults; user settings override the
        # keyword and title count bounds so that Settings > Market in priority.
        return _build_system_prompt(
            market_name=market_rules.name,
            title_min=user_title_min if user_title_min is not None else market_rules.title_min,
            title_max=user_title_max if user_title_max is not None else market_rules.title_max,
            kw_min=user_kw_min if user_kw_min is not None else market_rules.keyword_min,
            kw_max=user_kw_max if user_kw_max is not None else market_rules.keyword_max,
            kw_max_len=market_rules.keyword_max_len,
            description_max=market_rules.description_max,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_available_providers(self) -> list:
        """Return providers with API keys configured, in user-defined fallback order."""
        order = self.config.get_fallback_provider_order()
        # Include any provider not in the saved order (future-proofing)
        all_providers = list(order) + [p for p in SUPPORTED_PROVIDERS if p not in order]
        return [p for p in all_providers if self.config.get("api_keys", p)]

    def generate_metadata_with_fallback(
        self,
        preferred_provider: str,
        image_bytes: Optional[bytes],
        text_fallback_prompt: str = "Generate metadata for a creative stock graphic.",
        market_rules=None,
        max_retries: int = 2,
    ) -> Dict[str, Any]:
        """
        Try `preferred_provider` first.  If it fails with any error, try every
        other provider that has an API key configured, in priority order.
        Returns the first successful result, or the last error dict if all fail.
        Also returns which provider ultimately succeeded via the '_provider_used'
        key (stripped before returning to callers that don't expect it).
        """
        candidates = [preferred_provider] + [
            p for p in self.get_available_providers() if p != preferred_provider
        ]

        last_result = None
        for provider in candidates:
            api_key = self.config.get("api_keys", provider)
            if not api_key:
                continue
            result = self.generate_metadata(
                provider=provider,
                image_bytes=image_bytes,
                text_fallback_prompt=text_fallback_prompt,
                market_rules=market_rules,
                max_retries=max_retries,
            )
            if not result.get("error"):
                result["_provider_used"] = provider
                return result
            logger.warning(
                "Provider '%s' failed (%s); trying next fallback.",
                provider, result.get("description", "unknown error"),
            )
            last_result = result

        # All providers failed — return the last error
        if last_result is None:
            last_result = self._error_response("No AI providers have an API key configured.")
        last_result["_provider_used"] = None
        return last_result

    def generate_metadata(
        self,
        provider: str,
        image_bytes: Optional[bytes],
        text_fallback_prompt: str = "Generate metadata for a creative stock graphic.",
        market_rules=None,
        max_retries: int = 2,
    ) -> Dict[str, Any]:
        """
        Generate metadata via the chosen AI provider's SDK.
        market_rules optionally customises the system prompt constraints.
        Always returns {title, description, keywords}; errors include 'error': True.

        Automatically retries transient failures (timeouts, rate limits,
        malformed/incomplete JSON) up to `max_retries` times with a short
        backoff before giving up and returning an error response.
        """
        provider = provider.lower().strip()

        if provider == "deepseek":
            return self._error_response(
                "DeepSeek has been removed as a provider: its public API does not "
                "currently support image input, so it cannot generate metadata from "
                "the actual photo. Please choose Google, OpenAI, OpenRouter, or Groq."
            )

        if provider not in SUPPORTED_PROVIDERS:
            return self._error_response(f"Unsupported provider: {provider}")

        api_key = self.config.get("api_keys", provider)
        model = self.config.get("default_models", provider)

        if not api_key:
            return self._error_response(f"API key missing for provider: {provider}")
        if not model:
            return self._error_response(f"No model configured for provider: {provider}")

        # Groq-specific guard: silently "fixing" the model is worse than telling
        # the user, but for batch reliability we transparently swap to a vision
        # model and say so in the logs rather than failing every single image.
        if provider == "groq" and not self._groq_model_supports_vision(model):
            logger.warning(
                "Configured Groq model '%s' has no vision support; using '%s' "
                "for this request so the image is actually analyzed.",
                model, GROQ_VISION_FALLBACK_MODEL,
            )
            model = GROQ_VISION_FALLBACK_MODEL

        system_prompt = self._get_system_prompt(market_rules)

        mime_type = "image/jpeg"
        if image_bytes:
            mime_type = _detect_mime(image_bytes)

        dispatch = {
            "google":     self._call_google,
            "openai":     self._call_openai,
            "openrouter": self._call_openrouter,
            "groq":       self._call_groq,
        }

        last_error = "Unknown error"
        for attempt in range(max_retries + 1):
            try:
                raw_text = dispatch[provider](
                    api_key, model, system_prompt, text_fallback_prompt,
                    image_bytes, mime_type,
                )
                return self._parse_model_text(raw_text)

            except _RetryableProviderError as exc:
                last_error = str(exc)
                logger.warning(
                    "Retryable error from %s (attempt %d/%d): %s",
                    provider, attempt + 1, max_retries + 1, last_error,
                )
                if attempt < max_retries:
                    time.sleep(min(2 ** attempt, 8) * 0.5)
                    continue
                return self._error_response(
                    f"{last_error} (failed after {attempt + 1} attempt(s))"
                )

            except _FatalProviderError as exc:
                return self._error_response(str(exc))

            except Exception as exc:  # noqa: BLE001 — last-resort safety net
                logger.exception("Unexpected error calling provider %s", provider)
                return self._error_response(f"Unexpected error ({provider}): {exc}")

        return self._error_response(last_error)

    # ------------------------------------------------------------------
    # Provider implementations — each returns the *raw text* the model
    # produced (expected to be a JSON string); parsing/repair/sanitizing
    # happens centrally in `_parse_model_text`.
    # ------------------------------------------------------------------

    def _require_openai_sdk(self):
        if not _HAVE_OPENAI_SDK:
            raise _FatalProviderError(
                "The 'openai' package is not installed. Run: pip install openai"
            )

    def _require_google_sdk(self):
        if not _HAVE_GOOGLE_SDK:
            raise _FatalProviderError(
                "The 'google-genai' package is not installed. Run: pip install google-genai"
            )

    def _get_openai_client(self, api_key: str):
        self._require_openai_sdk()
        if self._openai_client is None or self._cached_keys.get("openai") != api_key:
            self._openai_client = _OpenAIClient(api_key=api_key, timeout=60.0, max_retries=0)
            self._cached_keys["openai"] = api_key
        return self._openai_client

    def _get_openrouter_client(self, api_key: str):
        self._require_openai_sdk()
        if self._openrouter_client is None or self._cached_keys.get("openrouter") != api_key:
            self._openrouter_client = _OpenAIClient(
                api_key=api_key,
                base_url="https://openrouter.ai/api/v1",
                timeout=60.0,
                max_retries=0,
                default_headers={
                    "HTTP-Referer": "https://metaembed.ai",
                    "X-Title": "MetaEmbed AI Client",
                },
            )
            self._cached_keys["openrouter"] = api_key
        return self._openrouter_client

    def _get_groq_client(self, api_key: str):
        self._require_openai_sdk()
        if self._groq_client is None or self._cached_keys.get("groq") != api_key:
            self._groq_client = _OpenAIClient(
                api_key=api_key,
                base_url="https://api.groq.com/openai/v1",
                timeout=60.0,
                max_retries=0,
            )
            self._cached_keys["groq"] = api_key
        return self._groq_client

    def _get_google_client(self, api_key: str):
        self._require_google_sdk()
        if self._google_client is None or self._cached_keys.get("google") != api_key:
            self._google_client = _genai.Client(api_key=api_key)
            self._cached_keys["google"] = api_key
        return self._google_client

    @staticmethod
    def _groq_model_supports_vision(model: str) -> bool:
        model = (model or "").lower()
        # Groq's current vision-capable family (Llama 4 scout/maverick, and
        # any future "vision"-tagged model). Pure text models like the
        # llama-3.x and mixtral lines cannot accept image input.
        return "llama-4" in model or "vision" in model

    # -- OpenAI (Responses API) ---------------------------------------

    def _call_openai(self, api_key, model, system_prompt, text_prompt,
                      image_bytes, mime_type) -> str:
        client = self._get_openai_client(api_key)

        content = [{"type": "input_text", "text": text_prompt}]
        if image_bytes:
            import base64
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            content.append({
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{b64}",
            })

        try:
            response = client.responses.create(
                model=model,
                instructions=system_prompt,
                input=[{"role": "user", "content": content}],
                max_output_tokens=2048,
                text={"format": {"type": "json_object"}},
            )
        except _openai_errors.AuthenticationError as exc:
            raise _FatalProviderError("Invalid OpenAI API key (authentication failed).") from exc
        except _openai_errors.RateLimitError as exc:
            raise _RetryableProviderError(f"OpenAI rate limit exceeded: {exc}") from exc
        except _openai_errors.APITimeoutError as exc:
            raise _RetryableProviderError("OpenAI request timed out.") from exc
        except _openai_errors.APIConnectionError as exc:
            raise _RetryableProviderError(f"Network error contacting OpenAI: {exc}") from exc
        except _openai_errors.InternalServerError as exc:
            raise _RetryableProviderError(f"OpenAI server error: {exc}") from exc
        except _openai_errors.BadRequestError as exc:
            # e.g. unknown model name — not worth retrying
            raise _FatalProviderError(f"OpenAI rejected the request: {exc}") from exc
        except _openai_errors.APIStatusError as exc:
            raise _FatalProviderError(f"OpenAI HTTP {exc.status_code}: {exc.message}") from exc

        text = getattr(response, "output_text", None)
        if not text:
            raise _RetryableProviderError("OpenAI returned an empty response.")
        return text

    # -- Google (google-genai SDK) -------------------------------------

    def _call_google(self, api_key, model, system_prompt, text_prompt,
                      image_bytes, mime_type) -> str:
        client = self._get_google_client(api_key)

        parts = [_genai_types.Part.from_text(text=text_prompt)]
        if image_bytes:
            parts.append(_genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type))

        try:
            response = client.models.generate_content(
                model=model,
                contents=_genai_types.Content(role="user", parts=parts),
                config=_genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0.2,
                    max_output_tokens=2048,
                ),
            )
        except _genai_errors.ClientError as exc:
            code = getattr(exc, "code", None)
            if code in (401, 403):
                raise _FatalProviderError("Invalid Google API key or insufficient permissions.") from exc
            if code == 429:
                raise _RetryableProviderError(f"Google rate limit exceeded: {exc}") from exc
            if code == 400:
                raise _FatalProviderError(f"Google rejected the request: {exc}") from exc
            raise _RetryableProviderError(f"Google API client error: {exc}") from exc
        except _genai_errors.ServerError as exc:
            raise _RetryableProviderError(f"Google server error: {exc}") from exc
        except _genai_errors.APIError as exc:
            raise _RetryableProviderError(f"Google API error: {exc}") from exc

        text = getattr(response, "text", None)
        if not text:
            raise _RetryableProviderError("Google returned an empty response.")
        return text

    # -- OpenRouter (openai SDK, custom base_url) ----------------------

    def _call_openrouter(self, api_key, model, system_prompt, text_prompt,
                          image_bytes, mime_type) -> str:
        client = self._get_openrouter_client(api_key)
        return self._call_chat_completions_style(
            client, "OpenRouter", model, system_prompt, text_prompt,
            image_bytes, mime_type, use_json_object_mode=True,
        )

    # -- Groq (openai SDK, custom base_url) ----------------------------

    def _call_groq(self, api_key, model, system_prompt, text_prompt,
                    image_bytes, mime_type) -> str:
        client = self._get_groq_client(api_key)
        return self._call_chat_completions_style(
            client, "Groq", model, system_prompt, text_prompt,
            image_bytes, mime_type, use_json_object_mode=True,
        )

    # -- Shared Chat Completions helper (OpenAI-compatible providers) --

    def _call_chat_completions_style(self, client, provider_label, model,
                                      system_prompt, text_prompt,
                                      image_bytes, mime_type,
                                      use_json_object_mode: bool) -> str:
        content = [{"type": "text", "text": text_prompt}]
        if image_bytes:
            import base64
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64}"},
            })

        kwargs = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=0.2,
            max_tokens=2048,
        )
        if use_json_object_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            completion = client.chat.completions.create(**kwargs)
        except _openai_errors.AuthenticationError as exc:
            raise _FatalProviderError(f"Invalid {provider_label} API key (authentication failed).") from exc
        except _openai_errors.RateLimitError as exc:
            raise _RetryableProviderError(f"{provider_label} rate limit exceeded: {exc}") from exc
        except _openai_errors.APITimeoutError as exc:
            raise _RetryableProviderError(f"{provider_label} request timed out.") from exc
        except _openai_errors.APIConnectionError as exc:
            raise _RetryableProviderError(f"Network error contacting {provider_label}: {exc}") from exc
        except _openai_errors.InternalServerError as exc:
            raise _RetryableProviderError(f"{provider_label} server error: {exc}") from exc
        except _openai_errors.BadRequestError as exc:
            raise _FatalProviderError(f"{provider_label} rejected the request: {exc}") from exc
        except _openai_errors.APIStatusError as exc:
            raise _FatalProviderError(f"{provider_label} HTTP {exc.status_code}: {exc.message}") from exc

        if not completion.choices:
            raise _RetryableProviderError(f"{provider_label} returned no choices.")
        text = completion.choices[0].message.content
        if not text:
            raise _RetryableProviderError(f"{provider_label} returned an empty response.")
        return text

    # ------------------------------------------------------------------
    # Shared response parsing / sanitization
    # ------------------------------------------------------------------

    def _parse_model_text(self, raw_text: str) -> Dict[str, Any]:
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE
        ).strip()

        if not cleaned:
            raise _RetryableProviderError("Provider returned an empty response body.")

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as first_exc:
            logger.warning("Initial JSON parse failed (%s); attempting repair…", first_exc)
            try:
                repaired = _repair_json(cleaned)
                parsed = json.loads(repaired)
                logger.info("JSON repair succeeded.")
            except (ValueError, json.JSONDecodeError):
                raise _RetryableProviderError(f"Malformed/incomplete JSON from provider: {first_exc}")

        if not isinstance(parsed, dict):
            raise _RetryableProviderError("Provider response was valid JSON but not an object.")

        missing = [f for f in ("title", "description", "keywords") if f not in parsed]
        if missing:
            raise _RetryableProviderError(f"Response missing required field(s): {', '.join(missing)}")

        return self._sanitize_metadata(parsed)

    def _sanitize_metadata(self, data: Dict[str, Any]) -> Dict[str, Any]:
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        raw_kw = data.get("keywords", [])
        if isinstance(raw_kw, str):
            raw_kw = [k.strip() for k in raw_kw.split(",")]
        keywords = [str(k).strip() for k in raw_kw if k]
        # Item #3: remove duplicates (case-insensitive), empties, numeric-only,
        # and special-character-only keywords, while preserving order.
        keywords = clean_keywords(keywords)
        return {"title": title, "description": description, "keywords": keywords}

    @staticmethod
    def _error_response(msg: str) -> Dict[str, Any]:
        logger.error("AIService error: %s", msg)
        return {"title": "Error Processing Request", "description": msg,
                "keywords": ["error"], "error": True}


# ---------------------------------------------------------------------------
# Internal control-flow exceptions (never surfaced directly to callers —
# `generate_metadata` always converts these into the standard error dict).
# ---------------------------------------------------------------------------

class _RetryableProviderError(Exception):
    """Transient failure (timeout, rate limit, malformed JSON) worth retrying."""


class _FatalProviderError(Exception):
    """Non-transient failure (bad API key, bad request, missing SDK) — don't retry."""

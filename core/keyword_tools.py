"""
keyword_tools.py
~~~~~~~~~~~~~~~~~
Keyword cleanup and lightweight quality/relevance scoring.

This is intentionally a separate, single-purpose module (rather than more
logic bolted onto AIService or MarketRules) so cleanup behaviour has one
clear home and can be reused by both the batch worker and the UI's
quality-score card.

Nothing here calls the network — it's pure text processing over whatever
title/description/keywords the AI (or the user, when hand-editing) produced.
"""

import re
from typing import Dict, List, Tuple, Optional

# Generic, low-signal words that add little commercial search value as
# *keywords* in stock-photo metadata. This is intentionally small and
# conservative — the goal is to catch obviously irrelevant tokens (stray
# articles/pronouns an AI sometimes emits), not to second-guess legitimate
# descriptive keywords.
_LOW_VALUE_KEYWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "is",
    "it", "this", "that", "image", "photo", "picture", "stock",
}

_SPECIAL_CHARS_ONLY_RE = re.compile(r"^[^a-zA-Z0-9]+$")
_NUMERIC_ONLY_RE = re.compile(r"^\d+$")


# ---------------------------------------------------------------------------
# Item #3 — Keyword cleanup
# ---------------------------------------------------------------------------

def clean_keywords(keywords: List[str]) -> List[str]:
    """
    Clean a list of keywords:
      - strip unnecessary whitespace
      - drop empty keywords
      - drop numeric-only keywords ("123")
      - drop special-character-only keywords ("!!!", "---")
      - de-duplicate case-insensitively
      - drop near-duplicate phrasings of the SAME core word(s) — e.g.
        "coffee cup" and "cup of coffee" both reduce to the word-set
        {"coffee", "cup"} and only the first occurrence is kept. This is
        intentionally an EXACT word-set match (after dropping filler
        words like "of"/"a"/"the"), not a subset match — "work" and
        "remote work" have different word-sets and are both kept, since
        Adobe Stock's own guidance treats single-concept keywords like
        "red" and "dress" as distinct, valuable keywords rather than
        redundant variants of "red dress." This is a safety net behind
        the AI prompt's instruction to avoid redundant variants, since
        models occasionally still emit them.
      - preserve original order (first occurrence wins, which also
        preserves the AI's commercial-rank ordering — earlier keywords
        are higher-ranked, so when a near-duplicate pair is found, the
        EARLIER one is always the one kept)
    """
    _FILLER_WORDS = {"of", "a", "an", "the"}

    seen: set = set()
    seen_word_sets: list = []  # list of frozensets of core words, in keep order
    cleaned: List[str] = []

    for raw in keywords:
        kw = str(raw).strip()
        kw = re.sub(r"\s+", " ", kw)  # collapse internal whitespace

        if not kw:
            continue
        if _NUMERIC_ONLY_RE.match(kw):
            continue
        if _SPECIAL_CHARS_ONLY_RE.match(kw):
            continue

        key = kw.lower()
        if key in seen:
            continue

        # Near-duplicate check: only catches phrases that are pure
        # word-reorderings/fillers of the SAME core word set (multi-word
        # phrases only — single words never collide with anything here
        # except an exact match, which `seen` already caught above).
        core_words = frozenset(w for w in key.split() if w not in _FILLER_WORDS)
        if len(core_words) >= 2:
            if core_words in seen_word_sets:
                continue
            seen_word_sets.append(core_words)

        seen.add(key)
        cleaned.append(kw)

    return cleaned


# ---------------------------------------------------------------------------
# Item #18 — Keyword relevance scoring
# ---------------------------------------------------------------------------

def score_keyword_relevance(
    keyword: str,
    title: str = "",
    description: str = "",
    position: int = 0,
    total: int = 1,
) -> float:
    """
    Heuristic relevance score in [0.0, 1.0] for a single keyword, without
    calling any AI model. This is deliberately simple and explainable:

      - Keywords that also appear in the title or description get a strong
        boost (the AI presumably used them for a reason, and they tie
        directly to what a buyer would search for given the image).
      - Earlier keywords are weighted slightly higher, since the system
        prompt already asks the AI to order keywords "most relevant first".
      - Low-value generic words (articles, "stock", "photo", etc.) are
        penalized — they rarely help discovery.
      - Very short (<=2 char) or very long (>30 char) keywords are
        penalized slightly: stock platforms favour concise, specific terms.
    """
    kw = keyword.strip().lower()
    if not kw:
        return 0.0

    # Low-value generic words (articles, "stock", "photo", etc.) are capped
    # at a low score outright — a position bonus shouldn't be able to rescue
    # an obviously irrelevant token just because the AI listed it early.
    if kw in _LOW_VALUE_KEYWORDS:
        return 0.05

    score = 0.5  # baseline

    haystack = f"{title} {description}".lower()
    if kw in haystack:
        score += 0.30

    if len(kw) <= 2:
        score -= 0.15
    elif len(kw) > 30:
        score -= 0.10

    # Position bonus: first keyword gets the full +0.15, fading to 0 by the
    # end of the list (matches "most relevant first" prompt instruction).
    if total > 1:
        position_bonus = 0.15 * (1 - (position / (total - 1)))
    else:
        position_bonus = 0.15
    score += position_bonus

    return max(0.0, min(1.0, round(score, 3)))


def sort_keywords_by_relevance(
    keywords: List[str], title: str = "", description: str = ""
) -> List[Tuple[str, float]]:
    """
    Score every keyword and return (keyword, score) pairs sorted from
    highest relevance to lowest. Ties preserve original order (stable sort).
    """
    total = len(keywords)
    scored = [
        (kw, score_keyword_relevance(kw, title, description, i, total))
        for i, kw in enumerate(keywords)
    ]
    return sorted(scored, key=lambda pair: pair[1], reverse=True)


def remove_irrelevant_keywords(
    keywords: List[str], title: str = "", description: str = "",
    min_score: float = 0.25,
) -> List[str]:
    """
    Drop keywords whose relevance score falls below `min_score`. Returns
    keywords still in their *original relative order* (not re-sorted), since
    callers that also want sorting should use sort_keywords_by_relevance.
    """
    total = len(keywords)
    keep = []
    for i, kw in enumerate(keywords):
        if score_keyword_relevance(kw, title, description, i, total) >= min_score:
            keep.append(kw)
    return keep


# ---------------------------------------------------------------------------
# Item #20 — Overall metadata quality score
# ---------------------------------------------------------------------------

def compute_quality_score(
    title: str,
    description: str,
    keywords: List[str],
    title_min: int = 5,
    title_max: int = 70,
    keyword_min: int = 7,
    keyword_max: int = 49,
    description_max: int = 200,
) -> Dict[str, object]:
    """
    Produce an overall 0-100 metadata quality score plus a per-dimension
    breakdown, so the UI can show *why* a score is what it is.

    Dimensions (each 0.0-1.0 before weighting):
      - title_quality:        length within the market's [min, max] window
      - description_quality:  non-empty, reasonable length, not just the title repeated
      - keyword_uniqueness:    ratio of unique (case-insensitive) keywords to total
      - keyword_relevance:     average heuristic relevance score across keywords
      - completeness:         are all three fields present and non-trivial
    """
    title = (title or "").strip()
    description = (description or "").strip()
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]

    # --- Title quality ---
    if not title:
        title_quality = 0.0
    elif title_min <= len(title) <= title_max:
        title_quality = 1.0
    else:
        # Linearly penalize distance outside the window
        if len(title) < title_min:
            distance = title_min - len(title)
        else:
            distance = len(title) - title_max
        title_quality = max(0.0, 1.0 - (distance / max(title_min, 1)) * 0.5)

    # --- Description quality ---
    if not description:
        description_quality = 0.0
    elif description.lower() == title.lower():
        description_quality = 0.3  # just repeats the title — low value
    elif len(description) > description_max:
        description_quality = 0.6  # present but over limit
    elif len(description) < 10:
        description_quality = 0.4  # too thin to be useful
    else:
        description_quality = 1.0

    # --- Keyword uniqueness ---
    if not keywords:
        keyword_uniqueness = 0.0
    else:
        unique_count = len({k.lower() for k in keywords})
        keyword_uniqueness = unique_count / len(keywords)

    # --- Keyword relevance (average heuristic score) ---
    if not keywords:
        keyword_relevance = 0.0
    else:
        scores = [
            score_keyword_relevance(k, title, description, i, len(keywords))
            for i, k in enumerate(keywords)
        ]
        keyword_relevance = sum(scores) / len(scores)

    # --- Completeness (counts toward the min/max keyword window too) ---
    completeness_parts = [
        1.0 if title else 0.0,
        1.0 if description else 0.0,
        1.0 if keyword_min <= len(keywords) <= keyword_max else (0.5 if keywords else 0.0),
    ]
    completeness = sum(completeness_parts) / len(completeness_parts)

    weights = {
        "title_quality":       0.25,
        "description_quality": 0.20,
        "keyword_uniqueness":  0.15,
        "keyword_relevance":   0.20,
        "completeness":        0.20,
    }
    dimensions = {
        "title_quality":       round(title_quality, 3),
        "description_quality": round(description_quality, 3),
        "keyword_uniqueness":  round(keyword_uniqueness, 3),
        "keyword_relevance":   round(keyword_relevance, 3),
        "completeness":        round(completeness, 3),
    }
    overall = sum(dimensions[k] * weights[k] for k in weights)
    overall_pct = round(overall * 100)

    if overall_pct >= 85:
        label = "Excellent"
    elif overall_pct >= 70:
        label = "Good"
    elif overall_pct >= 50:
        label = "Needs Improvement"
    else:
        label = "Poor"

    return {
        "overall": overall_pct,
        "label": label,
        "dimensions": dimensions,
    }


# ---------------------------------------------------------------------------
# Item #19 — optional reusable metadata templates
# ---------------------------------------------------------------------------

def apply_template(
    title: str,
    description: str,
    keywords: List[str],
    template: "Optional[Dict[str, object]]",
) -> Dict[str, object]:
    """
    Apply an optional reusable metadata template: title/description
    prefix+suffix, plus fixed keywords prepended.

    `template` is the dict shape stored by ConfigManager.get_active_template():
    {name, title_prefix, title_suffix, description_prefix,
     description_suffix, fixed_keywords: [...]}.
    A falsy `template` (None / {}) is a no-op — templates are optional.
    """
    if not template:
        return {"title": title, "description": description, "keywords": list(keywords)}

    title_prefix = str(template.get("title_prefix", "") or "")
    title_suffix = str(template.get("title_suffix", "") or "")
    desc_prefix = str(template.get("description_prefix", "") or "")
    desc_suffix = str(template.get("description_suffix", "") or "")
    fixed_keywords = template.get("fixed_keywords", []) or []

    new_title = f"{title_prefix}{title}{title_suffix}".strip()
    new_description = f"{desc_prefix}{description}{desc_suffix}".strip()
    new_keywords = clean_keywords(list(fixed_keywords) + list(keywords))

    return {"title": new_title, "description": new_description, "keywords": new_keywords}


# ---------------------------------------------------------------------------
# Item #17 — pre-embedding metadata quality checks
# ---------------------------------------------------------------------------

def check_metadata_quality(
    title: str, description: str, keywords: List[str],
) -> List[str]:
    """
    Return a list of human-readable warnings for problems that should be
    surfaced to the user *before* writing metadata to a file. An empty
    list means no warnings.
    """
    warnings: List[str] = []
    title = (title or "").strip()
    description = (description or "").strip()
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]

    if not title:
        warnings.append("Title is empty.")
    if not description:
        warnings.append("Description is empty.")
    if not keywords:
        warnings.append("No keywords provided.")
    else:
        lowered = [k.lower() for k in keywords]
        if len(lowered) != len(set(lowered)):
            warnings.append("Keyword list contains duplicates.")

    return warnings

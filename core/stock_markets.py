"""
stock_markets.py
~~~~~~~~~~~~~~~~
Defines the rules and constraints for each supported stock marketplace.
All limits are sourced from each platform's contributor guidelines.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class MarketRules:
    name: str                        # Display name
    key: str                         # Internal key  e.g. "adobe"
    title_min: int                   # Minimum title length (chars)
    title_max: int                   # Maximum title length (chars)
    keyword_min: int                 # Minimum number of keywords
    keyword_max: int                 # Maximum number of keywords
    keyword_max_len: int             # Max chars per individual keyword
    description_max: int             # Max description length (chars)
    allows_description: bool         # Some markets ignore the description field
    sentence_case_title: bool        # Whether to sentence-case the title
    csv_columns: List[str]           # Column order for export CSV
    notes: str = ""                  # Freetext contributor notes shown in UI


# ---------------------------------------------------------------------------
# Market definitions
# ---------------------------------------------------------------------------

MARKETS: Dict[str, MarketRules] = {
    "adobe": MarketRules(
        name="Adobe Stock",
        key="adobe",
        title_min=5,
        title_max=70,
        keyword_min=7,
        keyword_max=49,
        keyword_max_len=50,
        description_max=200,
        allows_description=True,
        sentence_case_title=False,
        csv_columns=["Filename", "Title", "Keywords", "Category", "Editorial"],
        notes=(
            "Adobe Stock: 7–49 keywords; title 5–70 chars; "
            "no adult/editorial flags needed for standard content."
        ),
    ),
    "shutterstock": MarketRules(
        name="Shutterstock",
        key="shutterstock",
        title_min=5,
        title_max=200,
        keyword_min=5,
        keyword_max=50,
        keyword_max_len=45,
        description_max=500,
        allows_description=True,
        sentence_case_title=True,
        csv_columns=["Filename", "Description", "Keywords", "Categories", "Editorial", "Mature Content"],
        notes=(
            "Shutterstock: 5–50 keywords (comma separated); "
            "description = title field; max 45 chars per keyword."
        ),
    ),
    "getty": MarketRules(
        name="Getty / iStock",
        key="getty",
        title_min=3,
        title_max=200,
        keyword_min=5,
        keyword_max=50,
        keyword_max_len=64,
        description_max=2000,
        allows_description=True,
        sentence_case_title=False,
        csv_columns=["Filename", "Title", "Description", "Keywords", "Country", "Date Taken", "Editorial"],
        notes="Getty/iStock: thorough descriptions valued; up to 50 keywords.",
    ),
    "freepik": MarketRules(
        name="Freepik",
        key="freepik",
        title_min=10,
        title_max=100,
        keyword_min=10,
        keyword_max=20,
        keyword_max_len=40,
        description_max=300,
        allows_description=True,
        sentence_case_title=False,
        csv_columns=["Filename", "Title", "Description", "Keywords"],
        notes="Freepik: 10–20 focused keywords; avoid broad generic terms.",
    ),
    "pond5": MarketRules(
        name="Pond5",
        key="pond5",
        title_min=5,
        title_max=100,
        keyword_min=5,
        keyword_max=100,
        keyword_max_len=64,
        description_max=1000,
        allows_description=True,
        sentence_case_title=False,
        csv_columns=["Filename", "Title", "Description", "Keywords"],
        notes="Pond5: up to 100 keywords; very thorough metadata encouraged.",
    ),
}

MARKET_DISPLAY_NAMES = {v.name: k for k, v in MARKETS.items()}


def get_market(key: str) -> Optional[MarketRules]:
    return MARKETS.get(key)


def get_all_market_names() -> List[str]:
    return [m.name for m in MARKETS.values()]


def apply_rules(
    title: str,
    description: str,
    keywords: List[str],
    rules: MarketRules,
    custom_keywords: List[str],
) -> Dict:
    """
    Enforce market rules on AI-generated metadata.

    SEO fix: custom_keywords are now APPENDED after the AI's
    commercially-ranked keywords (deduplicated), not prepended. The AI
    is prompted to emit keywords in strict commercial-rank order
    (primary subject first, technical/category terms last) — prepending
    custom keywords used to bump that #1 keyword out of pole position,
    and worse, made it more likely the AI's top-ranked keywords got
    truncated off the end when `keyword_max` was hit. Appending means
    the AI's ranked list is only trimmed from the bottom (lowest-value
    keywords first), which is the correct truncation direction.

    Returns cleaned dict ready for writing / export.

    Item #21: this function is only ever called when the user has
    explicitly enabled "Marketplace Rule Validation" in Settings — it is
    no longer invoked unconditionally by the batch worker. When it does
    run, the returned dict also reports exactly which fields were changed
    (`modified_fields`) so the UI can show the user what was trimmed.
    """
    original_title = title.strip()
    original_description = description.strip()
    original_keywords = [k.strip() for k in keywords if k.strip()]

    # --- Title ---
    title = original_title
    if rules.sentence_case_title and title:
        title = title[0].upper() + title[1:]
    title = title[:rules.title_max]
    if len(title) < rules.title_min:
        # pad with ellipsis if somehow too short (shouldn't normally happen)
        title = title.ljust(rules.title_min, " ").strip()

    # --- Description ---
    description = original_description[:rules.description_max]

    # --- Keywords: append custom after AI-ranked, deduplicate, enforce limits ---
    seen = set()
    merged: List[str] = []
    for kw in (keywords + custom_keywords):
        kw = kw.strip()[:rules.keyword_max_len]
        kw_lower = kw.lower()
        if kw and kw_lower not in seen:
            seen.add(kw_lower)
            merged.append(kw)

    # Ensure within [min, max]
    merged = merged[:rules.keyword_max]
    # Don't warn if below min — the caller (Worker) can log it

    modified_fields: List[str] = []
    if title != original_title:
        modified_fields.append("title")
    if description != original_description:
        modified_fields.append("description")
    if merged != (original_keywords + custom_keywords)[:len(merged)] or \
            len(merged) != len(original_keywords + custom_keywords):
        modified_fields.append("keywords")

    return {
        "title": title,
        "description": description,
        "keywords": merged,
        "keyword_count": len(merged),
        "meets_min_keywords": len(merged) >= rules.keyword_min,
        "modified_fields": modified_fields,
    }
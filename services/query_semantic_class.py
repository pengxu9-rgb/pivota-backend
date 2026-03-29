from __future__ import annotations

import re
from typing import Optional


def classify_query_semantic_class(query: Optional[str]) -> str:
    q = str(query or "").strip().lower()
    if not q:
        return "default"
    q_compact = re.sub(r"[^a-z0-9]+", "", q)
    if (
        "fragrance free" in q
        or "fragrance-free" in q
        or "free fragrance" in q
        or "sin fragancia" in q
    ):
        return "beauty"
    if re.search(
        r"\b(perfume|perfumes|fragrance|fragrances|parfum|parfums|cologne|eau de parfum|eau de toilette|body mist)\b",
        q,
    ):
        return "fragrance"
    if any(
        token in q_compact
        for token in (
            "perfume",
            "perfumes",
            "fragrance",
            "fragrances",
            "parfum",
            "parfums",
            "cologne",
            "bodymist",
            "eaudeparfum",
            "eaudetoilette",
            "edp",
            "edt",
        )
    ):
        return "fragrance"
    if re.search(
        r"\b(lingerie|underwear|bra|panties|panty|briefs|thong|lencer[ií]a|ropa interior)\b",
        q,
    ):
        return "lingerie"
    if re.search(
        r"\b("
        r"beauty|skincare|skin care|cosmetic|cosmetics|makeup|"
        r"serum|toner|moisturizer|moisturiser|cleanser|"
        r"foundation|lipstick|blush|gloss"
        r")\b",
        q,
    ):
        return "beauty"
    return "default"

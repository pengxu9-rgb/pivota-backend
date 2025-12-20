from __future__ import annotations

from typing import Any, Dict, List, Optional


_BRUSH_INTENT_KEYWORDS = (
    "brush",
    "makeup brush",
    "cosmetic brush",
    "メイクブラシ",
    "ブラシ",
    "化粧筆",
)


def infer_query_overrides(
    query: Optional[str],
    category: Optional[str],
) -> Dict[str, Any]:
    """
    Heuristics to make long natural-language queries more searchable.

    Key goal: avoid returning unrelated categories when the user clearly asks
    for makeup brushes (e.g., Japanese queries containing 「ブラシ」).
    """
    if not query:
        return {
            "query": query,
            "category": category,
            "terms": [],
            "brush_intent": False,
        }

    query_stripped = query.strip()
    query_lower = query_stripped.lower()

    brush_intent = any(
        kw in query_lower for kw in _BRUSH_INTENT_KEYWORDS if kw.isascii()
    ) or any(kw in query_stripped for kw in _BRUSH_INTENT_KEYWORDS if not kw.isascii())

    effective_category = category
    effective_query = query_stripped
    terms: List[str] = []

    if brush_intent:
        if not effective_category:
            effective_category = "brush"

        # Normalize long NL queries (JP/EN) to a stable token for matching.
        if len(query_stripped) > 40:
            effective_query = "brush"

        terms = [
            "brush",
            "makeup brush",
            "cosmetic brush",
            "メイクブラシ",
            "ブラシ",
            "化粧筆",
        ]
    else:
        terms = [t for t in query_lower.split() if t]

    return {
        "query": effective_query,
        "category": effective_category,
        "terms": terms,
        "brush_intent": brush_intent,
    }


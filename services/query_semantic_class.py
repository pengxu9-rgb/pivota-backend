from __future__ import annotations

import re
from typing import Optional


def classify_query_semantic_class(query: Optional[str]) -> str:
    q = str(query or "").strip().lower()
    if not q:
        return "default"
    q_compact = re.sub(r"[^a-z0-9]+", "", q)
    if re.search(
        r"\b("
        r"eye cream|eye serum|night cream|overnight mask|face mask|sleeping mask|"
        r"makeup remover|remover balm|cleansing balm|hand cream|"
        r"retinal|retinol|niacinamide|hyaluronic acid|salicylic acid|vitamin c|"
        r"dark circles|acne|pores|dry skin|sensitive skin"
        r")\b",
        q,
    ):
        return "beauty"
    if any(
        token in q_compact
        for token in (
            "eyecream",
            "eyeserum",
            "nightcream",
            "overnightmask",
            "facemask",
            "sleepingmask",
            "makeupremover",
            "removerbalm",
            "cleansingbalm",
            "handcream",
            "retinal",
            "retinol",
            "niacinamide",
            "hyaluronicacid",
            "salicylicacid",
            "vitaminc",
            "darkcircles",
            "acne",
            "pores",
            "dryskin",
            "sensitiveskin",
        )
    ):
        return "beauty"
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
    if re.search(r"\bsantal\s*33\b", q):
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
            "santal33",
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
        r"sunscreen|sun screen|sunblock|spf|"
        r"foundation|lipstick|blush|gloss|"
        # Additional unambiguous beauty categories that were falling through to "default"
        # and getting their external-seed recall blocked (semantic_class_blocked) → 0 results
        # despite real eligible supply (e.g. "nail polish", "lip balm").
        r"nail polish|lip balm|lip liner|mascara|eyeliner|eye liner|concealer|"
        r"bronzer|eyeshadow|eye shadow|setting spray|cuticle oil|ampoule|"
        r"body wash|body lotion|hair mask|shampoo|deodorant"
        r")\b",
        q,
    ):
        return "beauty"
    return "default"

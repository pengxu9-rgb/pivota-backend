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
    # K-beauty / skincare vocabulary missed by the lists above. These are real,
    # high-count catalog inventory (essence 61, ampoule 72, cica 58, pdrn 51,
    # centella 44, ceramide 25, ...); missing them classified "snail mucin essence"
    # / "centella ampoule" as "default", which BLOCKS the external-seed (beauty)
    # recall leg and applies the generic-default precision gate — zeroing legitimate
    # results. Placed AFTER fragrance/lingerie so a perfume/lingerie query still wins.
    # PRECISION: generic product-forms are QUALIFIED (cushion foundation, face/body
    # scrub, chemical peel — never bare cushion/scrub/peel/emulsion), and only
    # skincare-EXCLUSIVE actives are listed (cica/pdrn/ceramide/…). Food/supplement/
    # garden-colliding terms (collagen, peptide, green tea, rice water, propolis,
    # mugwort, tea tree, bare snail) are omitted — they still classify beauty when
    # combined with a form word ("collagen ampoule" → ampoule, "tea tree toner" →
    # toner). Word-boundary matched; compact list is compounds-only (no substring
    # over-match).
    if re.search(
        r"\b("
        r"essence|ampoule|ampule|"
        r"face mist|facial mist|"
        r"cushion foundation|cushion compact|bb cushion|"
        r"face scrub|body scrub|lip scrub|sugar scrub|exfoliating scrub|"
        r"chemical peel|peeling gel|exfoliating peel|exfoliant|exfoliator|"
        r"sheet mask|clay mask|sleeping mask|"
        r"cleansing oil|cleansing balm|face oil|facial oil|"
        r"toner pad|spot patch|pimple patch|micellar water|"
        r"cica|centella|centella asiatica|madecassoside|heartleaf|houttuynia|"
        r"snail mucin|pdrn|polynucleotide|ceramide|ceramides|bakuchiol|"
        r"azelaic|glycolic acid|mandelic acid|lactic acid|tranexamic|arbutin|"
        r"panthenol|allantoin"
        r")\b",
        q,
    ):
        return "beauty"
    if any(
        token in q_compact
        for token in (
            "snailmucin", "centellaasiatica", "madecassoside", "polynucleotide",
            "sheetmask", "claymask", "sleepingmask",
            "cleansingoil", "cleansingbalm", "micellarwater",
        )
    ):
        return "beauty"
    if re.search(
        r"\b("
        r"beauty|skincare|skin care|cosmetic|cosmetics|makeup|"
        r"serum|toner|moisturizer|moisturiser|cleanser|"
        r"sunscreen|sun screen|sunblock|spf|"
        r"foundation|lipstick|blush|gloss"
        r")\b",
        q,
    ):
        return "beauty"
    return "default"

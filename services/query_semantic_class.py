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
    # K-beauty / skincare vocabulary. The catalog is beauty-dominant and these are
    # real, high-count inventory (essence 61, ampoule 72, mist 50, cica 58, pdrn 51,
    # centella 44, tea tree 43, ceramide 25, peptide 73, collagen 141, ...), but the
    # lists above missed them — so "snail mucin essence" / "centella ampoule" /
    # "ceramide emulsion" fell to "default", which BLOCKS the external-seed (beauty)
    # leg and applies the generic-default precision gate, zeroing legitimate results.
    # Only clearly-skincare forms/actives (no bare "cream"/"oil"/"mask"/"gel").
    if re.search(
        r"\b("
        r"essence|ampoule|ampule|emulsion|face mist|facial mist|cushion|"
        r"scrub|peel|exfoliant|exfoliator|"
        r"sheet mask|clay mask|sleeping mask|cleansing oil|cleansing balm|"
        r"face oil|facial oil|toner pad|spot patch|pimple patch|micellar water|"
        r"cica|centella|centella asiatica|madecassoside|heartleaf|houttuynia|mugwort|"
        r"snail mucin|snail|propolis|pdrn|polynucleotide|ceramide|ceramides|"
        r"peptide|peptides|collagen|tea tree|green tea|rice water|bakuchiol|"
        r"azelaic|glycolic acid|mandelic acid|lactic acid|tranexamic|arbutin|"
        r"panthenol|allantoin"
        r")\b",
        q,
    ):
        return "beauty"
    if any(
        token in q_compact
        for token in (
            "essence", "ampoule", "ampule", "emulsion", "facemist", "facialmist",
            "cushion", "exfoliant", "exfoliator", "sheetmask", "claymask",
            "sleepingmask", "cleansingoil", "cleansingbalm", "faceoil", "facialoil",
            "spotpatch", "pimplepatch", "micellarwater", "cica", "centella",
            "madecassoside", "heartleaf", "houttuynia", "mugwort", "snailmucin",
            "propolis", "pdrn", "polynucleotide", "ceramide", "peptide", "collagen",
            "teatree", "greentea", "ricewater", "bakuchiol", "azelaic",
            "tranexamic", "arbutin", "panthenol", "allantoin",
        )
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
        r"foundation|lipstick|blush|gloss"
        r")\b",
        q,
    ):
        return "beauty"
    return "default"

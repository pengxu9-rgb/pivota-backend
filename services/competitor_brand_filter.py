"""Drop ingredient/category *types* from the merchant-facing competitor list.

For CATEGORY questions ("best collagen", "best magnesium supplement"), the
grounded AI answer lists ingredient / supplement / actives TYPES rather than
competing brands, and competitor-extraction captures them verbatim. A K-beauty /
supplement merchant then sees "competitors" like Magnesium, Ashwagandha,
Probiotics, Vitamin D, Calcium, Iron — none of which is a brand. Only names like
"Thorne" or "Vital Proteins" are real competitors.

Approach (a maintainable generalization of a plain denylist): an ingredient /
category TYPE is composed *entirely* of generic terms — the ingredient name and,
at most, a category/form word ("supplement", "powder", "acid", "glycinate") or a
vitamin letter-code ("D", "B12"). A real brand almost always carries at least one
identity-bearing token ("Vital", "Ancient", "Thorne", "Ordinary"). So a name is
treated as a non-brand type when every one of its content tokens is generic.

This keeps the term list as the single data-driven knob — extend `_GENERIC_TERMS`
to cover a newly-seen ingredient and the rule does the rest. Honesty discipline:
the filter only ever DROPS type-like names; it never invents or rewrites a brand,
and callers degrade to an empty list when nothing brand-like remains.
"""

from __future__ import annotations

import re
from typing import FrozenSet, List, Optional

# Single-token ingredient / actives / supplement names that are categories, not
# brands. Grouped for readability; the rule below only fires when EVERY content
# token of a name is in here (or is a category/form word or vitamin code), so a
# real brand that happens to contain one of these ("Vital Proteins", "Ancient
# Nutrition") is kept because its identity token is not generic.
_INGREDIENTS = frozenset({
    # minerals / electrolytes
    "magnesium", "calcium", "iron", "zinc", "potassium", "sodium", "selenium",
    "copper", "manganese", "chromium", "iodine", "phosphorus", "molybdenum",
    "boron", "electrolyte", "electrolytes",
    # botanicals / adaptogens / herbals
    "ashwagandha", "turmeric", "curcumin", "ginseng", "ginkgo", "echinacea",
    "elderberry", "spirulina", "chlorella", "maca", "rhodiola", "valerian",
    "ginger", "garlic", "moringa", "berberine", "quercetin", "resveratrol",
    "thistle", "palmetto", "bacopa", "reishi", "cordyceps",
    # supplement actives / aminos
    "creatine", "glutamine", "taurine", "carnitine", "glucosamine",
    "chondroitin", "msm", "coq10", "glutathione", "lutein", "lycopene",
    "melatonin", "biotin", "choline", "inositol", "lecithin", "theanine",
    "gaba", "tryptophan", "tyrosine", "arginine", "citrulline", "betaine",
    "collagen", "elastin", "keratin", "whey", "casein", "creatinine",
    "caffeine", "nootropic", "nootropics",
    # pro/pre/post-biotics + fiber
    "probiotic", "probiotics", "prebiotic", "prebiotics", "postbiotic",
    "postbiotics", "fiber", "fibre", "psyllium", "inulin",
    # omega / fatty acids
    "omega", "dha", "epa", "fish", "krill", "flaxseed",
    # B-family + named vitamins / coenzymes
    "niacin", "niacinamide", "folate", "folic", "riboflavin", "thiamine",
    "cobalamin", "pyridoxine", "ascorbate", "ascorbic", "tocopherol",
    "retinol", "retinal", "retinoid", "retinoids", "panthenol", "carotene",
    # skincare actives
    "hyaluronic", "ceramide", "ceramides", "peptide", "peptides", "salicylic",
    "glycolic", "lactic", "azelaic", "mandelic", "kojic", "arbutin",
    "tranexamic", "ferulic", "allantoin", "squalane", "squalene", "adapalene",
    "benzoyl", "bakuchiol", "centella", "cica", "snail", "mucin", "glycerin",
    "urea", "propolis", "panthenol", "madecassoside",
    # hair / body botanical oils + butters (an "X butter"/"X oil" is an
    # ingredient TYPE, not a brand — e.g. "Shea Butter", "Castor Oil")
    "shea", "cocoa", "mango", "murumuru", "cupuacu", "kokum", "argan",
    "jojoba", "castor", "coconut", "marula", "monoi", "baobab", "almond",
    "avocado", "hemp", "amla", "batana", "mongongo", "ucuuba", "abyssinian",
    "grapeseed", "rosehip", "sunflower", "olive", "rosemary", "chebe",
})

# Category / form words. On their own these never make a brand — they describe
# the dosage form or product class ("powder", "serum", "supplement", "acid").
_CATEGORY_FORM = frozenset({
    "supplement", "supplements", "supplementation", "vitamin", "vitamins",
    "multivitamin", "multivitamins", "mineral", "minerals", "protein",
    "proteins", "amino", "aminos", "acid", "acids", "oil", "oils", "extract",
    "extracts", "root", "powder", "powders", "capsule", "capsules", "tablet",
    "tablets", "gummy", "gummies", "softgel", "softgels", "drops", "complex",
    "blend", "formula", "nutrient", "nutrients", "nutrition", "serum", "cream",
    "creme", "moisturizer", "moisturiser", "cleanser", "toner", "essence",
    "sunscreen", "spf", "ampoule", "mask", "lotion", "gel", "balm",
    "butter", "butters", "pomade", "conditioner", "shampoo", "treatment",
    # mineral / ester forms ("magnesium glycinate", "vitamin c ascorbate")
    "glycinate", "citrate", "oxide", "bisglycinate", "malate", "threonate",
    "chloride", "sulfate", "sulphate", "carbonate", "gluconate", "picolinate",
    "monohydrate", "hcl", "chelate", "orotate", "taurate",
})

# Connectives ignored when judging whether a name is "all-generic" — they carry
# no brand identity ("Magnesium and Zinc", "Vitamin C with Collagen").
_CONNECTIVES = frozenset({"and", "with", "of", "the", "for", "plus", "in", "a"})

# A vitamin letter-code token: "D", "B12", "K2", "D3", "C". Letters a–k optionally
# followed by 1–2 digits. Used so "Vitamin D" / "Vitamin B12" read as all-generic.
_VITAMIN_CODE = re.compile(r"^[a-k]\d{0,2}$")


def _tokens(name: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def _is_generic_token(
    token: str,
    ingredient_tokens: FrozenSet[str],
    form_tokens: FrozenSet[str],
) -> bool:
    if token in ingredient_tokens or token in form_tokens:
        return True
    # vitamin codes (d, b12, k2) and bare numbers (omega "3", "6")
    if token.isdigit() or _VITAMIN_CODE.match(token):
        return True
    return False


def is_ingredient_or_category_type(
    name: str,
    *,
    ingredient_tokens: Optional[FrozenSet[str]] = None,
    form_tokens: Optional[FrozenSet[str]] = None,
) -> bool:
    """True when `name` is a generic ingredient / supplement / actives / product
    TYPE rather than a competitor brand — i.e. every content token is generic.

    Multi-word type names ('hyaluronic acid', 'magnesium glycinate', 'omega-3',
    and — for electronics — 'wireless earbuds', 'noise cancelling headphones')
    are caught because each of their tokens is generic; brands ('Thorne', 'The
    Ordinary', 'Bose', 'Shokz') survive because at least one token carries
    identity.

    `ingredient_tokens`/`form_tokens` default to the beauty sets (byte-identical
    to pre-vertical behavior). A caller passes a vertical profile's token sets to
    make the drop category-correct (e.g. drop 'wireless earbuds' for audio).
    """
    ing = _INGREDIENTS if ingredient_tokens is None else ingredient_tokens
    form = _CATEGORY_FORM if form_tokens is None else form_tokens
    content = [t for t in _tokens(name) if t not in _CONNECTIVES]
    if not content:
        return False  # connectives-only / empty — leave for the caller's checks
    if not any(t in ing or t in form for t in content):
        return False  # must mention at least one real ingredient/category word
    return all(_is_generic_token(t, ing, form) for t in content)


def filter_competitor_brands(
    names: List[str],
    *,
    ingredient_tokens: Optional[FrozenSet[str]] = None,
    form_tokens: Optional[FrozenSet[str]] = None,
) -> List[str]:
    """Keep only competitor-brand-like names, dropping ingredient/category types.
    Order-preserving; never rewrites a name. Returns [] when nothing remains."""
    return [
        n
        for n in names
        if not is_ingredient_or_category_type(
            str(n or ""), ingredient_tokens=ingredient_tokens, form_tokens=form_tokens
        )
    ]


# --- competitor-name normalization (dedup among OBSERVED names) ---------------
#
# Live Mojawa runs surfaced three dirt patterns in competitor panels:
#   * case duplicates            — "Shokz" and "SHOKZ" counted as two brands;
#   * digit-zero typo variants   — "H2O Audio" vs "H20 Audio" across panels;
#   * product-names-as-brands    — "Nank Runner Diver2 Pro" beside "Nank",
#                                  "Suunto Aqua" beside "Suunto".
# Split counts also UNDERSTATE competitor durability (_durable_competitor needs
# a name repeated >=2x — variants divide that signal).
#
# Honesty contract: this is DEDUP among OBSERVED names, never invention. The
# display form is always one of the observed variants (never synthesized), and
# a product-name collapses onto a brand only when that brand was independently
# observed in the same pool ("Nothing Ear (Open)" stays as-is when "Nothing"
# never appeared).

_CANON_STRIP_RE = re.compile(r"[^a-z0-9]+")


def canonical_competitor_key(name: object) -> str:
    """Case/punctuation-insensitive identity key for a competitor name.
    Digit zero folds to letter o so the classic "H20"-for-"H2O" typo variant
    lands on the same key (brands differing ONLY by 0-vs-o don't occur in
    practice; the typo does, in grounded answers)."""
    key = _CANON_STRIP_RE.sub("", str(name or "").lower())
    return key.replace("0", "o")


def _canonical_tokens(name: str) -> tuple:
    return tuple(
        t for t in (canonical_competitor_key(tok) for tok in str(name).split()) if t
    )


def _better_display(a: str, a_count: int, b: str, b_count: int) -> str:
    """Pick the better display variant among two OBSERVED spellings of the same
    brand: majority first; then not-ALL-CAPS (SHOKZ -> Shokz); then fewer
    digits (H20 -> H2O); then the shorter; then lexicographic (determinism)."""
    if a_count != b_count:
        return a if a_count > b_count else b
    a_caps = a.isupper() and len(a) > 1
    b_caps = b.isupper() and len(b) > 1
    if a_caps != b_caps:
        return b if a_caps else a
    a_digits = sum(ch.isdigit() for ch in a)
    b_digits = sum(ch.isdigit() for ch in b)
    if a_digits != b_digits:
        return a if a_digits < b_digits else b
    if len(a) != len(b):
        return a if len(a) < len(b) else b
    return min(a, b)


def canonicalize_competitor_counts(
    counts: "dict[str, int]",
) -> "tuple[dict[str, int], dict[str, str]]":
    """Collapse case/typo variants and product-names onto their observed brand.

    Returns ``(collapsed, alias_map)`` where ``collapsed`` maps the chosen
    display name -> summed count and ``alias_map`` maps every input name to
    its display name (identity for untouched names).
    """
    # Stage 1 — variant grouping by canonical key (case / punctuation / 0-vs-o).
    by_key: "dict[str, dict[str, int]]" = {}
    for name, count in (counts or {}).items():
        text = str(name or "").strip()
        if not text:
            continue
        bucket = by_key.setdefault(canonical_competitor_key(text), {})
        bucket[text] = bucket.get(text, 0) + int(count or 0)
    groups: "dict[str, dict]" = {}
    key_of_variant: "dict[str, str]" = {}
    for key, variants in by_key.items():
        display, display_count = None, 0
        for variant, count in variants.items():
            if display is None:
                display, display_count = variant, count
                continue
            chosen = _better_display(display, display_count, variant, count)
            if chosen != display:
                display, display_count = variant, count
        groups[key] = {"display": display, "count": sum(variants.values())}
        for variant in variants:
            key_of_variant[variant] = key

    # Stage 2 — product-name -> brand collapse, only onto an independently
    # OBSERVED brand: merge a group whose token sequence starts with another
    # group's full token sequence ("Suunto Aqua" -> "Suunto"; "H2O Audio Tri
    # Pro Multi Sport" -> "H2O Audio"). Longest observed prefix wins; short
    # (<3 char) prefixes never absorb (junk-key guard).
    token_index = {
        key: _canonical_tokens(info["display"]) for key, info in groups.items()
    }
    merged_into: "dict[str, str]" = {}
    for key, tokens in token_index.items():
        if len(tokens) < 2:
            continue
        best_parent, best_len = None, 0
        for parent_key, parent_tokens in token_index.items():
            if parent_key == key or not parent_tokens:
                continue
            if len(parent_tokens) >= len(tokens):
                continue
            if len("".join(parent_tokens)) < 3:
                continue
            if tokens[: len(parent_tokens)] == parent_tokens:
                if len(parent_tokens) > best_len:
                    best_parent, best_len = parent_key, len(parent_tokens)
        if best_parent:
            merged_into[key] = best_parent

    def _root(key: str) -> str:
        seen = set()
        while key in merged_into and key not in seen:
            seen.add(key)
            key = merged_into[key]
        return key

    collapsed: "dict[str, int]" = {}
    for key, info in groups.items():
        root_display = groups[_root(key)]["display"]
        collapsed[root_display] = collapsed.get(root_display, 0) + info["count"]
    alias_map = {
        variant: groups[_root(key)]["display"]
        for variant, key in key_of_variant.items()
    }
    return collapsed, alias_map


def normalize_competitor_list(names: List[str]) -> List[str]:
    """Order-preserving competitor-name dedup for plain lists (win-plan
    benchmarks): same collapse rules; the first-seen group keeps its slot."""
    cleaned = [str(n or "").strip() for n in names or []]
    cleaned = [n for n in cleaned if n]
    if not cleaned:
        return []
    counts: "dict[str, int]" = {}
    for n in cleaned:
        counts[n] = counts.get(n, 0) + 1
    _collapsed, alias_map = canonicalize_competitor_counts(counts)
    out: List[str] = []
    seen: set = set()
    for n in cleaned:
        display = alias_map.get(n, n)
        if display not in seen:
            seen.add(display)
            out.append(display)
    return out

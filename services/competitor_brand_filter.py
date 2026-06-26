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
from typing import List

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


def _is_generic_token(token: str) -> bool:
    if token in _INGREDIENTS or token in _CATEGORY_FORM:
        return True
    # vitamin codes (d, b12, k2) and bare numbers (omega "3", "6")
    if token.isdigit() or _VITAMIN_CODE.match(token):
        return True
    return False


def is_ingredient_or_category_type(name: str) -> bool:
    """True when `name` is a generic ingredient / supplement / actives TYPE
    rather than a competitor brand — i.e. every content token is generic.

    Multi-word ingredient names ('hyaluronic acid', 'magnesium glycinate',
    'vitamin b12', 'omega-3') are caught because each of their tokens is generic;
    brands ('Thorne', 'Vital Proteins', 'The Ordinary') survive because at least
    one token carries identity.
    """
    content = [t for t in _tokens(name) if t not in _CONNECTIVES]
    if not content:
        return False  # connectives-only / empty — leave for the caller's checks
    if not any(t in _INGREDIENTS or t in _CATEGORY_FORM for t in content):
        return False  # must mention at least one real ingredient/category word
    return all(_is_generic_token(t) for t in content)


def filter_competitor_brands(names: List[str]) -> List[str]:
    """Keep only competitor-brand-like names, dropping ingredient/category types.
    Order-preserving; never rewrites a name. Returns [] when nothing remains."""
    return [n for n in names if not is_ingredient_or_category_type(str(n or ""))]

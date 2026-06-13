"""Deterministic K-beauty enrichment: derive the decision-grade structure from
the raw content a brand provides on onboarding (title / description / INCI),
with no LLM and full provenance.

The eval showed the bottleneck: even profiled SKUs are shells -- concerns,
active_ingredients, and claims are empty -- because ingestion only fills them
when the merchant supplies STRUCTURED metadata, which almost no brand does. What
every product DOES have is a title, a description, and a raw INCI string. This
module bridges that gap deterministically:

  * extract_key_actives(raw_inci, ...) -- parse the INCI list and match it
    against a curated K-beauty key-actives dictionary. This is the highest-value,
    cheapest fill: it satisfies the `find` dimension's "key actives" requirement,
    and it is VERIFIED, not inferred -- the INCI list IS the authoritative
    source, so each active carries source="inci".
  * infer_concerns(category_kind, title, description) -- match a per-category
    FIT-concern vocabulary (dryness / brightening / sensitivity / ...). These are
    cosmetic FIT descriptors, never therapeutic conditions, so they stay
    claim-safe; they are inferred from marketing copy, so they carry
    source="text" (lower grade than INCI-verified actives).
  * enrich_beauty_record(...) -- the one call an onboarding enrichment job makes:
    actives + concerns + format + (haircare) formulation/cert flags, each tagged
    with its provenance, ready to write to the beauty_* tables.

Pure functions (regex + dictionaries only) -- unit-tested, reused by the
enrichment job and any backfill. Format / sulfate-silicone / vegan-cruelty-free
reuse the per-category attribute modules so there is one source of truth.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from services import haircare_attributes
from services.claim_safety import CATEGORY_HAIRCARE, CATEGORY_SKINCARE, CATEGORY_SUPPLEMENT
from services.skincare_attributes import (
    extract_format as extract_skincare_format,
    merge_concentration_into_actives,
)

SOURCE_INCI = "inci"
SOURCE_TEXT = "text"


# --- Curated K-beauty key actives -------------------------------------------
# Each active: a display label + the INCI names / synonyms that identify it.
# Patterns match case-insensitively as whole tokens/phrases inside the INCI list
# (or, as a fallback, the product text). Ordered roughly by how decision-relevant
# the active is for K-beauty fit. Extend deliberately -- a wrong match pollutes
# the `find` signal.
_KEY_ACTIVES: List[Dict[str, Any]] = [
    {"label": "Niacinamide", "patterns": [r"niacinamide", r"nicotinamide"]},
    {"label": "Centella Asiatica", "patterns": [r"\bcentella\b", r"\bcica\b", r"madecassoside", r"asiaticoside", r"madecassic\s+acid"]},
    {"label": "Snail Mucin", "patterns": [r"snail\s+secretion\s+filtrate", r"snail\s+mucin"]},
    {"label": "Hyaluronic Acid", "patterns": [r"hyaluronic\s+acid", r"sodium\s+hyaluronate", r"sodium\s+acetylated\s+hyaluronate"]},
    {"label": "Vitamin C", "patterns": [r"vitamin\s*c\b", r"ascorbic\s+acid", r"ascorbyl\s+glucoside", r"sodium\s+ascorbyl\s+phosphate", r"3-?o-?ethyl\s+ascorbic\s+acid", r"ethyl\s+ascorbic\s+acid", r"tetrahexyldecyl\s+ascorbate", r"ascorbyl\s+tetraisopalmitate"]},
    {"label": "Retinol", "patterns": [r"\bretinol\b", r"retinyl\s+palmitate", r"\bretinal\b", r"retinaldehyde"]},
    {"label": "Salicylic Acid (BHA)", "patterns": [r"salicylic\s+acid", r"betaine\s+salicylate"]},
    {"label": "AHA", "patterns": [r"glycolic\s+acid", r"lactic\s+acid", r"mandelic\s+acid"]},
    {"label": "Ceramides", "patterns": [r"ceramide\s+np", r"ceramide\s+ap", r"ceramide\s+eop", r"\bceramide\s*3\b", r"\bceramides?\b"]},
    {"label": "Peptides", "patterns": [r"palmitoyl\s+\w*peptide", r"copper\s+tripeptide", r"acetyl\s+hexapeptide", r"\bpeptides?\b"]},
    {"label": "Panthenol", "patterns": [r"\bpanthenol\b", r"provitamin\s+b5"]},
    {"label": "Adenosine", "patterns": [r"\badenosine\b"]},
    {"label": "Allantoin", "patterns": [r"\ballantoin\b"]},
    {"label": "Azelaic Acid", "patterns": [r"azelaic\s+acid"]},
    {"label": "Tranexamic Acid", "patterns": [r"tranexamic\s+acid"]},
    {"label": "Green Tea", "patterns": [r"camellia\s+sinensis", r"green\s+tea"]},
    {"label": "Propolis", "patterns": [r"\bpropolis\b"]},
    {"label": "Mugwort", "patterns": [r"artemisia\s+\w+\s+extract", r"\bmugwort\b"]},
    {"label": "Rice Extract", "patterns": [r"oryza\s+sativa", r"\brice\s+(?:extract|ferment|water|bran)"]},
    {"label": "Ginseng", "patterns": [r"panax\s+ginseng", r"\bginseng\b"]},
    # Haircare-leaning actives.
    {"label": "Hydrolyzed Keratin", "patterns": [r"hydrolyzed\s+keratin", r"\bkeratin\b"]},
    {"label": "Hydrolyzed Rice Protein", "patterns": [r"hydrolyzed\s+rice\s+protein"]},
    {"label": "Argan Oil", "patterns": [r"argania\s+spinosa", r"argan\s+oil"]},
    {"label": "Biotin", "patterns": [r"\bbiotin\b"]},
    {"label": "Collagen", "patterns": [r"hydrolyzed\s+collagen", r"\bcollagen\b"]},
]

# Compile once.
for _a in _KEY_ACTIVES:
    _a["_re"] = re.compile("|".join(_a["patterns"]), re.IGNORECASE)

_INCI_SPLIT_RE = re.compile(r"[,\n;•·•/]+")
# Strip trailing concentration / parenthetical notes from an INCI token.
_INCI_CLEAN_RE = re.compile(r"\s*\([^)]*\)|\s*\d+(?:\.\d+)?\s*%.*$")


def parse_inci(raw_inci: Optional[str]) -> List[str]:
    """Split a raw INCI string into normalized ingredient tokens."""
    if not raw_inci:
        return []
    tokens = []
    for part in _INCI_SPLIT_RE.split(str(raw_inci)):
        cleaned = _INCI_CLEAN_RE.sub("", part).strip().strip(".")
        if cleaned and len(cleaned) > 1:
            tokens.append(cleaned)
    return tokens


def extract_key_actives(
    raw_inci: Optional[str],
    *,
    concentration_notes: Any = None,
    fallback_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Identify curated key actives present in the INCI list (verified) or, when
    no INCI is given, in the product text (fallback). Returns active dicts shaped
    for active_ingredients_json: {label, source, concentration?}. Order follows
    the curated priority; each active appears once.

    raw_inci wins -- it is the authoritative source (source="inci"). fallback_text
    is only consulted when raw_inci yields nothing, and is marked source="text".
    """
    tokens = parse_inci(raw_inci)
    blob = " ".join(tokens)
    source = SOURCE_INCI
    if not blob and fallback_text:
        blob = str(fallback_text)
        source = SOURCE_TEXT
    if not blob.strip():
        return []
    actives: List[Dict[str, Any]] = []
    for active in _KEY_ACTIVES:
        if active["_re"].search(blob):
            actives.append({"label": active["label"], "source": source})
    if concentration_notes:
        actives = merge_concentration_into_actives(actives, concentration_notes)
    return actives


# --- Per-category FIT concern vocabulary ------------------------------------
# Concern label -> trigger terms. These are cosmetic FIT descriptors (skin/hair
# type and goal), NOT therapeutic conditions: "acne-prone" is a fit tag, whereas
# "treats acne" is a drug claim handled by services.claim_screening. Keep this
# line clean so concern inference never asserts a medical claim.
_CONCERN_VOCAB: Dict[str, List[tuple]] = {
    CATEGORY_SKINCARE: [
        ("dryness", [r"\bdry\b", r"dehydrat", r"hydrat", r"moistur"]),
        ("dullness", [r"\bdull", r"brighten", r"radian", r"\bglow", r"luminous"]),
        ("acne-prone", [r"acne-?prone", r"blemish", r"breakout", r"\bpimple"]),
        ("oiliness", [r"\boily\b", r"oil[\s-]?control", r"excess\s+sebum", r"\bshine\b"]),
        ("sensitivity", [r"sensitive", r"soothing", r"\bcalm", r"redness", r"irritat"]),
        ("aging", [r"anti-?aging", r"\bwrinkle", r"fine\s+lines?", r"\bfirm", r"elasticity", r"sagging"]),
        ("pores", [r"\bpores?\b", r"enlarged\s+pores"]),
        ("hyperpigmentation", [r"dark\s+spots?", r"hyperpigment", r"pigmentation", r"even\s+(?:skin\s+)?tone", r"discolor"]),
        ("uneven texture", [r"\btexture", r"\bsmooth", r"exfoliat", r"\brough"]),
    ],
    CATEGORY_HAIRCARE: [
        ("dryness", [r"\bdry\b", r"hydrat", r"moistur"]),
        ("damage", [r"\bdamage", r"\brepair", r"breakage", r"split\s+ends?", r"strengthen", r"\bbond"]),
        ("color-treated", [r"color-?treated", r"colou?r[\s-]?safe", r"\bdyed\b", r"bleach"]),
        ("scalp", [r"\bscalp\b", r"flak", r"itchy\s+scalp"]),
        ("frizz", [r"\bfrizz", r"\bsmooth", r"unmanageab"]),
        ("oiliness", [r"\boily\b", r"\bgreasy", r"oil[\s-]?control"]),
        ("volume", [r"\bvolume", r"\bthinning\b", r"fine\s+hair", r"\blimp\b", r"flat\s+hair"]),
    ],
    CATEGORY_SUPPLEMENT: [
        # Supplements get their own structured attributes in the supplement
        # module; no FIT-concern inference here (claim-safety is strictest).
    ],
}
for _ck, _entries in _CONCERN_VOCAB.items():
    for _i, (_label, _terms) in enumerate(_entries):
        _entries[_i] = (_label, re.compile("|".join(_terms), re.IGNORECASE))


def infer_concerns(category_kind: Optional[str], *texts: Any) -> List[str]:
    """Infer cosmetic FIT concerns for a category from product text. Returns the
    matched concern labels in vocabulary order, de-duplicated. Empty for
    categories without a concern vocabulary (e.g. supplements)."""
    vocab = _CONCERN_VOCAB.get(category_kind or "")
    if not vocab:
        return []
    blob = " ".join(str(t or "") for t in texts)
    if not blob.strip():
        return []
    return [label for label, pattern in vocab if pattern.search(blob)]


def enrich_beauty_record(
    category_kind: Optional[str],
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    raw_inci: Optional[str] = None,
    concentration_notes: Any = None,
    product_type: Optional[str] = None,
    category_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Derive the decision-grade structure from raw content -- the single call an
    onboarding enrichment job makes. Returns the fields to write to the beauty_*
    tables plus a provenance block. category_kind-aware: format and (haircare)
    formulation/cert flags come from the per-category attribute modules.
    """
    text = " ".join(s for s in (title, description) if s)
    actives = extract_key_actives(
        raw_inci, concentration_notes=concentration_notes, fallback_text=text
    )
    concerns = infer_concerns(category_kind, title, description)

    result: Dict[str, Any] = {
        "category_kind": category_kind,
        "active_ingredients": actives,
        "concerns": concerns,
        "provenance": {
            # actives are verified iff they came from the INCI list.
            "active_ingredients": (
                SOURCE_INCI if (actives and actives[0].get("source") == SOURCE_INCI) else SOURCE_TEXT
            )
            if actives
            else None,
            "concerns": SOURCE_TEXT if concerns else None,
        },
    }

    if category_kind == CATEGORY_SKINCARE:
        result["skincare_format"] = extract_skincare_format(title, product_type, category_path)
    elif category_kind == CATEGORY_HAIRCARE:
        result["haircare_format"] = haircare_attributes.extract_format(title, product_type, category_path)
        result["sulfate_free"] = haircare_attributes.detect_sulfate_free(text, product_type)
        result["silicone_free"] = haircare_attributes.detect_silicone_free(text, product_type)
        result["vegan_status"] = haircare_attributes.classify_vegan(None, text, product_type)
        result["cruelty_free_status"] = haircare_attributes.classify_cruelty_free(None, text, product_type)

    return result

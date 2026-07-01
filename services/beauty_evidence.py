"""Canonical-first evidence: ingredient-mechanism substantiated benefit claims.

Per ADR-001, the canonical product record's claims are Pivota-owned and must be
cited + claim-safe so an agent can JUSTIFY a recommendation. This derives
SUBSTANTIATED benefit claims honestly: a benefit is substantiated only when the
product BOTH claims it (in the canonical text) AND carries a key active known to
deliver it -- the ingredient mechanism. The claim text is generated from a
controlled, appearance-based cosmetic-safe vocabulary (so emitted claims are
cosmetic by construction), and every claim is still run through the cosmetic-vs-
drug screener as a guard. No LLM.

This is the K-beauty ingredient-authority moat: native models assert benefits
ungrounded; Pivota ties each surfaced benefit to a verified ingredient and cites
it (source_type="ingredient_mechanism", source_ref=the active(s)).

Operates on the canonical actives + text, not reseller marketing copy -- when the
brand-official sourcing engine lands (ADR-001 action #2) it feeds better text and
canonical INCI; this substantiation engine is unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from models.catalog import EvidenceProfile, ProductClaim
from services.claim_safety import REVIEW_OBSERVED, SUBSTANTIATION_SUBSTANTIATED
from services.claim_screening import STATUS_DRUG_CLAIM, STATUS_OTC_DRUG, screen_claim

SOURCE_TYPE_INGREDIENT = "ingredient_mechanism"
EVIDENCE_GRADE_INGREDIENT = "ingredient_inference"

# Canonical benefit -> (trigger terms in product text, cosmetic-safe claim text).
# Claim phrasings are appearance-based ("the look of ...") so they stay cosmetic.
_BENEFIT_VOCAB: Dict[str, tuple] = {
    "brightening": (
        [r"brighten", r"radian", r"\bglow", r"even.{0,6}tone", r"\bdull", r"dark\s+spots?", r"luminous", r"complexion"],
        "Helps brighten the look of dull, uneven skin tone",
    ),
    "hydrating": (
        [r"hydrat", r"moistur", r"\bdewy", r"\bplump", r"\bdry\b", r"dehydrat"],
        "Hydrates and helps replenish the skin's moisture",
    ),
    "soothing": (
        [r"sooth", r"\bcalm", r"comfort", r"redness", r"sensitiv", r"irritat"],
        "Soothes the appearance of redness and sensitivity",
    ),
    "anti-aging": (
        [r"anti-?aging", r"\bwrinkle", r"fine\s+lines?", r"\bfirm", r"elasticity", r"\blift", r"youthful"],
        "Helps improve the appearance of fine lines and firmness",
    ),
    "barrier-support": (
        [r"barrier", r"strengthen.{0,10}skin", r"resilien", r"protect.{0,10}skin"],
        "Supports the look of a healthy, resilient skin barrier",
    ),
    "exfoliating": (
        [r"exfoliat", r"smooth.{0,8}texture", r"refine", r"resurfac", r"renew"],
        "Helps smooth and refine the skin's texture",
    ),
    "oil-control": (
        [r"oil[\s-]?control", r"mattif", r"\bsebum", r"excess\s+shine", r"\bpores?\b"],
        "Helps control excess shine and minimize the look of pores",
    ),
    # Haircare-leaning benefits.
    "nourishing": (
        [r"nourish", r"replenish", r"condition", r"\bsoft", r"dry\s+hair"],
        "Nourishes and helps condition for softer-looking hair",
    ),
    "strengthening": (
        [r"strengthen", r"\brepair", r"breakage", r"\bbond", r"fortif", r"damaged?\s+hair"],
        "Helps strengthen and reduce the look of breakage",
    ),
    "smoothing": (
        [r"\bsmooth", r"\bfrizz", r"\bsleek", r"manageab"],
        "Smooths and helps tame the look of frizz",
    ),
    # Antioxidant defense — cosmetic-safe environmental/free-radical framing
    # (never therapeutic; still screened by screen_claim below).
    "antioxidant": (
        [r"antioxidant", r"free[\s-]?radical", r"environmental", r"\bpollution", r"oxidative", r"\bdefend"],
        "Helps defend the look of skin against environmental stressors",
    ),
}
_BENEFIT_RE = {b: re.compile("|".join(terms), re.IGNORECASE) for b, (terms, _) in _BENEFIT_VOCAB.items()}

# Key active (label from beauty_enrichment._KEY_ACTIVES) -> benefits it
# substantiates. The K-beauty ingredient-authority map. Keep labels in sync with
# beauty_enrichment (a unit test guards against drift).
_ACTIVE_BENEFITS: Dict[str, tuple] = {
    "Niacinamide": ("brightening", "barrier-support", "oil-control"),
    "Vitamin C": ("brightening", "anti-aging"),
    "Tranexamic Acid": ("brightening",),
    "Azelaic Acid": ("brightening", "oil-control"),
    "AHA": ("exfoliating", "brightening"),
    "Salicylic Acid (BHA)": ("exfoliating", "oil-control"),
    "Hyaluronic Acid": ("hydrating",),
    "Panthenol": ("hydrating", "soothing", "barrier-support", "nourishing"),
    "Snail Mucin": ("hydrating", "soothing", "barrier-support"),
    "Centella Asiatica": ("soothing", "barrier-support"),
    "Allantoin": ("soothing",),
    "Green Tea": ("soothing", "oil-control"),
    "Propolis": ("soothing", "barrier-support"),
    "Mugwort": ("soothing",),
    "Ceramides": ("barrier-support", "hydrating"),
    "Retinol": ("anti-aging",),
    "Peptides": ("anti-aging", "barrier-support", "strengthening"),
    "Adenosine": ("anti-aging",),
    "Ginseng": ("anti-aging",),
    "Rice Extract": ("brightening",),
    "Collagen": ("hydrating", "anti-aging"),
    "Hydrolyzed Keratin": ("strengthening", "smoothing"),
    "Hydrolyzed Rice Protein": ("strengthening",),
    "Argan Oil": ("nourishing", "smoothing", "hydrating"),
    "Biotin": ("strengthening",),
    # Expansion — labels MUST match beauty_enrichment._KEY_ACTIVES (drift test).
    # Benefits mapped conservatively to well-established, cosmetic-safe outcomes.
    "Vitamin E": ("antioxidant", "barrier-support"),
    "Squalane": ("hydrating", "barrier-support"),
    "Shea Butter": ("nourishing", "hydrating"),
    "Jojoba Oil": ("nourishing", "hydrating"),
    "Trehalose": ("hydrating",),
    "Sunflower Oil": ("nourishing",),
    "Heartleaf": ("soothing", "oil-control"),
    "Beta-Glucan": ("soothing", "hydrating"),
    "Licorice": ("brightening", "soothing"),
    "Galactomyces": ("brightening", "barrier-support"),
    "Arbutin": ("brightening",),
    "Bakuchiol": ("anti-aging",),
}


def _active_labels(active_ingredients: Any) -> List[str]:
    out = []
    for a in active_ingredients or []:
        if isinstance(a, dict) and a.get("label"):
            out.append(str(a["label"]))
    return out


def derive_substantiated_claims(
    category_kind: Optional[str],
    active_ingredients: Any,
    *texts: Any,
) -> List[ProductClaim]:
    """Benefit claims that are BOTH asserted in text AND backed by a key active.

    Returns provenance-backed ProductClaims (source_type=ingredient_mechanism,
    source_ref=the supporting active(s), substantiation=substantiated). A benefit
    claimed without a supporting active is omitted (verified-only); the cosmetic-
    safe phrasing is still screened, so a drug/OTC outcome is never emitted.
    """
    actives = _active_labels(active_ingredients)
    if not actives:
        return []
    blob = " ".join(str(t or "") for t in texts)
    if not blob.strip():
        return []

    claims: List[ProductClaim] = []
    for benefit, (_terms, claim_text) in _BENEFIT_VOCAB.items():
        if not _BENEFIT_RE[benefit].search(blob):
            continue  # the product doesn't claim this benefit
        supporting = [a for a in actives if benefit in _ACTIVE_BENEFITS.get(a, ())]
        if not supporting:
            continue  # claimed but not ingredient-substantiated -> not surfaced
        screening = screen_claim(category_kind, claim_text)
        if screening.status in (STATUS_DRUG_CLAIM, STATUS_OTC_DRUG):
            continue  # guard: never surface a drug/OTC claim
        claims.append(
            ProductClaim(
                claim_text=claim_text,
                source_ref=", ".join(dict.fromkeys(supporting)),
                source_type=SOURCE_TYPE_INGREDIENT,
                evidence_grade=EVIDENCE_GRADE_INGREDIENT,
                substantiation_status=SUBSTANTIATION_SUBSTANTIATED,
            )
        )
    return claims


def build_evidence_profile(
    category_kind: Optional[str],
    active_ingredients: Any,
    *texts: Any,
    review_state: str = REVIEW_OBSERVED,
) -> Optional[EvidenceProfile]:
    """Assemble an EvidenceProfile of substantiated claims, or None when none can
    be substantiated (no claim to make)."""
    claims = derive_substantiated_claims(category_kind, active_ingredients, *texts)
    if not claims:
        return None
    return EvidenceProfile(claims=claims, review_state=review_state)

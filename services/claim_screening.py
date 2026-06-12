"""Cosmetic-vs-drug claim screening (K-beauty skincare + haircare modules).

A cosmetic may claim to affect *appearance*; a claim to treat a disease or alter
the body's structure/function makes the product an unapproved drug (FDA
misbranding). This module screens a claim string and classifies it, so the
authoring pipeline can set an honest substantiation_status and the serving gate
can keep drug claims off the agent surface.

It plugs into the claim-safety framework (services.claim_safety): the framework
owns the claim/disclaimer SCHEMA; this owns the per-category SCREENING rules.
Skincare and haircare are implemented (cosmetic-vs-drug); haircare adds its own
drug claims (hair-growth/loss = minoxidil-class, anti-dandruff = OTC drug).
Supplements get their stricter disease-claim rules in the supplement module.

Conservative by design: clear therapeutic/structure-function claims are blocked,
SPF is flagged as an OTC drug, and borderline condition wording without an
"appearance of" safe harbor is sent to review rather than silently allowed. Pure
functions (regex only) -- unit-testable, reused by authoring + the gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from services.claim_safety import (
    CATEGORY_HAIRCARE,
    CATEGORY_SKINCARE,
    SUBSTANTIATION_FLAGGED,
    SUBSTANTIATION_REJECTED,
)

# Screening outcomes.
STATUS_COSMETIC_OK = "cosmetic_ok"
STATUS_DRUG_CLAIM = "drug_claim"
STATUS_OTC_DRUG = "otc_drug"
STATUS_REVIEW = "review_required"


@dataclass(frozen=True)
class ClaimScreening:
    status: str
    rule_id: str
    reason: str


# Skin diseases / medical conditions. Treating these is a drug claim.
_CONDITION = (
    r"(?:acne|eczema|psoriasis|rosacea|dermatitis|melasma|warts?|"
    r"fungus|fungal|infections?|inflammation|wounds?)"
)

# SPF / sunscreen products are OTC drugs, not cosmetics.
_OTC_SUNSCREEN_RE = re.compile(
    r"\b(?:spf\s*\d+|broad[\s-]?spectrum|sunscreens?|sun\s*protection|uva?\s*/?\s*uvb)\b",
    re.IGNORECASE,
)

# Hard therapeutic verbs targeting a condition -> drug claim, no safe harbor.
_HARD_DRUG_RE = re.compile(
    r"\b(?:treats?|cures?|heals?|prevents?|eliminates?|clears?\s+up|"
    r"gets?\s+rid\s+of|fights?)\b[^.]*?\b" + _CONDITION + r"\b",
    re.IGNORECASE,
)

# Structure/function or therapeutic claims -> drug claim on a cosmetic.
_STRUCTURE_FUNCTION_RE = re.compile(
    r"\b(?:repairs?\s+(?:the\s+)?skin\s+barrier|stimulat\w+\s+collagen|"
    r"boosts?\s+collagen(?:\s+production)?|rebuilds?\s+collagen|regenerat\w+\s+skin|"
    r"reverses?\s+(?:the\s+)?(?:signs?\s+of\s+)?aging|anti[\s-]?inflammatory|"
    r"reduces?\s+inflammation|antibacterial|antimicrobial|antifungal|"
    r"heals?\s+(?:the\s+)?skin)\b",
    re.IGNORECASE,
)

# Soft verbs touching a condition -> needs review UNLESS an "appearance" safe
# harbor is present.
_SOFT_VERB_CONDITION_RE = re.compile(
    r"\b(?:reduces?|minimi[sz]es?|improves?|diminish\w+|fades?)\b[^.]*?\b"
    + _CONDITION
    + r"\b",
    re.IGNORECASE,
)

# Cosmetic safe harbor: claims about the look/appearance.
_APPEARANCE_RE = re.compile(r"\b(?:appearance|look)\s+of\b|\bappears?\b", re.IGNORECASE)

# Unambiguous drug-action terms (apply across cosmetic categories).
_GENERIC_DRUG_TERM_RE = re.compile(
    r"\b(?:antibacterial|antimicrobial|antifungal)\b", re.IGNORECASE
)

# --- Haircare-specific drug claims ---
# Hair growth / anti-hair-loss = minoxidil-class drug claims.
_HAIR_LOSS_GROWTH_RE = re.compile(
    r"\b(?:"
    r"re-?grow(?:s|th|ing)?\s+(?:new\s+)?hair|hair\s+re-?growth|"
    r"(?:treats?|prevents?|stops?|reverses?)\s+hair\s*loss|hair\s*loss\s+treatment|"
    r"(?:promotes?|stimulat\w+|boosts?|encourages?)\s+(?:new\s+)?hair\s+growth|"
    r"grows?\s+(?:new|thicker|longer)\s+hair|"
    r"prevents?\s+(?:balding|baldness|thinning)|"
    r"minoxidil"
    r")\b",
    re.IGNORECASE,
)

# Anti-dandruff = an OTC drug (zinc pyrithione / ketoconazole / selenium sulfide).
_ANTI_DANDRUFF_RE = re.compile(
    r"\b(?:anti[\s-]?dandruff|(?:treats?|fights?|eliminates?|controls?|removes?)\s+dandruff|"
    r"dandruff\s+(?:control|treatment|relief))\b",
    re.IGNORECASE,
)


def _screen_cosmetic(text: str) -> ClaimScreening:
    """Shared cosmetic-vs-drug screen (skincare today; haircare reuses it)."""
    if _OTC_SUNSCREEN_RE.search(text):
        return ClaimScreening(
            STATUS_OTC_DRUG,
            "otc_sunscreen",
            "SPF/sunscreen is an OTC drug and needs OTC-drug handling, not a cosmetic claim",
        )
    if _HARD_DRUG_RE.search(text):
        return ClaimScreening(
            STATUS_DRUG_CLAIM,
            "therapeutic_condition",
            "claim to treat/cure/prevent a skin condition is a drug claim",
        )
    if _STRUCTURE_FUNCTION_RE.search(text):
        return ClaimScreening(
            STATUS_DRUG_CLAIM,
            "structure_function",
            "structure/function or therapeutic claim is a drug claim on a cosmetic",
        )
    if _SOFT_VERB_CONDITION_RE.search(text) and not _APPEARANCE_RE.search(text):
        return ClaimScreening(
            STATUS_REVIEW,
            "condition_without_appearance",
            "claim about a skin condition without 'appearance of' wording needs cosmetic-vs-drug review",
        )
    return ClaimScreening(STATUS_COSMETIC_OK, "cosmetic_appearance", "cosmetic appearance claim")


def _screen_haircare(text: str) -> ClaimScreening:
    """Cosmetic-vs-drug screen for haircare. "cleanses / adds shine /
    strengthens the appearance" is fine; hair-growth/loss and anti-dandruff are
    drug claims."""
    if _HAIR_LOSS_GROWTH_RE.search(text):
        return ClaimScreening(
            STATUS_DRUG_CLAIM,
            "hair_growth_loss",
            "hair-growth or anti-hair-loss claim is a drug claim (minoxidil-class)",
        )
    if _ANTI_DANDRUFF_RE.search(text):
        return ClaimScreening(
            STATUS_OTC_DRUG,
            "anti_dandruff",
            "anti-dandruff is an OTC drug and needs OTC-drug handling, not a cosmetic claim",
        )
    if _GENERIC_DRUG_TERM_RE.search(text):
        return ClaimScreening(
            STATUS_DRUG_CLAIM,
            "drug_action_term",
            "antibacterial/antimicrobial/antifungal is a drug claim",
        )
    if _HARD_DRUG_RE.search(text):
        return ClaimScreening(
            STATUS_DRUG_CLAIM,
            "therapeutic_condition",
            "claim to treat/cure a scalp condition is a drug claim",
        )
    if _SOFT_VERB_CONDITION_RE.search(text) and not _APPEARANCE_RE.search(text):
        return ClaimScreening(
            STATUS_REVIEW,
            "condition_without_appearance",
            "claim about a scalp condition without 'appearance of' wording needs cosmetic-vs-drug review",
        )
    return ClaimScreening(STATUS_COSMETIC_OK, "cosmetic_appearance", "cosmetic appearance claim")


def screen_claim(category_kind: Optional[str], claim_text: Optional[str]) -> ClaimScreening:
    """Screen a single claim for its category. Categories without a screening
    ruleset yet return cosmetic_ok / not_screened (their modules add rules)."""
    text = (claim_text or "").strip()
    if not text:
        return ClaimScreening(STATUS_COSMETIC_OK, "empty", "empty claim")
    if category_kind == CATEGORY_SKINCARE:
        return _screen_cosmetic(text)
    if category_kind == CATEGORY_HAIRCARE:
        return _screen_haircare(text)
    return ClaimScreening(
        STATUS_COSMETIC_OK, "not_screened", f"no screening rules for category {category_kind!r}"
    )


def screen_claims(
    category_kind: Optional[str], claim_texts: List[str]
) -> List[Tuple[str, ClaimScreening]]:
    """Screen many claims; returns (claim_text, screening) pairs."""
    return [(t, screen_claim(category_kind, t)) for t in (claim_texts or [])]


def substantiation_status_for_screening(screening: ClaimScreening) -> Optional[str]:
    """Map a screening outcome onto a claim substantiation_status, or None to
    leave the existing status untouched (cosmetic-safe claims)."""
    if screening.status == STATUS_DRUG_CLAIM:
        # A drug claim can't be substantiated as a cosmetic claim -> reject.
        return SUBSTANTIATION_REJECTED
    if screening.status in (STATUS_OTC_DRUG, STATUS_REVIEW):
        return SUBSTANTIATION_FLAGGED
    return None

"""Claim provenance + disclaimer framework (K-beauty common-core hardening).

The agent-decision-grade data contract requires every benefit a product asserts
to be a *provenance-backed claim* (traceable to a source, with a substantiation
status) and any legally required disclaimers to be present -- so an agent can
justify a recommendation with cited, claim-safe evidence.

This module is the COMMON-CORE framework:
  * the substantiation / evidence-grade / source-type vocabularies,
  * `normalize_claims` -- coerce raw claim strings/dicts into structured
    ProductClaim objects with an honest default substantiation_status, and
  * `required_disclaimers_for_category` -- the disclaimer registry, seeded with
    the one disclaimer the contract names explicitly: the mandatory FDA/DSHEA
    supplement disclaimer.

The per-category DRUG-CLAIM SCREENING rules (cosmetic-vs-drug for skincare /
haircare, disease-claim blocks for supplements) are category-module concerns and
land with those PRs; this framework gives them a place to plug in. Pure
functions (no DB / no I/O) so they unit-test cleanly and are reused by the
authoring writer, the read path, and PR4's serving gate.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from models.catalog import ProductClaim, RequiredDisclaimer

# Honest defaults: a claim is "unverified" until a source substantiates it.
SUBSTANTIATION_UNVERIFIED = "unverified"
SUBSTANTIATION_SUBSTANTIATED = "substantiated"
SUBSTANTIATION_FLAGGED = "flagged"
SUBSTANTIATION_REJECTED = "rejected"
SUBSTANTIATION_STATUSES = frozenset(
    {
        SUBSTANTIATION_UNVERIFIED,
        SUBSTANTIATION_SUBSTANTIATED,
        SUBSTANTIATION_FLAGGED,
        SUBSTANTIATION_REJECTED,
    }
)

# review_state for an evidence_profile as a whole.
REVIEW_OBSERVED = "observed"
REVIEW_REVIEWED = "reviewed"
REVIEW_FLAGGED = "flagged"

# Category kinds the contract recognizes.
CATEGORY_SKINCARE = "skincare"
CATEGORY_HAIRCARE = "haircare"
CATEGORY_SUPPLEMENT = "supplement"

# The mandatory FDA/DSHEA structure-function disclaimer for ingestible
# supplements. Verbatim regulatory text -- do not paraphrase.
FDA_DSHEA_DISCLAIMER_CODE = "fda_dshea_supplement"
FDA_DSHEA_DISCLAIMER_TEXT = (
    "This statement has not been evaluated by the Food and Drug Administration. "
    "This product is not intended to diagnose, treat, cure, or prevent any disease."
)


def required_disclaimers_for_category(
    category_kind: Optional[str],
) -> List[RequiredDisclaimer]:
    """Mandatory disclaimers for a category_kind.

    Supplements carry the FDA/DSHEA disclaimer. Skincare/haircare carry none by
    default here -- their cosmetic-vs-drug guards arrive with the category
    modules. Returns a fresh list each call (safe to mutate).
    """
    if category_kind == CATEGORY_SUPPLEMENT:
        return [
            RequiredDisclaimer(
                code=FDA_DSHEA_DISCLAIMER_CODE,
                text=FDA_DSHEA_DISCLAIMER_TEXT,
                applies_to=CATEGORY_SUPPLEMENT,
            )
        ]
    return []


def ensure_category_disclaimers(
    authored: Any,
    category_kind: Optional[str],
) -> Optional[List[Dict[str, Any]]]:
    """Floor-safety merge: guarantee the category-mandatory disclaimers ride
    along with any authored ones, deduped by ``code`` (authored wins).

    A supplement must carry the FDA/DSHEA disclaimer even when the merchant
    authored none -- and even when they authored *other* disclaimers but not
    that one. Returns the authored value unchanged when the category mandates
    nothing, so it is a no-op for non-supplement rows (``None`` in -> ``None``
    out). Accepts authored items as dicts or ``RequiredDisclaimer`` objects.
    """
    mandatory = required_disclaimers_for_category(category_kind)
    if not mandatory:
        return authored

    out: List[Dict[str, Any]] = []
    seen: set = set()

    def _add(items: Any) -> None:
        for item in items or []:
            if isinstance(item, RequiredDisclaimer):
                item = {"code": item.code, "text": item.text, "applies_to": item.applies_to}
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip().lower()
            if code and code in seen:
                continue
            if code:
                seen.add(code)
            out.append(item)

    # Authored first -> an authored disclaimer of the same code takes precedence
    # over the canned mandatory text (a merchant may word it more specifically).
    _add(authored if isinstance(authored, list) else [])
    _add(mandatory)
    return out or None


def _coerce_claim(raw: Any) -> Optional[ProductClaim]:
    """One raw claim (a plain string or a dict) -> a ProductClaim, or None."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return ProductClaim(claim_text=text, substantiation_status=SUBSTANTIATION_UNVERIFIED)
    if isinstance(raw, dict):
        text = str(raw.get("claim_text") or raw.get("claim") or "").strip()
        if not text:
            return None
        status = str(raw.get("substantiation_status") or "").strip().lower()
        if status not in SUBSTANTIATION_STATUSES:
            status = SUBSTANTIATION_UNVERIFIED
        return ProductClaim(
            claim_text=text,
            source_ref=(str(raw.get("source_ref")).strip() or None) if raw.get("source_ref") else None,
            source_type=(str(raw.get("source_type")).strip() or None) if raw.get("source_type") else None,
            evidence_grade=(str(raw.get("evidence_grade")).strip() or None) if raw.get("evidence_grade") else None,
            substantiation_status=status,
        )
    return None


def normalize_claims(raw_claims: Any) -> List[ProductClaim]:
    """Coerce a raw claims payload (legacy flat strings or structured dicts)
    into a list of ProductClaim. Drops empties; defaults status to unverified.
    """
    if not isinstance(raw_claims, list):
        return []
    out: List[ProductClaim] = []
    for raw in raw_claims:
        claim = _coerce_claim(raw)
        if claim is not None:
            out.append(claim)
    return out


def substantiated_claims(evidence_profile: Any) -> List[Dict[str, Any]]:
    """Serve gate: return ONLY substantiated claims from an evidence_profile
    (a dict with a "claims" list, a raw claims list, or a JSON string) as plain
    dicts safe to emit to agents.

    Unverified / flagged / rejected claims are dropped — so an unsubstantiated
    merchant assertion can improve PDP copy but is never served to an agent as a
    grounded, citable claim. Pure; no I/O. This is the single chokepoint every
    agent-facing surface (PDP API, public PDP/JSON-LD, MCP, ACP) routes through.
    """
    raw: Any = evidence_profile
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        raw = raw.get("claims")
    out: List[Dict[str, Any]] = []
    for claim in normalize_claims(raw):
        if claim.substantiation_status != SUBSTANTIATION_SUBSTANTIATED:
            continue
        out.append(
            {
                "claim_text": claim.claim_text,
                "source_ref": claim.source_ref,
                "source_type": claim.source_type,
                "evidence_grade": claim.evidence_grade,
            }
        )
    return out

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
from ipaddress import ip_address
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit

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

# These source labels can be supplied through the merchant evidence endpoint.
# Only a claim-level review from a trusted writer can authorize publication.
# Legacy rows may have both a grade and a merchant-supplied profile review_state.
MERCHANT_EVIDENCE_SOURCE_TYPES = frozenset({
    "merchant_lab_report", "third_party_test", "certification",
    "third_party_review", "editorial_press", "merchant_positioning",
})
REVIEWED_MERCHANT_EVIDENCE_SOURCE_TYPES = (
    MERCHANT_EVIDENCE_SOURCE_TYPES - {"merchant_positioning"}
) | {"brand_statement", "retailer_listing"}
_PRIVATE_CITATION_QUERY = re.compile(r"token|password|secret|authorization|api.?key", re.IGNORECASE)


def review_source_url(value: Any) -> Optional[str]:
    """A reviewer-provided web citation, excluding credentials and non-web refs."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if len(url) > 2000 or "\\" in url or any(char.isspace() or ord(char) < 32 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".")
        if (parsed.scheme == "https" and "." in host
                and not host.endswith((".local", ".internal", ".localhost"))
                and parsed.username is None and parsed.password is None and not parsed.fragment
                and not any(_PRIVATE_CITATION_QUERY.search(name)
                            for name, _ in parse_qsl(parsed.query, keep_blank_values=True))):
            try:
                return url if ip_address(host).is_global else None
            except ValueError:
                return url
    except ValueError:
        pass
    return None


def has_verified_merchant_evidence(claim: Dict[str, Any]) -> bool:
    return (claim.get("review_status") == "verified"
            and bool(str(claim.get("reviewed_by") or "").strip())
            and claim.get("evidence_grade") in ("a", "b", "c")
            and str(claim.get("review_source_type") or "") in REVIEWED_MERCHANT_EVIDENCE_SOURCE_TYPES
            and review_source_url(claim.get("review_source_url")) is not None)

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
        source_type = str(raw.get("source_type") or "").strip()
        source_ref = str(raw.get("source_ref") or "").strip() or None
        grade = str(raw.get("evidence_grade") or "").strip() or None
        if status == SUBSTANTIATION_SUBSTANTIATED and source_type.lower() in MERCHANT_EVIDENCE_SOURCE_TYPES:
            # This normalized object is a public projection. Keep the original
            # submission and the reviewer identity only in the evidence store.
            if has_verified_merchant_evidence(raw):
                source_ref = raw["review_source_url"].strip()
                source_type = raw["review_source_type"]
            else:
                status, grade = SUBSTANTIATION_UNVERIFIED, None
        return ProductClaim(
            claim_text=text,
            source_ref=source_ref,
            source_type=source_type or None,
            evidence_grade=grade,
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


def substantiated_product_claims(evidence_profile: Any) -> List[ProductClaim]:
    """The substantiation serve-gate as typed ProductClaims: ONLY substantiated
    claims from an evidence_profile (a dict with a "claims" list, a raw claims
    list, or a JSON string). Unverified / flagged / rejected claims are dropped.

    THE shared substantiation filter — every agent-facing surface routes through
    it so the gate can't drift: the flat-dict PDP API via substantiated_claims(),
    and the search path's EvidenceProfile. Pure; no I/O.
    """
    raw: Any = evidence_profile
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        raw = raw.get("claims")
    return [c for c in normalize_claims(raw) if c.substantiation_status == SUBSTANTIATION_SUBSTANTIATED]


def substantiated_claims(evidence_profile: Any) -> List[Dict[str, Any]]:
    """Flat-dict form of the substantiation serve-gate (PDP API / public
    PDP+JSON-LD / MCP / ACP): substantiated claims only, as plain dicts safe to
    emit to agents. Wraps substantiated_product_claims so the filter is shared."""
    return [
        {
            "claim_text": c.claim_text,
            "source_ref": c.source_ref,
            "source_type": c.source_type,
            "evidence_grade": c.evidence_grade,
        }
        for c in substantiated_product_claims(evidence_profile)
    ]

"""Layer C1 — catalog_trust_policy (Python port).

Pure function: (inputs) -> catalog_row_trust shape.

This is the Python parity port of PIVOTA-Agent/src/services/catalogTrustPolicy.js
(POLICY_VERSION ``c1.v0.4``). Producer dual-write call sites in pivota-backend
(catalog_sync_service, source_quarantine helpers) invoke ``derive_trust`` to
populate ``catalog_row_trust`` so the reader contract stays live between
periodic backfills.

Inputs are collected from the existing tables this module does NOT own:
  catalog_products, catalog_offers, index_pipeline_state,
  pdp_identity_listing, external_product_seeds, merchant_stores,
  catalog_source_quarantine, pdp_identity_override.

Output is a row matching ``db/migrations/136_catalog_row_trust.sql``.

Contract: every reader downstream depends on (serving_decision,
serving_reason_codes). The other fields are advisory and used for ranking,
debugging, and shadow-comparison.

Versioning: POLICY_VERSION must bump on any change to derivation logic.
The backfill job uses POLICY_VERSION to detect stale rows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

POLICY_VERSION = "c1.v0.4"

# ---- Reason codes (authoritative vocabulary) -------------------------------
#
# Public:
#   none required, but PUBLIC_PASSTHROUGH may be set for traceability.
#
# Shadow (would have served under legacy gates, but contract says caution):
#   IDENTITY_REVIEW_REQUIRED_LIVE_READ — pdp_identity_listing.identity_status=
#     'review_required'. Audit counted ~60 of these among external mirror rows.
#   IDENTITY_CONFIDENCE_NULL — IPS serving_eligible=true but no identity row
#     or identity_confidence IS NULL. Only emitted for non-first-party sources
#     (i.e., external_seed). For first-party merchants the corresponding
#     advisory is IDENTITY_NOT_APPLICABLE_FIRST_PARTY (see below).
#   IDENTITY_LIVE_READ_DISABLED — identity_status='approved' but
#     live_read_enabled=false. First-party sources are exempt.
#   FRESHNESS_UNVERIFIED — never observed a verification timestamp.
#
# Advisory (does not flip decision):
#   IDENTITY_NOT_APPLICABLE_FIRST_PARTY — c1.v0.3+. Marks rows where the
#     merchant IS the source of truth, so the identity-pipeline gates (which
#     exist to verify scraped third-party content) don't apply. Emitted for
#     ``product.merchant_id != 'external_seed'`` when identity is missing or
#     low-info.
#
# Blocked (no public surface):
#   SOURCE_QUARANTINED               — catalog_source_quarantine active match.
#   ROW_TOMBSTONED                   — catalog_products.suppression_reason set.
#   EXTERNAL_SEED_INACTIVE           — external_product_seeds.status != 'active'.
#   MERCHANT_STORE_INACTIVE          — merchant_stores.status != 'active'.
#   INDEX_NOT_SERVING_ELIGIBLE       — index_pipeline_state.serving_eligible=false.
#   PUBLISH_STATE_NOT_PUBLIC         — catalog_products.publish_state != 'public'.
#   IDENTITY_CONFLICT                — identity_status='conflict'.
#   OFFER_SUPPRESSED                 — subject_type='offer' with offer.suppression_reason set.


class _ReasonCodes:
    PUBLIC_PASSTHROUGH = "PUBLIC_PASSTHROUGH"

    IDENTITY_REVIEW_REQUIRED_LIVE_READ = "IDENTITY_REVIEW_REQUIRED_LIVE_READ"
    IDENTITY_CONFIDENCE_NULL = "IDENTITY_CONFIDENCE_NULL"
    IDENTITY_LIVE_READ_DISABLED = "IDENTITY_LIVE_READ_DISABLED"
    IDENTITY_NOT_APPLICABLE_FIRST_PARTY = "IDENTITY_NOT_APPLICABLE_FIRST_PARTY"
    FRESHNESS_UNVERIFIED = "FRESHNESS_UNVERIFIED"

    SOURCE_QUARANTINED = "SOURCE_QUARANTINED"
    ROW_TOMBSTONED = "ROW_TOMBSTONED"
    EXTERNAL_SEED_INACTIVE = "EXTERNAL_SEED_INACTIVE"
    MERCHANT_STORE_INACTIVE = "MERCHANT_STORE_INACTIVE"
    INDEX_NOT_SERVING_ELIGIBLE = "INDEX_NOT_SERVING_ELIGIBLE"
    PUBLISH_STATE_NOT_PUBLIC = "PUBLISH_STATE_NOT_PUBLIC"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    OFFER_SUPPRESSED = "OFFER_SUPPRESSED"


REASON_CODES = _ReasonCodes()
REASON_CODE_VOCABULARY = frozenset(
    {
        v
        for k, v in vars(_ReasonCodes).items()
        if not k.startswith("_") and isinstance(v, str)
    }
)

VALID_SUBJECT_TYPES = frozenset({"product", "offer", "listing", "content_key"})


def _index_eligible_read_enabled() -> bool:
    """ADR-008 SLICE 1 read flag. When ON, the index-pipeline serving gate in
    the trust policy widens from serving_eligible to
    (serving_eligible OR index_eligible) — the OFFER-FREE citation floor.
    Default OFF ⇒ byte-identical to today (serving_eligible only)."""
    return (
        (os.getenv("INDEX_ELIGIBLE_READ") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

# Freshness thresholds (seconds). Tuned to keep external_seed which refreshes
# ~24h in the 'fresh' bucket and to mark internal merchant catalog stale after
# a week without sync.
FRESH_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
STALE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


# ---- Public API ------------------------------------------------------------


def derive_trust(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the catalog_row_trust row for a single subject.

    See the module docstring and the Node module for the full input shape.
    ``inputs`` is a mapping with the following optional keys:
      subject_type, subject_key — required
      product, offer, identity, ips, external_seed, merchant_store, override,
      active_quarantines, now

    Returns a dict matching the catalog_row_trust columns (without updated_at).
    """
    now = inputs.get("now")
    if not isinstance(now, datetime):
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    subject_type = inputs.get("subject_type")
    subject_key = inputs.get("subject_key")

    if subject_type not in VALID_SUBJECT_TYPES:
        raise ValueError(f"invalid subject_type: {subject_type!r}")
    if not isinstance(subject_key, str) or not subject_key:
        raise ValueError("subject_key is required")

    product = inputs.get("product") or None
    offer = inputs.get("offer") or None
    identity = inputs.get("identity") or None
    ips = inputs.get("ips") or None
    external_seed = inputs.get("external_seed") or None
    merchant_store = inputs.get("merchant_store") or None
    override = inputs.get("override") or None
    raw_quarantines = inputs.get("active_quarantines") or []
    active_quarantines = list(raw_quarantines) if isinstance(raw_quarantines, Iterable) else []

    reasons: list[str] = []

    source_lifecycle = _derive_source_lifecycle(
        product=product,
        external_seed=external_seed,
        merchant_store=merchant_store,
        active_quarantines=active_quarantines,
        reasons=reasons,
        now=now,
    )

    identity_decision = _derive_identity(
        identity=identity,
        override=override,
        reasons=reasons,
    )

    freshness = _derive_freshness(
        product=product,
        ips=ips,
        external_seed=external_seed,
        now=now,
    )
    if freshness["state"] == "unverified":
        reasons.append(REASON_CODES.FRESHNESS_UNVERIFIED)

    serving = _derive_serving_decision(
        subject_type=subject_type,
        product=product,
        offer=offer,
        ips=ips,
        source_lifecycle=source_lifecycle,
        identity_decision=identity_decision,
        reasons=reasons,
    )

    return {
        "subject_type": subject_type,
        "subject_key": subject_key,
        "product_key": _get(product, "product_key"),
        # The identity listing's PK doubles as the source_listing_ref; for
        # external_seed rows the external_product_seeds.id is preferred.
        "source_listing_ref": (
            _get(identity, "source_listing_ref")
            or (
                str(_get(external_seed, "id"))
                if _get(external_seed, "id") is not None
                else None
            )
            or _get(product, "source_ref")
        ),
        "content_key": _get(product, "content_key"),
        "source_id": None,  # forward-compat — Layer A1 source registry not yet shipped
        "source_domain": (
            _get(product, "source_domain")
            or _get(external_seed, "domain")
            or _get(merchant_store, "domain")
        ),
        "source_lifecycle_state": source_lifecycle["state"],
        "source_last_checked_at": (
            _get(external_seed, "last_seen_at")
            or _get(merchant_store, "last_sync")
        ),
        "identity_status": identity_decision["status"],
        "identity_confidence": identity_decision["confidence"],
        # Phase 1: only the sellable_item_group_id is populated directly from
        # pdp_identity_listing. matched_product_key / matched_content_key
        # require a sibling-row lookup (resolved in Phase 2 dual-write); leave
        # null here.
        "matched_product_key": None,
        "matched_content_key": None,
        "matched_sellable_item_group_id": _get(identity, "sellable_item_group_id"),
        "freshness_state": freshness["state"],
        "last_verified_at": freshness["last_verified_at"],
        "verification_source": freshness["verification_source"],
        "serving_decision": serving["decision"],
        "serving_reason_codes": _dedupe(reasons),
        "manual_override_id": _get(override, "id"),
        "policy_version": POLICY_VERSION,
    }


# ---- Source lifecycle ------------------------------------------------------


def _derive_source_lifecycle(
    *,
    product: Optional[Mapping[str, Any]],
    external_seed: Optional[Mapping[str, Any]],
    merchant_store: Optional[Mapping[str, Any]],
    active_quarantines: list[Any],
    reasons: list[str],
    now: datetime,
) -> dict[str, Any]:
    # Quarantine wins over everything.
    if _is_quarantined(
        product=product,
        external_seed=external_seed,
        merchant_store=merchant_store,
        active_quarantines=active_quarantines,
        now=now,
    ):
        reasons.append(REASON_CODES.SOURCE_QUARANTINED)
        return {"state": "quarantined"}

    # Tombstone (PR #666 / migration 135).
    if _get(product, "suppression_reason"):
        reasons.append(REASON_CODES.ROW_TOMBSTONED)
        return {"state": "tombstoned"}

    # External seed lifecycle.
    if external_seed is not None:
        status = str(_get(external_seed, "status") or "").lower()
        if status == "active":
            return {"state": "active"}
        if status in ("disabled", "inactive"):
            reasons.append(REASON_CODES.EXTERNAL_SEED_INACTIVE)
            return {"state": "inactive"}
        if status == "suspect":
            return {"state": "suspect"}
        return {"state": "unknown"}

    # Merchant store lifecycle.
    if merchant_store is not None:
        status = str(_get(merchant_store, "status") or "").lower()
        if status in ("active", "connected"):
            return {"state": "active"}
        # retired_test_rig is a decommissioned demo/test store: it must be treated
        # as inactive (not "unknown"), else its catalog leaks into serving.
        if status in ("inactive", "disconnected", "retired_test_rig"):
            reasons.append(REASON_CODES.MERCHANT_STORE_INACTIVE)
            return {"state": "inactive"}
        return {"state": "unknown"}

    return {"state": "unknown"}


def _is_quarantined(
    *,
    product: Optional[Mapping[str, Any]],
    external_seed: Optional[Mapping[str, Any]],
    merchant_store: Optional[Mapping[str, Any]],
    active_quarantines: list[Any],
    now: datetime,
) -> bool:
    if not active_quarantines:
        return False
    now_ms = now.timestamp() * 1000.0

    domain = str(
        _get(product, "source_domain")
        or _get(external_seed, "domain")
        or _get(merchant_store, "domain")
        or ""
    ).lower()
    merchant_id = _get(product, "merchant_id") or _get(merchant_store, "merchant_id")
    platform = _get(product, "platform") or _get(merchant_store, "platform")
    source_system = _get(product, "source_system")
    source_ref = _get(product, "source_system_ref")

    for q in active_quarantines:
        if _get(q, "state") != "active":
            continue
        expires_at = _get(q, "expires_at")
        if expires_at is not None:
            ts = _to_datetime(expires_at)
            if ts is not None and ts.timestamp() * 1000.0 <= now_ms:
                continue

        match_type = _get(q, "match_type")
        match_value = _get(q, "match_value")
        if match_value is None:
            continue

        if match_type == "domain" and domain:
            if str(match_value).lower() == domain:
                return True
        elif match_type == "merchant_platform" and merchant_id and platform:
            if match_value == f"{merchant_id}:{platform}":
                return True
        elif match_type == "source_system_ref" and source_system and source_ref:
            if match_value == f"{source_system}:{source_ref}":
                return True
    return False


# ---- Identity --------------------------------------------------------------


def _derive_identity(
    *,
    identity: Optional[Mapping[str, Any]],
    override: Optional[Mapping[str, Any]],
    reasons: list[str],
) -> dict[str, Any]:
    # Manual override of identity wins (rare but authoritative).
    if (
        override is not None
        and _get(override, "action_type") == "force_exact_group"
        and _get(override, "active")
    ):
        return {
            "status": "approved",
            "confidence": 1.0,
            "live_read": True,
            "review_required": False,
        }
    if (
        override is not None
        and _get(override, "action_type") == "force_review_required"
        and _get(override, "active")
    ):
        return {
            "status": "review_required",
            "confidence": _get(identity, "identity_confidence"),
            "live_read": _get(identity, "live_read_enabled") is True,
            "review_required": True,
        }

    if identity is None:
        # No identity row at all. The 504 external-mirror cases in the audit
        # largely fall here.
        return {
            "status": "unknown",
            "confidence": None,
            "live_read": None,
            "review_required": None,
        }

    status = str(_get(identity, "identity_status") or "").lower()
    live_read = _get(identity, "live_read_enabled") is True
    review_required = _get(identity, "review_required") is True
    raw_confidence = _get(identity, "identity_confidence")
    confidence = None if raw_confidence is None else _clamp(float(raw_confidence), 0.0, 1.0)

    if status == "conflict":
        reasons.append(REASON_CODES.IDENTITY_CONFLICT)
        return {
            "status": "conflict",
            "confidence": confidence,
            "live_read": live_read,
            "review_required": review_required,
        }

    if status == "approved":
        if review_required:
            # Approved but flagged for review — degrade to review_required so
            # downstream readers don't treat as fully trusted.
            return {
                "status": "review_required",
                "confidence": confidence,
                "live_read": live_read,
                "review_required": review_required,
            }
        return {
            "status": "approved",
            "confidence": confidence,
            "live_read": live_read,
            "review_required": review_required,
        }

    if status == "review_required":
        return {
            "status": "review_required",
            "confidence": confidence,
            "live_read": live_read,
            "review_required": review_required,
        }

    return {
        "status": "unknown",
        "confidence": confidence,
        "live_read": live_read,
        "review_required": review_required,
    }


# ---- Freshness -------------------------------------------------------------


def _derive_freshness(
    *,
    product: Optional[Mapping[str, Any]],
    ips: Optional[Mapping[str, Any]],
    external_seed: Optional[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    candidates = [
        (_get(ips, "last_extracted_at"), "identity_resolver"),
        (_get(product, "last_seen_in_sync_at"), _derive_verification_source(product)),
        (_get(external_seed, "last_seen_at"), "external_seed_scrape"),
        (_get(ips, "quality_scored_at"), "index_pipeline"),
    ]

    chosen_ts: Optional[datetime] = None
    chosen_src: Optional[str] = None
    for raw_ts, src in candidates:
        if raw_ts is None:
            continue
        ts = _to_datetime(raw_ts)
        if ts is None:
            continue
        if chosen_ts is None or ts > chosen_ts:
            chosen_ts = ts
            chosen_src = src

    if chosen_ts is None:
        return {
            "state": "unverified",
            "last_verified_at": None,
            "verification_source": None,
        }

    age_seconds = (now - chosen_ts).total_seconds()
    if age_seconds <= FRESH_MAX_AGE_SECONDS:
        state = "fresh"
    elif age_seconds <= STALE_MAX_AGE_SECONDS:
        state = "stale"
    else:
        state = "expired"

    return {
        "state": state,
        "last_verified_at": chosen_ts,
        "verification_source": chosen_src,
    }


def _derive_verification_source(product: Optional[Mapping[str, Any]]) -> Optional[str]:
    if product is None:
        return None
    if _get(product, "merchant_id") == "external_seed":
        return "external_seed_scrape"
    platform = _get(product, "platform")
    if platform == "shopify":
        return "shopify_sync"
    if platform == "wix":
        return "wix_sync"
    return "merchant_sync"


# ---- Serving decision ------------------------------------------------------


def _derive_serving_decision(
    *,
    subject_type: str,
    product: Optional[Mapping[str, Any]],
    offer: Optional[Mapping[str, Any]],
    ips: Optional[Mapping[str, Any]],
    source_lifecycle: Mapping[str, Any],
    identity_decision: Mapping[str, Any],
    reasons: list[str],
) -> dict[str, Any]:
    # Offer-specific block: suppressed offers never surface.
    if subject_type == "offer" and offer is not None and _get(offer, "suppression_reason"):
        reasons.append(REASON_CODES.OFFER_SUPPRESSED)
        return {"decision": "blocked"}

    # Hard blocks.
    lifecycle_state = source_lifecycle["state"]
    blocked = (
        lifecycle_state == "quarantined"
        or lifecycle_state == "tombstoned"
        or lifecycle_state == "inactive"
        or identity_decision["status"] == "conflict"
    )
    if blocked:
        return {"decision": "blocked"}

    # Index pipeline gate. All public readers honor this today; the contract
    # makes it explicit. sync_status='live' is the equivalent for catalog rows
    # before they reach IPS — see migration 084.
    #
    # c1.v0.4: for non-first-party (external_seed) catalog rows, a missing IPS
    # row is treated as INDEX_NOT_SERVING_ELIGIBLE. Pre-c1.v0.4 the policy let
    # ips=None pass on the assumption "no IPS opinion = no reason to block",
    # but Phase 3c parity found 80 external_seed catalog products with public
    # trust + no IPS row — i.e., shipping content the index pipeline hasn't
    # quality-gated yet. First-party rows (MOYU/GR/PawStyle/etc.) keep the
    # legacy behavior since first-party merchants are the source of truth and
    # IPS coverage there is sparse by design.
    # ADR-009 observed-seller trust tier (docs/adr009_observed_seller_trust_decision.md,
    # Option C). Classify by content SOURCE, not the legacy merchant_id='external_seed'
    # string: external seeds now mirror under per-brand observed sellers (merch_obs_…).
    #   - is_external_seed_content: legacy 'external_seed' lump OR an observed seller —
    #     both are scraped supply and must clear the index/quality gate.
    #   - is_observed_seller: the brand's own D2C crawl (merch_obs_), authoritative
    #     for its own content, so exempt from the identity-COVERAGE shadow gates
    #     (below) like a first-party merchant — but NOT from the index/quality gate.
    _merchant_id = str(_get(product, "merchant_id") or "") if product is not None else ""
    _platform = str(_get(product, "platform") or "").lower() if product is not None else ""
    is_external_seed_content = (
        _platform == "external_seed"
        or _merchant_id == "external_seed"
        or _merchant_id.startswith("merch_obs_")
    )
    is_observed_seller = _merchant_id.startswith("merch_obs_")
    if product is not None:
        if is_external_seed_content and ips is None:
            reasons.append(REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE)
            return {"decision": "blocked"}
        if ips is not None:
            # ADR-008 SLICE 1: the citation read surface accepts the OFFER-FREE
            # index_eligible floor when INDEX_ELIGIBLE_READ is ON. Flag OFF ⇒
            # serving_eligible-only, byte-identical to the pre-ADR-008 gate.
            ips_eligible = _get(ips, "serving_eligible") is True or (
                _index_eligible_read_enabled()
                and _get(ips, "index_eligible") is True
            )
            if not ips_eligible:
                reasons.append(REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE)
                return {"decision": "blocked"}
        sync_status = str(_get(product, "sync_status") or "").lower()
        if sync_status and sync_status != "live":
            reasons.append(REASON_CODES.PUBLISH_STATE_NOT_PUBLIC)
            return {"decision": "blocked"}

    # Shadow conditions — would have served under legacy gates, but the
    # contract gates them out of public reads.
    #
    # c1.v0.3 + ADR-009 Option C: exempt from the identity-COVERAGE shadow gates
    # both (a) connected first-party merchants (the merchant IS the source of
    # truth — the pipeline exists to verify scraped third-party content) and
    # (b) per-brand observed sellers (merch_obs_, the brand's own D2C crawl,
    # authoritative for its own content). The legacy anonymous 'external_seed'
    # lump stays subject. review_required and IDENTITY_CONFLICT still apply to
    # everyone (explicit moderation/data-quality signals, not coverage gaps).
    is_identity_coverage_exempt = product is not None and (
        not is_external_seed_content or is_observed_seller
    )

    if identity_decision["status"] == "review_required":
        reasons.append(REASON_CODES.IDENTITY_REVIEW_REQUIRED_LIVE_READ)

    missing_confidence = (
        identity_decision["status"] in ("unknown", "approved")
        and identity_decision["confidence"] is None
    )
    if missing_confidence:
        if is_identity_coverage_exempt:
            reasons.append(REASON_CODES.IDENTITY_NOT_APPLICABLE_FIRST_PARTY)
        else:
            reasons.append(REASON_CODES.IDENTITY_CONFIDENCE_NULL)

    if identity_decision["status"] == "approved" and identity_decision["live_read"] is False:
        if not is_identity_coverage_exempt:
            reasons.append(REASON_CODES.IDENTITY_LIVE_READ_DISABLED)

    shadow = (
        identity_decision["status"] == "review_required"
        or REASON_CODES.IDENTITY_CONFIDENCE_NULL in reasons
        or REASON_CODES.IDENTITY_LIVE_READ_DISABLED in reasons
        or (identity_decision["status"] == "unknown" and not is_identity_coverage_exempt)
    )

    if shadow:
        return {"decision": "shadow"}

    reasons.append(REASON_CODES.PUBLIC_PASSTHROUGH)
    return {"decision": "public"}


# ---- Utilities -------------------------------------------------------------


def _get(obj: Optional[Mapping[str, Any]], key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _clamp(value: float, lo: float, hi: float) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN check
        return None
    return min(hi, max(lo, v))


def _dedupe(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            # Postgres/JS ISO-8601 with trailing Z or with offset.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class TrustOutput:
    """Typed view of a derive_trust result, optional for type-aware callers."""

    subject_type: str
    subject_key: str
    serving_decision: str
    serving_reason_codes: tuple[str, ...]
    policy_version: str

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "TrustOutput":
        return cls(
            subject_type=row["subject_type"],
            subject_key=row["subject_key"],
            serving_decision=row["serving_decision"],
            serving_reason_codes=tuple(row["serving_reason_codes"]),
            policy_version=row["policy_version"],
        )


__all__ = [
    "POLICY_VERSION",
    "REASON_CODES",
    "REASON_CODE_VOCABULARY",
    "VALID_SUBJECT_TYPES",
    "FRESH_MAX_AGE_SECONDS",
    "STALE_MAX_AGE_SECONDS",
    "derive_trust",
    "TrustOutput",
]

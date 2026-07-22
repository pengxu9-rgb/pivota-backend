"""Realtime recompute for index_pipeline_state.serving_eligible.

Provides a realtime-callable version of the nightly health job's eligibility
check, so writers that mutate signals (catalog sync, quality snapshot, APV
refresh) can flip rows live instead of waiting for the nightly batch.

Keep jobs/nightly_index_health_job.py as the eventual-consistency safety net
for any signal change that doesn't flow through a hook here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from db.database import database
from services.agent_decision_gates import (
    agent_decision_gates_enabled,
    evaluate_agent_decision_gates,
)

logger = logging.getLogger(__name__)

CONSOLIDATION_VERSION = "nightly_index_health_v1"

# content_quality_score is on a 0-100 scale; see services/product_quality_service.py
# (`round(avg_score * 100.0, 1)`). A threshold of 65 ~= "two-thirds of facets passed".
QUALITY_SCORE_THRESHOLD = 65.0
MIN_DESCRIPTION_LENGTH = 50

# Seed audit statuses that do NOT block serving.
_SEED_AUDIT_OK = {"no_issues_found", "auto_corrected", "not_audited"}
_SEED_AUDIT_BLOCKING = {"issues_unresolved"}
_SEED_AUDIT_KNOWN = _SEED_AUDIT_OK | _SEED_AUDIT_BLOCKING
_RESOLVED_PDP_SCOPES = {
    "canonical",  # legacy pre-070 label used in tests / old rows
    "merchant_owned",
    "multi_merchant_canonical",
}

_PIPELINE_STAGE_RANK = {
    "discovered": 0,
    "crawled": 1,
    "extracted": 2,
    "quality_gated": 3,
    "shadow_indexed": 4,
    "public_indexed": 5,
}

_NON_CORE_PRODUCT_RE = re.compile(
    r"\b(?:"
    r"sample\s+(?:card|vial|pack|sachet)|"
    r"deluxe\s+sample|"
    r"gift\s+card|e\s*gift\s+card|"
    r"shipping\s+protection|route\s+package\s+protection|delivery\s+protection|"
    r"(?:decal|enamel)\s+stickers?|tirtir\s+stickers?|packette|"
    r"gwp|gift\s+with\s+purchase|free\s+gift|"
    r"100\s*%\s*off|mystery\s+.*product"
    r")\b",
    re.IGNORECASE,
)

_NON_CORE_COMPACT_RE = re.compile(
    r"(?:"
    r"samplecard|samplevial|deluxesample|"
    r"giftcard|egiftcard|"
    r"shippingprotection|deliveryprotection|packageprotection|"
    r"narvardeliveryprotection|"
    r"packette"
    r")",
    re.IGNORECASE,
)


def _make_extraction_content_hash(
    title: Optional[str],
    description: Optional[str],
    image_url: Optional[str],
) -> str:
    blob = f"{title or ''}::{description or ''}::{image_url or ''}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _coerce_dict(value: Any) -> Dict[str, Any]:
    """Best-effort JSON -> dict coercion. Returns {} on any failure."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _resolve_seed_title(seed_data: Dict[str, Any], row: Dict[str, Any]) -> str:
    """Resolve seed title across the shapes used in external_product_seeds.

    Matches services/external_seed_audit.py:audit_external_seed_row resolution
    order: snapshot.title -> row.title -> seed_data.title. Returns "" if none.
    """
    snapshot = _coerce_dict(seed_data.get("snapshot"))
    for candidate in (
        snapshot.get("title"),
        row.get("seed_title"),  # selected from external_product_seeds.title in query
        seed_data.get("title"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _has_seed_row(row: Dict[str, Any]) -> bool:
    """Infer whether the lateral seed join found a row.

    The query does not need the seed id for classification. Presence of
    either seed_data or top-level seed title is enough to distinguish "no seed"
    from "seed row exists but extraction shape is thin/malformed".
    """
    return row.get("seed_data_json") is not None or row.get("seed_title") is not None


def _is_non_core_product(row: Dict[str, Any], *, seed_title: str) -> bool:
    """Return True for sellable artifacts that should not enter PDP serving.

    These rows are operational or promo artifacts rather than products users
    should discover through the commerce index: sample cards/vials, shipping
    protection, gift cards, GWP placeholders, stickers, packettes, and promo
    placeholders. The matcher is deliberately narrow to avoid suppressing a
    legitimate product that happens to contain a broad word like "sample".
    """
    values = [
        seed_title,
        row.get("seed_title"),
        row.get("pdp_title"),
        row.get("canonical_url"),
    ]
    raw = " ".join(str(value or "") for value in values)
    normalized = re.sub(r"[^a-z0-9%]+", " ", raw.lower())
    compact = re.sub(r"[^a-z0-9%]+", "", raw.lower())
    return bool(
        _NON_CORE_PRODUCT_RE.search(normalized)
        or _NON_CORE_COMPACT_RE.search(compact)
    )


def _extract_domain(url: str) -> Optional[str]:
    """Extract apex domain from a URL. Returns None if URL is empty/invalid."""
    if not url:
        return None
    try:
        # Minimal domain extraction: strip scheme + path.
        s = url.split("//", 1)[-1]
        host = s.split("/")[0].split("?")[0].split("#")[0]
        host = host.lower().strip()
        if not host:
            return None
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:  # noqa: BLE001
        return None


def _classify_product(
    row: Dict[str, Any],
    regression_domains: Set[str],
) -> Dict[str, Any]:
    """Derive pipeline_stage, blocker_code, serving_eligible from a fetched row.

    `row` contains columns from the eligibility query.
    Returns a dict of index_pipeline_state columns ready for upsert.
    """
    now = datetime.now(timezone.utc)

    # --- Extract seed signals (snapshot-shape aware) ---
    seed_data = _coerce_dict(row.get("seed_data_json"))
    has_seed_row = _has_seed_row(row)
    seed_title = _resolve_seed_title(seed_data, row)
    snapshot = _coerce_dict(seed_data.get("snapshot"))
    # review_summary may live on the seed_data itself or under snapshot.review_summary
    review_summary = _coerce_dict(
        seed_data.get("review_summary") or snapshot.get("review_summary")
    )
    seed_review_status = str(review_summary.get("review_status") or "").strip()
    # `review_summary.review_status` is overloaded by retailer/public-review
    # aggregate importers. Only known seed-audit statuses should participate in
    # the serving gate; unrelated review aggregate states are not seed audit
    # failures and must not produce serving_eligible=false with blocker none.
    seed_audit_status = (
        seed_review_status
        if seed_review_status in _SEED_AUDIT_KNOWN
        else "not_audited"
    )

    # --- Extract quality signals ---
    quality_score = row.get("content_quality_score")
    model_readiness = row.get("model_readiness_score")
    conversion_potential = row.get("conversion_potential_score")
    quality_rules_version = row.get("quality_rules_version")
    quality_scored_at = row.get("quality_scored_at")

    # --- Extract PDP view signals ---
    pdp_title = (row.get("pdp_title") or "").strip()
    pdp_image_url = (row.get("image_url") or "").strip()
    pdp_description = (row.get("pdp_description") or "").strip()
    pdp_sync_status = (row.get("pdp_sync_status") or "").strip()
    pdp_lifecycle_stage = (row.get("pdp_lifecycle_stage") or "").strip()
    pdp_refreshed_at = row.get("refreshed_at")
    # catalog_products.sync_status (the canonical source); apv mirrors it but can lag
    cp_sync_status = (row.get("sync_status") or "").strip()

    has_image = bool(pdp_image_url)
    has_price = bool(row.get("has_price"))
    description_length = len(pdp_description)

    has_seed_document = bool(seed_title)
    has_apv_document = bool(pdp_title and pdp_description)
    source_title = seed_title or (pdp_title if has_apv_document else "")
    source_document_present = has_seed_document or has_apv_document

    # --- Extract identity signals ---
    pdp_scope = (row.get("pdp_scope") or "").strip()
    product_group_id = row.get("product_group_id")
    identity_resolved = bool(product_group_id) or (pdp_scope in _RESOLVED_PDP_SCOPES)

    # --- Domain regression check ---
    canonical_url = (row.get("canonical_url") or "").strip()
    domain = _extract_domain(canonical_url)
    domain_has_regression = domain in regression_domains if domain else False

    # --- Extraction content hash ---
    extraction_content_hash = _make_extraction_content_hash(
        source_title or pdp_description[:80] or None,
        pdp_description or None,
        pdp_image_url or None,
    )

    # --- Merchant store lifecycle ---
    # A CONNECTED merchant (has ≥1 merchant_stores row) whose stores are ALL
    # non-active (retired_test_rig / inactive / disconnected) can no longer be
    # trusted or fulfilled, so its catalog must stop serving. External seeds have
    # NO merchant_stores row → merchant_has_store=False → merchant_store_retired
    # stays False → exempt (governed by external_product_seeds.status instead).
    merchant_has_store = bool(row.get("merchant_has_store"))
    merchant_has_active_store = bool(row.get("merchant_has_active_store"))
    merchant_store_retired = merchant_has_store and not merchant_has_active_store

    # --- Determine blocker (first failing check wins) ---
    blocker_code = "none"
    blocker_detail: Optional[str] = None

    if cp_sync_status and cp_sync_status != "live":
        blocker_code = "not_live"
        blocker_detail = f"catalog_products.sync_status={cp_sync_status!r}"
    elif merchant_store_retired:
        blocker_code = "merchant_store_inactive"
        blocker_detail = (
            "merchant has no active/connected store "
            "(all merchant_stores are retired/inactive/disconnected)"
        )
    elif _is_non_core_product(row, seed_title=seed_title):
        blocker_code = "non_core_product"
        blocker_detail = (
            "sample/gift/protection/GWP row is not eligible for commerce index serving"
        )
    elif not source_document_present:
        if not has_seed_row:
            blocker_code = "no_seed"
            blocker_detail = (
                "no external_product_seeds row and no agent_pdp_view "
                "title+description source document"
            )
        else:
            blocker_code = "no_extraction"
            blocker_detail = (
                "source row present but title is missing in seed snapshot/top-level "
                "and no APV title+description fallback is available"
            )
    elif quality_score is None or quality_score < QUALITY_SCORE_THRESHOLD:
        blocker_code = "low_quality"
        blocker_detail = (
            f"content_quality_score={quality_score} < {QUALITY_SCORE_THRESHOLD}"
            if quality_score is not None
            else "no quality snapshot found"
        )
    elif not has_image:
        blocker_code = "no_image"
        blocker_detail = "agent_pdp_view.image_url is null or empty"
    elif not has_price:
        blocker_code = "no_price"
        blocker_detail = "no catalog_offers row with list_price > 0"
    elif description_length < MIN_DESCRIPTION_LENGTH:
        blocker_code = "short_description"
        blocker_detail = (
            f"description length {description_length} < {MIN_DESCRIPTION_LENGTH} chars"
        )
    elif not identity_resolved:
        blocker_code = "entity_unresolved"
        blocker_detail = (
            "no product_group_members row and pdp_scope is not a resolved scope"
        )
    elif seed_audit_status in _SEED_AUDIT_BLOCKING:
        blocker_code = "seed_audit_fail"
        blocker_detail = f"seed content audit reported {seed_audit_status}"
    elif domain_has_regression:
        blocker_code = "extractor_regression"
        blocker_detail = f"domain {domain!r} has regression alert in domain_extractor_baselines"
    elif agent_decision_gates_enabled():
        # Additive agent-decision-grade gates (PR4), only after all PDP gates
        # pass. Flag-gated, so default behavior is unchanged. Picked up by the
        # serving_eligible formula below via blocker_code == "none".
        _adg = evaluate_agent_decision_gates(row)
        if _adg is not None:
            blocker_code, blocker_detail = _adg

    # --- Serving eligibility (all criteria must pass) ---
    serving_eligible = (
        blocker_code == "none"
        and cp_sync_status == "live"
        and not merchant_store_retired
        and bool(source_title)
        and quality_score is not None
        and quality_score >= QUALITY_SCORE_THRESHOLD
        and has_image
        and has_price
        and description_length >= MIN_DESCRIPTION_LENGTH
        and identity_resolved
        and seed_audit_status in _SEED_AUDIT_OK
        and not domain_has_regression
    )

    # --- Index eligibility (ADR-008 SLICE 1: OFFER-FREE citation floor) ---
    # `index_eligible` is the parallel trust+quality+identity gate that drives
    # the citation READ surface. It is the FULL serving_eligible predicate set
    # MINUS has_price — a brand can be worth citing without us carrying a
    # buyable offer. It does NOT gate commerce/shopping/checkout.
    #
    # has_image is REQUIRED: a citation without a product image is not a usable
    # PDP, so we keep the image floor even though we drop the price floor.
    #
    # CRITICAL: this is computed from the RAW predicates, NOT from
    # `blocker_code == "none"`. `blocker_code` short-circuits on the first
    # failing check, and `no_price` fires BEFORE `short_description` /
    # `entity_unresolved` (see the blocker ladder above) — and for an
    # offer-free row it ALSO short-circuits before the non-core and agent-
    # decision-gate checks. Deriving from blocker_code would therefore both
    # mark a no-price product index_eligible when its description/identity is
    # bad, AND let a no-price sample/gift/protection row slip through. So we
    # re-derive `non_core` from the raw signal independent of blocker_code.
    #
    # The agent-decision gates (no_us_offer / missing_disclaimers / etc.) are
    # NOT part of the SLICE 1 predicate set: they are a separate, default-OFF
    # flag and never reach the blocker ladder for an offer-free row anyway.
    # index_eligible intentionally tracks only the listed trust/quality/identity
    # predicates plus the non-core exclusion.
    non_core_product = _is_non_core_product(row, seed_title=seed_title)
    index_eligible = (
        cp_sync_status == "live"
        and not merchant_store_retired
        and source_document_present
        and bool(source_title)
        and quality_score is not None
        and quality_score >= QUALITY_SCORE_THRESHOLD
        and has_image
        and description_length >= MIN_DESCRIPTION_LENGTH
        and identity_resolved
        and seed_audit_status in _SEED_AUDIT_OK
        and not domain_has_regression
        and not non_core_product
    )

    # --- Stage assignment (highest satisfied stage wins) ---
    # Each stage is a strict superset of the previous:
    #   public_indexed > shadow_indexed > quality_gated > extracted > crawled > discovered
    # public_indexed maps to apv.pdp_lifecycle_stage='published' AND a refreshed apv row;
    # apv.sync_status is inherited from catalog_products (live/stale/archived)
    # and is NOT the published/shadow distinction.
    if (
        serving_eligible
        and pdp_refreshed_at is not None
        and pdp_lifecycle_stage == "published"
    ):
        pipeline_stage = "public_indexed"
    elif serving_eligible and pdp_refreshed_at is not None:
        pipeline_stage = "shadow_indexed"
    elif (
        source_document_present
        and quality_score is not None
        and quality_score >= QUALITY_SCORE_THRESHOLD
        and has_image
        and has_price
        and description_length >= MIN_DESCRIPTION_LENGTH
    ):
        pipeline_stage = "quality_gated"
    elif source_document_present:
        pipeline_stage = "extracted"
    elif row.get("has_offer_snapshot"):
        pipeline_stage = "crawled"
    else:
        pipeline_stage = "discovered"

    return {
        "content_key": row["content_key"],
        "pivota_signature_id": row.get("pivota_signature_id"),
        "merchant_id": row.get("merchant_id"),
        "pipeline_stage": pipeline_stage,
        "blocker_code": blocker_code,
        "blocker_detail": blocker_detail,
        "serving_eligible": serving_eligible,
        "index_eligible": index_eligible,
        "content_quality_score": quality_score,
        "model_readiness_score": model_readiness,
        "conversion_potential_score": conversion_potential,
        "quality_rules_version": quality_rules_version,
        "quality_scored_at": quality_scored_at,
        "extraction_content_hash": extraction_content_hash,
        "extractor_version": None,  # set by extraction service when it writes
        "last_extracted_at": None,
        "seed_audit_status": seed_audit_status,
        "identity_resolved": identity_resolved,
        "product_group_id": product_group_id,
        "has_image": has_image,
        "has_price": has_price,
        "description_length": description_length,
        "consolidation_version": CONSOLIDATION_VERSION,
        "last_consolidated_at": now,
    }


def _state_rank(state: Dict[str, Any]) -> tuple:
    """Rank candidate states for the same content_key.

    `index_pipeline_state` is keyed by content_key while catalog_products can
    contain multiple source rows for that key. Pick the row that best
    represents the content-key PDP instead of letting row order decide which
    source row overwrites the state.
    """
    return (
        1 if state.get("serving_eligible") else 0,
        _PIPELINE_STAGE_RANK.get(str(state.get("pipeline_stage") or ""), -1),
        1 if state.get("blocker_code") == "none" else 0,
        float(state.get("content_quality_score") or -1),
        1 if state.get("has_image") else 0,
        1 if state.get("has_price") else 0,
        int(state.get("description_length") or 0),
        1 if state.get("identity_resolved") else 0,
    )


def _select_content_key_state(states: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Select one deterministic index state for a content_key group."""
    if not states:
        raise ValueError("states must not be empty")
    return max(states, key=_state_rank)


def _classify_content_key_rows(
    rows: List[Dict[str, Any]],
    regression_domains: Set[str],
) -> Optional[Dict[str, Any]]:
    """Classify all catalog rows for a content_key and select the winning state."""
    states = [
        _classify_product(dict(row), regression_domains)
        for row in rows
    ]
    if not states:
        return None
    return _select_content_key_state(states)


_HAS_OFFER_SNAPSHOT_COLUMN = """
    (
        SELECT TRUE
        FROM external_offer_snapshots eos
        WHERE eos.canonical_url = cp.canonical_url
        LIMIT 1
    )                           AS has_offer_snapshot
""".strip()

_ELIGIBILITY_COLUMNS = f"""
    cp.content_key,
    cp.product_key,
    cp.pivota_signature_id,
    cp.merchant_id,
    cp.platform,
    cp.source_product_id,
    cp.pdp_scope,
    cp.sync_status,
    cp.canonical_url,
    cp.category_kind,
    pqs.content_quality_score,
    pqs.model_readiness_score,
    pqs.conversion_potential_score,
    pqs.rules_version           AS quality_rules_version,
    pqs.snapshot_date           AS quality_scored_at,
    apv.image_url,
    apv.title                   AS pdp_title,
    apv.description             AS pdp_description,
    apv.sync_status             AS pdp_sync_status,
    apv.pdp_lifecycle_stage     AS pdp_lifecycle_stage,
    apv.refreshed_at,
    eps.seed_data               AS seed_data_json,
    eps.title                   AS seed_title,
    (
        SELECT TRUE
        FROM catalog_offers co
        WHERE co.product_key = cp.product_key
          AND co.suppressed_at IS NULL
          AND co.list_price > 0
        LIMIT 1
    )                           AS has_price,
    -- US-buyable offer. Derived from CURRENCY, not catalog_offers.market:
    -- market is a NOT NULL DEFAULT 'US' (mig 149, "refine to real per-offer geo
    -- when modeled") that the external-seed writers never set, so it is 'US' for
    -- every row and carries no signal. currency IS written truthfully by every
    -- writer (0 of 18,279 live priced offers have it null), which is exactly how
    -- the INR/ZAR/GBP mispricing was detected. EXISTS (not SELECT TRUE) so the
    -- value is TRUE/FALSE and never NULL — an ABSENT key then unambiguously means
    -- "not computed by this query" rather than "no US offer".
    EXISTS (
        SELECT 1
        FROM catalog_offers co
        WHERE co.product_key = cp.product_key
          AND co.suppressed_at IS NULL
          AND co.list_price > 0
          AND upper(trim(coalesce(co.currency, ''))) = 'USD'
    )                           AS has_us_offer,
    -- Category concern list (skin concern for skincare, hair concern for
    -- haircare); both read the shared concerns_json. The category-attributes
    -- gate selects which category_kind this blocks for.
    (
        SELECT TRUE
        FROM beauty_product_profiles bpp
        WHERE bpp.product_key = cp.product_key
          AND jsonb_typeof(bpp.concerns_json) = 'array'
          AND jsonb_array_length(bpp.concerns_json) > 0
        LIMIT 1
    )                           AS has_category_concern,
    (
        SELECT TRUE
        FROM beauty_sku_ingredients bsi
        WHERE bsi.product_key = cp.product_key
          AND jsonb_typeof(bsi.active_ingredients_json) = 'array'
          AND jsonb_array_length(bsi.active_ingredients_json) > 0
        LIMIT 1
    )                           AS has_key_actives,
{_HAS_OFFER_SNAPSHOT_COLUMN},
    pgm.product_group_id,
    -- Merchant store lifecycle. A CONNECTED merchant whose stores are all
    -- non-active (retired/inactive/disconnected) can no longer be trusted or
    -- fulfilled, so its catalog must stop serving. External seeds have NO
    -- merchant_stores row → merchant_has_store=FALSE → exempt (they are governed
    -- by external_product_seeds.status, not store status).
    COALESCE(mstore.has_store, FALSE)        AS merchant_has_store,
    COALESCE(mstore.has_active_store, FALSE) AS merchant_has_active_store
"""

# `has_offer_snapshot` only influences the low-confidence crawled/discovered
# stage, not public serving eligibility. Full audit pages must avoid probing
# external_offer_snapshots by canonical_url because prod has no index on that
# column; single-key recompute keeps the complete canonical input query.
_AUDIT_ELIGIBILITY_COLUMNS = _ELIGIBILITY_COLUMNS.replace(
    _HAS_OFFER_SNAPSHOT_COLUMN,
    "    FALSE                       AS has_offer_snapshot",
)

_ELIGIBILITY_LATERAL_JOINS = """
LEFT JOIN LATERAL (
    SELECT
        content_quality_score,
        model_readiness_score,
        conversion_potential_score,
        rules_version,
        snapshot_date
    FROM product_quality_snapshot
    WHERE merchant_id = cp.merchant_id
      AND platform = cp.platform
      AND platform_product_id = cp.source_product_id
    ORDER BY snapshot_date DESC
    LIMIT 1
) pqs ON TRUE
LEFT JOIN agent_pdp_view apv
    ON apv.content_key = cp.content_key
LEFT JOIN LATERAL (
    SELECT seed_data, title
    FROM external_product_seeds
    WHERE attached_product_key = cp.product_key
    ORDER BY updated_at DESC
    LIMIT 1
) eps ON TRUE
LEFT JOIN LATERAL (
    SELECT product_group_id
    FROM product_group_members
    WHERE merchant_id = cp.merchant_id
      AND platform = cp.platform
      AND platform_product_id = cp.source_product_id
    LIMIT 1
) pgm ON TRUE
LEFT JOIN LATERAL (
    SELECT
        bool_or(TRUE)                                                    AS has_store,
        bool_or(lower(coalesce(ms.status, '')) IN ('active', 'connected')) AS has_active_store
    FROM merchant_stores ms
    WHERE ms.merchant_id = cp.merchant_id
) mstore ON TRUE
"""

_BATCH_QUERY = f"""
WITH content_keys AS (
    SELECT DISTINCT cp.content_key
    FROM catalog_products cp
    WHERE cp.content_key IS NOT NULL
      AND cp.sync_status = 'live'
      AND cp.content_key > :cursor
    ORDER BY cp.content_key
    LIMIT :batch_size
)
SELECT
{_ELIGIBILITY_COLUMNS}
FROM content_keys ck
JOIN catalog_products cp
  ON cp.content_key = ck.content_key
{_ELIGIBILITY_LATERAL_JOINS}
WHERE cp.sync_status = 'live'
ORDER BY cp.content_key, cp.updated_at DESC NULLS LAST, cp.product_key
"""

_SINGLE_QUERY = f"""
SELECT
{_ELIGIBILITY_COLUMNS}
FROM catalog_products cp
{_ELIGIBILITY_LATERAL_JOINS}
WHERE cp.content_key = :content_key
ORDER BY cp.content_key, cp.updated_at DESC NULLS LAST, cp.product_key
"""

_MULTI_QUERY = f"""
SELECT
{_AUDIT_ELIGIBILITY_COLUMNS}
FROM catalog_products cp
{_ELIGIBILITY_LATERAL_JOINS}
WHERE cp.content_key = ANY(:content_keys)
ORDER BY cp.content_key, cp.updated_at DESC NULLS LAST, cp.product_key
"""

_SINGLE_QUERY_SQLITE = """
SELECT
    cp.content_key,
    cp.product_key,
    cp.pivota_signature_id,
    cp.merchant_id,
    cp.platform,
    cp.source_product_id,
    cp.pdp_scope,
    cp.sync_status,
    cp.canonical_url,
    -- SQLite (test) path: placeholder; durable category_kind is read on the
    -- Postgres path. The category-aware disclaimer gate is flag-off in tests.
    NULL AS category_kind,
    (
        SELECT pqs.content_quality_score
        FROM product_quality_snapshot pqs
        WHERE pqs.merchant_id = cp.merchant_id
          AND pqs.platform = cp.platform
          AND pqs.platform_product_id = cp.source_product_id
        ORDER BY pqs.snapshot_date DESC
        LIMIT 1
    ) AS content_quality_score,
    (
        SELECT pqs.model_readiness_score
        FROM product_quality_snapshot pqs
        WHERE pqs.merchant_id = cp.merchant_id
          AND pqs.platform = cp.platform
          AND pqs.platform_product_id = cp.source_product_id
        ORDER BY pqs.snapshot_date DESC
        LIMIT 1
    ) AS model_readiness_score,
    (
        SELECT pqs.conversion_potential_score
        FROM product_quality_snapshot pqs
        WHERE pqs.merchant_id = cp.merchant_id
          AND pqs.platform = cp.platform
          AND pqs.platform_product_id = cp.source_product_id
        ORDER BY pqs.snapshot_date DESC
        LIMIT 1
    ) AS conversion_potential_score,
    (
        SELECT pqs.rules_version
        FROM product_quality_snapshot pqs
        WHERE pqs.merchant_id = cp.merchant_id
          AND pqs.platform = cp.platform
          AND pqs.platform_product_id = cp.source_product_id
        ORDER BY pqs.snapshot_date DESC
        LIMIT 1
    ) AS quality_rules_version,
    (
        SELECT pqs.snapshot_date
        FROM product_quality_snapshot pqs
        WHERE pqs.merchant_id = cp.merchant_id
          AND pqs.platform = cp.platform
          AND pqs.platform_product_id = cp.source_product_id
        ORDER BY pqs.snapshot_date DESC
        LIMIT 1
    ) AS quality_scored_at,
    apv.image_url,
    apv.title AS pdp_title,
    apv.description AS pdp_description,
    apv.sync_status AS pdp_sync_status,
    apv.pdp_lifecycle_stage AS pdp_lifecycle_stage,
    apv.refreshed_at,
    (
        SELECT eps.seed_data
        FROM external_product_seeds eps
        WHERE eps.attached_product_key = cp.product_key
        ORDER BY eps.updated_at DESC
        LIMIT 1
    ) AS seed_data_json,
    (
        SELECT eps.title
        FROM external_product_seeds eps
        WHERE eps.attached_product_key = cp.product_key
        ORDER BY eps.updated_at DESC
        LIMIT 1
    ) AS seed_title,
    EXISTS (
        SELECT 1
        FROM catalog_offers co
        WHERE co.product_key = cp.product_key
          AND co.suppressed_at IS NULL
          AND co.list_price > 0
    ) AS has_price,
    -- has_us_offer is computed for REAL on this path (same currency predicate as
    -- Postgres) so the US-buyable gate is reachable in SQLite-backed tests; it
    -- used to be a hardcoded 0, which made the gate untestable.
    -- The two columns BELOW remain column-independent placeholders: beauty
    -- concerns/actives are computed only on the Postgres path.
    EXISTS (
        SELECT 1
        FROM catalog_offers co
        WHERE co.product_key = cp.product_key
          AND co.suppressed_at IS NULL
          AND co.list_price > 0
          AND upper(trim(coalesce(co.currency, ''))) = 'USD'
    ) AS has_us_offer,
    NULL AS has_category_concern,
    NULL AS has_key_actives,
    EXISTS (
        SELECT 1
        FROM external_offer_snapshots eos
        WHERE eos.canonical_url = cp.canonical_url
    ) AS has_offer_snapshot,
    (
        SELECT pgm.product_group_id
        FROM product_group_members pgm
        WHERE pgm.merchant_id = cp.merchant_id
          AND pgm.platform = cp.platform
          AND pgm.platform_product_id = cp.source_product_id
        LIMIT 1
    ) AS product_group_id,
    -- SQLite (test) path: non-blocking placeholders. The merchant-store
    -- lifecycle gate is computed on the Postgres path; a False/False pair here
    -- means merchant_store_retired is never true in tests unless a row is
    -- passed to _classify_product directly (the guard's unit test does this).
    0 AS merchant_has_store,
    0 AS merchant_has_active_store
FROM catalog_products cp
LEFT JOIN agent_pdp_view apv
    ON apv.content_key = cp.content_key
WHERE cp.content_key = :content_key
ORDER BY cp.content_key, cp.updated_at DESC, cp.product_key
"""

_UPSERT_QUERY = """
INSERT INTO index_pipeline_state (
    content_key,
    pivota_signature_id,
    merchant_id,
    pipeline_stage,
    blocker_code,
    blocker_detail,
    serving_eligible,
    index_eligible,
    content_quality_score,
    model_readiness_score,
    conversion_potential_score,
    quality_rules_version,
    quality_scored_at,
    extraction_content_hash,
    extractor_version,
    last_extracted_at,
    seed_audit_status,
    identity_resolved,
    product_group_id,
    has_image,
    has_price,
    description_length,
    consolidation_version,
    last_consolidated_at
) VALUES (
    :content_key,
    :pivota_signature_id,
    :merchant_id,
    :pipeline_stage,
    :blocker_code,
    :blocker_detail,
    :serving_eligible,
    :index_eligible,
    :content_quality_score,
    :model_readiness_score,
    :conversion_potential_score,
    :quality_rules_version,
    :quality_scored_at,
    :extraction_content_hash,
    :extractor_version,
    :last_extracted_at,
    :seed_audit_status,
    :identity_resolved,
    :product_group_id,
    :has_image,
    :has_price,
    :description_length,
    :consolidation_version,
    :last_consolidated_at
)
ON CONFLICT (content_key) DO UPDATE SET
    pivota_signature_id        = EXCLUDED.pivota_signature_id,
    merchant_id                = EXCLUDED.merchant_id,
    pipeline_stage             = EXCLUDED.pipeline_stage,
    blocker_code               = EXCLUDED.blocker_code,
    blocker_detail             = EXCLUDED.blocker_detail,
    serving_eligible           = EXCLUDED.serving_eligible,
    index_eligible             = EXCLUDED.index_eligible,
    content_quality_score      = EXCLUDED.content_quality_score,
    model_readiness_score      = EXCLUDED.model_readiness_score,
    conversion_potential_score = EXCLUDED.conversion_potential_score,
    quality_rules_version      = EXCLUDED.quality_rules_version,
    quality_scored_at          = EXCLUDED.quality_scored_at,
    extraction_content_hash    = EXCLUDED.extraction_content_hash,
    seed_audit_status          = EXCLUDED.seed_audit_status,
    identity_resolved          = EXCLUDED.identity_resolved,
    product_group_id           = EXCLUDED.product_group_id,
    has_image                  = EXCLUDED.has_image,
    has_price                  = EXCLUDED.has_price,
    description_length         = EXCLUDED.description_length,
    consolidation_version      = EXCLUDED.consolidation_version,
    last_consolidated_at       = EXCLUDED.last_consolidated_at
"""


_SERVING_ELIGIBLE_INDEX_ROWS_QUERY = """
SELECT
    content_key,
    blocker_code,
    pipeline_stage,
    last_consolidated_at
FROM index_pipeline_state
WHERE serving_eligible = TRUE
  AND content_key > :cursor
ORDER BY content_key
LIMIT :batch_size
"""

_SERVING_ELIGIBLE_INDEX_ROW_QUERY = """
SELECT
    content_key,
    blocker_code,
    pipeline_stage,
    last_consolidated_at
FROM index_pipeline_state
WHERE serving_eligible = TRUE
  AND content_key = :content_key
LIMIT 1
"""

_FAIL_CLOSE_QUERY = """
UPDATE index_pipeline_state
SET
    serving_eligible     = FALSE,
    index_eligible       = FALSE,
    blocker_code         = :blocker_code,
    blocker_detail       = :blocker_detail,
    consolidation_version = :consolidation_version,
    last_consolidated_at = :last_consolidated_at
WHERE content_key = :content_key
"""


def _is_sqlite_database() -> bool:
    db_url = str(getattr(database, "url", "") or "").lower()
    return db_url.startswith(("sqlite://", "sqlite+aiosqlite://"))


async def _fetch_regression_domains() -> Set[str]:
    rows = await database.fetch_all(
        "SELECT domain FROM domain_extractor_baselines "
        "WHERE alert_state = 'regression'"
    )
    return {dict(row)["domain"] for row in rows}


async def _fetch_eligibility_inputs(content_key: str) -> List[Dict[str, Any]]:
    """Fetch the current eligibility signal rows for one content_key."""
    if not content_key:
        return []
    query = _SINGLE_QUERY_SQLITE if _is_sqlite_database() else _SINGLE_QUERY
    rows = await database.fetch_all(query, {"content_key": content_key})
    return [dict(row) for row in rows]


async def _fetch_eligibility_inputs_for_content_keys(
    content_keys: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch eligibility signal rows for a page of content_keys.

    Production Postgres can fetch a full audit page in one query. SQLite tests
    keep the single-key fallback because the query shapes intentionally differ.
    """
    keys = [str(key or "").strip() for key in content_keys if str(key or "").strip()]
    if not keys:
        return {}
    grouped: Dict[str, List[Dict[str, Any]]] = {key: [] for key in keys}
    if _is_sqlite_database():
        for key in keys:
            grouped[key] = await _fetch_eligibility_inputs(key)
        return grouped

    rows = await database.fetch_all(_MULTI_QUERY, {"content_keys": keys})
    for row in rows or []:
        item = dict(row)
        key = str(item.get("content_key") or "").strip()
        if key:
            grouped.setdefault(key, []).append(item)
    return grouped


async def _upsert_index_pipeline_state(
    content_key: str,
    state: Dict[str, Any],
) -> None:
    """Upsert one classified state row into index_pipeline_state."""
    upsert_values = dict(state)
    upsert_values["content_key"] = content_key
    await database.execute(_UPSERT_QUERY, upsert_values)


async def _fetch_serving_eligible_index_rows(
    *,
    cursor: str = "",
    batch_size: int = 500,
    content_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch current IPS rows that claim public serving eligibility."""
    if content_key:
        rows = await database.fetch_all(
            _SERVING_ELIGIBLE_INDEX_ROW_QUERY,
            {"content_key": content_key},
        )
    else:
        rows = await database.fetch_all(
            _SERVING_ELIGIBLE_INDEX_ROWS_QUERY,
            {
                "cursor": cursor or "",
                "batch_size": max(int(batch_size or 500), 1),
            },
        )
    return [dict(row) for row in rows or []]


async def fail_close_index_pipeline_state(
    content_key: str,
    *,
    blocker_code: str,
    blocker_detail: str,
) -> None:
    """Demote an IPS row when source rows are gone and recompute cannot classify.

    Normal recompute derives state from catalog_products. If the catalog row was
    hard-deleted, there is nothing to classify, but public serving must still
    fail closed until the nightly orphan cleanup deletes derived IPS state.
    """
    await database.execute(
        _FAIL_CLOSE_QUERY,
        {
            "content_key": content_key,
            "blocker_code": blocker_code,
            "blocker_detail": blocker_detail,
            "consolidation_version": CONSOLIDATION_VERSION,
            "last_consolidated_at": datetime.now(timezone.utc),
        },
    )


async def upsert_classified_index_pipeline_state(
    content_key: str,
    state: Dict[str, Any],
) -> None:
    """Persist a state already produced by the canonical classifier.

    Bulk serving-contract repair uses the audit query to classify rows once.
    Reusing that state avoids a second single-key fetch per violation while
    still writing through the same index_pipeline_state upsert contract.
    """
    if not content_key:
        raise ValueError("content_key is required")
    if not isinstance(state, dict) or not state:
        raise ValueError("classified state is required")
    await _upsert_index_pipeline_state(content_key, state)


def _serving_contract_violation_from_state(
    current: Dict[str, Any],
    *,
    input_rows: int,
    expected: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ck = str(current.get("content_key") or "").strip()
    if expected is None:
        return {
            "content_key": ck,
            "current_blocker_code": current.get("blocker_code"),
            "current_pipeline_stage": current.get("pipeline_stage"),
            "expected_serving_eligible": False,
            "expected_blocker_code": "not_live",
            "expected_blocker_detail": (
                "no catalog_products rows found for content_key during "
                "serving contract audit"
            ),
            "expected_pipeline_stage": "discovered",
            "input_rows": input_rows,
        }
    return {
        "content_key": ck,
        "current_blocker_code": current.get("blocker_code"),
        "current_pipeline_stage": current.get("pipeline_stage"),
        "expected_serving_eligible": False,
        "expected_blocker_code": expected.get("blocker_code"),
        "expected_blocker_detail": expected.get("blocker_detail"),
        "expected_pipeline_stage": expected.get("pipeline_stage"),
        "input_rows": input_rows,
        "has_image": expected.get("has_image"),
        "has_price": expected.get("has_price"),
        "identity_resolved": expected.get("identity_resolved"),
        "description_length": expected.get("description_length"),
        "content_quality_score": expected.get("content_quality_score"),
        "_expected_state": expected,
    }


async def audit_serving_contract_violation_page(
    *,
    cursor: str = "",
    batch_size: int = 500,
    content_key: Optional[str] = None,
    regression_domains: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Classify one serving-eligible IPS page against the canonical contract."""
    page_size = max(int(batch_size or 500), 1)
    domains = (
        regression_domains
        if regression_domains is not None
        else await _fetch_regression_domains()
    )
    current_rows = await _fetch_serving_eligible_index_rows(
        cursor=cursor,
        batch_size=page_size,
        content_key=content_key,
    )
    violations: List[Dict[str, Any]] = []
    if not current_rows:
        return {
            "cursor": cursor or "",
            "next_cursor": cursor or "",
            "rows_scanned": 0,
            "done": True,
            "violations": violations,
        }

    content_keys = [
        str(current.get("content_key") or "").strip()
        for current in current_rows
        if str(current.get("content_key") or "").strip()
    ]
    input_rows_by_key = await _fetch_eligibility_inputs_for_content_keys(content_keys)

    for current in current_rows:
        ck = str(current.get("content_key") or "").strip()
        if not ck:
            continue
        input_rows = input_rows_by_key.get(ck, [])
        if not input_rows:
            violations.append(
                _serving_contract_violation_from_state(
                    current,
                    input_rows=0,
                    expected=None,
                )
            )
            continue
        expected = _classify_content_key_rows(input_rows, domains)
        if expected is not None and not bool(expected.get("serving_eligible")):
            violations.append(
                _serving_contract_violation_from_state(
                    current,
                    input_rows=len(input_rows),
                    expected=expected,
                )
            )

    next_cursor = str(current_rows[-1].get("content_key") or "")
    return {
        "cursor": cursor or "",
        "next_cursor": next_cursor,
        "rows_scanned": len(current_rows),
        "done": bool(content_key) or len(current_rows) < page_size,
        "violations": violations,
    }


async def audit_serving_contract_violations(
    *,
    limit: int = 500,
    batch_size: int = 500,
    content_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return IPS rows that are marked serving_eligible but no longer pass.

    This is the canonical serving-contract audit: it reuses the same
    _fetch_eligibility_inputs + _classify_content_key_rows path as realtime
    recompute instead of duplicating the gate in selector SQL.
    """
    max_results = int(limit or 0)
    page_size = max(int(batch_size or 500), 1)
    regression_domains = await _fetch_regression_domains()
    cursor = ""
    violations: List[Dict[str, Any]] = []

    while True:
        page = await audit_serving_contract_violation_page(
            cursor=cursor,
            batch_size=page_size,
            content_key=content_key,
            regression_domains=regression_domains,
        )
        if not int(page.get("rows_scanned") or 0):
            break
        for violation in page.get("violations") or []:
            violations.append(violation)
            if max_results > 0 and len(violations) >= max_results:
                return violations

        if page.get("done"):
            break
        cursor = str(page.get("next_cursor") or "")

    return violations


async def recompute_serving_eligibility(
    content_key: str,
    *,
    reason: Optional[str] = None,
) -> bool:
    """Re-evaluate serving_eligible for one content_key and upsert.

    Idempotent: calling repeatedly with the same DB state produces the same
    result + UPSERT. Safe under concurrent callers; the UPSERT handles
    last-writer-wins and subsequent callers see the latest data.

    Returns the computed serving_eligible boolean, or False if the row didn't
    exist / couldn't be evaluated. Never raises: failures are logged and the
    row stays in its current state for the nightly safety net to catch.
    """
    try:
        rows = await _fetch_eligibility_inputs(content_key)
        if not rows:
            return False

        regression_domains = await _fetch_regression_domains()
        new_state = _classify_content_key_rows(rows, regression_domains)
        if new_state is None:
            return False

        # Capture prior eligibility (cheap PK lookup) to detect the false->true
        # transition that makes a canonical PDP newly citable.
        prev_eligible = await database.fetch_val(
            "SELECT serving_eligible FROM index_pipeline_state "
            "WHERE content_key = :ck",
            {"ck": content_key},
        )

        await _upsert_index_pipeline_state(content_key, new_state)

        # IndexNow: when a page becomes newly serving-eligible (and has a minted
        # signature, so a canonical PDP exists), ask participating engines (Bing →
        # ChatGPT search, Yandex, …) to crawl it. Non-blocking + best-effort — it
        # never affects the recompute result.
        if new_state.get("serving_eligible") and not prev_eligible:
            sig = new_state.get("pivota_signature_id")
            if sig and str(sig).startswith("sig_"):
                from services.catalog_sync_service import pivota_canonical_pdp_url
                from services.indexnow import schedule_submit_url

                schedule_submit_url(pivota_canonical_pdp_url(str(sig)))

        if reason:
            logger.info({
                "event": "serving_eligibility_recomputed",
                "content_key": content_key,
                "reason": reason,
                "serving_eligible": new_state["serving_eligible"],
                "blocker_code": new_state["blocker_code"],
            })
        return bool(new_state["serving_eligible"])
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "event": "serving_eligibility_recompute_failed",
            "content_key": content_key,
            "reason": reason,
            "error": str(exc),
        })
        return False

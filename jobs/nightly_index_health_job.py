"""Nightly commerce index health consolidation job.

Fires at 04:00 UTC daily (see services/audit_scheduler.py). Reads from
three existing signal sources and writes a unified pipeline state row to
index_pipeline_state for every catalog_products row with a content_key.

Signal sources:
  1. product_quality_snapshot     — 3-axis quality scores
  2. external_product_seeds       — seed_data content + seed audit review_summary
  3. catalog_products             — sync_status, pdp_scope, pdp_lifecycle_stage
  4. catalog_offers               — has_price (list_price > 0)
  5. agent_pdp_view               — image_url, description (serving-layer fields)
  6. product_group_members        — identity resolution status

No LLM calls. All stage/blocker decisions are deterministic threshold checks.

Stage progression (highest satisfied stage wins):
  discovered    → catalog_products row with content_key exists
  crawled       → external_offer_snapshots row for the domain exists
  extracted     → seed_data non-empty with title present
  quality_gated → content_quality_score >= 0.65 AND has_image AND has_price
                  AND description_length >= 50
  shadow_indexed → serving_eligible=TRUE AND agent_pdp_view refreshed_at IS NOT NULL
  public_indexed → serving_eligible=TRUE AND agent_pdp_view sync_status='public'

Serving eligibility (ALL must be true):
  - content_quality_score >= 0.65
  - has_image = TRUE  (agent_pdp_view.image_url non-null/non-empty)
  - has_price = TRUE  (catalog_offers row with list_price > 0)
  - description_length >= 50 (agent_pdp_view.description stripped)
  - identity_resolved = TRUE (product_group_members row OR pdp_scope='canonical')
  - seed_audit_status IN ('no_issues_found', 'auto_corrected', 'not_audited')
  - sync_status = 'live' (catalog_products)
  - no extractor_regression blocker on the product's domain

Blocker codes (first failing check wins):
  no_seed           — no external_product_seeds row linked to this product_key
  no_extraction     — seed_data null or title missing
  low_quality       — content_quality_score < 0.65
  no_image          — image_url null or empty
  no_price          — no catalog_offers row with list_price > 0
  short_description — description present but stripped length < 50
  entity_unresolved — no product_group_members row AND pdp_scope != 'canonical'
  seed_audit_fail   — seed_data review_summary review_status = 'issues_unresolved'
  extractor_regression — domain in domain_extractor_baselines with alert_state='regression'
  none              — no blocker; product is at its natural pipeline stage

Design notes:
  - Batch-paginated: 500 products per batch, cursor on content_key.
  - Per-batch data fetched via a single LATERAL JOIN query (one round trip per batch).
  - Regression domains fetched once per run, not per batch.
  - Bulk upserted to index_pipeline_state via execute_many.
  - Domain scorecard computation runs after all batches complete.
  - A second-pass UPDATE applies extractor_regression blocker.
  - Best-effort per-batch: errors are caught and logged, job continues.
  - Advisory lock prevents double-runs from cron misfire stacking.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

CONSOLIDATION_VERSION = "nightly_index_health_v1"
SCORECARD_VERSION = "scorecard_v1"

TRACKED_FIELDS = ["title", "description", "image_url", "price"]
DEGRADED_THRESHOLD = 0.10    # coverage drop > 10% → 'degraded'
REGRESSION_THRESHOLD = 0.20  # coverage drop > 20% → 'regression'
MIN_DOMAIN_SAMPLE = 5        # minimum products per domain for scoring
BATCH_SIZE = 500

QUALITY_SCORE_THRESHOLD = 0.65
MIN_DESCRIPTION_LENGTH = 50

# Stable signed-int64 advisory lock id for this job.
# Derived from the job name so it never collides with per-merchant locks.
_JOB_LOCK_ID: int = int.from_bytes(
    hashlib.sha256(b"nightly_index_health_job").digest()[:8],
    byteorder="big",
    signed=True,
)

# Seed audit statuses that do NOT block serving.
_SEED_AUDIT_OK = {"no_issues_found", "auto_corrected", "not_audited"}


# ---------------------------------------------------------------------------
# Advisory lock helpers (single lock for the whole job run)
# ---------------------------------------------------------------------------

async def _try_acquire_job_lock() -> bool:
    from db.database import database
    db_url = str(getattr(database, "url", "") or "")
    if not db_url.startswith(("postgres://", "postgresql://")):
        return True  # SQLite / test backend — no advisory lock needed
    try:
        row = await database.fetch_one(
            "SELECT pg_try_advisory_lock(:lock_id) AS got",
            {"lock_id": _JOB_LOCK_ID},
        )
        return bool(row and row["got"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("nightly_index_health: advisory lock attempt failed: %s", exc)
        return True  # fail open — better to double-run than block


async def _release_job_lock() -> None:
    from db.database import database
    db_url = str(getattr(database, "url", "") or "")
    if not db_url.startswith(("postgres://", "postgresql://")):
        return
    try:
        await database.execute(
            "SELECT pg_advisory_unlock(:lock_id)",
            {"lock_id": _JOB_LOCK_ID},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("nightly_index_health: advisory unlock failed: %s", exc)


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------

def _make_extraction_content_hash(
    title: Optional[str],
    description: Optional[str],
    image_url: Optional[str],
) -> str:
    blob = f"{title or ''}::{description or ''}::{image_url or ''}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Per-product classification
# ---------------------------------------------------------------------------

def _classify_product(
    row: Dict[str, Any],
    regression_domains: Set[str],
) -> Dict[str, Any]:
    """Derive pipeline_stage, blocker_code, serving_eligible from a fetched row.

    `row` contains columns from the LATERAL JOIN batch query.
    Returns a dict of index_pipeline_state columns ready for upsert.
    """
    now = datetime.now(timezone.utc)

    # --- Extract seed signals ---
    seed_data = row.get("seed_data_json") or {}
    if isinstance(seed_data, str):
        try:
            seed_data = json.loads(seed_data)
        except (json.JSONDecodeError, TypeError):
            seed_data = {}

    seed_title = seed_data.get("title") or ""
    review_summary = seed_data.get("review_summary") or {}
    if isinstance(review_summary, str):
        try:
            review_summary = json.loads(review_summary)
        except (json.JSONDecodeError, TypeError):
            review_summary = {}
    seed_review_status = review_summary.get("review_status") or ""
    seed_audit_status = (
        seed_review_status if seed_review_status else "not_audited"
    )

    # --- Extract quality signals ---
    quality_score = row.get("content_quality_score")
    model_readiness = row.get("model_readiness_score")
    conversion_potential = row.get("conversion_potential_score")
    quality_rules_version = row.get("quality_rules_version")
    quality_scored_at = row.get("quality_scored_at")

    # --- Extract PDP view signals ---
    pdp_image_url = (row.get("image_url") or "").strip()
    pdp_description = (row.get("pdp_description") or "").strip()
    pdp_sync_status = row.get("pdp_sync_status") or ""
    pdp_refreshed_at = row.get("refreshed_at")

    has_image = bool(pdp_image_url)
    has_price = bool(row.get("has_price"))
    description_length = len(pdp_description)

    # --- Extract identity signals ---
    pdp_scope = (row.get("pdp_scope") or "").strip()
    product_group_id = row.get("product_group_id")
    identity_resolved = bool(product_group_id) or (pdp_scope == "canonical")

    # --- Domain regression check ---
    canonical_url = (row.get("canonical_url") or "").strip()
    domain = _extract_domain(canonical_url)
    domain_has_regression = domain in regression_domains if domain else False

    # --- Extraction content hash ---
    extraction_content_hash = _make_extraction_content_hash(
        seed_title or pdp_description[:80] or None,
        pdp_description or None,
        pdp_image_url or None,
    )

    # --- Determine blocker (first failing check wins) ---
    blocker_code = "none"
    blocker_detail: Optional[str] = None

    if not seed_data or not seed_title:
        if not seed_data:
            blocker_code = "no_seed"
            blocker_detail = "no external_product_seeds row attached to this product_key"
        else:
            blocker_code = "no_extraction"
            blocker_detail = "seed_data present but title is missing"
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
        blocker_detail = "no product_group_members row and pdp_scope != 'canonical'"
    elif seed_audit_status == "issues_unresolved":
        blocker_code = "seed_audit_fail"
        blocker_detail = "seed content audit reported issues_unresolved"
    elif domain_has_regression:
        blocker_code = "extractor_regression"
        blocker_detail = f"domain {domain!r} has regression alert in domain_extractor_baselines"

    # --- Serving eligibility (all criteria must pass) ---
    serving_eligible = (
        blocker_code == "none"
        and bool(seed_title)
        and quality_score is not None
        and quality_score >= QUALITY_SCORE_THRESHOLD
        and has_image
        and has_price
        and description_length >= MIN_DESCRIPTION_LENGTH
        and identity_resolved
        and seed_audit_status in _SEED_AUDIT_OK
        and not domain_has_regression
    )

    # --- Stage assignment (highest satisfied stage wins) ---
    if serving_eligible and pdp_sync_status == "public":
        pipeline_stage = "public_indexed"
    elif serving_eligible and pdp_refreshed_at is not None:
        pipeline_stage = "shadow_indexed"
    elif (
        quality_score is not None
        and quality_score >= QUALITY_SCORE_THRESHOLD
        and has_image
        and has_price
        and description_length >= MIN_DESCRIPTION_LENGTH
    ):
        pipeline_stage = "quality_gated"
    elif seed_title:
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


def _extract_domain(url: str) -> Optional[str]:
    """Extract apex domain from a URL. Returns None if URL is empty/invalid."""
    if not url:
        return None
    try:
        # Minimal domain extraction: strip scheme + path
        s = url.split("//", 1)[-1]  # remove scheme
        host = s.split("/")[0].split("?")[0].split("#")[0]  # remove path
        host = host.lower().strip()
        if not host:
            return None
        # Strip www.
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Batch query
# ---------------------------------------------------------------------------

_BATCH_QUERY = """
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
    pqs.content_quality_score,
    pqs.model_readiness_score,
    pqs.conversion_potential_score,
    pqs.rules_version           AS quality_rules_version,
    pqs.snapshot_date           AS quality_scored_at,
    apv.image_url,
    apv.description             AS pdp_description,
    apv.sync_status             AS pdp_sync_status,
    apv.refreshed_at,
    eps.seed_data               AS seed_data_json,
    (
        SELECT TRUE
        FROM catalog_offers co
        WHERE co.product_key = cp.product_key
          AND co.list_price > 0
        LIMIT 1
    )                           AS has_price,
    (
        SELECT TRUE
        FROM external_offer_snapshots eos
        WHERE eos.canonical_url = cp.canonical_url
        LIMIT 1
    )                           AS has_offer_snapshot,
    pgm.product_group_id
FROM catalog_products cp
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
    SELECT seed_data
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
WHERE cp.content_key IS NOT NULL
  AND cp.sync_status = 'live'
  AND cp.content_key > :cursor
ORDER BY cp.content_key
LIMIT :batch_size
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


# ---------------------------------------------------------------------------
# Domain extractor scorecards
# ---------------------------------------------------------------------------

_SCORECARD_QUERY = """
SELECT
    eos.domain,
    COUNT(*)                                                          AS total,
    COUNT(CASE WHEN (eps.seed_data->>'title') IS NOT NULL
                AND LENGTH(eps.seed_data->>'title') > 0
               THEN 1 END)                                           AS has_title,
    COUNT(CASE WHEN (eps.seed_data->>'description') IS NOT NULL
                AND LENGTH(eps.seed_data->>'description') >= 10
               THEN 1 END)                                           AS has_description,
    COUNT(CASE WHEN (eps.seed_data->>'image_url') IS NOT NULL
               THEN 1 END)                                           AS has_image,
    COUNT(CASE WHEN co.list_price > 0
               THEN 1 END)                                           AS has_price
FROM external_offer_snapshots eos
JOIN external_product_seeds eps
    ON eps.canonical_url = eos.canonical_url
LEFT JOIN catalog_products cp
    ON cp.product_key = eps.attached_product_key
LEFT JOIN catalog_offers co
    ON co.product_key = cp.product_key
   AND co.list_price > 0
WHERE eos.last_checked_at > NOW() - INTERVAL '72 hours'
GROUP BY eos.domain
HAVING COUNT(*) >= :min_sample
ORDER BY COUNT(*) DESC
LIMIT 500
"""

_BASELINE_FETCH_QUERY = """
SELECT domain, baseline_coverage, current_coverage, alert_state, baseline_set_at
FROM domain_extractor_baselines
WHERE domain = ANY(:domains)
"""

_BASELINE_UPSERT_QUERY = """
INSERT INTO domain_extractor_baselines (
    domain,
    baseline_coverage,
    current_coverage,
    sample_size,
    alert_state,
    regression_details,
    scorecard_version,
    last_scored_at,
    baseline_set_at
) VALUES (
    :domain,
    :baseline_coverage,
    :current_coverage,
    :sample_size,
    :alert_state,
    :regression_details,
    :scorecard_version,
    :last_scored_at,
    :baseline_set_at
)
ON CONFLICT (domain) DO UPDATE SET
    current_coverage  = EXCLUDED.current_coverage,
    sample_size       = EXCLUDED.sample_size,
    alert_state       = EXCLUDED.alert_state,
    regression_details = EXCLUDED.regression_details,
    scorecard_version = EXCLUDED.scorecard_version,
    last_scored_at    = EXCLUDED.last_scored_at,
    baseline_coverage = EXCLUDED.baseline_coverage,
    baseline_set_at   = EXCLUDED.baseline_set_at
"""

# Second-pass UPDATE: mark index_pipeline_state rows with extractor_regression
# blocker for any product whose canonical_url maps to a domain in regression.
# Only overrides 'none' blocker — more fundamental blockers take precedence.
_REGRESSION_BLOCKER_UPDATE = """
UPDATE index_pipeline_state ips
SET
    blocker_code     = 'extractor_regression',
    blocker_detail   = 'domain extractor regression detected; fix domain template before serving',
    serving_eligible = FALSE,
    last_consolidated_at = NOW()
FROM external_product_seeds eps
JOIN external_offer_snapshots eos
    ON eos.canonical_url = eps.canonical_url
JOIN domain_extractor_baselines deb
    ON deb.domain = eos.domain
WHERE eps.attached_product_key = (
    SELECT product_key FROM catalog_products WHERE content_key = ips.content_key LIMIT 1
)
  AND deb.alert_state = 'regression'
  AND ips.blocker_code = 'none'
"""


async def _compute_domain_extractor_scorecards() -> Dict[str, Any]:
    """Compute per-domain extraction quality scores and update baselines.

    Returns a summary of domains updated + regression count.
    """
    from db.database import database

    now = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {
        "domains_scored": 0,
        "domains_regressed": 0,
        "domains_recovered": 0,
        "errors": [],
    }

    try:
        rows = await database.fetch_all(
            _SCORECARD_QUERY,
            {"min_sample": MIN_DOMAIN_SAMPLE},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("nightly_index_health: scorecard query failed: %s", exc)
        summary["errors"].append(f"scorecard_query: {exc!r}")
        return summary

    if not rows:
        return summary

    domains = [dict(r)["domain"] for r in rows]

    # Fetch existing baselines in one query
    try:
        baseline_rows = await database.fetch_all(
            _BASELINE_FETCH_QUERY,
            {"domains": domains},
        )
        baselines: Dict[str, Dict] = {
            dict(r)["domain"]: dict(r) for r in baseline_rows
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("nightly_index_health: baseline fetch failed: %s", exc)
        baselines = {}

    upsert_values: List[Dict[str, Any]] = []

    for raw in rows:
        row = dict(raw)
        domain = row["domain"]
        total = row["total"] or 0
        if total < MIN_DOMAIN_SAMPLE:
            continue

        current_coverage = {
            "title": round((row.get("has_title") or 0) / total, 4),
            "description": round((row.get("has_description") or 0) / total, 4),
            "image_url": round((row.get("has_image") or 0) / total, 4),
            "price": round((row.get("has_price") or 0) / total, 4),
        }

        prior = baselines.get(domain)
        prior_alert = prior["alert_state"] if prior else "new"
        prior_baseline = prior["baseline_coverage"] if prior else {}
        if isinstance(prior_baseline, str):
            try:
                prior_baseline = json.loads(prior_baseline)
            except (json.JSONDecodeError, TypeError):
                prior_baseline = {}

        # Compute drops vs. baseline
        drops = {}
        for field in TRACKED_FIELDS:
            baseline_val = prior_baseline.get(field)
            if baseline_val is not None:
                drop = float(baseline_val) - current_coverage.get(field, 0.0)
                if drop > 0:
                    drops[field] = drop

        max_drop = max(drops.values(), default=0.0)

        if not prior or prior_alert == "new":
            alert_state = "ok"
            new_baseline = current_coverage
            baseline_set_at = now
        elif max_drop >= REGRESSION_THRESHOLD:
            alert_state = "regression"
            new_baseline = prior_baseline
            baseline_set_at = prior["baseline_set_at"]
            summary["domains_regressed"] += 1
        elif max_drop >= DEGRADED_THRESHOLD:
            alert_state = "degraded"
            new_baseline = prior_baseline
            baseline_set_at = prior["baseline_set_at"]
        else:
            alert_state = "ok"
            if prior_alert in ("degraded", "regression"):
                # Recovery: reset baseline to current
                new_baseline = current_coverage
                baseline_set_at = now
                summary["domains_recovered"] += 1
            else:
                new_baseline = prior_baseline if prior_baseline else current_coverage
                baseline_set_at = prior["baseline_set_at"] if prior else now

        regression_details: List[Dict] = []
        if alert_state in ("degraded", "regression"):
            for field, drop in drops.items():
                if drop >= DEGRADED_THRESHOLD:
                    regression_details.append({
                        "field": field,
                        "baseline": prior_baseline.get(field),
                        "current": current_coverage.get(field),
                        "drop": round(drop, 4),
                    })

        upsert_values.append({
            "domain": domain,
            "baseline_coverage": json.dumps(new_baseline),
            "current_coverage": json.dumps(current_coverage),
            "sample_size": total,
            "alert_state": alert_state,
            "regression_details": json.dumps(regression_details),
            "scorecard_version": SCORECARD_VERSION,
            "last_scored_at": now,
            "baseline_set_at": baseline_set_at if isinstance(baseline_set_at, datetime)
                               else now,
        })

    if upsert_values:
        try:
            await database.execute_many(_BASELINE_UPSERT_QUERY, upsert_values)
            summary["domains_scored"] = len(upsert_values)
        except Exception as exc:  # noqa: BLE001
            logger.warning("nightly_index_health: baseline upsert failed: %s", exc)
            summary["errors"].append(f"baseline_upsert: {exc!r}")

    return summary


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_nightly_index_health() -> Dict[str, Any]:
    """Entry point called by APScheduler at 04:00 UTC.

    Returns a summary dict for logging. Never raises — all errors are
    caught, logged, and included in the summary.
    """
    from db.database import database

    run_start = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {
        "run_start": run_start.isoformat(),
        "consolidation_version": CONSOLIDATION_VERSION,
        "total_processed": 0,
        "eligible_count": 0,
        "stage_counts": {},
        "domain_baselines_updated": 0,
        "regression_domains": 0,
        "recovered_domains": 0,
        "batch_errors": 0,
        "errors": [],
    }

    # 1. Acquire job-level advisory lock
    if not await _try_acquire_job_lock():
        logger.info(
            "nightly_index_health: another instance holds the advisory lock; "
            "skipping this run"
        )
        summary["skipped"] = True
        return summary

    try:
        # 2. Fetch regression domains once per run (used in per-product classification)
        try:
            regression_rows = await database.fetch_all(
                "SELECT domain FROM domain_extractor_baselines "
                "WHERE alert_state = 'regression'"
            )
            regression_domains: Set[str] = {
                dict(r)["domain"] for r in regression_rows
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "nightly_index_health: regression domain fetch failed: %s", exc
            )
            regression_domains = set()

        # 3. Cursor-paginated batch loop
        cursor = ""  # empty string sorts before all content_keys (VARCHAR)
        stage_counts: Dict[str, int] = {}
        total_processed = 0
        eligible_count = 0

        while True:
            try:
                rows = await database.fetch_all(
                    _BATCH_QUERY,
                    {"cursor": cursor, "batch_size": BATCH_SIZE},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "nightly_index_health: batch fetch failed (cursor=%r): %s",
                    cursor, exc,
                )
                summary["batch_errors"] += 1
                summary["errors"].append(f"batch_fetch cursor={cursor!r}: {exc!r}")
                break

            if not rows:
                break

            batch_states: List[Dict[str, Any]] = []
            for raw in rows:
                row = dict(raw)
                try:
                    state = _classify_product(row, regression_domains)
                    batch_states.append(state)
                    stage = state["pipeline_stage"]
                    stage_counts[stage] = stage_counts.get(stage, 0) + 1
                    if state["serving_eligible"]:
                        eligible_count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "nightly_index_health: classify failed for "
                        "content_key=%r: %s",
                        row.get("content_key"), exc,
                    )

            if batch_states:
                try:
                    await database.execute_many(_UPSERT_QUERY, batch_states)
                    total_processed += len(batch_states)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "nightly_index_health: upsert failed for batch "
                        "starting at cursor=%r: %s",
                        cursor, exc,
                    )
                    summary["batch_errors"] += 1
                    summary["errors"].append(f"batch_upsert cursor={cursor!r}: {exc!r}")

            # Advance cursor to the last content_key in this batch
            cursor = dict(rows[-1])["content_key"]

            if len(rows) < BATCH_SIZE:
                break  # last batch

        summary["total_processed"] = total_processed
        summary["eligible_count"] = eligible_count
        summary["stage_counts"] = stage_counts

        # 4. Compute domain extractor scorecards
        try:
            scorecard_summary = await _compute_domain_extractor_scorecards()
            summary["domain_baselines_updated"] = scorecard_summary.get(
                "domains_scored", 0
            )
            summary["regression_domains"] = scorecard_summary.get(
                "domains_regressed", 0
            )
            summary["recovered_domains"] = scorecard_summary.get(
                "domains_recovered", 0
            )
            if scorecard_summary.get("errors"):
                summary["errors"].extend(scorecard_summary["errors"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "nightly_index_health: scorecard computation failed: %s", exc
            )
            summary["errors"].append(f"scorecards: {exc!r}")

        # 5. Second-pass UPDATE: apply extractor_regression blocker
        # (only runs if any regression domains exist after scorecard update)
        try:
            regression_count_row = await database.fetch_one(
                "SELECT COUNT(*) AS n FROM domain_extractor_baselines "
                "WHERE alert_state = 'regression'"
            )
            has_regressions = (
                regression_count_row and dict(regression_count_row).get("n", 0) > 0
            )
            if has_regressions:
                await database.execute(_REGRESSION_BLOCKER_UPDATE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "nightly_index_health: regression blocker UPDATE failed: %s", exc
            )
            summary["errors"].append(f"regression_blocker_update: {exc!r}")

    finally:
        await _release_job_lock()

    run_end = datetime.now(timezone.utc)
    duration_s = (run_end - run_start).total_seconds()
    summary["run_end"] = run_end.isoformat()
    summary["duration_seconds"] = round(duration_s, 1)

    logger.info(
        "nightly_index_health: completed in %.1fs — "
        "processed=%d eligible=%d stages=%s regression_domains=%d errors=%d",
        duration_s,
        summary["total_processed"],
        summary["eligible_count"],
        summary["stage_counts"],
        summary["regression_domains"],
        len(summary["errors"]),
    )
    return summary

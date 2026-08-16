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
  extracted     → source document present:
                  external seed title OR agent_pdp_view title+description
  quality_gated → content_quality_score >= QUALITY_SCORE_THRESHOLD AND has_image
                  AND has_price
                  AND description_length >= 50
  shadow_indexed → serving_eligible=TRUE AND agent_pdp_view refreshed_at IS NOT NULL
  public_indexed → serving_eligible=TRUE AND apv.pdp_lifecycle_stage='published'

Serving eligibility (ALL must be true):
  - content_quality_score >= QUALITY_SCORE_THRESHOLD  (0-100 scale; the constant
    lives in services/index_pipeline_state_service.py and is 71.4, not 65 —
    see services/product_quality_service.py for why the scale moved)
  - has_image = TRUE  (agent_pdp_view.image_url non-null/non-empty)
  - has_price = TRUE  (catalog_offers row with list_price > 0)
  - description_length >= 50 (agent_pdp_view.description stripped)
  - identity_resolved = TRUE (product_group_members row OR resolved pdp_scope)
  - seed_audit_status IN ('no_issues_found', 'auto_corrected', 'not_audited')
  - catalog_products.sync_status = 'live'
  - no extractor_regression blocker on the product's domain

Stale invalidation: a second-pass UPDATE demotes any index_pipeline_state row
whose catalog_products.sync_status is no longer 'live' to serving_eligible=FALSE
with blocker_code='not_live'. This protects against rows that were eligible
yesterday but archived today.

Blocker codes (first failing check wins):
  not_live          — catalog_products.sync_status is not 'live' (stale/archived)
  non_core_product  — sample/gift/protection/GWP row not eligible for commerce index
  no_seed           — no external seed and no APV-backed source document
  no_extraction     — a source row exists but no usable source title/document
  not_scored        — content_quality_score IS NULL (never scored; NOT a quality verdict)
  low_quality       — content_quality_score < QUALITY_SCORE_THRESHOLD (0-100 scale)
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

from services.index_pipeline_state_service import (
    CONSOLIDATION_VERSION,
    MIN_DESCRIPTION_LENGTH,
    QUALITY_SCORE_THRESHOLD,
    _BATCH_QUERY,
    _UPSERT_QUERY,
    _classify_product,
    _coerce_dict,
    _extract_domain,
    _has_seed_row,
    _is_non_core_product,
    _make_extraction_content_hash,
    _resolve_seed_title,
    _select_content_key_state,
    _state_rank,
)

logger = logging.getLogger(__name__)

SCORECARD_VERSION = "scorecard_v1"

TRACKED_FIELDS = ["title", "description", "image_url", "price"]
DEGRADED_THRESHOLD = 0.10    # coverage drop > 10% → 'degraded'
REGRESSION_THRESHOLD = 0.20  # coverage drop > 20% → 'regression'
MIN_DOMAIN_SAMPLE = 5        # minimum products per domain for scoring
BATCH_SIZE = 500

# Stable signed-int64 advisory lock id for this job.
# Derived from the job name so it never collides with per-merchant locks.
_JOB_LOCK_ID: int = int.from_bytes(
    hashlib.sha256(b"nightly_index_health_job").digest()[:8],
    byteorder="big",
    signed=True,
)


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
# Domain extractor scorecards
# ---------------------------------------------------------------------------

_SCORECARD_QUERY = """
WITH seeds_in_window AS (
    -- Dedupe to one row per (domain, seed). Without DISTINCT, a seed with N
    -- offer snapshots in the window would be counted N times, skewing
    -- coverage rates and triggering false regressions.
    SELECT DISTINCT
        eos.domain,
        eps.id          AS seed_id,
        eps.title       AS seed_title_top,
        eps.seed_data   AS seed_data,
        eps.attached_product_key
    FROM external_offer_snapshots eos
    JOIN external_product_seeds eps
        ON eps.canonical_url = eos.canonical_url
    WHERE eos.last_checked_at > NOW() - INTERVAL '72 hours'
)
SELECT
    s.domain,
    COUNT(*) AS total,
    -- Title: snapshot.title → top-level seed.title → seed_data.title
    COUNT(CASE WHEN length(coalesce(
            nullif(s.seed_data->'snapshot'->>'title', ''),
            nullif(s.seed_title_top, ''),
            nullif(s.seed_data->>'title', '')
        )) > 0 THEN 1 END) AS has_title,
    -- Description: snapshot.description → seed_data.description (>=10 chars)
    COUNT(CASE WHEN length(coalesce(
            nullif(s.seed_data->'snapshot'->>'description', ''),
            nullif(s.seed_data->>'description', '')
        )) >= 10 THEN 1 END) AS has_description,
    -- Image: snapshot.image_url → seed_data.image_url → first images[]
    COUNT(CASE WHEN length(coalesce(
            nullif(s.seed_data->'snapshot'->>'image_url', ''),
            nullif(s.seed_data->>'image_url', ''),
            nullif(s.seed_data->'images'->>0, '')
        )) > 0 THEN 1 END) AS has_image,
    -- Price: EXISTS on catalog_offers — never multiplies the seed count
    COUNT(CASE WHEN EXISTS (
            SELECT 1
            FROM catalog_offers co
            WHERE co.product_key = s.attached_product_key
              AND co.suppressed_at IS NULL
              AND co.list_price > 0
        ) THEN 1 END) AS has_price
FROM seeds_in_window s
GROUP BY s.domain
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

# Orphan count / DELETE: catalog_products rows can be hard-deleted by the
# Shopify catalog source prune (services/catalog_sync_service.py:1450). An
# orphan IPS row (no matching catalog_products.content_key) can otherwise
# stay serving_eligible=TRUE forever because the live-only batch query
# never reaches it. Delete rather than UPDATE — IPS is derived state and
# the source row is gone; if catalog re-imports the product, the nightly
# job will rebuild the IPS row from scratch.
_ORPHAN_COUNT_QUERY = """
SELECT COUNT(*) AS n
FROM index_pipeline_state ips
WHERE NOT EXISTS (
    SELECT 1 FROM catalog_products cp WHERE cp.content_key = ips.content_key
)
"""

_IPS_TOTAL_COUNT_QUERY = "SELECT COUNT(*) AS n FROM index_pipeline_state"

_ORPHAN_DELETE = """
DELETE FROM index_pipeline_state ips
WHERE NOT EXISTS (
    SELECT 1 FROM catalog_products cp WHERE cp.content_key = ips.content_key
)
"""

# Stale invalidation UPDATE: any IPS row whose catalog_products.sync_status
# is no longer 'live' must lose serving_eligible. Runs unconditionally before
# the regression blocker pass so a previously-eligible product that was
# archived since the last run never lingers as eligible.
_STALE_INVALIDATION_UPDATE = """
UPDATE index_pipeline_state ips
SET
    serving_eligible     = FALSE,
    index_eligible       = FALSE,
    blocker_code         = 'not_live',
    blocker_detail       = 'catalog_products.sync_status=' || quote_literal(cp.sync_status),
    last_consolidated_at = NOW()
FROM catalog_products cp
WHERE cp.content_key = ips.content_key
  AND cp.sync_status IS DISTINCT FROM 'live'
  AND NOT EXISTS (
      SELECT 1
      FROM catalog_products live_cp
      WHERE live_cp.content_key = ips.content_key
        AND live_cp.sync_status = 'live'
  )
  AND (
        ips.serving_eligible = TRUE
        OR ips.index_eligible = TRUE
        OR ips.blocker_code <> 'not_live'
      )
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
    index_eligible   = FALSE,
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
# IndexNow transition detection (snapshot / diff)
# ---------------------------------------------------------------------------
# Why snapshot-and-diff rather than per-batch bookkeeping: this job demotes rows
# through FOUR different paths and only one of them passes through the batch
# upsert. `_STALE_INVALIDATION_UPDATE` (archived products — the
# advertised-URL-now-404 case this feature exists for), `_ORPHAN_DELETE` and
# `_REGRESSION_BLOCKER_UPDATE` are all raw SQL against index_pipeline_state, and
# `_BATCH_QUERY` filters `sync_status = 'live'` so an archived row never enters
# batch_states at all. Diffing the whole table before/after also removes the
# ordering hazard in the other direction: the regression blocker runs AFTER the
# batches, so a per-batch tally could queue "please crawl" for a row the same
# run then made ineligible.
#
# EVERYTHING HERE FAILS OPEN TO SILENCE. A snapshot that cannot be taken
# returns None and the diff is skipped — this is a best-effort discovery ping,
# and it must never be able to break the nightly reclassification.

_ELIGIBILITY_SNAPSHOT_SQL = """
    SELECT content_key, serving_eligible, pivota_signature_id
    FROM index_pipeline_state
"""


async def _snapshot_serving_eligibility() -> Optional[Dict[str, Dict[str, Any]]]:
    """content_key -> {serving_eligible, pivota_signature_id}, or None on failure."""
    from db.database import database
    try:
        rows = await database.fetch_all(_ELIGIBILITY_SNAPSHOT_SQL)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "nightly_index_health: eligibility snapshot failed (indexnow "
            "transition pings disabled for this run): %s", exc,
        )
        return None
    snapshot: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        content_key = row.get("content_key")
        if not content_key:
            continue
        snapshot[str(content_key)] = {
            "serving_eligible": bool(row.get("serving_eligible")),
            "pivota_signature_id": str(row.get("pivota_signature_id") or ""),
        }
    return snapshot


async def _submit_indexnow_transitions(
    before: Optional[Dict[str, Dict[str, Any]]],
    summary: Dict[str, Any],
) -> None:
    """Diff post-run eligibility against `before` and ping IndexNow once."""
    summary["indexnow_added"] = 0
    summary["indexnow_removed"] = 0
    if before is None:
        summary["indexnow_skipped"] = "snapshot_unavailable"
        return

    after = await _snapshot_serving_eligibility()
    if after is None:
        summary["indexnow_skipped"] = "post_snapshot_unavailable"
        return

    from services.catalog_sync_service import pivota_canonical_pdp_url

    added: List[str] = []
    removed: List[str] = []

    def _sig_url(sig: str) -> Optional[str]:
        return pivota_canonical_pdp_url(sig) if sig.startswith("sig_") else None

    for content_key, now in after.items():
        was = before.get(content_key)
        if now["serving_eligible"] and not (was and was["serving_eligible"]):
            url = _sig_url(now["pivota_signature_id"])
            if url:
                added.append(url)
        elif was and was["serving_eligible"] and not now["serving_eligible"]:
            # Ping the sig that was ADVERTISED, not the survivor: IPS is
            # content_key-grained and stores the MAX-rank row's sig, so a
            # takedown can change which sig the row reports.
            url = _sig_url(was["pivota_signature_id"])
            if url:
                removed.append(url)

    # Rows deleted outright (_ORPHAN_DELETE) are removals too — they leave the
    # after-snapshot entirely, so the loop above never sees them.
    for content_key, was in before.items():
        if content_key in after or not was["serving_eligible"]:
            continue
        url = _sig_url(was["pivota_signature_id"])
        if url:
            removed.append(url)

    summary["indexnow_added"] = len(added)
    summary["indexnow_removed"] = len(removed)
    if not added and not removed:
        return

    # REMOVALS FIRST: the protocol caps a submission at 10k URLs, and a large
    # sweep is exactly when the cap bites. Truncating adds costs a delayed
    # crawl of a live page (organic discovery still finds it); truncating
    # removals leaves a dead URL advertised, which is the churn this exists to
    # stop. Order encodes that priority.
    transition_urls = (removed + added)[:10_000]
    try:
        from services.indexnow import submit_urls

        await submit_urls(transition_urls)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nightly_index_health: indexnow submit failed: %s", exc)


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

        # IndexNow: eligibility SNAPSHOT taken before any mutation in this run.
        # Diffed against a second snapshot after ALL mutations (batch upserts,
        # orphan delete, stale invalidation, scorecards, regression blocker), so
        # every demotion path this job owns is covered — a per-batch tally
        # missed three of them (_STALE_INVALIDATION_UPDATE, _ORPHAN_DELETE and
        # _REGRESSION_BLOCKER_UPDATE never pass through batch_states, and the
        # stale-invalidation path is exactly the archived-product-with-a-dead-URL
        # case this feature exists for), and it could also queue an ADD for a row
        # the same run demoted moments later via the regression blocker.
        # Snapshot-diff makes both classes structurally impossible.
        eligibility_before = await _snapshot_serving_eligibility()

        # 3. Cursor-paginated batch loop
        cursor = ""  # empty string sorts before all content_keys (VARCHAR)
        stage_counts: Dict[str, int] = {}
        total_processed = 0
        eligible_count = 0
        # P1.5 graduation ladder: check the kill-switch once per run so the
        # per-content_key loop is skipped entirely (no overhead) when dark.
        from services.index_graduation_ladder import (
            advance_from_state,
            graduation_ladder_enabled,
        )
        graduation_enabled = graduation_ladder_enabled()
        graduated_count = 0

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

            states_by_content_key: Dict[str, List[Dict[str, Any]]] = {}
            for raw in rows:
                row = dict(raw)
                try:
                    state = _classify_product(row, regression_domains)
                    states_by_content_key.setdefault(row["content_key"], []).append(state)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "nightly_index_health: classify failed for "
                        "content_key=%r: %s",
                        row.get("content_key"), exc,
                    )

            batch_states = [
                _select_content_key_state(candidate_states)
                for candidate_states in states_by_content_key.values()
            ]
            for state in batch_states:
                stage = state["pipeline_stage"]
                stage_counts[stage] = stage_counts.get(stage, 0) + 1
                if state["serving_eligible"]:
                    eligible_count += 1

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

            # Convergence P1.5: advance the observed-track graduation ladder for
            # each freshly classified content_key, using the state we just
            # computed (no extra recompute). Dark unless
            # INDEX_GRADUATION_LADDER_ENABLED; every call is best-effort and the
            # writer is monotonic (referral_only → knowledge_ready →
            # commerce_ready), so a re-run never downgrades.
            if graduation_enabled and batch_states:
                for state in batch_states:
                    try:
                        result = await advance_from_state(
                            state["content_key"], state, reason="nightly_index_health",
                        )
                        graduated_count += int(result.get("advanced") or 0)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "nightly_index_health: graduation advance failed for "
                            "content_key=%r: %s",
                            state.get("content_key"), exc,
                        )

            # Advance cursor to the last distinct content_key in this batch.
            # The query pages by content key but returns all live catalog rows
            # for each key, so raw row count can exceed BATCH_SIZE.
            batch_content_keys = sorted({dict(row)["content_key"] for row in rows})
            if not batch_content_keys:
                break
            cursor = batch_content_keys[-1]

            if len(batch_content_keys) < BATCH_SIZE:
                break  # last batch

        summary["total_processed"] = total_processed
        summary["eligible_count"] = eligible_count
        summary["stage_counts"] = stage_counts
        summary["graduation_enabled"] = graduation_enabled
        summary["graduated_rows"] = graduated_count

        # 4. Orphan invalidation: catalog_products rows can be hard-deleted by
        # the Shopify catalog source prune. An IPS row whose catalog product
        # no longer exists must be removed — the live-only batch query above
        # never touches it, so without this it would stay serving_eligible=TRUE
        # forever. Logs the orphan count before deleting for observability;
        # warns loudly if the orphan share is suspicious so we notice a
        # catastrophic delete upstream rather than silently shrinking IPS.
        orphans_deleted = 0
        try:
            orphan_row = await database.fetch_one(_ORPHAN_COUNT_QUERY)
            ips_total_row = await database.fetch_one(_IPS_TOTAL_COUNT_QUERY)
            orphans = int(dict(orphan_row).get("n", 0)) if orphan_row else 0
            ips_total_before = (
                int(dict(ips_total_row).get("n", 0)) if ips_total_row else 0
            )
            if orphans > 0:
                # Pre-delete share answers "what fraction of IPS was orphaned"
                # — the right question for catching a catastrophic prune.
                share = orphans / max(ips_total_before, 1)
                post_delete_count = max(ips_total_before - orphans, 0)
                if orphans >= 100 or share >= 0.10:
                    logger.warning(
                        "nightly_index_health: orphan IPS rows count=%d "
                        "(%.1f%% of %d pre-delete; projected %d after) — "
                        "verify catalog prune didn't run incorrectly",
                        orphans, share * 100, ips_total_before, post_delete_count,
                    )
                await database.execute(_ORPHAN_DELETE)
                orphans_deleted = orphans
            summary["ips_total_before"] = ips_total_before
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "nightly_index_health: orphan invalidation failed: %s", exc
            )
            summary["errors"].append(f"orphan_invalidation: {exc!r}")
        summary["orphans_deleted"] = orphans_deleted

        # 5. Stale-product invalidation: any IPS row whose catalog_products
        # row is no longer 'live' must be demoted (was the P0 risk where a
        # previously-eligible product could linger as eligible after archival).
        try:
            await database.execute(_STALE_INVALIDATION_UPDATE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "nightly_index_health: stale invalidation UPDATE failed: %s", exc
            )
            summary["errors"].append(f"stale_invalidation: {exc!r}")

        # 6. Compute domain extractor scorecards
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

        # 7. Second-pass UPDATE: apply extractor_regression blocker
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

    # IndexNow: diff the pre-mutation snapshot against the post-mutation state
    # and ping both directions in one batched submit. Best-effort by design
    # (submit_urls never raises); counts land in the summary either way so the
    # run report shows what was pinged.
    await _submit_indexnow_transitions(eligibility_before, summary)

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

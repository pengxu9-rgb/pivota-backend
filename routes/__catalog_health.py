"""ADR-012 Phase 0 — per-cohort catalog funnel + correctness invariants.

Read-only ops endpoints (double-underscore = internal), mirroring
/__trust_health. 200 in all cases; the response shape carries the verdict.

GET /__catalog_health      — the funnel per cohort: quality reported against
    the right denominator (retired-by-design rows are separated from the
    serving corpus), overall and per source_system.
GET /__catalog_invariants  — runs the internal-consistency checks from
    services/catalog_invariant_checks.py on demand.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter(tags=["catalog-health"])

# One bucket per row. Precedence: no trust row first (invisible), then
# retired-by-design (deliberate removals — never a quality gap), then the
# decision tiers. 'quality_blocked' = the only recoverable blocked bucket.
_COHORT_SQL = """
    SELECT
        COALESCE(cp.source_system, cp.platform, 'unknown') AS source_system,
        CASE
            WHEN crt.subject_key IS NULL THEN 'no_trust_row'
            WHEN crt.serving_reason_codes && ARRAY[
                'ROW_TOMBSTONED','SOURCE_QUARANTINED',
                'MERCHANT_STORE_INACTIVE','EXTERNAL_SEED_INACTIVE'
            ]::text[] THEN 'retired_by_design'
            WHEN crt.serving_decision = 'public' THEN 'public'
            WHEN crt.serving_decision = 'shadow' THEN 'identity_gated'
            WHEN 'INDEX_NOT_SERVING_ELIGIBLE' = ANY(crt.serving_reason_codes)
                THEN 'quality_blocked'
            ELSE 'lifecycle_blocked'
        END AS cohort,
        count(*) AS cnt
    FROM catalog_products cp
    LEFT JOIN catalog_row_trust crt
        ON crt.subject_type = 'product'
       AND crt.subject_key  = cp.product_key
    GROUP BY 1, 2
"""


# In-process TTL cache for the agent_pdp_view drift count. Errors are
# returned but never cached, so a transient failure heals on the next hit.
_APV_DRIFT_TTL_SECONDS = 300.0
_apv_drift_cache: Dict[str, Any] = {"at": 0.0, "value": None}


_PDP_WILL_RENDER_DRIFT_TTL_SECONDS = 300.0
_will_render_drift_cache: Dict[str, Any] = {"at": 0.0, "value": None}


async def _cached_pdp_will_render_drift(database: Any) -> Dict[str, Any]:
    import time

    now = time.monotonic()
    cached = _will_render_drift_cache["value"]
    if cached is not None and (now - _will_render_drift_cache["at"]) < _PDP_WILL_RENDER_DRIFT_TTL_SECONDS:
        return {**cached, "cached": True}
    try:
        from jobs.pdp_renderability_reconciler_cron import count_pdp_will_render_drift

        value = await count_pdp_will_render_drift(database)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    _will_render_drift_cache["value"] = value
    _will_render_drift_cache["at"] = now
    return {**value, "cached": False}


async def _cached_agent_pdp_view_drift(database: Any) -> Dict[str, Any]:
    import time

    now = time.monotonic()
    cached = _apv_drift_cache["value"]
    if cached is not None and (now - _apv_drift_cache["at"]) < _APV_DRIFT_TTL_SECONDS:
        return {**cached, "cached": True}
    try:
        from jobs.agent_pdp_view_reconciler_cron import count_agent_pdp_view_drift

        value = await count_agent_pdp_view_drift(database)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    _apv_drift_cache["value"] = value
    _apv_drift_cache["at"] = now
    return {**value, "cached": False}


@router.get("/__catalog_health")
async def catalog_health() -> Dict[str, Any]:
    from db.database import database

    try:
        rows = await database.fetch_all(_COHORT_SQL)
        totals: Dict[str, int] = {}
        by_source: Dict[str, Dict[str, int]] = {}
        for r in rows:
            cohort = r["cohort"]
            cnt = int(r["cnt"] or 0)
            totals[cohort] = totals.get(cohort, 0) + cnt
            by_source.setdefault(r["source_system"], {})[cohort] = cnt
        catalog_total = sum(totals.values())
        # The serving corpus excludes deliberate removals and invisible rows —
        # THIS is the denominator quality work should be measured against.
        corpus = catalog_total - totals.get("retired_by_design", 0) - totals.get("no_trust_row", 0)
        public = totals.get("public", 0)
        # ADR-012 Phase 1 — agent_pdp_view drift (stale + missing-vs-public
        # view rows). Same count the reconciler alarms on; best-effort so a
        # drift-query failure never hides the funnel. TTL-cached: the count
        # only moves on the reconciler's 6h cadence (plus event pokes), and
        # the query is a full truth-CTE aggregate on a 140ms-RTT shared DB —
        # a polled health endpoint must not re-pay it every hit.
        # WHY the 5,561-row question needed database credentials to answer.
        # This endpoint reported `quality_blocked` as one number, and the only
        # way to learn WHY those rows were blocked was a psql session against
        # the Railway proxy — which is how the 2026-07-28 audit spent its first
        # hour. blocker_code lives on index_pipeline_state (content_key grain,
        # ~11k rows), so the histogram is one cheap GROUP BY; the expensive
        # per-row renderability question deliberately stays OUT of it.
        #
        # Grain note, stated so the two numbers are not "reconciled" into a bug
        # report: `cohorts` above counts catalog_products ROWS via trust;
        # this counts index_pipeline_state CONTENT_KEYS. They will not match,
        # and rows blocked by trust for row-level reasons (tombstones,
        # quarantine) appear here under blocker='none' when their KEY is
        # serving-eligible.
        blocker_rows = await database.fetch_all(
            """
            SELECT COALESCE(blocker_code, '(null)') AS blocker, count(*) AS n
            FROM index_pipeline_state
            WHERE serving_eligible IS NOT TRUE
            GROUP BY 1
            ORDER BY 2 DESC
            """
        )
        blocker_histogram = {r["blocker"]: int(r["n"] or 0) for r in blocker_rows}
        apv_drift = await _cached_agent_pdp_view_drift(database)
        # P1.13 — freshness of catalog_products.pdp_will_render. The store's own
        # instruction before any consumer reads that column is to "prove
        # freshness — not that the column is correct once, but that it STAYS
        # correct under writes", and this number is that proof. `never_computed`
        # falling to 0 says the reconciler has walked the table; `disagreeing`
        # staying at ~0 afterwards says writers are not outrunning it.
        #
        # Best-effort and TTL-cached for the same reason as the view drift: it
        # carries pdp_will_render_expression's correlated subqueries over the
        # full table, and a polled health endpoint must not re-pay that per hit.
        will_render_drift = await _cached_pdp_will_render_drift(database)
        return {
            "catalog_total": catalog_total,
            "serving_corpus": corpus,
            "public": public,
            "serving_pct_of_corpus": round(100.0 * public / corpus, 1) if corpus else None,
            "cohorts": totals,
            "by_source_system": by_source,
            "blocker_histogram": blocker_histogram,
            "agent_pdp_view_drift": apv_drift,
            "pdp_will_render_drift": will_render_drift,
        }
    except Exception as exc:  # noqa: BLE001 — ops endpoint never 500s
        return {"error": str(exc)}


@router.get("/__catalog_invariants")
async def catalog_invariants() -> Dict[str, Any]:
    from db.database import database
    from services.catalog_invariant_checks import run_catalog_invariant_checks

    try:
        return await run_catalog_invariant_checks(database)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

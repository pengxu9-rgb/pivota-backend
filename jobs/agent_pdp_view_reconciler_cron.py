"""agent_pdp_view convergent reconciler — ADR-012 Phase 1, slice 1.

agent_pdp_view is the denormalized PDP serve-cache (mig 085), keyed by
content_key and rebuilt exclusively through
``services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key``.
Until now every rebuild was an EVENT POKE (catalog_sync, seed writer,
enrichment publish, …): whoever wrote catalog truth had to remember to
re-point the view, and a missed/failed poke silently served a stale PDP —
or, for a row that never got its first poke, no PDP at all (the manual
"PBA delta gap-fill after each backfill" runbook is the tell).

This job is the convergent sweep behind those pokes ("pokes for speed,
sweeps for truth"). Each pass:

  1. Selects up to AGENT_PDP_VIEW_RECONCILE_LIMIT content_keys whose view
     row is STALE — catalog_products.updated_at / content_changed_at or any
     catalog_offers.updated_at under the key is newer than the view row's
     refreshed_at — or MISSING entirely while the key has at least one
     trust-public product (a public row with no view row is an invisible
     product). Ordered stalest-first (missing first, then oldest
     refreshed_at), so the per-run bound never starves the tail: full
     coverage over successive runs, exactly the trust-backfill shape.
  2. Re-runs the canonical refresh primitive per key — never a
     reimplemented assembly — with per-key error isolation, counting
     writes that LANDED (refresh returned True), never attempts.
  3. Recounts the drift metric (stale + missing view rows) and ERROR-logs
     when it exceeds AGENT_PDP_VIEW_DRIFT_ALERT_THRESHOLD (prod filters
     INFO). The same count is exposed on GET /__catalog_health.

Idempotent by construction: the refresh UPSERTs on content_key and stamps
refreshed_at=NOW(), so a converged row drops out of the candidate set and
idle passes write nothing. Orphan view rows (content_key with no
catalog_products row left) are deliberately out of scope — the orphan
reaper owns deletes; this job only converges rows that upstream truth says
should exist.

Env vars:
  AGENT_PDP_VIEW_RECONCILE_ENABLED       default true — set false to pause
  AGENT_PDP_VIEW_RECONCILE_LIMIT         default 300 — content_keys per run
      (each refresh is ~8 sequential queries PLUS one enrichment lookup per
      cluster member, so ~1s+ per key at the ~140ms app↔DB RTT; a full run
      stays in the minutes range, well inside the 6h cron cadence)
  AGENT_PDP_VIEW_DRIFT_ALERT_THRESHOLD   default 200 — ERROR-log when more
      stale+missing view rows than this remain after a pass
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 300
_DEFAULT_DRIFT_ALERT_THRESHOLD = 200
_ENV_ENABLED = "AGENT_PDP_VIEW_RECONCILE_ENABLED"
_ENV_LIMIT = "AGENT_PDP_VIEW_RECONCILE_LIMIT"
_ENV_DRIFT_THRESHOLD = "AGENT_PDP_VIEW_DRIFT_ALERT_THRESHOLD"

REFRESH_SOURCE = "reconciler_cron"


def _is_enabled() -> bool:
    return os.getenv(_ENV_ENABLED, "true").strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int, *, floor: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(floor, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "agent_pdp_view_reconcile: invalid %s=%r; using %d", name, raw, default
        )
        return default


def _limit() -> int:
    return _int_env(_ENV_LIMIT, _DEFAULT_LIMIT, floor=1)


def _drift_alert_threshold() -> int:
    return _int_env(_ENV_DRIFT_THRESHOLD, _DEFAULT_DRIFT_ALERT_THRESHOLD, floor=0)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
#
# "Truth freshness" per content_key = the newest of every member product's
# updated_at / content_changed_at and every member offer's updated_at.
# GREATEST ignores NULL args, and MAX over the fanned-out offer join
# collapses it back to one row per content_key (14k rows — this aggregate
# is cheap; revisit at 10x).
#
# catalog_products/catalog_offers timestamps are naive TIMESTAMP (UTC by
# server convention) while agent_pdp_view.refreshed_at is TIMESTAMPTZ, so
# refreshed_at is normalized with AT TIME ZONE 'UTC' to keep the comparison
# deterministic regardless of session timezone. (Invariant: those naive
# columns hold UTC wall-clock — true for every writer today; a session-tz
# drift would skew every staleness comparison at once, which the drift
# alarm itself would surface as a sudden all-stale spike.)
#
# CANDIDACY MUST MATCH BUILDABILITY. The refresh primitive's product fetch
# applies the source-quarantine anti-join and assemble_row declines keys
# with no title — a key the assembler declines gets NO refreshed_at bump,
# so if the truth CTE still selected it, it would sit at the head of the
# stalest-first queue FOREVER, burning budget every pass and (at >= limit
# stuck keys) starving the deterministic tail: exactly the #1578 class.
# So the CTE mirrors both predicates: quarantined member rows are
# anti-joined out before aggregation, and `buildable` requires a surviving
# member with a non-empty title. Public-but-unbuildable contradictions are
# the Phase 0b invariant sweep's job to surface, not this reconciler's job
# to retry forever; residual assembler declines (predicate drift between
# this CTE and the assembler) are WARNING-logged via skipped_no_row.

_TRUTH_CTE = """
    WITH truth AS (
        SELECT
            cp.content_key,
            GREATEST(
                MAX(cp.updated_at),
                MAX(cp.content_changed_at),
                MAX(co.updated_at)
            ) AS truth_changed_at,
            bool_or(crt.serving_decision = 'public') AS any_public,
            bool_or(cp.title IS NOT NULL AND btrim(cp.title) <> '') AS buildable
        FROM catalog_products cp
        LEFT JOIN catalog_offers co
            ON co.product_key = cp.product_key
        LEFT JOIN catalog_row_trust crt
            ON crt.subject_type = 'product'
           AND crt.subject_key  = cp.product_key
        WHERE cp.content_key IS NOT NULL AND cp.content_key <> ''
        {quarantine_anti_join}
        GROUP BY cp.content_key
    )
"""


def _truth_cte() -> str:
    # Same anti-join fragment (and the same cp.* expressions) the assembler's
    # fetch_products_for_key uses — the two predicates must never diverge.
    from services.source_quarantine import (
        CATALOG_PRODUCT_DOMAIN_SQL,
        build_quarantine_anti_join_sql,
    )

    return _TRUTH_CTE.format(
        quarantine_anti_join=build_quarantine_anti_join_sql(
            row_domain_expr=CATALOG_PRODUCT_DOMAIN_SQL,
            row_merchant_expr="cp.merchant_id",
            row_platform_expr="cp.platform",
            row_source_system_expr="cp.source_system",
            row_source_ref_expr="cp.source_ref",
        )
    )

_CANDIDATES_TAIL = """
    SELECT t.content_key
    FROM truth t
    LEFT JOIN agent_pdp_view av
        ON av.content_key = t.content_key
    WHERE t.buildable
      AND ((av.content_key IS NULL AND t.any_public)
        OR (av.content_key IS NOT NULL
            AND (av.refreshed_at AT TIME ZONE 'UTC') < t.truth_changed_at))
    ORDER BY
        (av.refreshed_at AT TIME ZONE 'UTC') ASC NULLS FIRST,
        t.truth_changed_at ASC,
        t.content_key
    LIMIT :limit
"""

# Drift counts only divergence the reconciler can actually converge (same
# buildable gate as candidacy) — so a healthy system reaches drift=0 and the
# threshold alarm stays meaningful instead of pinning at a permanent floor
# of unbuildable keys. Unservable-public contradictions surface in Phase 0b.
_DRIFT_TAIL = """
    SELECT
        count(*) FILTER (
            WHERE av.content_key IS NULL AND t.any_public
        ) AS missing_public,
        count(*) FILTER (
            WHERE av.content_key IS NOT NULL
              AND (av.refreshed_at AT TIME ZONE 'UTC') < t.truth_changed_at
        ) AS stale
    FROM truth t
    LEFT JOIN agent_pdp_view av
        ON av.content_key = t.content_key
    WHERE t.buildable
"""


def candidates_sql() -> str:
    return _truth_cte() + _CANDIDATES_TAIL


def drift_sql() -> str:
    return _truth_cte() + _DRIFT_TAIL


RefreshFn = Callable[..., Awaitable[bool]]


async def count_agent_pdp_view_drift(db: Any) -> Dict[str, int]:
    """Drift metric: view rows disagreeing with (or missing vs) catalog truth.

    ``missing_public`` — content_keys with a trust-public product but no view
    row (invisible products). ``stale`` — view rows whose refreshed_at
    predates the newest upstream change. Consumed by /__catalog_health and
    the post-pass alarm below.
    """
    row = await db.fetch_one(drift_sql())
    missing = int((row["missing_public"] if row is not None else 0) or 0)
    stale = int((row["stale"] if row is not None else 0) or 0)
    return {"missing_public": missing, "stale": stale, "total": missing + stale}


async def reconcile_agent_pdp_view(
    *,
    db: Any,
    limit: int,
    refresh: Optional[RefreshFn] = None,
) -> Dict[str, int]:
    """One bounded, stalest-first convergence pass. Returns outcome counters.

    ``refreshed`` counts writes that LANDED (the primitive returned True —
    it upserted a row), never attempts. ``skipped_no_row`` counts keys the
    assembler declined to build (no catalog rows / too thin); they remain in
    the drift count when trust-public, which is the honest signal. ``errors``
    counts per-key failures — one poisoned key must never abort the pass
    (the #1574 lesson), and an errored key is NOT counted as refreshed.

    Accepted race (documented, not guarded): the event pokes (catalog_sync,
    seed writer, enrichment publish) call the SAME unconditional
    read-assemble-upsert primitive concurrently. If an upstream write plus a
    poke both land inside this pass's ~1s per-key read->write window, the
    reconciler can overwrite the poke's fresher payload while stamping a
    fresh refreshed_at, masking that key's staleness until the NEXT upstream
    write re-flags it. This race is identical to the pre-existing
    poke-vs-poke race (the primitive has never had a freshness CAS), the
    window only contains keys that just changed (which keep changing, so
    they self-heal), and a CAS belongs in the shared primitive for ALL
    callers if it ever matters — not here.
    """
    if refresh is None:
        from services.agent_pdp_view_assembler import (
            refresh_agent_pdp_view_for_content_key,
        )

        refresh = refresh_agent_pdp_view_for_content_key

    rows = await db.fetch_all(candidates_sql(), {"limit": max(1, int(limit))})
    content_keys = [r["content_key"] for r in rows if r["content_key"]]
    counters = {
        "candidates": len(content_keys),
        "refreshed": 0,
        "skipped_no_row": 0,
        "errors": 0,
    }
    # Candidate order is load-bearing (stalest-first); process ALL fetched
    # keys in that order — the LIMIT above is the only bound, and it is a
    # per-run budget, not a coverage cap: unconverged keys stay stalest and
    # lead the next pass.
    for content_key in content_keys:
        try:
            wrote = await refresh(content_key, refresh_source=REFRESH_SOURCE, db=db)
            if wrote:
                counters["refreshed"] += 1
            else:
                counters["skipped_no_row"] += 1
        except Exception:  # noqa: BLE001 — per-key isolation
            counters["errors"] += 1
            logger.exception(
                "agent_pdp_view_reconcile: refresh failed for content_key=%s",
                content_key,
            )
    return counters


async def run_agent_pdp_view_reconcile_tick(
    *,
    db: Any = None,
    refresh: Optional[RefreshFn] = None,
) -> None:
    """Entry point called by APScheduler (cron — see audit_scheduler)."""
    if not _is_enabled():
        logger.debug("agent_pdp_view_reconcile: disabled via %s", _ENV_ENABLED)
        return

    try:
        if db is None:
            from db.database import database as db  # type: ignore[no-redef]

        counters = await reconcile_agent_pdp_view(db=db, limit=_limit(), refresh=refresh)
        logger.info(
            "agent_pdp_view_reconcile: candidates=%d refreshed=%d "
            "skipped_no_row=%d errors=%d",
            counters["candidates"],
            counters["refreshed"],
            counters["skipped_no_row"],
            counters["errors"],
        )
        if counters["skipped_no_row"]:
            # Candidacy mirrors the assembler's buildability predicates
            # (quarantine anti-join + title), so a decline here means the two
            # have DRIFTED — such keys re-enter every pass without ever
            # converging. WARNING (prod filters INFO): fix the predicate
            # mirror, don't wait for the drift alarm.
            logger.warning(
                "agent_pdp_view_reconcile: %d candidate(s) declined by the "
                "refresh primitive — candidate SQL and assembler buildability "
                "predicates have drifted; these keys will churn every pass",
                counters["skipped_no_row"],
            )

        # Post-pass drift guard. After a pass, remaining drift should be at
        # most (drift_before - limit) plus rows touched mid-run; a count
        # above threshold means the sweep is underwater (limit too low,
        # refreshes failing, or an upstream writer churning faster than the
        # cadence). Readers serve this table directly — stale rows are
        # wrong-served PDPs, missing-public rows are invisible products.
        drift = await count_agent_pdp_view_drift(db)
        threshold = _drift_alert_threshold()
        if drift["total"] > threshold:
            logger.error(
                "agent_pdp_view_reconcile: %d view rows still diverge from "
                "catalog truth after a pass (stale=%d missing_public=%d, "
                "threshold %d) — served PDPs are stale and/or public rows "
                "are invisible",
                drift["total"],
                drift["stale"],
                drift["missing_public"],
                threshold,
            )
        else:
            logger.info(
                "agent_pdp_view_reconcile: drift total=%d (stale=%d "
                "missing_public=%d) within threshold %d",
                drift["total"],
                drift["stale"],
                drift["missing_public"],
                threshold,
            )
    except Exception:  # noqa: BLE001 — cron tick must never raise into APScheduler
        logger.exception("agent_pdp_view_reconcile: tick failed")

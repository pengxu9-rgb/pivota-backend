"""Keep `catalog_products.pdp_will_render` converged with the live predicate.

WHY A RECONCILER AND NOT PER-WRITER INVALIDATION. `services/pdp_renderability_store`
shipped the column and both refresh entry points in #1604 and deliberately left
them with NO CALLER, because the obvious wiring was measured and rejected: calling
`refresh_for_content_key` inline from `recompute_serving_eligibility` costs
(N+1)x140ms per call across the app/DB region split, times ~3 recomputes per
product — ~14 minutes for a 1,000-product merchant sync — and produces a sparse,
BIASED trickle, since a row is only written when its content_key happens to be
recomputed. **45 code paths INSERT/UPDATE catalog_products, 32 of them one-off
scripts.** Per-writer invalidation is not a design; it is how catalog_row_trust
became a stale derivative.

So this is the periodic drift reconciler that module asked for (worklist P1.13),
modelled on `jobs/agent_pdp_view_reconciler_cron` and its /__catalog_health drift
count: one code path, set-based, self-healing, and the DRIFT NUMBER — not a
timestamp — is what says the column is trustworthy enough to read.

WHAT UNBLOCKS WHAT. The column is 100% NULL in prod today, which is why the
serving-vs-render invariant could not be built on it: **779 rows are
`serving_eligible` yet the gateway will not render them**, and none of the six
`catalog_invariant_checks` can see that, because every one of them is anchored on
"trust says public". This job is the prerequisite. It writes the column and
publishes `drift`; the invariant lands only once drift is observed at ~0 across
several runs, per the store's own instruction to "prove freshness — not that the
column is correct once, but that it STAYS correct under writes".

NOTHING READS THE COLUMN YET. `routes/pivota_canonical_routes._renderable_column`
still evaluates the live expression and is untouched, and
`persisted_read_enabled()` remains the gate for any future reader. A wrong value
therefore still costs nothing; that ordering is what makes it safe to turn this
on immediately rather than staging it.

DRIFT IS `IS DISTINCT FROM`, NOT `!=`. The column starts NULL on every row, and
`NULL != true` is NULL — not true — so a `!=` predicate would report zero drift
against a completely empty column. That is the exact shape of "no-op behind a
success signal" this codebase keeps producing, so it is spelled out rather than
left to the reader.

Env:
  PDP_WILL_RENDER_RECONCILE_ENABLED   default true — off without a deploy
  PDP_WILL_RENDER_RECONCILE_LIMIT     default 2000 rows per tick
  PDP_WILL_RENDER_DRIFT_ALERT_THRESHOLD  default 500 — post-pass alarm
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import sqlalchemy as sa

from db.catalog import catalog_products
from services.pdp_renderability import pdp_will_render_expression
from services.pdp_renderability_store import (
    COLUMN_COMPUTED_AT,
    COLUMN_WILL_RENDER,
    refresh_for_product_keys,
)

logger = logging.getLogger("pdp_renderability_reconciler")

_ENV_ENABLED = "PDP_WILL_RENDER_RECONCILE_ENABLED"
_ENV_LIMIT = "PDP_WILL_RENDER_RECONCILE_LIMIT"
_ENV_DRIFT_THRESHOLD = "PDP_WILL_RENDER_DRIFT_ALERT_THRESHOLD"

# 2000/tick against 14,104 rows converges a cold column in ~8 six-hourly passes,
# or one afternoon of manual ticks. Deliberately not "all of it": the drift query
# carries pdp_will_render_expression's correlated subqueries, and an unbounded
# UPDATE on the serving database is the kind of thing that should not be one
# forgotten env var away from running.
_DEFAULT_LIMIT = 2000
# Post-convergence this should sit near 0. 500 is a first threshold, NOT a
# measured baseline — it cannot be measured until the column has been written
# once. Re-baseline it from observed steady-state drift before treating a breach
# as an incident.
_DEFAULT_DRIFT_ALERT_THRESHOLD = 500


def _is_enabled() -> bool:
    return os.getenv(_ENV_ENABLED, "true").strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int, *, floor: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(floor, int(str(raw).strip()))
    except Exception:  # noqa: BLE001
        return default


def _limit() -> int:
    return _int_env(_ENV_LIMIT, _DEFAULT_LIMIT, floor=1)


def _drift_alert_threshold() -> int:
    return _int_env(_ENV_DRIFT_THRESHOLD, _DEFAULT_DRIFT_ALERT_THRESHOLD, floor=0)


def _persisted_col():
    """The stored value, as a literal column.

    `pdp_will_render` was added by migration and is not on the `db.catalog`
    Table def, the same reason `pdp_renderability_store` reaches for
    `literal_column` rather than an attribute.
    """
    return sa.literal_column(f"catalog_products.{COLUMN_WILL_RENDER}")


def drift_predicate():
    """Stored value disagrees with the live expression — NULL counting as drift.

    `IS DISTINCT FROM`, never `!=`: the column is NULL on every row until this
    job first runs, and `NULL != true` evaluates to NULL, which a WHERE clause
    discards. A `!=` predicate would therefore report ZERO drift against a
    100%-empty column — a green light that means nothing has happened.
    """
    return _persisted_col().is_distinct_from(pdp_will_render_expression(catalog_products))


def drift_select():
    return sa.select(sa.func.count()).select_from(catalog_products).where(drift_predicate())


def candidates_select(limit: int):
    """Stalest-first: never-computed rows before merely-outdated ones.

    NULLS FIRST is load-bearing on the first runs — without it a cold column
    would be walked in arbitrary order and the "has every row been visited yet"
    question becomes unanswerable.
    """
    computed_at = sa.literal_column(f"catalog_products.{COLUMN_COMPUTED_AT}")
    return (
        sa.select(catalog_products.c.product_key)
        .where(drift_predicate())
        .order_by(computed_at.asc().nullsfirst())
        .limit(limit)
    )


async def count_pdp_will_render_drift(db: Any) -> Dict[str, int]:
    """Rows whose stored value disagrees with the live predicate.

    Split so a cold column reads differently from a genuinely wrong one:
    `never_computed` is expected to fall to 0 as this job walks the table, while
    a non-zero `disagreeing` after convergence means a writer changed a row and
    nothing recomputed it — which is the signal the whole design rests on.
    """
    computed_at = sa.literal_column(f"catalog_products.{COLUMN_COMPUTED_AT}")
    row = await db.fetch_one(
        sa.select(
            sa.func.count().filter(drift_predicate()).label("total"),
            sa.func.count().filter(_persisted_col().is_(None)).label("never_computed"),
            sa.func.count().filter(computed_at.is_(None)).label("never_stamped"),
        ).select_from(catalog_products)
    )
    total = int((row["total"] if row is not None else 0) or 0)
    never = int((row["never_computed"] if row is not None else 0) or 0)
    stamped = int((row["never_stamped"] if row is not None else 0) or 0)
    return {
        "total": total,
        "never_computed": never,
        "never_stamped": stamped,
        "disagreeing": max(0, total - never),
    }


async def reconcile_pdp_will_render(
    *,
    db: Any,
    limit: int,
    refresh: Optional[Any] = None,
) -> Dict[str, int]:
    """One bounded, stalest-first convergence pass.

    `written` counts rows the UPDATE actually touched (`_persist` returns
    RETURNING rows, not offered keys), so a candidate that vanishes between the
    SELECT and the UPDATE is not counted as success.
    """
    refresh_fn = refresh or refresh_for_product_keys
    rows = await db.fetch_all(candidates_select(limit))
    keys = [str(dict(r._mapping if hasattr(r, "_mapping") else r)["product_key"]) for r in (rows or [])]
    if not keys:
        return {"candidates": 0, "written": 0}
    written = await refresh_fn(keys, database=db)
    return {"candidates": len(keys), "written": int(written or 0)}


async def run_pdp_will_render_reconcile_tick() -> Dict[str, Any]:
    """Scheduler entry point. Never raises — a bookkeeping column must not page."""
    from db.database import database

    if not _is_enabled():
        return {"skipped": "disabled"}

    try:
        before = await count_pdp_will_render_drift(database)
        result = await reconcile_pdp_will_render(db=database, limit=_limit())
        after = await count_pdp_will_render_drift(database)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdp_will_render reconcile failed: %r", exc)
        return {"error": repr(exc)}

    summary = {
        "candidates": result["candidates"],
        "written": result["written"],
        "drift_before": before["total"],
        "drift_after": after["total"],
        "never_computed_after": after["never_computed"],
    }

    # Post-pass alarm, mirroring the agent_pdp_view reconciler: a pass that runs
    # cleanly while drift stays high is the failure mode worth naming, because
    # every counter above still looks healthy.
    threshold = _drift_alert_threshold()
    if after["total"] > threshold and after["never_computed"] == 0:
        logger.warning(
            "pdp_will_render drift %s exceeds threshold %s after a clean pass "
            "(never_computed=0, so this is genuine disagreement, not a cold column)",
            after["total"],
            threshold,
        )
        summary["drift_alert"] = True

    logger.info("pdp_will_render reconcile: %s", summary)
    return summary


__all__ = [
    "count_pdp_will_render_drift",
    "reconcile_pdp_will_render",
    "run_pdp_will_render_reconcile_tick",
    "drift_predicate",
    "candidates_select",
    "drift_select",
]

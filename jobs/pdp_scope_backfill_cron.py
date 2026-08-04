"""pdp_scope promotion cron — D3 of docs/PDP_SCOPE_REDESIGN.md. PROMOTE-ONLY.

One UPDATE per tick: rows at `'unverified'` (and not suppressed) that satisfy
`CANONICAL_SCOPE_PREDICATE` — the promotion writer's own string, interpolated,
never retyped — are promoted to canonical. Rows that do NOT qualify are LEFT
at `'unverified'`, which is the whole point: the redesign needs CONTINUOUS
RE-QUALIFICATION, and `'unverified'` is the only state every promotion writer
re-checks. A row that gains its second seller next week is promoted by the
tick after that.

WHY THIS IS NOT run_backfill (review finding F1 on PR #1680): the CLI
backfill's classify() loop resolves EVERY unverified row and never returns
'unverified' — single-seller rows get 'merchant_owned', an AFFIRMED state that
exits the `WHERE pdp_scope='unverified'` promotion gate forever. Run on a
schedule, that converts the P4 demotion (all 2,201 rows single-seller by
construction) into demote-to-merchant_owned with a 6-hour delay — the exact
one-way door D1 rejected — and parks every future not-yet-multi-seller mirror
row the same way. One-shot resolution and continuous re-qualification are
different operations; this cron performs only the second.

DISABLED BY DEFAULT — a stated deviation from house style (sibling crons
default enabled): the first enabled tick promotes the currently-qualified
'unverified' cohort (measured 2026-08-04: 350 rows — 349 real-merchant, 1
bucket), each gaining the +200 rank term in three backend sites plus the Node
ranker's +200 and market-filter exemption. The rule is vetted but the ranking
impact of the batch is unmeasured, so enabling is an explicit operator step:
measure, set PDP_SCOPE_BACKFILL_ENABLED=true, watch the first tick. Sequence
in docs/PDP_SCOPE_REDESIGN.md.

Env vars:
  PDP_SCOPE_BACKFILL_ENABLED   default FALSE — see above; set true to enable
  PDP_SCOPE_BACKFILL_LIMIT     default 5000 — max promotions per tick (a tick
      that hits the cap leaves the tail for the next tick)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from db.database import database
from services.pdp_identity_recovery import CANONICAL_SCOPE_PREDICATE
from services.pdp_scope_classifier import SCOPE_CANONICAL

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 5_000
_ENV_ENABLED = "PDP_SCOPE_BACKFILL_ENABLED"
_ENV_LIMIT = "PDP_SCOPE_BACKFILL_LIMIT"

# pdp_scope_source marker (varchar(32)) — greppable back to this cron.
PROMOTION_SOURCE = "d3_promotion_cron"

# Postgres UPDATE takes no LIMIT, so the cap lives in a subselect. The outer
# WHERE repeats BOTH gates — `pdp_scope = 'unverified'` AND
# `suppressed_at IS NULL` — because under READ COMMITTED a row locked
# mid-write is re-evaluated against the OUTER quals only (EvalPlanQual); the
# subselect's copies protect nothing at write time. Round-11 review of PR
# #1680 drove the miss: with only the pdp_scope re-check, a row suppressed
# between subselect and write WAS promoted — and a suppressed-canonical row
# then escapes the P4 demotion path forever (its selection requires
# `suppressed_at IS NULL`). RETURNING because databases.execute() returns
# None on this stack (driven on 0.7.0 and 0.9.0) — rowcounts must come from
# RETURNING.
_PROMOTE_SQL = """
    UPDATE catalog_products
    SET pdp_scope = :scope,
        pdp_scope_source = :source,
        pdp_scope_set_at = NOW()
    WHERE product_key IN (
        SELECT cp.product_key
        FROM catalog_products cp
        WHERE cp.pdp_scope = 'unverified'
          AND cp.suppressed_at IS NULL
          AND (
{predicate}
          )
        LIMIT :limit
    )
      AND pdp_scope = 'unverified'
      AND suppressed_at IS NULL
    RETURNING product_key
""".format(predicate=CANONICAL_SCOPE_PREDICATE)


def _is_enabled() -> bool:
    return os.getenv(_ENV_ENABLED, "false").strip().lower() in {"1", "true", "yes", "on"}


def _limit() -> int:
    raw = os.getenv(_ENV_LIMIT)
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning("pdp_scope_promotion: invalid %s=%r; using %d",
                       _ENV_LIMIT, raw, _DEFAULT_LIMIT)
        return _DEFAULT_LIMIT


async def run_promotion_pass(*, limit: int = _DEFAULT_LIMIT) -> Dict[str, Any]:
    """The one UPDATE. No classify() loop, no second spelling of the rule —
    the predicate is interpolated from services.pdp_identity_recovery."""
    rows = await database.fetch_all(
        _PROMOTE_SQL,
        {"scope": SCOPE_CANONICAL, "source": PROMOTION_SOURCE, "limit": limit},
    )
    return {"promoted": len(rows or [])}


async def run_pdp_scope_backfill_tick() -> None:
    """One scheduled pass. Fire-and-forget from the scheduler's point of view —
    an exception is logged, never raised into APScheduler."""
    if not _is_enabled():
        logger.info("pdp_scope_promotion: disabled via %s (default) — operator-enable only", _ENV_ENABLED)
        return
    try:
        if not getattr(database, "is_connected", False):
            await database.connect()
        result = await run_promotion_pass(limit=_limit())
        if result.get("promoted"):
            logger.info("pdp_scope_promotion: %s", result)
        else:
            logger.debug("pdp_scope_promotion: no unverified row qualifies")
    except Exception:
        logger.exception("pdp_scope promotion tick failed")

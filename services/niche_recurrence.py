"""Cross-merchant niche-query recurrence — the Phase 2 v2 demand proxy.

How many DISTINCT merchants a niche query recurs across is a proprietary,
compounding demand signal: a niche that keeps showing up as Pivota probes more
brands is more likely to have real shopper demand than one seen once. This is
Pivota's own cross-brand audit history — no competitor has it.

- record_niche_queries: on each audit, upsert the probed niche queries (per
  merchant). The signal compounds as more audits run.
- recurrence_for_queries: read distinct-merchant + total-run counts for a set of
  queries, so build_where_you_can_win can rank winnable niches by recurrence.

Both are best-effort: a DB miss degrades to "no recurrence data" (the default
probe-demand ranking still works), never an error that breaks the audit.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

from db.database import database

logger = logging.getLogger(__name__)


def _normalize(q: Any) -> str:
    return str(q or "").strip().lower()


def _dedupe(queries: Iterable[Any]) -> List[str]:
    seen: Dict[str, None] = {}
    for q in queries or []:
        n = _normalize(q)
        if n and n not in seen:
            seen[n] = None
    return list(seen.keys())


async def record_niche_queries(
    *,
    queries: Iterable[Any],
    merchant_id: str,
    db: Any = None,
) -> int:
    """Upsert this audit's probed niche queries for the merchant. Returns the
    number of distinct queries recorded. Best-effort."""
    norm = _dedupe(queries)
    if not norm or not merchant_id:
        return 0
    write_db = db or database
    written = 0
    for q in norm:
        try:
            await write_db.execute(
                """
                INSERT INTO niche_query_recurrence (normalized_query, merchant_id, runs)
                VALUES (:q, :mid, 1)
                ON CONFLICT (normalized_query, merchant_id) DO UPDATE
                  SET runs = niche_query_recurrence.runs + 1,
                      last_seen_at = NOW()
                """,
                {"q": q, "mid": str(merchant_id)},
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("niche_recurrence upsert failed for %r: %s", q, str(exc)[:160])
            break  # table likely absent (pre-migration) — stop quietly
    return written


async def recurrence_for_queries(
    queries: Iterable[Any],
    *,
    db: Any = None,
) -> Dict[str, Dict[str, int]]:
    """Map normalized_query -> {distinct_merchants, total_runs} for the given
    queries. Empty dict on any miss (table absent, no rows). Best-effort."""
    norm = _dedupe(queries)
    if not norm:
        return {}
    read_db = db or database
    try:
        rows = await read_db.fetch_all(
            """
            SELECT normalized_query,
                   COUNT(*) AS distinct_merchants,
                   COALESCE(SUM(runs), 0) AS total_runs
            FROM niche_query_recurrence
            WHERE normalized_query = ANY(:queries)
            GROUP BY normalized_query
            """,
            {"queries": norm},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("niche_recurrence read failed: %s", str(exc)[:160])
        return {}
    return {
        r["normalized_query"]: {
            "distinct_merchants": int(r["distinct_merchants"] or 0),
            "total_runs": int(r["total_runs"] or 0),
        }
        for r in rows
    }

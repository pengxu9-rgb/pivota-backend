"""Is a catalog row's storefront page still there? — for the rows that have no seed to ask.

`services/external_seed_destination_liveness` re-reads the seed mirror's pages every night. The
catalog enrichment lane (`catalog_products.source_system = 'catalog_enrichment_agent_v1'`) writes
storefront rows with no seed behind them, so nothing ever re-read THEIR pages. On 2026-10-09 that
lane served 12,944 in-stock storefront rows, two-thirds of all served storefront rows, and a
throttled live sample found 9 of 150 (6%) on a page the store answered 404 for — none of them
known to anything.

THIS IS THE SEED SWEEP, POINTED AT A DIFFERENT TABLE. Every rule that makes the seed sweep safe to
automate is reused, not restated:

  * stage 1 reads each host's `/products.json` once (`read_brand_catalogue`); a host we cannot
    read produces NO verdicts. Stage 2 probes only handles the store no longer lists
    (`probe_destination`, politeness-gated). A 404 is `corroborated` only when stage 1 read the
    catalogue AND found the handle missing;
  * the failure streak and its clock come from `next_streak_state` — the same pure function, so a
    streak here steps exactly when it would on a seed;
  * the first corroborated death hides the row under a PENDING reason, the next live answer lifts
    it, and the second corroborated death (24h+ later) withdraws it under the final reason
    (services/destination_dead_suppression).

WHAT IS DIFFERENT, AND WHY.

  * State lives in `catalog_destination_liveness` (migration 261), one row per product, because
    there is no seed row to carry it. The job applies that migration itself.
  * Withdrawal is by `product_key` — there is no seed to deactivate. A withdrawn row leaves the
    queue (the candidate query takes only live or pending rows), so reversal is an operator action,
    as it is for a retired seed.
  * Nothing is withdrawn unless the caller passes `suppress=True` (the job's `--suppress`). The
    first runs of a new lane over two-thirds of the served storefront corpus should observe.
  * The queue orders dead verdicts first, then by `last_attempt_at` — a clock EVERY attempt
    stamps, so a host that never answers rotates to the back instead of heading the queue forever.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from db.database import database
from services import destination_dead_suppression
from services.external_seed_destination_liveness import (
    ALL_VERDICTS,
    SWEEP_HOST_CONCURRENCY,
    VERDICT_LIVE,
    VERDICT_UNVERIFIABLE,
    DestinationObservation,
    destination_of,
    group_by_host,
    next_streak_state,
    probe_destination,
    read_brand_catalogue,
    should_retire,
)
from services.outbound_warm_handoff import extract_product_handle

logger = logging.getLogger("catalog_destination_liveness")

LANE_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"
PENDING_SUPPRESSION_REASON = "catalog_destination_dead_pending"
SUPPRESSION_REASON = "catalog_destination_dead"
MIGRATION_GLOB = "261_catalog_destination_liveness.sql"

#: Served rows of this lane that are still ours to judge: live, or pending under OUR reason, with at
#: least one offer in the same state, on a URL that names a product handle (`group_by_host` drops any
#: other, and a row that is never attempted would head a NULLS FIRST queue forever). Dead verdicts
#: first, then the attempt clock, NULLS FIRST.
#:
#: A LITERAL, not an f-string, so the repo's PREPARE sweep can plan it. The dead list is
#: CONFIRMED_DEAD_VERDICTS spelled out; a test pins the two together.
CANDIDATES_SQL = """
    SELECT p.product_key, p.canonical_url
      FROM catalog_products p
      LEFT JOIN catalog_destination_liveness l ON l.product_key = p.product_key
     WHERE p.source_system = :source_system
       AND p.catalog_track = 'external_referral'
       AND (p.suppressed_at IS NULL OR p.suppression_reason = :pending_reason)
       AND p.canonical_url LIKE 'https://%'
       AND p.canonical_url ~* '/products/[^/?#]+'
       AND EXISTS (
           SELECT 1 FROM catalog_offers o
            WHERE o.product_key = p.product_key
              AND (o.suppressed_at IS NULL OR o.suppression_reason = :pending_reason)
       )
     ORDER BY CASE WHEN l.destination_verdict IN ('dead_404', 'redirected_off_product') THEN 0 ELSE 1 END,
              l.last_attempt_at ASC NULLS FIRST,
              p.product_key
     LIMIT :limit
"""

CURRENT_SQL = """
    SELECT destination_verdict, destination_failure_streak, destination_corroborated_dead_at,
           destination_checked_at
      FROM catalog_destination_liveness
     WHERE product_key = :product_key
"""

#: One observation. Verdict, status and the origin clock move only on an answer FROM the origin
#: (`reached_origin`); the attempt clock always moves. Same CASE guard as the seed writer.
UPSERT_SQL = """
    INSERT INTO catalog_destination_liveness AS l (
        product_key, canonical_url, host, destination_verdict, destination_http_status,
        destination_failure_streak, destination_corroborated_dead_at, destination_checked_at,
        last_attempt_at, updated_at
    ) VALUES (
        :product_key, :canonical_url, :host,
        CASE WHEN CAST(:reached_origin AS BOOLEAN) THEN CAST(:verdict AS text) END,
        CASE WHEN CAST(:reached_origin AS BOOLEAN) THEN CAST(:http_status AS integer) END,
        :streak, :corroborated_dead_at,
        CASE WHEN CAST(:reached_origin AS BOOLEAN) THEN CAST(:stamp AS timestamptz) END,
        :stamp, NOW()
    )
    ON CONFLICT (product_key) DO UPDATE SET
        canonical_url = EXCLUDED.canonical_url,
        host = EXCLUDED.host,
        destination_verdict = CASE WHEN CAST(:reached_origin AS BOOLEAN)
            THEN EXCLUDED.destination_verdict ELSE l.destination_verdict END,
        destination_http_status = CASE WHEN CAST(:reached_origin AS BOOLEAN)
            THEN EXCLUDED.destination_http_status ELSE l.destination_http_status END,
        destination_failure_streak = EXCLUDED.destination_failure_streak,
        destination_corroborated_dead_at = EXCLUDED.destination_corroborated_dead_at,
        destination_checked_at = CASE WHEN CAST(:reached_origin AS BOOLEAN)
            THEN EXCLUDED.destination_checked_at ELSE l.destination_checked_at END,
        last_attempt_at = EXCLUDED.last_attempt_at,
        updated_at = NOW()
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _host(url: str) -> Optional[str]:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return None
    return (host[4:] if host.startswith("www.") else host) or None


async def ensure_table(db: Any = None) -> None:
    """Apply migration 261 from the deployed tree: the ONE declaration of this table."""
    from db.sql_migrations import split_statements

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = glob.glob(os.path.join(here, "db", "migrations", MIGRATION_GLOB))
    if len(paths) != 1:
        raise RuntimeError(f"expected exactly one {MIGRATION_GLOB}, found {len(paths)}")
    with open(paths[0], encoding="utf-8") as fh:
        statements = split_statements(fh.read())
    write_db = db or database
    for statement in statements:
        await write_db.execute(statement)


async def get_candidates(limit: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        CANDIDATES_SQL,
        {"source_system": LANE_SOURCE_SYSTEM, "pending_reason": PENDING_SUPPRESSION_REASON,
         "limit": max(1, int(limit or 1))},
    )
    return [dict(r) for r in rows or []]


async def record_catalog_observation(
    product_key: str,
    canonical_url: str,
    observation: DestinationObservation,
    *,
    now: Optional[datetime] = None,
    suppress: bool = False,
) -> Dict[str, Any]:
    """Write one observation for one catalog product, and act on it. Returns what happened.

    Actions, only on a CORROBORATED confirmed-dead observation and only with `suppress`:
    streak 1 hides under PENDING_SUPPRESSION_REASON; streak 2 withdraws under SUPPRESSION_REASON
    (converting a pending row). Any non-dead answer from the origin that resets a streak lifts
    the pending reason, `suppress` or not — a lift only undoes this lane's own decision.
    """
    stamp = now or _now()
    row = await database.fetch_one(CURRENT_SQL, {"product_key": product_key})
    current = dict(row) if row else {}
    streak = int(current.get("destination_failure_streak") or 0)
    next_streak, next_anchor = next_streak_state(current, observation, stamp)
    await database.execute(
        UPSERT_SQL,
        {
            "product_key": product_key,
            "canonical_url": canonical_url,
            "host": _host(canonical_url),
            "verdict": observation.verdict,
            "http_status": observation.http_status,
            "reached_origin": bool(observation.reached_origin),
            "streak": next_streak,
            "corroborated_dead_at": next_anchor,
            "stamp": stamp,
        },
    )
    retire = should_retire(observation.verdict, next_streak)
    result: Dict[str, Any] = {
        "product_key": product_key,
        "verdict": observation.verdict,
        "failure_streak": next_streak,
        "retire": retire,
    }
    if suppress and observation.confirmed_dead and observation.corroborated and next_streak >= 1:
        if retire:
            moved = await destination_dead_suppression.finalize_dead(
                [product_key], final_reason=SUPPRESSION_REASON,
                pending_reason=PENDING_SUPPRESSION_REASON, stamp=stamp, db=database,
            )
            result["withdrawn"] = len(moved["products"])
        else:
            moved = await destination_dead_suppression.suppress_pending(
                [product_key], reason=PENDING_SUPPRESSION_REASON, stamp=stamp, db=database,
            )
            result["pending_suppressed"] = len(moved["products"])
    elif observation.reached_origin and not observation.confirmed_dead and streak > 0:
        moved = await destination_dead_suppression.lift_pending(
            [product_key], reason=PENDING_SUPPRESSION_REASON, db=database,
        )
        result["pending_lifted"] = len(moved["products"])
    return result


async def run_catalog_destination_sweep(
    *,
    limit: int = 4000,
    client: Optional[httpx.AsyncClient] = None,
    suppress: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Stage 1 per host, stage 2 only for handles the store no longer lists. See the module docstring."""
    candidates = await get_candidates(limit)
    grouped = group_by_host(candidates)
    summary: Dict[str, Any] = {
        "candidates": len(candidates),
        "hosts": len(grouped),
        "hosts_unverifiable": 0,
        "listed": 0,
        "probed": 0,
        "dead_links_found": 0,
        "pending_suppressed": 0,
        "pending_lifted": 0,
        "withdrawn": 0,
        "suppress": suppress,
        "verdicts": {v: 0 for v in ALL_VERDICTS},
        "catalogue_status": {},
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=25.0, follow_redirects=True)
    host_slot = asyncio.Semaphore(SWEEP_HOST_CONCURRENCY)

    async def note(row: Dict[str, Any], observation: DestinationObservation) -> None:
        summary["verdicts"][observation.verdict] = summary["verdicts"].get(observation.verdict, 0) + 1
        result = await record_catalog_observation(
            row["product_key"], destination_of(row), observation, now=now, suppress=suppress
        )
        if observation.confirmed_dead:
            summary["dead_links_found"] += 1
        for key in ("pending_suppressed", "pending_lifted", "withdrawn"):
            summary[key] += int(result.get(key) or 0)

    async def sweep_one_host(host: str, rows: List[Dict[str, Any]]) -> None:
        async with host_slot:
            catalogue = await read_brand_catalogue(client, host)
            summary["catalogue_status"][catalogue.status] = (
                summary["catalogue_status"].get(catalogue.status, 0) + 1
            )
            if not catalogue.usable:
                # No verdicts — but every row's ATTEMPT is stamped, so this host rotates to the back
                # of the queue rather than heading it every night.
                summary["hosts_unverifiable"] += 1
                for row in rows:
                    await note(row, DestinationObservation(VERDICT_UNVERIFIABLE, None, None, f"catalogue {catalogue.status}"))
                return
            for row in rows:
                dest = destination_of(row)
                handle = (extract_product_handle(dest) or "").lower()
                if handle in catalogue.handles:
                    observation = DestinationObservation(VERDICT_LIVE, None, None, "listed in products.json")
                    summary["listed"] += 1
                else:
                    observation = await probe_destination(client, dest, listed_in_catalogue=False)
                    summary["probed"] += 1
                await note(row, observation)

    try:
        results = await asyncio.gather(
            *(sweep_one_host(h, rows) for h, rows in grouped.items()), return_exceptions=True
        )
        for host, outcome in zip(grouped, results):
            if isinstance(outcome, BaseException):
                summary["hosts_unverifiable"] += 1
                logger.warning(
                    "catalog destination sweep host failed",
                    extra={"host": host, "error": f"{type(outcome).__name__}: {outcome}"},
                )
    finally:
        if owns_client:
            await client.aclose()
    logger.info("catalog destination sweep complete", extra={"summary": summary})
    return summary


__all__ = (
    "CANDIDATES_SQL",
    "LANE_SOURCE_SYSTEM",
    "PENDING_SUPPRESSION_REASON",
    "SUPPRESSION_REASON",
    "UPSERT_SQL",
    "ensure_table",
    "get_candidates",
    "record_catalog_observation",
    "run_catalog_destination_sweep",
)

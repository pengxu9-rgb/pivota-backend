"""Take down ad-campaign landing pages ALREADY ingested as products (#1926).

The companion to the ingestion gate in
``scripts/onboard_external_brand_from_crawl.py``. That gate stops new campaign
pages ever becoming products; this remediates the ones already in the catalog.
Deliberately a separate runner: it changes SERVING state for rows that are live
today, which is a different blast radius from declining to create a row.

Why a suppression sweep already ran and did nothing
---------------------------------------------------
``scripts/step5_lane3_campaign_clone_dedup.py`` adjudicated exactly this cohort
on 2026-07-10 and wrote ``catalog_products.suppression_reason`` +
``external_product_seeds.status='inactive'``. Those PDPs are still up. The two
writes de-advertise (recall, discovery, canonical candidacy, sitemap
renderability) but do not take down, because:

* the PDP resolver has NO suppression predicate — ``PIVOTA-Agent/src/server.js``
  says so in a comment, having measured 431 tombstoned mirrors serving as their
  own canonical on 2026-07-25;
* the serving gate reads a DIFFERENT column —
  ``services/index_pipeline_state_service`` keys row suppression on
  ``suppressed_at``, which that sweep never set;
* the seed-status 404 never fires for these rows: it is guarded on the
  ``external_seed`` merchant, and ADR-009 mints crawl rows under per-brand
  ``merch_obs_…`` sellers.

So this runner adds the two things that actually bite: ``suppressed_at``, and an
explicit ``recompute_serving_eligibility`` (nothing propagates the stamp on its
own — there is no trigger).

What it can and cannot close
----------------------------
Eligibility is keyed on ``content_key`` and ``_select_content_key_state`` takes
the MAX state across that key's rows. So a campaign page that is the SOLE
occupant of its content_key goes dark; one that shares its key with a real
product keeps serving through the sibling — correctly, since the real product
must not go dark with it. Those rows need a row-grain suppression predicate in
the PIVOTA-Agent resolver, which is a paired change in that repo. This runner
reports that residual per host instead of implying a clean sweep.

Adjudication is not re-derived here: it calls the SAME
``services.shopify_publication_signal`` oracle the ingestion gate uses, so the
two entry points cannot drift about what "unpublished" means. Every uncertainty
is UNKNOWN and is left alone.

Reversible: no hard deletes. ``--revert`` clears ``suppressed_at`` for rows this
runner stamped (identified by suppression_reason) and recomputes.

Usage:
  python -m scripts.remediate_unpublished_crawl_rows                     # dry run, all hosts
  python -m scripts.remediate_unpublished_crawl_rows --host biodance.com
  python -m scripts.remediate_unpublished_crawl_rows --host biodance.com --apply
  python -m scripts.remediate_unpublished_crawl_rows --host biodance.com --revert --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from scripts.onboard_external_brand_from_crawl import (
    TOOL,
    UNPUBLISHED_SUPPRESSION_REASON,
)
from services.index_pipeline_state_service import recompute_serving_eligibility
from services.shopify_publication_signal import (
    PUBLISHED,
    UNKNOWN,
    UNPUBLISHED,
    PublicationOracle,
)

# Candidate rows: every crawl-mirrored catalog row that is not already withdrawn.
# Deliberately NOT filtered on suppression_reason — the step5-suppressed rows are
# exactly the ones whose takedown never landed, so they must be re-examined.
CANDIDATES_SQL = """
SELECT s.id            AS seed_id,
       s.status        AS seed_status,
       s.destination_url,
       cp.product_key,
       cp.content_key,
       cp.suppression_reason,
       cp.suppressed_at
FROM external_product_seeds s
JOIN catalog_products cp ON cp.source_ref = s.id
WHERE s.tool = :tool
  AND s.destination_url IS NOT NULL
  AND cp.suppressed_at IS NULL
ORDER BY s.destination_url
"""

REVERT_SQL = """
UPDATE catalog_products
   SET suppressed_at = NULL, updated_at = NOW()
 WHERE suppression_reason = :reason
   AND suppressed_at IS NOT NULL
   AND source_ref = ANY(:ids)
"""


def _host_of(url: Optional[str]) -> str:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


async def _load_candidates(host_filter: Optional[str]) -> List[Dict[str, Any]]:
    rows = [dict(r) for r in await database.fetch_all(CANDIDATES_SQL, {"tool": TOOL})]
    if host_filter:
        wanted = host_filter.lower().removeprefix("www.")
        rows = [r for r in rows if _host_of(r["destination_url"]) == wanted]
    return rows


async def _adjudicate(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Classify each row with the ingestion gate's own oracle. Only UNPUBLISHED
    is actionable; PUBLISHED and UNKNOWN are left untouched."""
    oracle = PublicationOracle()
    counts: Dict[str, int] = defaultdict(int)
    actionable: List[Dict[str, Any]] = []
    for row in rows:
        verdict = await oracle.classify({"destination_url": row["destination_url"]})
        counts[verdict] += 1
        row["verdict"] = verdict
        if verdict == UNPUBLISHED:
            actionable.append(row)
    return actionable, dict(counts)


async def _withdraw(rows: List[Dict[str, Any]]) -> Dict[str, bool]:
    """Stamp suppressed_at (+ reason where absent), deactivate the seed, then
    recompute every touched content_key. Each write is idempotency-guarded, so a
    re-run is a no-op. Returns {content_key: still_serving}."""
    for row in rows:
        sid = row["seed_id"]
        await database.execute(
            "UPDATE external_product_seeds SET status='inactive', updated_at=NOW() "
            "WHERE id=:id AND status='active'",
            {"id": sid},
        )
        # Preserve an existing reason (step5's provenance is worth keeping);
        # only fill it when the row carries none.
        await database.execute(
            "UPDATE catalog_products SET suppression_reason=:reason, updated_at=NOW() "
            "WHERE source_ref=:id AND suppression_reason IS NULL",
            {"id": sid, "reason": UNPUBLISHED_SUPPRESSION_REASON},
        )
        await database.execute(
            "UPDATE catalog_products SET suppressed_at=NOW(), updated_at=NOW() "
            "WHERE source_ref=:id AND suppressed_at IS NULL",
            {"id": sid},
        )
    states: Dict[str, bool] = {}
    for content_key in sorted({r["content_key"] for r in rows if r.get("content_key")}):
        states[content_key] = await recompute_serving_eligibility(
            content_key, reason=UNPUBLISHED_SUPPRESSION_REASON
        )
    return states


async def _revert(rows: List[Dict[str, Any]]) -> Dict[str, bool]:
    ids = [r["seed_id"] for r in rows]
    await database.execute(REVERT_SQL, {"reason": UNPUBLISHED_SUPPRESSION_REASON, "ids": ids})
    states: Dict[str, bool] = {}
    for content_key in sorted({r["content_key"] for r in rows if r.get("content_key")}):
        states[content_key] = await recompute_serving_eligibility(
            content_key, reason=f"{UNPUBLISHED_SUPPRESSION_REASON}_revert"
        )
    return states


def _report_plan(rows: List[Dict[str, Any]], counts: Dict[str, int], actionable: List[Dict[str, Any]]) -> None:
    by_host: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_host[_host_of(r["destination_url"])].append(r)

    print(
        f"candidates: {len(rows)} live crawl row(s) across {len(by_host)} host(s) — "
        f"{counts.get(PUBLISHED, 0)} published, {counts.get(UNPUBLISHED, 0)} unpublished, "
        f"{counts.get(UNKNOWN, 0)} unknown (left alone)"
    )
    for host in sorted(by_host):
        rs = by_host[host]
        unpub = [r for r in rs if r.get("verdict") == UNPUBLISHED]
        if not unpub:
            continue
        # A host where EVERY row is unpublished is either an all-campaign crawl
        # (biodance) or a sitemap/crawl URL-space mismatch. Say which it looks
        # like rather than quietly withdrawing a whole brand.
        flag = "  <-- ENTIRE HOST" if len(unpub) == len(rs) else ""
        print(f"  {host:28} {len(unpub)}/{len(rs)} to withdraw{flag}")

    shared = [r for r in actionable if r.get("content_key")]
    keys = {r["content_key"] for r in shared}
    print(f"  → would stamp suppressed_at on {len(actionable)} row(s), recompute {len(keys)} content_key(s)")


async def _drive(args: argparse.Namespace) -> None:
    await database.connect()
    try:
        rows = await _load_candidates(args.host)
        if not rows:
            print("nothing to do: no live crawl-mirrored rows matched")
            return
        actionable, counts = await _adjudicate(rows)
        _report_plan(rows, counts, actionable)

        if not actionable:
            return
        if not args.apply:
            for r in actionable[: args.show]:
                print(f"    would withdraw {r['destination_url']}")
            if len(actionable) > args.show:
                print(f"    … and {len(actionable) - args.show} more")
            print("DRY RUN — pass --apply to write")
            return

        states = await (_revert(actionable) if args.revert else _withdraw(actionable))
        verb = "reverted" if args.revert else "withdrawn"
        still = sorted(k for k, serving in states.items() if serving)
        print(f"APPLIED: {len(actionable)} row(s) {verb}; {len(states)} content_key(s) recomputed")
        if not args.revert and still:
            print(
                f"  RESIDUAL: {len(still)}/{len(states)} content_key(s) still serve. A real "
                "product shares each key and holds eligibility (eligibility is content_key "
                "grain; the PDP resolver is row grain). These need the paired PIVOTA-Agent "
                "resolver predicate — they are NOT closed by this run:"
            )
            for k in still[:10]:
                print(f"    {k}")
            if len(still) > 10:
                print(f"    … and {len(still) - 10} more")
    finally:
        await database.disconnect()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", help="restrict to one registrable host (e.g. biodance.com)")
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    p.add_argument("--revert", action="store_true", help="clear suppressed_at this runner set")
    p.add_argument("--show", type=int, default=20, help="rows to list in dry-run output")
    asyncio.run(_drive(p.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

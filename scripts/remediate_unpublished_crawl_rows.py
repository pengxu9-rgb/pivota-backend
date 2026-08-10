"""Take down ad-campaign landing pages ALREADY ingested as products (#1926).

The companion to the ingestion gate in
``scripts/onboard_external_brand_from_crawl.py``. That gate stops new campaign
pages ever becoming products; this remediates the ones already in the catalog.
Deliberately a separate runner: it changes SERVING state for rows that are live
today, which is a different blast radius from declining to create a row.

Why the July sweep did not take anything down
----------------------------------------------
``scripts/step5_lane3_campaign_clone_dedup.py`` adjudicated exactly this cohort
on 2026-07-10 and those PDPs are still up. NOT, as a first review supposed,
because it wrote ``suppression_reason`` without ``suppressed_at`` — measured in
prod, it wrote BOTH, on 198 of biodance's 250 mirrors, all stamped at
``2026-07-10T03:10:57Z`` with its ``run_id`` in ``suppression_metadata``.

It failed on GRAIN. Serving eligibility is keyed on ``content_key`` and
``_select_content_key_state`` takes the MAX state across that key's rows, so one
unsuppressed row keeps the whole key eligible. step5 spared exactly one keeper
per dedup group by design — and the keeper it elected is itself a campaign page.
23 keepers plus 14 rows on keys it never visited left all 34 content_keys
serving. 213 suppressed rows outvoted by 37 stragglers.

So the takedown needs COMPLETENESS, not a new mechanism: adjudicate every row on
the key by one rule, then recompute (nothing propagates ``suppressed_at`` on its
own — there is no trigger).

WHAT "UNPUBLISHED" HERE DOES AND DOES NOT MEAN
----------------------------------------------
It means ABSENT FROM THE MERCHANT'S PRODUCT SITEMAP. It does NOT mean the page
is gone: measured across the 272 rows this runner would act on, 102 return HTTP
200 with ``og:type=product`` and ``InStock`` — and so do biodance's ad landing
pages, which are real Shopify products with real PDP markup. Fetching the URL
therefore CANNOT separate an ad variant from a delisted-but-live product; both
look identical. (A review proposed exactly that corroboration; it would have
demoted the entire biodance cohort too.)

That ambiguity is why this runner will not withdraw a LONE row. See
``--min-cluster``.

The cluster predicate
---------------------
An ad-variant farm mints many listings that collapse onto ONE ``content_key``
(same brand+title, N campaign URLs); biodance has 45 rows on a single key. A
real product that merely left the sitemap is the SOLE occupant of its key.
Measured over every crawled host:

    tonymoly.us     189/189 sitemap-absent rows are lone occupants
    celimax.us, goongbe.us, manyo.us, milktouch.us, mixsoon.us, pcalm.us — all lone
    biodance.com    242/250 rows sit on keys carrying 2..45 rows

Every false positive found in review (21 live TONYMOLY sheet masks, 15 live
arencia.us serums) is a lone occupant. Every biodance ad-farm row is clustered.
So ``--min-cluster`` (default 2) withdraws only rows whose content_key carries
another crawl row, and a lone sitemap-absent listing is left alone as the
genuinely ambiguous case it is. This is structural, not a string heuristic.

It also composes with the grain rule for free: on a multi-row key, a sibling
that IS in the sitemap stays unsuppressed and holds the key eligible, so a real
product can never go dark because of a clone next to it.

Adjudication is not re-derived here — it calls the SAME
``services.shopify_publication_signal`` oracle the ingestion gate uses, so the
two entry points cannot drift. Every uncertainty is UNKNOWN and left alone.

Reversible: no hard deletes. ``--revert`` restores both halves — it clears
``suppressed_at`` AND puts the seed back to the status it had, recorded in
``suppression_metadata`` at withdrawal time.

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

# Stamped into catalog_products.suppression_metadata so --revert can find
# exactly the rows THIS runner withdrew, and restore the seed status it found.
SCRIPT_NAME = "remediate_unpublished_crawl_rows"

# Candidate rows: every crawl-mirrored catalog row that is not already withdrawn.
# Deliberately NOT filtered on suppression_reason — the step5-suppressed rows are
# exactly the ones whose takedown never landed, so they must be re-examined.
CANDIDATES_SQL = """
WITH crawl AS (
  SELECT cp.content_key, cp.source_ref
  FROM external_product_seeds s2
  JOIN catalog_products cp ON cp.source_ref = s2.id
  WHERE s2.tool = :tool AND cp.content_key IS NOT NULL
), cluster AS (
  SELECT content_key, count(*) AS rows_on_key FROM crawl GROUP BY 1
)
SELECT s.id            AS seed_id,
       s.status        AS seed_status,
       s.destination_url,
       cp.product_key,
       cp.content_key,
       cp.suppression_reason,
       cp.suppressed_at,
       COALESCE(cl.rows_on_key, 1) AS rows_on_key
FROM external_product_seeds s
JOIN catalog_products cp ON cp.source_ref = s.id
LEFT JOIN cluster cl ON cl.content_key = cp.content_key
WHERE s.tool = :tool
  AND s.destination_url IS NOT NULL
  AND cp.suppressed_at IS NULL
ORDER BY s.destination_url
"""

# Revert reads a DIFFERENT row set than withdrawal: the rows to undo are exactly
# the ones already stamped, which CANDIDATES_SQL excludes by design. Sharing one
# query made --revert a silent no-op — it received only never-suppressed rows,
# and then only those the oracle still called UNPUBLISHED, which is the opposite
# of why anyone reverts.
REVERT_CANDIDATES_SQL = """
SELECT s.id AS seed_id, s.status AS seed_status, s.destination_url,
       cp.product_key, cp.content_key, cp.suppression_reason, cp.suppressed_at,
       cp.suppression_metadata
FROM external_product_seeds s
JOIN catalog_products cp ON cp.source_ref = s.id
WHERE s.tool = :tool
  AND cp.suppressed_at IS NOT NULL
  AND cp.suppression_metadata->>'script' = CAST(:script AS text)
ORDER BY s.destination_url
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


async def _recompute_and_verify(content_keys: List[str], reason: str) -> Dict[str, str]:
    """Recompute each key, then READ BACK what actually persisted.

    recompute_serving_eligibility swallows every exception and returns False on
    failure (it is designed to leave the nightly sweep to catch up), so its
    return value cannot distinguish "went dark" from "the recompute blew up".
    Reporting a failed recompute as a successful takedown is the worst outcome
    here — the operator believes a page is down while it is still serving. So we
    ask the table.

    Returns {content_key: 'dark' | 'serving' | 'unknown'}."""
    out: Dict[str, str] = {}
    for content_key in content_keys:
        await recompute_serving_eligibility(content_key, reason=reason)
        row = await database.fetch_one(
            "SELECT serving_eligible FROM index_pipeline_state WHERE content_key=:ck",
            {"ck": content_key},
        )
        if row is None:
            out[content_key] = "unknown"
        else:
            out[content_key] = "serving" if dict(row).get("serving_eligible") else "dark"
    return out


async def _withdraw(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Stamp suppressed_at (+ reason where absent), deactivate the seed, then
    recompute every touched content_key. Each write is idempotency-guarded, so a
    re-run is a no-op."""
    for row in rows:
        sid = row["seed_id"]
        # Record the seed's PRIOR status before changing it, so --revert can put
        # it back rather than blanket-activating seeds that were already dead.
        await database.execute(
            # CAST is load-bearing: jsonb_build_object is variadic "any", so
            # Postgres cannot infer a bind param's type from position and the
            # statement fails to PREPARE with "could not determine data type of
            # parameter $2". It fails at prepare time, before any write — which
            # is why the first --apply attempt aborted having changed nothing —
            # but it fails on EVERY row, so the runner is simply dead without
            # this. Not reachable by the unit tests: they record SQL strings
            # against a fake connection and never ask Postgres to plan it.
            "UPDATE catalog_products "
            "   SET suppression_metadata = COALESCE(suppression_metadata, '{}'::jsonb) "
            "       || jsonb_build_object("
            "            'script', CAST(:script AS text), "
            "            'prior_seed_status', CAST(:prior AS text)), "
            "       updated_at = NOW() "
            " WHERE source_ref = :id AND suppressed_at IS NULL",
            {"id": sid, "script": SCRIPT_NAME, "prior": row.get("seed_status") or "unknown"},
        )
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
    keys = sorted({r["content_key"] for r in rows if r.get("content_key")})
    return await _recompute_and_verify(keys, UNPUBLISHED_SUPPRESSION_REASON)


async def _revert(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Undo a withdrawal: clear suppressed_at AND restore the seed's prior
    status. Clearing only the catalog half would leave a new half-state (live
    row, dead seed) rather than the state we found."""
    for row in rows:
        sid = row["seed_id"]
        meta = row.get("suppression_metadata") or {}
        if isinstance(meta, str):
            import json as _json

            try:
                meta = _json.loads(meta)
            except ValueError:
                meta = {}
        prior = (meta or {}).get("prior_seed_status")
        await database.execute(
            "UPDATE catalog_products SET suppressed_at=NULL, updated_at=NOW() "
            "WHERE source_ref=:id AND suppressed_at IS NOT NULL",
            {"id": sid},
        )
        # Only fill the reason back in if it is OURS — a row that carried
        # step5's reason keeps it.
        await database.execute(
            "UPDATE catalog_products SET suppression_reason=NULL, updated_at=NOW() "
            "WHERE source_ref=:id AND suppression_reason=:reason",
            {"id": sid, "reason": UNPUBLISHED_SUPPRESSION_REASON},
        )
        if prior == "active":
            await database.execute(
                "UPDATE external_product_seeds SET status='active', updated_at=NOW() "
                "WHERE id=:id AND status='inactive'",
                {"id": sid},
            )
    keys = sorted({r["content_key"] for r in rows if r.get("content_key")})
    return await _recompute_and_verify(keys, f"{UNPUBLISHED_SUPPRESSION_REASON}_revert")


def _report_plan(rows: List[Dict[str, Any]], counts: Dict[str, int], actionable: List[Dict[str, Any]]) -> None:
    by_host: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_host[_host_of(r["destination_url"])].append(r)

    print(
        f"candidates: {len(rows)} live crawl row(s) across {len(by_host)} host(s) — "
        f"{counts.get(PUBLISHED, 0)} published, {counts.get(UNPUBLISHED, 0)} unpublished, "
        f"{counts.get(UNKNOWN, 0)} unknown (left alone)"
    )
    act_ids = {id(r) for r in actionable}
    for host in sorted(by_host):
        rs = by_host[host]
        unpub = [r for r in rs if r.get("verdict") == UNPUBLISHED]
        if not unpub:
            continue
        # Report what will ACTUALLY be withdrawn, not what merely classified
        # unpublished — the cluster guard spares a large share of the latter,
        # and conflating the two overstates the blast radius by 2.4x.
        take = [r for r in unpub if id(r) in act_ids]
        spared = len(unpub) - len(take)
        if not take:
            print(f"  {host:28} 0/{len(rs)} to withdraw  ({spared} lone row(s) spared)")
            continue
        # A host where every one of its rows is being withdrawn is either an
        # all-campaign crawl (biodance) or a sitemap/crawl URL-space mismatch.
        flag = "  <-- ENTIRE HOST" if len(take) == len(rs) else ""
        tail = f"  ({spared} lone spared)" if spared else ""
        print(f"  {host:28} {len(take)}/{len(rs)} to withdraw{tail}{flag}")

    keys = {r["content_key"] for r in actionable if r.get("content_key")}
    print(f"  → would stamp suppressed_at on {len(actionable)} row(s), recompute {len(keys)} content_key(s)")


async def _drive_revert(args: argparse.Namespace) -> int:
    """Returns the number of rows actually WRITTEN (0 for any dry run/no-op)."""
    rows = [dict(r) for r in await database.fetch_all(
        REVERT_CANDIDATES_SQL, {"tool": TOOL, "script": SCRIPT_NAME})]
    if args.host:
        wanted = args.host.lower().removeprefix("www.")
        rows = [r for r in rows if _host_of(r["destination_url"]) == wanted]
    if not rows:
        print(f"nothing to revert: no rows carry suppression_metadata.script={SCRIPT_NAME!r}")
        return 0
    print(f"revert: {len(rows)} row(s) previously withdrawn by this runner")
    if not args.apply:
        for r in rows[: args.show]:
            print(f"    would restore {r['destination_url']}")
        print("DRY RUN — pass --apply to write")
        return 0
    states = await _revert(rows)
    back = sum(1 for v in states.values() if v == "serving")
    print(f"REVERTED: {len(rows)} row(s); {back}/{len(states)} content_key(s) serving again")
    return len(rows)


async def _drive(args: argparse.Namespace) -> int:
    """Returns the number of rows actually WRITTEN (0 for any dry run/no-op)."""
    await database.connect()
    try:
        if args.revert:
            return await _drive_revert(args)
        rows = await _load_candidates(args.host)
        if not rows:
            print("nothing to do: no live crawl-mirrored rows matched")
            return 0
        actionable, counts = await _adjudicate(rows)

        # The cluster predicate — see the module docstring. A LONE sitemap-absent
        # row is the ambiguous case (a delisted-but-live real product looks
        # exactly like an ad variant: both 200, both og:type=product, both
        # InStock), so it is never withdrawn.
        lone = [r for r in actionable if int(r.get("rows_on_key") or 1) < args.min_cluster]
        actionable = [r for r in actionable if int(r.get("rows_on_key") or 1) >= args.min_cluster]
        _report_plan(rows, counts, actionable)
        if lone:
            print(
                f"  spared {len(lone)} lone sitemap-absent row(s) "
                f"(content_key carries <{args.min_cluster} crawl rows — could be a real "
                "product the merchant delisted; --min-cluster 1 to include them)"
            )

        if not actionable:
            return 0
        if not args.apply:
            for r in actionable[: args.show]:
                print(f"    would withdraw [{r['rows_on_key']:>2} on key] {r['destination_url']}")
            if len(actionable) > args.show:
                print(f"    … and {len(actionable) - args.show} more")
            print("DRY RUN — pass --apply to write")
            return 0

        wrote = len(actionable)
        states = await _withdraw(actionable)
        dark = sorted(k for k, v in states.items() if v == "dark")
        serving = sorted(k for k, v in states.items() if v == "serving")
        failed = sorted(k for k, v in states.items() if v == "unknown")
        print(
            f"APPLIED: {len(actionable)} row(s) withdrawn; of {len(states)} content_key(s) "
            f"{len(dark)} now dark, {len(serving)} still serving, {len(failed)} unverifiable"
        )
        if serving:
            print(
                f"  RESIDUAL: {len(serving)} content_key(s) still serve — a sibling row holds "
                "eligibility (eligibility is content_key grain; the PDP resolver is row grain). "
                "NOT closed by this run; needs the paired PIVOTA-Agent resolver predicate:"
            )
            for k in serving[:10]:
                print(f"    {k}")
        if failed:
            print(
                f"  WARNING: {len(failed)} content_key(s) have no index_pipeline_state row after "
                "recompute — treat as NOT withdrawn and re-run:"
            )
            for k in failed[:10]:
                print(f"    {k}")
        return wrote
    finally:
        await database.disconnect()


def _dispatch_sitemap_refresh() -> None:
    """Best-effort: kick agent-ui's sitemap workflow so the takedown leaves the
    advertised set within minutes, not at the next 6-hourly tick.

    The sitemap is generated static files in pivota-agent-ui, refreshed by its
    `sitemaps.yml` cron (17 1,7,13,19 UTC) — the backend has no push channel, so
    between a sweep and the next tick every withdrawn URL stays advertised while
    its PDP already 404s/410s: exactly the Search Console churn this runner's
    withdrawals create. Operators run this script from a machine with `gh`
    already authenticated, so a workflow_dispatch is one subprocess away.
    Failure is loud but non-fatal — the cron remains the backstop.
    """
    import shutil
    import subprocess

    if not shutil.which("gh"):
        print("  sitemap refresh NOT dispatched: `gh` not on PATH — the 6-hourly "
              "cron will pick the withdrawal up (worst case ~6h of advertised 404s).")
        return
    try:
        subprocess.run(
            ["gh", "workflow", "run", "sitemaps.yml", "-R", "pengxu9-rgb/pivota-agent-ui"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        print("  sitemap refresh dispatched (pivota-agent-ui sitemaps.yml).")
    except Exception as exc:  # noqa: BLE001
        print(f"  sitemap refresh dispatch FAILED ({exc}) — the 6-hourly cron "
              "will pick the withdrawal up.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", help="restrict to one registrable host (e.g. biodance.com)")
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    p.add_argument("--revert", action="store_true",
                   help="restore rows this runner withdrew (suppressed_at + seed status)")
    p.add_argument(
        "--min-cluster", type=int, default=2, dest="min_cluster",
        help=(
            "only withdraw a row whose content_key carries at least N crawl rows. "
            "Default 2: a LONE sitemap-absent row is indistinguishable from a real "
            "product the merchant delisted (both serve 200/og:type=product/InStock), "
            "so it is spared. 1 disables the guard — measured, that withdraws 102 "
            "live product pages including 21 TONYMOLY sheet masks."
        ),
    )
    p.add_argument("--show", type=int, default=20, help="rows to list in dry-run output")
    args = p.parse_args()
    wrote = asyncio.run(_drive(args))
    # Dispatch ONLY when this run actually changed the advertised set. Gating on
    # the flags instead fired a real GitHub workflow_dispatch on dry runs — note
    # `--revert` without `--apply` IS a dry run — and on --apply runs that
    # matched nothing. Both directions matter when they do write: a withdrawal
    # must leave the sitemap, a revert must return to it.
    if wrote > 0:
        _dispatch_sitemap_refresh()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

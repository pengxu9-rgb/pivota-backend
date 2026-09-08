"""Repair three vocabulary leaks in the external-referral lane's stored rows.

WHAT IS WRONG, MEASURED ON PROD 2026-09-08
------------------------------------------
Three columns hold values their own producer says they must not hold:

  1.   844 catalog_offers rows carry `catalog_track = 'external_referral'` together with
       `readiness_tier = 'commerce_ready'`. The lane's own writer disagrees with them:
       `services/catalog_enrichment_agent/ingestion.OFFER_READINESS_TIER` is
       `'referral_only'`, and it is written next to `OFFER_CATALOG_TRACK` in the same dict.
       An external-referral offer is, by construction, a link to somebody else's checkout —
       `commerce_ready` on it is a claim we cannot honour.

  2. 5,083 catalog_skus rows on external-seed products carry `readiness_tier =
       'commerce_ready'`. Here the writer does not disagree: `services/catalog_variant_promoter`
       writes the string `'commerce_ready'` as a SQL literal for every row it promotes,
       including this track's. That writer's fix is a SEPARATE PR owned by another change;
       this script repairs the DATA the merged writer already produced. Run it AFTER that
       writer lands, or the promoter will simply re-mint the leak on its next pass. Running it
       before is not harmful, only temporary — this script is idempotent and re-runnable.

  3.    32 catalog_offers rows carry `availability = 'low_stock'`, which is not a value
       `utils/availability_vocabulary` can produce.

WHY `low_stock` BECOMES `unknown`, AND NOT `in_stock`
----------------------------------------------------
`utils/availability_vocabulary` is the one vocabulary, and it has exactly three outcomes:
`in_stock`, `out_of_stock`, and None. It has NO `low_stock` member — checked, not assumed:
`normalize_availability('low_stock')` and `normalize_availability('low stock')` both return
None. Its `_IN_STOCK_TOKENS` do contain `limitedstock` and `limitedavailability`, so the
neighbouring concept IS classified in-stock, and it would be easy to reason "low stock is
limited stock, therefore in_stock". That reasoning is what this comment exists to refuse.
The module states the asymmetry in prose and the code obeys it: it searches generously for
out-of-stock and NEVER infers in-stock from anything it has not listed, because a fabricated
positive builds a cart that dies at checkout. `low_stock` is not listed. So the vocabulary's
answer is None, whose stored spelling is `unknown` — and `unknown` is SERVABLE, so nothing is
delisted by this repair.

This script therefore does not carry a mapping table of its own. It calls
`normalize_availability` on whatever out-of-vocabulary strings the database actually holds and
writes what that function returns. If a fourth spelling appears next quarter, the dry run
prints the verdict it would apply, per distinct raw value, BEFORE anything is written.

Note that `scripts/backfill_variant_identity_skus._AVAILABILITY` — a private map in another
script — maps `low_stock` to itself. That map is the likely origin of these 32 rows and it is
not the vocabulary; it is not imported here.

WHICH JOIN DEFINES "an external-seed SKU"
-----------------------------------------
catalog_skus has no `catalog_track` column, so the lane can only be read off the PRODUCT.
Membership is defined by the JOIN — `catalog_skus.product_key = catalog_products.product_key
AND catalog_products.platform = 'external_seed'` — and NOT by `catalog_skus.platform`, which
is a denormalised copy the promoter fills from `cp.platform` and which nothing re-checks.
The product row is the authority for what lane a product is in.

That choice can lose rows (a SKU whose product row is gone, or whose copied platform
disagrees), so it is not left to trust: `--report` prints both populations and the two ways
they can diverge, so an operator can see the join's cost in rows before running `--apply`.

WHAT IT DOES NOT DO
-------------------
It does not touch internal-merchant offers' readiness tier: repair 1 is predicated on
`catalog_track = 'external_referral'`, so a legitimately `commerce_ready` merchant offer is
outside every UPDATE's WHERE. Repair 3 IS lane-agnostic on purpose — the availability
vocabulary is repo-wide, and an internal offer holding `low_stock` is the same defect — but it
writes only the `availability` column and never a readiness tier.

    python3 scripts/fix_external_track_vocabulary.py --report   # read-only, before/after
    python3 scripts/fix_external_track_vocabulary.py            # dry run, prints the plan
    python3 scripts/fix_external_track_vocabulary.py --apply    # writes + writer_audit_log row

There is no `--expect-contract` token here (the pattern `backfill_variant_identity_skus` uses
to defend against a stale image running a merged earlier draft) because this file is NEW: an
image built before the merge does not contain it at all, so the run command fails loudly with
"can't open file" rather than quietly doing something else. Add one the first time this
script's behaviour CHANGES.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_offer_writer_guard import (  # noqa: E402
    WriterAuditAccumulator,
    make_batch_id,
    write_writer_audit_log,
)
from utils.availability_vocabulary import (  # noqa: E402
    IN_STOCK,
    OUT_OF_STOCK,
    normalize_availability,
)

SOURCE_SYSTEM = "external_track_vocabulary_fix_v1"
WRITER_NAME = "fix_external_track_vocabulary"

#: The stored spelling of `normalize_availability`'s None. The vocabulary module returns a
#: Python None and has no name for its stored form; this is that form, and it is NOT invented
#: here — it is `db.catalog.catalog_offers.availability`'s own server_default. The column is
#: NOT NULL, so None has to be spelled as something, and the schema already chose.
#: `tests/test_fix_external_track_vocabulary_postgres` asserts this against the model rather
#: than leaving the two to drift.
UNKNOWN = "unknown"

#: Every value catalog_offers.availability is allowed to hold. Derived from the vocabulary's
#: own outcomes — do not add a member here without adding it to the vocabulary first.
LEGAL_AVAILABILITY = frozenset({IN_STOCK, OUT_OF_STOCK, UNKNOWN})

#: Sentinels around the one-line report. `scripts/ops/run_oneoff_job.sh` reads the job's output
#: back from Cloud Logging, which DROPS LINES — a multi-line report arrives with arbitrary keys
#: missing and nothing saying so. One line cannot be partially dropped. Distinct tokens and an
#: ANCHORED strip, so a value that happens to contain the token is not corrupted:
#:
#:     ... | grep -o 'VOCABREPORT>>>{.*}<<<VOCABREPORT' \
#:         | sed 's/^VOCABREPORT>>>//; s/<<<VOCABREPORT$//' | python3 -m json.tool
REPORT_BEGIN = "VOCABREPORT>>>"
REPORT_END = "<<<VOCABREPORT"


# ---------------------------------------------------------------------------
# SQL. Top-level literals, so tests/test_repo_sql_prepare_postgres.py can collect and
# PREPARE each one. Nothing below may become an f-string or a .format().
#
# The COUNT and the UPDATE for each repair carry the same predicate written twice, which is a
# real drift hazard: a dry run that plans off a stale predicate lies about what --apply will
# do. It is pinned behaviourally instead of syntactically — the gate runs both modes over one
# fixture and asserts the planned count equals the applied count.
# ---------------------------------------------------------------------------

COUNT_OFFER_TIER_SQL = """
    SELECT count(*) AS n
    FROM catalog_offers
    WHERE catalog_track = 'external_referral'
      AND readiness_tier = 'commerce_ready'
"""

#: Keyset-paged. The IN (SELECT ... ORDER BY ... LIMIT) shape bounds each statement's row
#: count so the one-off job stays inside DB_STATEMENT_TIMEOUT_SECONDS, and `RETURNING` is the
#: only way to learn how many rows moved: `databases` + asyncpg reports NO rowcount from
#: execute() on an UPDATE (SQLite does, which is exactly how that trap stays hidden locally).
#:
#: The cursor is not strictly required — an updated row stops matching the predicate, so a
#: bare LIMIT loop would also terminate — but it makes each page's window deterministic and
#: independent of concurrent writers, and it cannot skip a row because it only moves forward
#: through a set that only shrinks.
UPDATE_OFFER_TIER_SQL = """
    UPDATE catalog_offers
       SET readiness_tier = 'referral_only', updated_at = NOW()
     WHERE offer_id IN (
        SELECT offer_id
          FROM catalog_offers
         WHERE catalog_track = 'external_referral'
           AND readiness_tier = 'commerce_ready'
           AND offer_id > :after
         ORDER BY offer_id
         LIMIT :page
     )
    RETURNING offer_id
"""

COUNT_SKU_TIER_SQL = """
    SELECT count(*) AS n
    FROM catalog_skus cs
    JOIN catalog_products cp ON cp.product_key = cs.product_key
    WHERE cp.platform = 'external_seed'
      AND cs.readiness_tier = 'commerce_ready'
"""

UPDATE_SKU_TIER_SQL = """
    UPDATE catalog_skus
       SET readiness_tier = 'referral_only', updated_at = NOW()
     WHERE sku_key IN (
        SELECT cs.sku_key
          FROM catalog_skus cs
          JOIN catalog_products cp ON cp.product_key = cs.product_key
         WHERE cp.platform = 'external_seed'
           AND cs.readiness_tier = 'commerce_ready'
           AND cs.sku_key > :after
         ORDER BY cs.sku_key
         LIMIT :page
     )
    RETURNING sku_key
"""

#: The out-of-vocabulary values actually present, so the mapping is computed from the database
#: rather than from a guess about which spellings exist. NULL groups on its own key; the column
#: is NOT NULL in both the model and prod, so that group should always be empty, but it is
#: collected rather than assumed away — see UPDATE_AVAILABILITY_NULL_SQL.
SELECT_OFFENDING_AVAILABILITY_SQL = """
    SELECT availability AS value, count(*) AS n
    FROM catalog_offers
    WHERE availability IS NULL
       OR availability NOT IN ('in_stock', 'out_of_stock', 'unknown')
    GROUP BY availability
    ORDER BY availability
"""

#: One statement per distinct raw value, so :target is whatever the vocabulary returned for
#: THAT string. A caller must never pass a :target equal to :raw — the row would keep matching
#: and the loop would not terminate — and `_repair_availability` refuses that case explicitly.
UPDATE_AVAILABILITY_SQL = """
    UPDATE catalog_offers
       SET availability = :target, updated_at = NOW()
     WHERE offer_id IN (
        SELECT offer_id
          FROM catalog_offers
         WHERE availability = :raw
           AND offer_id > :after
         ORDER BY offer_id
         LIMIT :page
     )
    RETURNING offer_id
"""

#: `availability = :raw` cannot match a NULL, so the impossible-by-NOT-NULL case needs its own
#: statement rather than being silently left behind by the one above.
UPDATE_AVAILABILITY_NULL_SQL = """
    UPDATE catalog_offers
       SET availability = 'unknown', updated_at = NOW()
     WHERE offer_id IN (
        SELECT offer_id
          FROM catalog_offers
         WHERE availability IS NULL
           AND offer_id > :after
         ORDER BY offer_id
         LIMIT :page
     )
    RETURNING offer_id
"""

REPORT_OFFER_TIER_TRACK_SQL = """
    SELECT readiness_tier, catalog_track, count(*) AS n
    FROM catalog_offers
    GROUP BY readiness_tier, catalog_track
    ORDER BY readiness_tier, catalog_track
"""

REPORT_OFFER_AVAILABILITY_SQL = """
    SELECT availability, count(*) AS n
    FROM catalog_offers
    GROUP BY availability
    ORDER BY availability
"""

REPORT_SKU_TIER_SQL = """
    SELECT cp.platform AS platform, cs.readiness_tier AS readiness_tier, count(*) AS n
    FROM catalog_skus cs
    JOIN catalog_products cp ON cp.product_key = cs.product_key
    GROUP BY cp.platform, cs.readiness_tier
    ORDER BY cp.platform, cs.readiness_tier
"""

#: The cost of choosing the JOIN over `catalog_skus.platform`, in rows, over exactly the
#: population repair 2 would touch. `orphan` and `disagrees` are the two ways the two
#: definitions differ; if either is non-zero, the join choice is losing (or gaining) rows and
#: an operator should decide before applying rather than discover it afterwards.
REPORT_SKU_JOIN_DIVERGENCE_SQL = """
    SELECT
      count(*) FILTER (
        WHERE cs.platform = 'external_seed'
      ) AS by_sku_platform_column,
      count(*) FILTER (
        WHERE cp.product_key IS NOT NULL AND cp.platform = 'external_seed'
      ) AS by_joined_product,
      count(*) FILTER (
        WHERE cs.platform = 'external_seed' AND cp.product_key IS NULL
      ) AS sku_platform_but_no_product_row,
      count(*) FILTER (
        WHERE cs.platform = 'external_seed'
          AND cp.product_key IS NOT NULL
          AND cp.platform <> 'external_seed'
      ) AS sku_platform_disagrees_with_product
    FROM catalog_skus cs
    LEFT JOIN catalog_products cp ON cp.product_key = cs.product_key
    WHERE cs.readiness_tier = 'commerce_ready'
"""


# ---------------------------------------------------------------------------


def availability_repair_for(raw: Optional[str]) -> str:
    """The vocabulary's verdict for a stored value, in its stored spelling.

    NULL and every unrecognised string resolve to `unknown`, never to a positive. This is a
    one-line wrapper on purpose: the mapping lives in `utils.availability_vocabulary` and this
    file must not accumulate a second copy of it.
    """
    return normalize_availability(raw) or UNKNOWN


async def _drain(fetch_page: Any, *, page: int, limit: int) -> int:
    """Run a keyset-paged UPDATE ... RETURNING until it stops matching rows.

    `fetch_page(after, page)` returns the cursor values of the rows it moved — an empty list
    means the repair is done. The SQL is NOT threaded through here as an argument, and that is
    not a style choice: `tests/test_repo_sql_prepare_postgres` resolves a call site's SQL only
    when it is a literal or a module-level constant NAMED AT THE CALL, and it treats a function
    parameter as an unresolvable binding on purpose. A generic `_paged_update(sql, ...)` helper
    would have hidden all four UPDATEs from the PREPARE gate while every test still passed.

    `limit` (0 = unbounded) caps the TOTAL rows this repair may move, so an operator can take a
    small first bite in production and read the audit row before committing to the rest.
    """
    moved = 0
    after = ""
    while True:
        this_page = page if not limit else min(page, limit - moved)
        if this_page <= 0:
            break
        cursors = await fetch_page(after, this_page)
        if not cursors:
            break
        moved += len(cursors)
        after = max(cursors)
    return moved


async def _page_offer_tier(db: Any, after: str, page: int) -> List[str]:
    rows = await db.fetch_all(UPDATE_OFFER_TIER_SQL, {"after": after, "page": page})
    return [str(dict(r)["offer_id"]) for r in rows]


async def _page_sku_tier(db: Any, after: str, page: int) -> List[str]:
    rows = await db.fetch_all(UPDATE_SKU_TIER_SQL, {"after": after, "page": page})
    return [str(dict(r)["sku_key"]) for r in rows]


async def _page_availability(db: Any, raw: str, target: str, after: str,
                             page: int) -> List[str]:
    rows = await db.fetch_all(
        UPDATE_AVAILABILITY_SQL,
        {"raw": raw, "target": target, "after": after, "page": page},
    )
    return [str(dict(r)["offer_id"]) for r in rows]


async def _page_availability_null(db: Any, after: str, page: int) -> List[str]:
    rows = await db.fetch_all(UPDATE_AVAILABILITY_NULL_SQL, {"after": after, "page": page})
    return [str(dict(r)["offer_id"]) for r in rows]


async def _repair_availability(*, apply: bool, page: int, limit: int, db: Any
                               ) -> Tuple[int, int, Dict[str, str], Dict[str, int]]:
    """Plan (and optionally apply) the availability repair.

    Returns (planned, moved, verdict-per-raw-value, row-count-per-raw-value). The verdicts are
    returned so the DRY RUN prints them: an operator seeing `{"low_stock": "unknown"}` before
    any write is the whole point, because that single decision is the one this script could
    plausibly get wrong.
    """
    db = db or database
    offending = [dict(r) for r in await db.fetch_all(SELECT_OFFENDING_AVAILABILITY_SQL)]
    verdicts: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    planned = 0
    for row in offending:
        raw = row["value"]
        n = int(row["n"] or 0)
        key = "<null>" if raw is None else str(raw)
        verdicts[key] = availability_repair_for(raw)
        counts[key] = n
        planned += n

    moved = 0
    if apply:
        for row in offending:
            raw = row["value"]
            if limit and moved >= limit:
                break
            budget = 0 if not limit else limit - moved
            if raw is None:
                moved += await _drain(
                    lambda after, page_size: _page_availability_null(db, after, page_size),
                    page=page, limit=budget)
                continue
            target = availability_repair_for(raw)
            if target == str(raw):
                # Unreachable while the selecting predicate excludes the legal vocabulary, and
                # refused rather than trusted: this is the shape that loops forever, because
                # the updated row keeps matching `availability = :raw`.
                raise AssertionError(
                    f"availability {raw!r} maps to itself; that value belongs in the "
                    f"vocabulary, not in a repair"
                )
            raw_text = str(raw)
            moved += await _drain(
                lambda after, page_size, r=raw_text, t=target: _page_availability(
                    db, r, t, after, page_size),
                page=page, limit=budget)
    return planned, moved, verdicts, counts


def _ints(rows: Any) -> List[Dict[str, Any]]:
    return [{k: (int(v or 0) if k == "n" else v) for k, v in dict(r).items()} for r in rows]


async def report(db: Any = None) -> Dict[str, Any]:
    """Read-only. The before/after an operator diffs across an --apply."""
    db = db or database
    divergence = await db.fetch_one(REPORT_SKU_JOIN_DIVERGENCE_SQL)
    return {
        "mode": "report",
        "offers_by_readiness_tier_and_track": _ints(
            await db.fetch_all(REPORT_OFFER_TIER_TRACK_SQL)),
        "offers_by_availability": _ints(await db.fetch_all(REPORT_OFFER_AVAILABILITY_SQL)),
        "skus_by_product_platform_and_tier": _ints(await db.fetch_all(REPORT_SKU_TIER_SQL)),
        # Named for what it is: the population repair 2 would touch, counted BOTH ways.
        "commerce_ready_skus_membership": {
            k: int(v or 0) for k, v in dict(divergence or {}).items()},
    }


async def run(*, apply: bool = False, page: int = 500, limit: int = 0,
              db: Any = None) -> Dict[str, Any]:
    db = db or database
    audit = WriterAuditAccumulator(
        writer_name=WRITER_NAME, batch_id=make_batch_id(SOURCE_SYSTEM))

    offer_tier_planned = int(dict(await db.fetch_one(COUNT_OFFER_TIER_SQL))["n"] or 0)
    sku_tier_planned = int(dict(await db.fetch_one(COUNT_SKU_TIER_SQL))["n"] or 0)

    offer_tier_moved = 0
    sku_tier_moved = 0
    if apply:
        offer_tier_moved = await _drain(
            lambda after, page_size: _page_offer_tier(db, after, page_size),
            page=page, limit=limit)
        sku_tier_moved = await _drain(
            lambda after, page_size: _page_sku_tier(db, after, page_size),
            page=page, limit=limit)

    avail_planned, avail_moved, verdicts, avail_counts = await _repair_availability(
        apply=apply, page=page, limit=limit, db=db)

    out: Dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "offer_readiness_tier": {"planned": offer_tier_planned, "updated": offer_tier_moved},
        "sku_readiness_tier": {"planned": sku_tier_planned, "updated": sku_tier_moved},
        "availability": {
            "planned": avail_planned,
            "updated": avail_moved,
            # The verdict per distinct stored value, printed on the DRY RUN too.
            "vocabulary_verdict": verdicts,
            "rows_per_raw_value": avail_counts,
        },
        "total_planned": offer_tier_planned + sku_tier_planned + avail_planned,
        "total_updated": offer_tier_moved + sku_tier_moved + avail_moved,
    }

    if apply:
        audit.record_applied(out["total_updated"])
        audit.record_info({
            "offer_readiness_tier_updated": offer_tier_moved,
            "sku_readiness_tier_updated": sku_tier_moved,
            "availability_updated": avail_moved,
        })
        # The verdicts go into the audit row, not just stdout: the log line can be dropped,
        # writer_audit_log cannot, and "what did we decide low_stock meant, on the run that
        # actually wrote" is the question a future reader will have.
        audit.reasons["availability_vocabulary_verdict"] = verdicts
        audit.reasons["source_system"] = SOURCE_SYSTEM
        await write_writer_audit_log(audit, db=db)
        out["batch_id"] = audit.batch_id
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Repair external-referral readiness_tier and out-of-vocabulary "
                    "availability. Dry run by default.")
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--report", action="store_true",
                    help="read-only census of the three columns; writes nothing, ignores "
                         "--apply")
    ap.add_argument("--page", type=int, default=500, help="rows per UPDATE statement")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the rows EACH repair may move (0 = all)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _go():
        await database.connect()
        try:
            if args.report:
                return await report()
            return await run(apply=args.apply, page=args.page, limit=args.limit)
        finally:
            await database.disconnect()

    out = asyncio.run(_go())
    print(REPORT_BEGIN + json.dumps(out, sort_keys=True, default=str) + REPORT_END, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

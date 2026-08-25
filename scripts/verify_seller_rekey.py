#!/usr/bin/env python3
"""Post-batch verification for the A9-4 seller-of-record flip. READ-ONLY.

The flip tool's own parity report is necessary but not sufficient — the tool
must never be the only grader of its own writes. This script re-derives the
verdict from the checkpoint table and the live rows, independently:

  moved            products checkpointed done in the catalog phase
  still_sentinel   moved rows whose catalog row still carries the banned bucket   (want 0)
  mismatched       moved rows whose merchant differs from the checkpointed target (want 0)
  servable         moved rows admitted by the D2 public gate                      (want == moved)
  stranded_quality_snapshots
                   moved rows whose quality score is still addressed by the
                   merchant they were moved OFF **and which the repair tool
                   would fix** — servable, yet unpublishable                   (want 0)
                   Deliberately NOT every stranded row: see the note above the
                   query for why counting the unrepairable ones would hold this
                   gate red forever.
  old_refs_left    identity listings still under 'external_seed:' for moved rows  (want 0)
  new_refs         listings under the new '<observed>:<pid>' refs
  overrides_on_new operator overrides attached to the NEW refs (work preserved)
  queue_on_new     review-queue rows attached to the NEW refs
  pgm_banned_left  group-membership rows still under the banned bucket            (want 0)
  pgm_moved        group-membership rows under the checkpointed target

Exit 0 when every "want" holds; exit 1 with the offending counts otherwise.

Runs inside production, so no public proxy is ever in the loop. Production is
Cloud Run (pivota-prod/us-west1) and there is no `railway ssh` equivalent — Cloud
Run has no running instance to attach to. Use a throwaway job on the production
image, which propagates this script's exit code as the job's:

    scripts/ops/run_oneoff_job.sh -m scripts.verify_seller_rekey

The exit code IS the verdict; the log is printed for detail only, behind a retry,
because Cloud Logging ingestion lag is unbounded. See
docs/runbooks/operating_on_gcp_production.md.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

Q1 = """
WITH moved AS (
  SELECT ref_id AS pk, observed_id
  FROM a9_4_backfill_checkpoint WHERE phase = 'catalog' AND status = 'done')
SELECT count(*) AS moved,
       count(*) FILTER (WHERE cp.merchant_id = 'external_seed') AS still_sentinel,
       count(*) FILTER (WHERE cp.merchant_id <> m.observed_id) AS mismatched,
       count(*) FILTER (WHERE cm.indexable IS TRUE
                          AND cm.status IN ('active','observed')
                          AND cp.content_key IS NOT NULL) AS servable
FROM moved m
JOIN catalog_products cp ON cp.product_key = m.pk
LEFT JOIN catalog_merchants cm ON cm.merchant_id = cp.merchant_id
"""

# STATE THE RE-KEY STRANDED — the check this verifier was missing on 2026-08-14.
#
# `servable` above is the D2 merchant gate (indexable + status + content_key).
# It is a STRICTLY WEAKER predicate than the one that actually publishes a URL,
# which also requires `index_pipeline_state.serving_eligible IS TRUE`. So the
# flip graded `servable: 11099 == moved` — a clean pass — while the
# sitemap-eligible set was halving underneath it, 8,222 -> 3,884 content_keys.
#
# The cause was a cascade MISS, not a bad re-key: `product_quality_snapshot`
# carries `merchant_id` but scopes products by `(platform, platform_product_id)`
# rather than product_key/content_key/sku_key, so `discover_cascade_tables`
# skipped it and `global_residue` files it under `history`. The classifier,
# however, reads it as CURRENT STATE with a lookup keyed on the row's present
# owner — so the instant `catalog_products.merchant_id` moved, the score read
# NULL and the row stopped serving. 6,424 products, every one still holding a
# perfectly good score under the merchant it was moved off.
#
# This query asks the OUTCOME question instead of re-litigating which tables are
# ownership and which are history: is a moved row unscored HERE while its score
# sits under the exact merchant the checkpoint moved it off? That is provable
# from the checkpoint alone, needs no table taxonomy, and catches any future
# dependent stranded the same way. Want 0.
# Deliberately does NOT reference `previous_value`: the catalog phase writes it
# as a constant, so it carries no per-row provenance, and rows predating its ADD
# COLUMN carry NULL — a NULL-excluding predicate would report 0 stranded for
# exactly the oldest rows.
#
# THIS MUST MATCH THE REPAIR'S COHORT, not merely overlap it. A looser "no score
# here, some score elsewhere" test is a strict SUPERSET: it also counts rows the
# repair deliberately skips (a shared natural key, several donor merchants),
# which are not stranded at all — their score belongs to a different live
# product and they are honestly unscored. Counting those would leave this gate
# red forever after a complete repair, and a gate that can never go green
# teaches operators to ignore it just as effectively as the false-green this
# metric was added to replace. Same conjuncts as
# scripts/repair_a9_4_orphaned_quality_snapshots.COHORT_SQL; if that predicate
# moves, this one moves with it.
Q_STRANDED_QUALITY = """
WITH moved AS (
  SELECT ref_id AS pk, observed_id
  FROM a9_4_backfill_checkpoint
  WHERE phase = 'catalog' AND status = 'done' AND observed_id IS NOT NULL),
catalog_key AS (
  SELECT platform, source_product_id, count(*) AS rows_on_key
  FROM catalog_products
  WHERE platform IS NOT NULL AND source_product_id IS NOT NULL
  GROUP BY platform, source_product_id),
snapshot_key AS (
  SELECT platform, platform_product_id,
         count(DISTINCT merchant_id) AS donor_count,
         min(merchant_id) AS donor_merchant
  FROM product_quality_snapshot
  WHERE platform IS NOT NULL AND platform_product_id IS NOT NULL
  GROUP BY platform, platform_product_id)
SELECT count(*) AS stranded_quality_snapshots
FROM moved m
JOIN catalog_products cp ON cp.product_key = m.pk
JOIN catalog_key ck
  ON ck.platform = cp.platform AND ck.source_product_id = cp.source_product_id
JOIN snapshot_key sk
  ON sk.platform = cp.platform AND sk.platform_product_id = cp.source_product_id
WHERE cp.merchant_id = m.observed_id
  AND cp.platform IS NOT NULL AND cp.source_product_id IS NOT NULL
  AND ck.rows_on_key = 1
  AND sk.donor_count = 1
  AND sk.donor_merchant <> cp.merchant_id
  AND sk.donor_merchant = 'external_seed'
"""

Q2 = """
WITH moved AS (
  SELECT ref_id AS pk, observed_id
  FROM a9_4_backfill_checkpoint WHERE phase = 'catalog' AND status = 'done'),
spids AS (
  -- A listing may key on the catalog row's source_product_id (Path B) OR the
  -- attached seed's external_product_id (Path C rows carry a NAME SLUG in
  -- source_product_id — measured 2026-08-07: 0 Path C listings under the
  -- slug). The tool migrates BOTH candidates; the verifier must check both,
  -- or new_refs reads 0 for a perfectly migrated Path C cohort.
  SELECT m.pk, m.observed_id, cp.source_product_id AS spid
  FROM moved m JOIN catalog_products cp ON cp.product_key = m.pk
  UNION
  SELECT m.pk, m.observed_id, e.external_product_id AS spid
  FROM moved m
  JOIN catalog_products cp ON cp.product_key = m.pk
  JOIN external_product_seeds e
    ON e.attached_product_key = cp.product_key AND e.status = 'active'
  WHERE e.external_product_id IS NOT NULL)
SELECT
  (SELECT count(*) FROM pdp_identity_listing pil
     JOIN spids s ON pil.product_id = s.spid
    WHERE pil.merchant_id = 'external_seed') AS old_refs_left,
  (SELECT count(*) FROM pdp_identity_listing pil
     JOIN spids s ON pil.source_listing_ref = s.observed_id || ':' || s.spid) AS new_refs,
  (SELECT count(*) FROM pdp_identity_override o
     JOIN spids s ON o.source_listing_ref = s.observed_id || ':' || s.spid) AS overrides_on_new,
  (SELECT count(*) FROM pdp_identity_review_queue q
     JOIN spids s ON q.source_listing_ref = s.observed_id || ':' || s.spid) AS queue_on_new,
  (SELECT count(*) FROM product_group_members pgm
     JOIN spids s ON pgm.platform_product_id = s.spid
    WHERE pgm.merchant_id = 'external_seed') AS pgm_banned_left,
  (SELECT count(*) FROM product_group_members pgm
     JOIN spids s ON pgm.platform_product_id = s.spid
      AND pgm.merchant_id = s.observed_id) AS pgm_moved
"""

SENTINEL_LANES = """
SELECT coalesce(source_system, '?') AS lane, count(*) AS remaining
FROM catalog_products WHERE merchant_id = 'external_seed' GROUP BY 1 ORDER BY 2 DESC
"""

# GLOBAL sentinel residue — every text-typed seller column of every public
# base table, counted without any checkpoint scope, straight from
# information_schema (deliberately NOT through the flip's reflection or its
# CASCADE_DENYLIST: a grader sharing the tool's blind spots is no grader).
#
# Two classes, and the distinction is PRINCIPLED, not a hand-picked list:
#   ownership — the table ALSO carries a product-scope column (product_key /
#               content_key / sku_key): the row belongs to a product and its
#               merchant is the product's seller-of-record. Sentinel residue
#               here after catalog_products is empty is an ORPHAN no re-key can
#               reach — FAILS.
#   history   — no product-scope column: events, logs, snapshots, runs,
#               telemetry. A row that recorded 'external_seed' at event time
#               is a TRUE record of what happened; rewriting it falsifies
#               history. REPORTED, never failed. (First live run 2026-08-15:
#               ~220k such rows across 14 tables, e.g. agent_product_events
#               151,698 — correct as they stand.)
# The catalog phase's Q1/Q2 grade the moved cohort; the rig-retirement step
# (which never enters the checkpoint) and any stray writer are graded HERE.
SELLER_COLUMNS_SQL = """
SELECT c.table_name, c.column_name,
       EXISTS (SELECT 1 FROM information_schema.columns s
                WHERE s.table_schema = c.table_schema AND s.table_name = c.table_name
                  AND s.column_name IN ('product_key', 'content_key', 'sku_key')) AS product_scoped
  FROM information_schema.columns c
  JOIN information_schema.tables t
    ON t.table_schema = c.table_schema AND t.table_name = c.table_name
 WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
   AND c.column_name IN ('merchant_id', 'primary_merchant_id')
   AND c.data_type IN ('text', 'character varying', 'character')
   AND c.table_name <> 'catalog_merchants'
 ORDER BY 1, 2
"""


# EVERY product-scope column a table carries — not the first by precedence.
#
# A table with more than one is the case that makes this matter, and two real
# ones exist: beauty_compatibility_rules and catalog_quote_snapshots (db/catalog.py)
# both carry product_key AND sku_key, both nullable. Picking one column by
# precedence and filing the row by THAT column's nullness excuses a sku-scoped
# row whose product_key happens to be NULL — genuine ownership residue, silently
# forgiven (measured against this file's own fixture, review 2026-08-17).
#
# So a row is SCOPED when ANY of its scope columns is non-NULL, and unscoped
# only when they are ALL NULL — the honest reading of "this row names no
# product".
SCOPE_COLUMNS_SQL = """
SELECT column_name FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = :table
   AND column_name IN ('product_key', 'content_key', 'sku_key')
 ORDER BY array_position(ARRAY['product_key','content_key','sku_key'], column_name)
"""


async def global_residue(database) -> dict:
    """Returns {"ownership": {t: n}, "unscoped": {t: n}, "history": {t: n}} —
    nonzero entries only, plus the catalog_products count under "ownership".

    THE THIRD BUCKET, and why it is not a loophole. A product-scoped table's
    sentinel row is an orphan only if it actually names a product. Three tables
    carry a scope column that is NULL on every sentinel row (measured
    2026-08-16: evidence_items 5, action_plan_items 2, niche_target_outcomes
    317) because their merchant_id is the audit's TENANT, not a seller — two BD
    audit runs were executed FOR the retired bucket on 2026-06-30, and
    migration 088 added merchant_id to those tables expressly for tenancy
    ("tenancy is single-layer … add merchant_id"), while niche_target_outcomes'
    content_key came later (migration 158) and NO writer fills it
    (services/niche_outcomes.py inserts merchant_id + query + run only).

    No re-key can follow a NULL scope, and rewriting the tenant would move one
    tenant's audit history onto another. So these are REPORTED, never failed —
    the same treatment history tables get, for the same reason.

    What this deliberately does NOT do is excuse a row that names a product.
    A sentinel row WITH a scope key is still an orphan and still FAILS, so a
    writer that stamps the sentinel on a real product cannot hide here; and
    because the split is counted per row, a table can appear in both buckets.
    """
    from scripts.backfill_seller_of_record import BANNED_BUCKET_MERCHANT_ID

    out = {"ownership": {}, "unscoped": {}, "history": {}}
    for r in await database.fetch_all(SELLER_COLUMNS_SQL):
        d = dict(r)
        table, col = d["table_name"], d["column_name"]
        product_scoped = bool(d["product_scoped"]) or table == "catalog_products"
        scope_cols: list = []
        if product_scoped and table != "catalog_products":
            scope_cols = [dict(r)["column_name"] for r in
                          await database.fetch_all(SCOPE_COLUMNS_SQL, {"table": table})]

        if scope_cols:
            # Scoped when ANY scope column names something; unscoped only when
            # every one of them is NULL.
            names = ", ".join(scope_cols)
            row = await database.fetch_one(
                f"SELECT count(*) FILTER (WHERE num_nonnulls({names}) > 0) AS scoped, "
                f"       count(*) FILTER (WHERE num_nonnulls({names}) = 0) AS unscoped "
                f"  FROM {table} WHERE {col} = :banned",
                {"banned": BANNED_BUCKET_MERCHANT_ID})
            dd = dict(row) if row else {"scoped": 0, "unscoped": 0}
            scoped, unscoped = int(dd["scoped"]), int(dd["unscoped"])
            if scoped:
                out["ownership"][table] = out["ownership"].get(table, 0) + scoped
            if unscoped:
                out["unscoped"][table] = out["unscoped"].get(table, 0) + unscoped
            continue

        row = await database.fetch_one(
            f"SELECT count(*) AS c FROM {table} WHERE {col} = :banned",
            {"banned": BANNED_BUCKET_MERCHANT_ID})
        n = int(dict(row)["c"]) if row else 0
        bucket = "ownership" if product_scoped else "history"
        if n or table == "catalog_products":
            out[bucket][table] = out[bucket].get(table, 0) + n
    return out


def orphan_failures(glob: dict) -> list:
    """Once catalog_products holds no sentinel rows, no OWNERSHIP table may
    either: a product-scoped row still on the sentinel is an orphan no re-key
    can reach. History tables are reported, never failed. While the bucket
    still holds rows, dependents legitimately share it — no failure."""
    if glob["ownership"].get("catalog_products", 0) != 0:
        return []
    return [f"orphan_residue:{t}={n}" for t, n in glob["ownership"].items() if n]


async def main() -> int:
    from db.database import database

    await database.connect()
    try:
        r1 = dict(await database.fetch_one(Q1))
        r2 = dict(await database.fetch_one(Q2))
        stranded = dict(await database.fetch_one(Q_STRANDED_QUALITY))
        lanes = {dict(r)["lane"]: dict(r)["remaining"]
                 for r in await database.fetch_all(SENTINEL_LANES)}
        glob = await global_residue(database)
    finally:
        await database.disconnect()

    failures = []
    if r1["still_sentinel"]:
        failures.append(f"still_sentinel={r1['still_sentinel']}")
    if r1["mismatched"]:
        failures.append(f"mismatched={r1['mismatched']}")
    if r1["servable"] != r1["moved"]:
        failures.append(f"servable={r1['servable']} != moved={r1['moved']}")
    if stranded["stranded_quality_snapshots"]:
        # Not cosmetic: each of these is a row that still passes `servable` and
        # still cannot be published, because its quality score is addressed by a
        # merchant it no longer has.
        failures.append(
            f"stranded_quality_snapshots={stranded['stranded_quality_snapshots']}"
        )
    if r2["old_refs_left"]:
        failures.append(f"old_refs_left={r2['old_refs_left']}")
    if r2["pgm_banned_left"]:
        failures.append(f"pgm_banned_left={r2['pgm_banned_left']}")
    failures.extend(orphan_failures(glob))

    print(json.dumps({"verdict": "FAIL" if failures else "OK",
                      "failures": failures, "rows": r1, "cascades": r2,
                      "stranded_state": stranded,
                      "sentinel_lanes_remaining": lanes,
                      "global_sentinel_residue": glob,
                      "unscoped_note": (
                          "unscoped = product-scoped tables whose sentinel rows carry a NULL "
                          "scope key: tenant attribution (audit runs executed FOR the retired "
                          "bucket), not seller-of-record. Reported, never failed — no re-key "
                          "can follow a NULL scope. A sentinel row that NAMES a product still "
                          "fails under ownership.")}, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

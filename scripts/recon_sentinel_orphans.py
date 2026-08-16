#!/usr/bin/env python3
"""ADR-009 orphan-residue reconnaissance. READ-ONLY — prints JSON, writes nothing.

After the A9-4 seller-of-record flip emptied the sentinel bucket
(catalog_products.merchant_id = BANNED_BUCKET_MERCHANT_ID -> 0), the
independent grader (scripts/verify_seller_rekey.py) still reports sentinel
residue on OWNERSHIP tables (rows carrying a product-scope column). Those rows
are ORPHANS: the flip's cascade is per-product ("for each moved product,
re-subject its dependents"), so a dependent row is left behind when the product
it points at was never in the bucket, no longer exists, or the row's scope key
is NULL and could never have been reached at all.

This script classifies every residue row so the founder can gate a per-table
disposition. For every table the verifier's SELLER_COLUMNS_SQL surfaces with
residue (ownership tables always; history tables only when named in
--also-history — pdp_identity_listing and product_enrichment carry identity /
content rows keyed on the product's SOURCE id, not product_key, so the
product-scope heuristic files them as history although they are ownership
-shaped), it reports:

  scope        which product-scope column exists (product_key / content_key /
               sku_key), and how many residue rows carry NULL there — a NULL
               scope row belongs to no product and no re-key can follow one;
  by_product   for product_key scope: does the product exist? under which
               merchant now? is it tombstoned? was it checkpointed by the flip
               (a9_4_backfill_checkpoint, phase 'catalog')?
  by_content   for content_key scope: how many catalog rows share the key, the
               distinct merchants among them, how many were checkpointed;
  by_sku       for sku_key scope: the same through catalog_skus;
  by_source_id for pdp_identity_listing (product_id) and product_enrichment
               (platform + platform_product_id): the catalog rows that carry
               that source id under ANY merchant, and the seed rows whose
               external_product_id equals it;
  sample       up to --sample rows (key columns + timestamps) per table.

Every table/column name is taken from information_schema and validated
against a strict identifier pattern before interpolation; nothing else is
interpolated. Bind parameters are exactly the names each statement uses.

Dispatch: gh workflow run adr009-orphan-recon.yml --repo pengxu9-rgb/pivota-backend
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

# Reflected per table: which columns exist. Only these names are ever
# interpolated, and only after _ident() accepts them.
COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = :table
 ORDER BY ordinal_position
"""

# The checkpoint phases the flip wrote (catalog = the product move; the others
# are seeds/barekey/edges). Only 'catalog' answers "was this product moved".
CHECKPOINT_PHASE = "catalog"

TIME_COLS = ("created_at", "updated_at", "seen_at", "observed_at", "last_seen_at")
KEY_COLS = ("product_key", "content_key", "sku_key", "product_id", "platform",
            "platform_product_id", "source_listing_ref", "audit_run_id", "id")


def _ident(name: str) -> str:
    if not _IDENT.match(name or ""):
        raise RuntimeError(f"refusing to interpolate identifier {name!r}")
    return name


async def _fetch(db, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(sql, params or {})
    return [dict(r) for r in rows or []]


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return str(v)


async def _columns(db, table: str) -> Dict[str, Dict[str, str]]:
    return {d["column_name"]: d for d in await _fetch(db, COLUMNS_SQL, {"table": table})}


async def _by_product_key(db, table: str, seller_col: str, banned: str) -> List[Dict[str, Any]]:
    t, sc = _ident(table), _ident(seller_col)
    return await _fetch(db, f"""
        SELECT (t.product_key IS NULL)            AS scope_null,
               (cp.product_key IS NOT NULL)       AS product_exists,
               cp.merchant_id                     AS current_merchant,
               (cp.suppression_reason IS NOT NULL) AS product_tombstoned,
               cp.suppression_reason              AS tombstone_reason,
               ck.status                          AS checkpoint_status,
               ck.observed_id                     AS checkpoint_target,
               count(*)                           AS n
          FROM {t} t
          LEFT JOIN catalog_products cp ON cp.product_key = t.product_key
          LEFT JOIN a9_4_backfill_checkpoint ck
                 ON ck.phase = :phase AND ck.ref_id = t.product_key
         WHERE t.{sc} = :banned
         GROUP BY 1, 2, 3, 4, 5, 6, 7
         ORDER BY n DESC
    """, {"phase": CHECKPOINT_PHASE, "banned": banned})


async def _by_content_key(db, table: str, seller_col: str, banned: str) -> List[Dict[str, Any]]:
    t, sc = _ident(table), _ident(seller_col)
    return await _fetch(db, f"""
        WITH res AS (
          SELECT t.content_key,
                 (SELECT count(*) FROM catalog_products cp
                   WHERE cp.content_key = t.content_key)                       AS n_products,
                 (SELECT array_agg(DISTINCT cp.merchant_id ORDER BY cp.merchant_id)
                    FROM catalog_products cp
                   WHERE cp.content_key = t.content_key)                       AS merchants,
                 (SELECT count(*) FROM catalog_products cp
                    JOIN a9_4_backfill_checkpoint ck
                      ON ck.phase = :phase AND ck.ref_id = cp.product_key
                   WHERE cp.content_key = t.content_key)                       AS n_checkpointed,
                 (SELECT count(*) FROM catalog_products cp
                   WHERE cp.content_key = t.content_key
                     AND cp.suppression_reason IS NULL)                        AS n_live
            FROM {t} t
           WHERE t.{sc} = :banned)
        SELECT (content_key IS NULL) AS scope_null,
               n_products, merchants, n_checkpointed, n_live, count(*) AS n
          FROM res
         GROUP BY 1, 2, 3, 4, 5
         ORDER BY n DESC
    """, {"phase": CHECKPOINT_PHASE, "banned": banned})


async def _by_sku_key(db, table: str, seller_col: str, banned: str) -> List[Dict[str, Any]]:
    t, sc = _ident(table), _ident(seller_col)
    return await _fetch(db, f"""
        SELECT (t.sku_key IS NULL)                AS scope_null,
               (cs.sku_key IS NOT NULL)           AS sku_exists,
               (cp.product_key IS NOT NULL)       AS product_exists,
               cp.merchant_id                     AS current_merchant,
               (cp.suppression_reason IS NOT NULL) AS product_tombstoned,
               ck.status                          AS checkpoint_status,
               ck.observed_id                     AS checkpoint_target,
               count(*)                           AS n
          FROM {t} t
          LEFT JOIN catalog_skus cs ON cs.sku_key = t.sku_key
          LEFT JOIN catalog_products cp ON cp.product_key = cs.product_key
          LEFT JOIN a9_4_backfill_checkpoint ck
                 ON ck.phase = :phase AND ck.ref_id = cp.product_key
         WHERE t.{sc} = :banned
         GROUP BY 1, 2, 3, 4, 5, 6, 7
         ORDER BY n DESC
    """, {"phase": CHECKPOINT_PHASE, "banned": banned})


async def _listing_by_source_id(db, banned: str) -> List[Dict[str, Any]]:
    """pdp_identity_listing keys on product_id = the catalog row's
    source_product_id (Path B) or the seed's external_product_id (Path C).

    The id is aliased to `pid` in the CTE deliberately. This classifier counts
    the catalog rows carrying the id under ANY merchant — the cross-merchant
    fan-out that tests/test_identity_join_sql.py forbids in a SERVING join is
    the measurement here — and the alias keeps that intent explicit while
    leaving this file under both identity-join lints for whatever is written
    in it next. (Muting the file was the first attempt; that test's own header
    documents why a file-scoped exemption is the wrong trade.)
    """
    return await _fetch(db, """
        WITH res AS (
          SELECT l.product_id AS pid, l.identity_status, l.live_read_enabled,
                 l.source_listing_ref AS ref
            FROM pdp_identity_listing l
           WHERE l.merchant_id = :banned),
        stats AS (
          SELECT r.identity_status, r.live_read_enabled,
                 (SELECT count(*) FROM catalog_products cp
                   WHERE cp.source_product_id = r.pid)                         AS n_products,
                 (SELECT array_agg(DISTINCT cp.merchant_id ORDER BY cp.merchant_id)
                    FROM catalog_products cp
                   WHERE cp.source_product_id = r.pid)                         AS merchants,
                 (SELECT count(*) FROM catalog_products cp
                    JOIN a9_4_backfill_checkpoint ck
                      ON ck.phase = :phase AND ck.ref_id = cp.product_key
                   WHERE cp.source_product_id = r.pid)                         AS n_checkpointed,
                 (SELECT count(*) FROM external_product_seeds e
                   WHERE e.external_product_id = r.pid)                        AS n_seeds,
                 (SELECT count(*) FROM external_product_seeds e
                   WHERE e.external_product_id = r.pid AND e.status = 'active') AS n_seeds_active,
                 -- attached to a catalog row that EXISTS and is not tombstoned;
                 -- "live" without the suppression test would call a tombstoned
                 -- product's seed live (review finding, 2026-08-16).
                 (SELECT count(*) FROM external_product_seeds e
                    JOIN catalog_products cp ON cp.product_key = e.attached_product_key
                   WHERE e.external_product_id = r.pid
                     AND e.status = 'active'
                     AND cp.suppression_reason IS NULL)                        AS n_seeds_attached_unsuppressed,
                 (SELECT count(*) FROM pdp_identity_override o
                   WHERE o.source_listing_ref = r.ref)                         AS n_overrides,
                 (SELECT count(*) FROM pdp_identity_review_queue q
                   WHERE q.source_listing_ref = r.ref)                         AS n_queue
            FROM res r)
        SELECT identity_status, live_read_enabled, n_products, merchants,
               n_checkpointed, n_seeds, n_seeds_active,
               n_seeds_attached_unsuppressed,
               (n_overrides > 0) AS has_overrides, (n_queue > 0) AS has_queue,
               count(*) AS n
          FROM stats
         GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
         ORDER BY n DESC
    """, {"phase": CHECKPOINT_PHASE, "banned": banned})


async def _enrichment_by_source_id(db, banned: str) -> List[Dict[str, Any]]:
    """Classify against the preconditions the EXISTING re-attribution tool
    (scripts/reattribute_orphaned_enrichment.py) actually applies, so the
    report answers "would H0 accept this row" rather than only "does a catalog
    row exist": its orphan guard requires absence from catalog_products AND
    products_cache under the row's own identity (n_cache_rows), and its apply
    rules require the TARGET identity to be vacant (target_occupied) and the
    mapping to be unique (n_orphans_to_same_target)."""
    return await _fetch(db, """
        WITH res AS (
          SELECT pe.platform AS plat, pe.platform_product_id AS ppid, pe.geo_code AS geo,
                 (SELECT count(*) FROM catalog_products cp
                   WHERE cp.platform = pe.platform
                     AND cp.source_product_id = pe.platform_product_id)        AS n_products_same_platform,
                 (SELECT array_agg(DISTINCT cp.merchant_id ORDER BY cp.merchant_id)
                    FROM catalog_products cp
                   WHERE cp.source_product_id = pe.platform_product_id)        AS merchants_any_platform,
                 (SELECT count(*) FROM products_cache pc
                   WHERE pc.merchant_id = pe.merchant_id
                     AND pc.platform = pe.platform
                     AND pc.platform_product_id = pe.platform_product_id)      AS n_cache_rows,
                 (SELECT count(*) FROM external_product_seeds e
                   WHERE e.external_product_id = pe.platform_product_id)       AS n_seeds,
                 -- '[]'::jsonb IS NOT NULL, so a bare NULL test calls an empty
                 -- bullet list "content" (review finding, 2026-08-16).
                 -- bullet_points is `json` in prod (the fresh-DB backstop DDL
                 -- in db/product_enrichment.py says JSONB, prod does not) — the
                 -- ::jsonb cast is what makes this run on BOTH, and coalescing
                 -- without it raises CannotCoerceError against prod.
                 (jsonb_array_length(coalesce(pe.bullet_points::jsonb, '[]'::jsonb)) > 0
                  OR (pe.description_markdown IS NOT NULL
                      AND btrim(pe.description_markdown) <> ''))               AS has_content,
                 EXISTS (SELECT 1 FROM catalog_products cp
                          JOIN product_enrichment t
                            ON t.merchant_id = cp.merchant_id
                           AND t.platform = pe.platform
                           AND t.platform_product_id = pe.platform_product_id
                         WHERE cp.platform = pe.platform
                           AND cp.source_product_id = pe.platform_product_id
                           AND cp.merchant_id <> pe.merchant_id)               AS target_occupied,
                 (SELECT count(*) FROM product_enrichment o
                    JOIN catalog_products cp2
                      ON cp2.platform = o.platform
                     AND cp2.source_product_id = o.platform_product_id
                   WHERE o.merchant_id = pe.merchant_id
                     AND cp2.source_product_id = pe.platform_product_id)       AS n_orphans_to_same_target
            FROM product_enrichment pe
           WHERE pe.merchant_id = :banned)
        SELECT plat AS platform, n_products_same_platform, merchants_any_platform,
               n_cache_rows, n_seeds, has_content, target_occupied,
               n_orphans_to_same_target, count(*) AS n
          FROM res
         GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
         ORDER BY n DESC
    """, {"banned": banned})


async def _sample(db, table: str, seller_col: str, cols: Dict[str, Any],
                  banned: str, limit: int) -> Dict[str, Any]:
    """Returns {"ordered_by": <col or None>, "rows": [...]}. A table with no
    known timestamp column has no ORDER BY, so WHICH rows come back is
    plan-dependent — say so in the output rather than let the reader assume
    "the newest N"."""
    t, sc = _ident(table), _ident(seller_col)
    pick = [c for c in KEY_COLS + TIME_COLS if c in cols]
    if not pick:
        pick = list(cols)[:6]
    sel = ", ".join(_ident(c) for c in pick)
    order = next((c for c in TIME_COLS if c in cols), None)
    order_sql = f"ORDER BY {_ident(order)} DESC NULLS LAST" if order else ""
    rows = await _fetch(
        db, f"SELECT {sel} FROM {t} WHERE {sc} = :banned {order_sql} LIMIT :lim",
        {"banned": banned, "lim": limit})
    return {"ordered_by": order, "rows": rows}


async def _scope_match_rate(db, table: str, seller_col: str, scope_col: str,
                            banned: str) -> Dict[str, Any]:
    """How many residue rows' scope keys resolve to a catalog row AT ALL.

    A table that keys in its OWN namespace (product_reviews builds
    `merchant|platform|id`, services/reviews_service.py) can never join the
    catalog's `product_key`, and the classification then reports
    `product_exists=false` for products that plainly exist — the right answer
    for the wrong reason. A 0/N match rate says "this table's keys are not
    catalog keys", which is the fact the reader needs."""
    t, sc, sk = _ident(table), _ident(seller_col), _ident(scope_col)
    if scope_col == "product_key":
        exists = "SELECT 1 FROM catalog_products cp WHERE cp.product_key = t.product_key"
    elif scope_col == "content_key":
        exists = "SELECT 1 FROM catalog_products cp WHERE cp.content_key = t.content_key"
    else:
        exists = "SELECT 1 FROM catalog_skus cs WHERE cs.sku_key = t.sku_key"
    row = await _fetch(db, f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE t.{sk} IS NOT NULL) AS scoped,
               count(*) FILTER (WHERE EXISTS ({exists})) AS resolves
          FROM {t} t WHERE t.{sc} = :banned
    """, {"banned": banned})
    d = row[0]
    return {"total": int(d["total"]), "scoped": int(d["scoped"]),
            "resolves_to_catalog": int(d["resolves"]),
            "keys_are_catalog_keys": bool(d["scoped"]) and bool(d["resolves"])}


async def _scope_nulls(db, table: str, seller_col: str, scope_col: str, banned: str) -> Dict[str, int]:
    t, sc, sk = _ident(table), _ident(seller_col), _ident(scope_col)
    row = await _fetch(db, f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE {sk} IS NULL) AS scope_null
          FROM {t} WHERE {sc} = :banned
    """, {"banned": banned})
    return {"total": int(row[0]["total"]), "scope_null": int(row[0]["scope_null"])}


async def recon(db, *, also_history: List[str], sample: int) -> Dict[str, Any]:
    from scripts.backfill_seller_of_record import BANNED_BUCKET_MERCHANT_ID
    from scripts.verify_seller_rekey import SELLER_COLUMNS_SQL, global_residue

    banned = BANNED_BUCKET_MERCHANT_ID
    # SELLER_COLUMNS_SQL emits one row PER seller column, and global_residue
    # SUMS every such column into the table's count. When a table carries both,
    # prefer 'merchant_id' — the same precedence the flip uses
    # (backfill_seller_of_record.discover_cascade_tables) — so the classifier
    # and the flip never disagree about which column is the seller.
    #
    # The whole seller surface is identifier-checked HERE, before anything is
    # counted: global_residue (in the verifier) interpolates table and column
    # names into its count query without a guard, and it runs first, so a name
    # that could not be safely interpolated has to abort before it — otherwise
    # this script's own guard is unreachable and the failure surfaces as a
    # syntax error from the driver, which says nothing about identifier safety.
    seller_cols: Dict[str, tuple] = {}
    for d in await _fetch(db, SELLER_COLUMNS_SQL):
        table, col = _ident(d["table_name"]), _ident(d["column_name"])
        prev = seller_cols.get(table)
        if prev is None or (prev[0] != "merchant_id" and col == "merchant_id"):
            seller_cols[table] = (col, bool(d["product_scoped"]))
    glob = await global_residue(db)

    targets: Dict[str, str] = {}
    for table, n in glob["ownership"].items():
        # `n` is nonzero by construction here (global_residue reports nonzero
        # entries plus catalog_products); the table test is what excludes the
        # bucket row itself, which is graded by the verifier, not classified.
        if table != "catalog_products":
            targets[table] = "ownership"
    for table in also_history:
        if table not in seller_cols:
            raise RuntimeError(f"--also-history names {table!r}, which carries no text seller column")
        if glob["history"].get(table):
            targets[table] = "history"

    out: Dict[str, Any] = {
        "banned": banned,
        "catalog_products_under_sentinel": glob["ownership"].get("catalog_products", 0),
        "global_residue": glob,
        "tables": {},
    }
    for table in sorted(targets):
        seller_col, _ = seller_cols[table]
        cols = await _columns(db, table)
        scope = next((c for c in ("product_key", "content_key", "sku_key") if c in cols), None)
        rep: Dict[str, Any] = {
            "verifier_bucket": targets[table],
            "seller_col": seller_col,
            "scope_col": scope,
            "residue": glob[targets[table]][table],
            "columns": {c: d["data_type"] for c, d in cols.items()},
        }
        if scope:
            rep["scope_nulls"] = await _scope_nulls(db, table, seller_col, scope, banned)
            rep["scope_key_space"] = await _scope_match_rate(
                db, table, seller_col, scope, banned)
            if scope == "product_key":
                rep["by_product"] = await _by_product_key(db, table, seller_col, banned)
            elif scope == "content_key":
                rep["by_content"] = await _by_content_key(db, table, seller_col, banned)
            else:
                rep["by_sku"] = await _by_sku_key(db, table, seller_col, banned)
        if table == "pdp_identity_listing":
            rep["by_source_id"] = await _listing_by_source_id(db, banned)
        elif table == "product_enrichment":
            rep["by_source_id"] = await _enrichment_by_source_id(db, banned)
        rep["sample"] = await _sample(db, table, seller_col, cols, banned, sample)
        out["tables"][table] = rep
    return _jsonable(out)


def _parse(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--also-history", default="pdp_identity_listing,product_enrichment",
                    help="comma-separated history-bucket tables to classify as well")
    ap.add_argument("--sample", type=int, default=5)
    args = ap.parse_args(argv)
    # A negative LIMIT dies inside asyncpg mid-run; 0 silently reports an empty
    # sample for every table, which reads like "no rows" rather than "you asked
    # for none". Both abort here instead.
    if args.sample < 1:
        ap.error("--sample must be >= 1")
    return args


async def _amain(args: argparse.Namespace) -> int:
    from db.database import database

    also = [t.strip() for t in args.also_history.split(",") if t.strip()]
    await database.connect()
    try:
        out = await recon(database, also_history=also, sample=args.sample)
    finally:
        await database.disconnect()
    print(json.dumps(out, indent=1, default=str))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(_amain(_parse(argv)))


if __name__ == "__main__":
    raise SystemExit(main())

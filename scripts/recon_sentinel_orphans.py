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
    source_product_id (Path B) or the seed's external_product_id (Path C)."""
    return await _fetch(db, """
        WITH res AS (
          SELECT l.product_id, l.identity_status, l.live_read_enabled,
                 (SELECT count(*) FROM catalog_products cp
                   WHERE cp.source_product_id = l.product_id)                  AS n_products,
                 (SELECT array_agg(DISTINCT cp.merchant_id ORDER BY cp.merchant_id)
                    FROM catalog_products cp
                   WHERE cp.source_product_id = l.product_id)                  AS merchants,
                 (SELECT count(*) FROM catalog_products cp
                    JOIN a9_4_backfill_checkpoint ck
                      ON ck.phase = :phase AND ck.ref_id = cp.product_key
                   WHERE cp.source_product_id = l.product_id)                  AS n_checkpointed,
                 (SELECT count(*) FROM external_product_seeds e
                   WHERE e.external_product_id = l.product_id)                 AS n_seeds,
                 (SELECT count(*) FROM external_product_seeds e
                    JOIN catalog_products cp ON cp.product_key = e.attached_product_key
                   WHERE e.external_product_id = l.product_id
                     AND e.status = 'active')                                  AS n_seeds_attached_live,
                 (SELECT count(*) FROM pdp_identity_override o
                   WHERE o.source_listing_ref = l.source_listing_ref)          AS n_overrides,
                 (SELECT count(*) FROM pdp_identity_review_queue q
                   WHERE q.source_listing_ref = l.source_listing_ref)          AS n_queue
            FROM pdp_identity_listing l
           WHERE l.merchant_id = :banned)
        SELECT identity_status, live_read_enabled, n_products, merchants,
               n_checkpointed, n_seeds, n_seeds_attached_live,
               (n_overrides > 0) AS has_overrides, (n_queue > 0) AS has_queue,
               count(*) AS n
          FROM res
         GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
         ORDER BY n DESC
    """, {"phase": CHECKPOINT_PHASE, "banned": banned})


async def _enrichment_by_source_id(db, banned: str) -> List[Dict[str, Any]]:
    return await _fetch(db, """
        WITH res AS (
          SELECT pe.platform, pe.platform_product_id, pe.geo_code,
                 (SELECT count(*) FROM catalog_products cp
                   WHERE cp.platform = pe.platform
                     AND cp.source_product_id = pe.platform_product_id)        AS n_products_same_platform,
                 (SELECT array_agg(DISTINCT cp.merchant_id ORDER BY cp.merchant_id)
                    FROM catalog_products cp
                   WHERE cp.source_product_id = pe.platform_product_id)        AS merchants_any_platform,
                 (SELECT count(*) FROM external_product_seeds e
                   WHERE e.external_product_id = pe.platform_product_id)       AS n_seeds,
                 (pe.bullet_points IS NOT NULL
                  OR (pe.description_markdown IS NOT NULL
                      AND btrim(pe.description_markdown) <> ''))              AS has_content
            FROM product_enrichment pe
           WHERE pe.merchant_id = :banned)
        SELECT platform, n_products_same_platform, merchants_any_platform,
               n_seeds, has_content, count(*) AS n
          FROM res
         GROUP BY 1, 2, 3, 4, 5
         ORDER BY n DESC
    """, {"banned": banned})


async def _sample(db, table: str, seller_col: str, cols: Dict[str, Any],
                  banned: str, limit: int) -> List[Dict[str, Any]]:
    t, sc = _ident(table), _ident(seller_col)
    pick = [c for c in KEY_COLS + TIME_COLS if c in cols]
    if not pick:
        pick = list(cols)[:6]
    sel = ", ".join(_ident(c) for c in pick)
    order = next((c for c in TIME_COLS if c in cols), None)
    order_sql = f"ORDER BY {_ident(order)} DESC NULLS LAST" if order else ""
    return await _fetch(db, f"SELECT {sel} FROM {t} WHERE {sc} = :banned {order_sql} LIMIT :lim",
                        {"banned": banned, "lim": limit})


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
    glob = await global_residue(db)
    seller_cols = {d["table_name"]: (d["column_name"], bool(d["product_scoped"]))
                   for d in await _fetch(db, SELLER_COLUMNS_SQL)}

    targets: Dict[str, str] = {}
    for table, n in glob["ownership"].items():
        if n and table != "catalog_products":
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
    return ap.parse_args(argv)


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

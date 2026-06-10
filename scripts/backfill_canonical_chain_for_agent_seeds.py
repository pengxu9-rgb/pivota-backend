#!/usr/bin/env python3
"""Phase 7a backfill — for catalog_products rows authored by
catalog_enrichment_agent_v1 that landed BEFORE Phase 7a, materialize
the missing catalog_skus + catalog_offers + catalog_merchants rows so
the canonical recall chain is complete.

The existing 15 lipstick PDPs (ingested 2026-05-06) were written via
the Phase 4 ingestion path which only created catalog_products +
external_product_seeds. They have no catalog_skus, so any canonical
recall path that JOINs through catalog_skus → catalog_offers cannot
reach them.

This script:
  1. Finds catalog_products rows with category_label_source = 'enrichment_agent_v1'.
  2. For each PDP, finds its existing external_product_seeds rows
     (attached_product_key matches).
  3. Reconstructs the validated_offers payload from each seed and feeds
     it through the existing builder functions to produce SKU + merchants
     + offers row dicts.
  4. UPSERTs them in FK order. Idempotent — safe to re-run.

Usage:
  python scripts/backfill_canonical_chain_for_agent_seeds.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from services.catalog_enrichment_agent.ingestion import (  # noqa: E402
    AGENT_VERSION,
    _build_merchant_upserts,
    _build_offer_inserts,
    _build_sku_insert,
    derive_sku_key,
)

logger = logging.getLogger("backfill_canonical_chain_for_agent_seeds")


# Use raw asyncpg with named-param helpers — the databases lib has been
# flaky over the Railway proxy in this session and we want a tight loop
# without pool overhead for a one-shot backfill.
class _Conn:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    @staticmethod
    def _bind(sql: str, params: Dict[str, Any]):
        # Convert :name placeholders to $N and gather positional args.
        ordered_keys: List[str] = []
        seen: Dict[str, int] = {}

        def _sub(match):
            key = match.group(1)
            if key not in seen:
                seen[key] = len(ordered_keys) + 1
                ordered_keys.append(key)
            return f"${seen[key]}"

        import re
        rendered = re.sub(r":([a-zA-Z_][a-zA-Z0-9_]*)", _sub, sql)
        args = [params.get(k) for k in ordered_keys]
        return rendered, args

    async def fetch_all(self, sql: str, params: Dict[str, Any] = None):
        rendered, args = self._bind(sql, params or {})
        rows = await self.conn.fetch(rendered, *args)
        return [dict(r) for r in rows]

    async def execute(self, sql: str, params: Dict[str, Any] = None):
        rendered, args = self._bind(sql, params or {})
        await self.conn.execute(rendered, *args)


_db: _Conn = None  # type: ignore


async def _fetch_agent_pdps() -> List[Dict[str, Any]]:
    return await _db.fetch_all(
        """
        SELECT product_key, brand, title, category_path, canonical_url, image_url
        FROM catalog_products
        WHERE category_label_source = 'enrichment_agent_v1'
        """,
        {},
    )


async def _fetch_agent_seeds_for(product_key: str) -> List[Dict[str, Any]]:
    return await _db.fetch_all(
        """
        SELECT canonical_url, destination_url, image_url, price_amount,
               domain, seed_data
        FROM external_product_seeds
        WHERE attached_product_key = :pk
          AND tool = :tool
          AND status = 'active'
        """,
        {"pk": product_key, "tool": AGENT_VERSION},
    )


def _seed_to_offer_dict(seed_row: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstruct the validated_offer dict shape from a seed row.

    seed_row.seed_data is JSONB on prod, may come back as either a
    decoded dict or a JSON-encoded string depending on driver version;
    handle both."""
    sd = seed_row.get("seed_data")
    if isinstance(sd, str):
        try:
            sd = json.loads(sd)
        except Exception:
            sd = {}
    if not isinstance(sd, dict):
        sd = {}
    return {
        "merchant_inferred": sd.get("merchant_inferred"),
        "canonical_url": seed_row.get("canonical_url") or "",
        "destination_url": seed_row.get("destination_url") or seed_row.get("canonical_url") or "",
        "image_url": seed_row.get("image_url") or "",
        "price": seed_row.get("price_amount"),
        "in_stock": bool(sd.get("in_stock") or False),
        "validated_at": sd.get("validated_at"),
    }


async def _upsert_merchant(row: Dict[str, Any]) -> None:
    await _db.execute(
        """
        INSERT INTO catalog_merchants
          (merchant_id, merchant_name, primary_platform, status,
           source_system, source_ref, metadata_json)
        VALUES
          (:merchant_id, :merchant_name, :primary_platform, :status,
           :source_system, :source_ref, CAST(:metadata_json AS jsonb))
        ON CONFLICT (merchant_id) DO UPDATE SET
          merchant_name = COALESCE(EXCLUDED.merchant_name, catalog_merchants.merchant_name),
          primary_platform = COALESCE(EXCLUDED.primary_platform, catalog_merchants.primary_platform),
          status = EXCLUDED.status,
          source_ref = COALESCE(EXCLUDED.source_ref, catalog_merchants.source_ref),
          metadata_json = EXCLUDED.metadata_json,
          updated_at = NOW()
        """,
        row,
    )


async def _upsert_sku(row: Dict[str, Any]) -> None:
    await _db.execute(
        """
        INSERT INTO catalog_skus
          (sku_key, product_key, merchant_id, platform,
           source_product_id, source_variant_id, sku, barcode,
           title, currency, image_url,
           visible_attributes, visible_option_labels, ingredient_ids,
           sku_payload, readiness_tier)
        VALUES
          (:sku_key, :product_key, :merchant_id, :platform,
           :source_product_id, :source_variant_id, :sku, :barcode,
           :title, :currency, :image_url,
           CAST(:visible_attributes AS jsonb),
           CAST(:visible_option_labels AS jsonb),
           CAST(:ingredient_ids AS jsonb),
           CAST(:sku_payload AS jsonb), :readiness_tier)
        ON CONFLICT (sku_key) DO UPDATE SET
          title = EXCLUDED.title,
          image_url = EXCLUDED.image_url,
          ingredient_ids = EXCLUDED.ingredient_ids,
          sku_payload = EXCLUDED.sku_payload,
          readiness_tier = EXCLUDED.readiness_tier,
          updated_at = NOW()
        """,
        row,
    )


async def _upsert_offer(row: Dict[str, Any]) -> None:
    await _db.execute(
        """
        INSERT INTO catalog_offers
          (offer_id, sku_key, product_key, merchant_id,
           catalog_track, truth_tier, readiness_tier, offer_mode,
           channel, availability, inventory_quantity, currency,
           list_price, merchant_effective_price, estimated_best_price,
           price_confidence, source_system, source_ref, offer_payload)
        VALUES
          (:offer_id, :sku_key, :product_key, :merchant_id,
           :catalog_track, :truth_tier, :readiness_tier, :offer_mode,
           :channel, :availability, :inventory_quantity, :currency,
           :list_price, :merchant_effective_price, :estimated_best_price,
           :price_confidence, :source_system, :source_ref,
           CAST(:offer_payload AS jsonb))
        ON CONFLICT (offer_id) DO UPDATE SET
          availability = EXCLUDED.availability,
          inventory_quantity = EXCLUDED.inventory_quantity,
          list_price = EXCLUDED.list_price,
          merchant_effective_price = EXCLUDED.merchant_effective_price,
          estimated_best_price = EXCLUDED.estimated_best_price,
          price_confidence = EXCLUDED.price_confidence,
          offer_payload = EXCLUDED.offer_payload,
          updated_at = NOW()
        """,
        row,
    )


async def _run(args: argparse.Namespace) -> int:
    global _db
    import os as _os
    dsn = _os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL not set")
        return 1
    conn = await asyncpg.connect(dsn)
    _db = _Conn(conn)
    try:
        pdps = await _fetch_agent_pdps()
        return await _drive(args, pdps)
    finally:
        await conn.close()


async def _drive(args: argparse.Namespace, pdps: List[Dict[str, Any]]) -> int:
    logger.info("found %d agent-authored PDPs", len(pdps))
    if not pdps:
        return 0

    counts = {"merchants": 0, "skus": 0, "offers": 0, "skipped_no_seeds": 0}

    for pdp in pdps:
        product_key = pdp["product_key"]
        seeds = await _fetch_agent_seeds_for(product_key)
        if not seeds:
            counts["skipped_no_seeds"] += 1
            logger.warning("no seeds for product_key=%s, skipping", product_key)
            continue

        offers = [_seed_to_offer_dict(s) for s in seeds]

        pdp_payload = {
            "brand": pdp.get("brand") or "",
            "product_name": pdp.get("title") or "",
            "category_path": pdp.get("category_path") or "",
        }
        sku_row = _build_sku_insert(
            product_key=product_key,
            pdp_payload=pdp_payload,
            canonical_url=pdp.get("canonical_url"),
            image_url=pdp.get("image_url"),
        )
        merchant_rows = _build_merchant_upserts(offers)
        offer_rows = _build_offer_inserts(
            product_key=product_key,
            sku_key=sku_row["sku_key"],
            offers=offers,
        )

        if args.dry_run:
            logger.info(
                "dry-run pk=%s sku=%s merchants=%d offers=%d",
                product_key,
                sku_row["sku_key"],
                len(merchant_rows),
                len(offer_rows),
            )
            counts["skus"] += 1
            counts["merchants"] += len(merchant_rows)
            counts["offers"] += len(offer_rows)
            continue

        for merchant in merchant_rows:
            try:
                await _upsert_merchant(merchant)
                counts["merchants"] += 1
            except Exception as exc:
                logger.exception("merchant upsert failed: %s", exc)
        try:
            await _upsert_sku(sku_row)
            counts["skus"] += 1
        except Exception as exc:
            logger.exception("sku upsert failed for pk=%s: %s", product_key, exc)
        for offer in offer_rows:
            try:
                await _upsert_offer(offer)
                counts["offers"] += 1
            except Exception as exc:
                logger.exception("offer upsert failed: %s", exc)

    logger.info("Phase 7a backfill complete: %s dry_run=%s", counts, args.dry_run)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print plan; no DB writes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

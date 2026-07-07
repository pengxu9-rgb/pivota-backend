"""Shared DB executor for the catalog-enrichment ingest plan.

`ingestion.ingest_validated_jsonl` produces a PURE plan (row dicts); this module
executes it against the DB in FK order (merchants → products → [skus → offers] →
seeds) with the same upsert SQL the `run_catalog_enrichment.py` CLI used. Extracted
so the CLI and the programmatic runner share ONE code path (no SQL drift — the
playbook's explicit goal). Behaviour-preserving move of the CLI's apply block.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from services.catalog_enrichment_agent.ingestion import AGENT_VERSION
from services.catalog_offer_writer_guard import (
    WriterAuditAccumulator,
    guard_catalog_offer_rows,
    make_batch_id,
    write_writer_audit_log,
)

logger = logging.getLogger("catalog_enrichment_agent.apply")


async def _derive_seed_seller_for_plan_row(seed: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Derive `(seller_ref, seed_kind)` for one enrichment plan seed row (ADR-009
    D3). Brand comes from the seed_data JSON (`_build_seed_inserts` stores it),
    the destination from `domain`/`destination_url`, and the anchor from
    `attached_product_key` (synthetic here → no tenant anchor → CROSS)."""
    import json as _json

    from services.seller_identity import (
        anchor_merchant_from_product_key,
        derive_seed_seller,
    )

    seed_data_raw = seed.get("seed_data")
    brand: Optional[str] = None
    if isinstance(seed_data_raw, str) and seed_data_raw.strip():
        try:
            brand = (_json.loads(seed_data_raw) or {}).get("brand")
        except Exception:  # noqa: BLE001 — brand is best-effort; NULL is honest
            brand = None
    elif isinstance(seed_data_raw, dict):
        brand = seed_data_raw.get("brand")
    return await derive_seed_seller(
        anchor_merchant_id=anchor_merchant_from_product_key(seed.get("attached_product_key")),
        brand=brand,
        destination_domain=seed.get("domain") or seed.get("destination_url"),
        source_system=str(seed.get("tool") or AGENT_VERSION),
    )


async def apply_ingest_plan(
    plan: Dict[str, Any],
    *,
    batch_label: str,
    db: Any = None,
) -> Dict[str, int]:
    """Execute an ingest plan (from `ingest_validated_jsonl`) against the DB in FK
    order. Returns counts. Per-row failures are logged and skipped (never abort the
    batch); the offer write goes through `guard_catalog_offer_rows`."""
    from db.database import database as _global_db

    database = db or _global_db
    if not getattr(database, "is_connected", False):
        await database.connect()

    pdps = plan.get("pdps") or []
    skus = plan.get("skus") or []
    merchants = plan.get("merchants") or []
    offers = plan.get("offers") or []
    seeds = plan.get("seeds") or []
    audit_reasons = plan.get("audit_reasons") or {}

    counts = {"merchants": 0, "pdps": 0, "skus": 0, "offers": 0, "seeds": 0, "offers_skipped": 0}
    audit = WriterAuditAccumulator(
        writer_name=AGENT_VERSION,
        batch_id=make_batch_id(AGENT_VERSION, batch_label),
    )
    audit.record_info(audit_reasons)

    # 1. catalog_merchants — UPSERT by merchant_id (FK target for offers).
    for merchant in merchants:
        try:
            await database.execute(
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
                merchant,
            )
            counts["merchants"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("insert merchant failed for merchant_id=%s — %s", merchant.get("merchant_id"), exc)

    # 2. catalog_products — UPSERT by product_key.
    for pdp in pdps:
        try:
            await database.execute(
                """
                INSERT INTO catalog_products
                  (product_key, merchant_id, platform, source_product_id,
                   pivota_signature_id, pivota_canonical_url, pivota_signature_minted_at,
                   catalog_track, truth_tier, readiness_tier, source_system, source_domain,
                   title, description, brand, product_type, category,
                   category_path, category_confidence, category_label_source,
                   canonical_url, image_url, product_payload, tags,
                   price_tier, use_case_tags, lifestyle_tags, demographic,
                   pdp_lifecycle_stage,
                   pdp_scope, pdp_scope_source, pdp_scope_set_at,
                   content_key)
                VALUES
                  (:product_key, :merchant_id, :platform, :source_product_id,
                   :pivota_signature_id, :pivota_canonical_url, :pivota_signature_minted_at,
                   :catalog_track, :truth_tier, :readiness_tier, :source_system, :source_domain,
                   :title, :description, :brand, :product_type, :category,
                   :category_path, :category_confidence, :category_label_source,
                   :canonical_url, :image_url, CAST(:product_payload AS jsonb),
                   CAST(:tags AS jsonb),
                   :price_tier,
                   CAST(:use_case_tags AS jsonb),
                   CAST(:lifestyle_tags AS jsonb),
                   :demographic,
                   :pdp_lifecycle_stage,
                   :pdp_scope, :pdp_scope_source, NOW(),
                   :content_key)
                ON CONFLICT (product_key) DO UPDATE SET
                  pivota_signature_id = COALESCE(catalog_products.pivota_signature_id, EXCLUDED.pivota_signature_id),
                  pivota_canonical_url = COALESCE(catalog_products.pivota_canonical_url, EXCLUDED.pivota_canonical_url),
                  pivota_signature_minted_at = COALESCE(catalog_products.pivota_signature_minted_at, EXCLUDED.pivota_signature_minted_at),
                  category_path = EXCLUDED.category_path,
                  source_domain = EXCLUDED.source_domain,
                  category_confidence = EXCLUDED.category_confidence,
                  category_label_source = EXCLUDED.category_label_source,
                  canonical_url = EXCLUDED.canonical_url,
                  image_url = EXCLUDED.image_url,
                  product_payload = EXCLUDED.product_payload,
                  tags = EXCLUDED.tags,
                  price_tier = EXCLUDED.price_tier,
                  use_case_tags = EXCLUDED.use_case_tags,
                  lifestyle_tags = EXCLUDED.lifestyle_tags,
                  demographic = EXCLUDED.demographic,
                  pdp_lifecycle_stage = EXCLUDED.pdp_lifecycle_stage,
                  pdp_scope = EXCLUDED.pdp_scope,
                  pdp_scope_source = EXCLUDED.pdp_scope_source,
                  pdp_scope_set_at = NOW(),
                  content_key = COALESCE(EXCLUDED.content_key, catalog_products.content_key),
                  updated_at = NOW()
                """,
                pdp,
            )
            counts["pdps"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("insert pdp failed for product_key=%s — %s", pdp.get("product_key"), exc)

        # ADR-009 ratified decision 1 (no-fallback): stamp the deterministic
        # SINGLETON product_group_id so this enriched product carries a pg (offer
        # path keys on pg with zero branching). ON CONFLICT DO NOTHING — never
        # overwrites a real/curated group (no auto-merge). content_key NULL →
        # pg-NULL + observable log.
        try:
            from services.product_group_autogrouper import (
                ensure_singleton_group_membership,
            )

            await ensure_singleton_group_membership(
                merchant_id=str(pdp.get("merchant_id") or ""),
                platform=str(pdp.get("platform") or ""),
                source_product_id=str(pdp.get("source_product_id") or ""),
                content_key=pdp.get("content_key"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("singleton pg mint failed for product_key=%s — %s",
                           pdp.get("product_key"), str(exc)[:200])

    async with database.transaction():
        # 3. catalog_skus — INSERT one synthetic 'canonical' SKU per PDP.
        for sku in skus:
            try:
                await database.execute(
                    """
                    INSERT INTO catalog_skus
                      (sku_key, product_key, merchant_id, platform,
                       source_product_id, source_variant_id, source_domain, sku, barcode,
                       title, currency, image_url,
                       visible_attributes, visible_option_labels, ingredient_ids,
                       sku_payload, readiness_tier)
                    VALUES
                      (:sku_key, :product_key, :merchant_id, :platform,
                       :source_product_id, :source_variant_id, :source_domain, :sku, :barcode,
                       :title, :currency, :image_url,
                       CAST(:visible_attributes AS jsonb),
                       CAST(:visible_option_labels AS jsonb),
                       CAST(:ingredient_ids AS jsonb),
                       CAST(:sku_payload AS jsonb), :readiness_tier)
                    ON CONFLICT (sku_key) DO UPDATE SET
                      source_domain = EXCLUDED.source_domain,
                      barcode = EXCLUDED.barcode,
                      title = EXCLUDED.title,
                      image_url = EXCLUDED.image_url,
                      ingredient_ids = EXCLUDED.ingredient_ids,
                      sku_payload = EXCLUDED.sku_payload,
                      readiness_tier = EXCLUDED.readiness_tier,
                      updated_at = NOW()
                    """,
                    sku,
                )
                counts["skus"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("insert sku failed for sku_key=%s — %s", sku.get("sku_key"), exc)

        accepted_offers, skip_reasons, _rejected_offers = await guard_catalog_offer_rows(offers)
        if skip_reasons:
            audit.record_skips(skip_reasons)
            counts["offers_skipped"] = sum(skip_reasons.values())

        # 4. catalog_offers — INSERT one row per validated retailer offer.
        for offer in accepted_offers:
            try:
                await database.execute(
                    """
                    INSERT INTO catalog_offers
                      (offer_id, sku_key, product_key, merchant_id,
                       catalog_track, truth_tier, readiness_tier, offer_mode,
                       channel, availability, inventory_quantity, currency,
                       list_price, merchant_effective_price, estimated_best_price,
                       price_confidence, source_system, source_ref, source_domain, offer_payload)
                    VALUES
                      (:offer_id, :sku_key, :product_key, :merchant_id,
                       :catalog_track, :truth_tier, :readiness_tier, :offer_mode,
                       :channel, :availability, :inventory_quantity, :currency,
                       :list_price, :merchant_effective_price, :estimated_best_price,
                       :price_confidence, :source_system, :source_ref, :source_domain,
                       CAST(:offer_payload AS jsonb))
                    ON CONFLICT (offer_id) DO UPDATE SET
                      availability = EXCLUDED.availability,
                      inventory_quantity = EXCLUDED.inventory_quantity,
                      list_price = EXCLUDED.list_price,
                      merchant_effective_price = EXCLUDED.merchant_effective_price,
                      estimated_best_price = EXCLUDED.estimated_best_price,
                      price_confidence = EXCLUDED.price_confidence,
                      source_domain = EXCLUDED.source_domain,
                      offer_payload = EXCLUDED.offer_payload,
                      updated_at = NOW()
                    """,
                    offer,
                )
                counts["offers"] += 1
                audit.record_applied(1)
            except Exception as exc:  # noqa: BLE001
                logger.exception("insert offer failed for offer_id=%s — %s", offer.get("offer_id"), exc)

    # 5. external_product_seeds — audit + legacy compatibility.
    for seed in seeds:
        try:
            # ADR-009 D3 (docs/adr/ADR-009-seller-of-record-identity.md; IDENTITY
            # _REFERENCE §4): derive the seller-of-record at write time. Enrichment
            # offers are external retailer offers whose `attached_product_key` is a
            # synthetic `pk_<hash>` (no tenant anchor) → these resolve CROSS to an
            # observed seller. NULL only when unmintable (derive logs loudly) —
            # never assumed 'self'.
            seller_ref, seed_kind = await _derive_seed_seller_for_plan_row(seed)
            await database.execute(
                """
                INSERT INTO external_product_seeds
                  (id, external_product_id, market, tool, title, image_url,
                   price_amount, price_currency, destination_url,
                   canonical_url, domain, attached_product_key, status,
                   availability, seed_data, seller_ref, seed_kind)
                VALUES
                  (:id, :external_product_id, :market, :tool, :title, :image_url,
                   :price_amount, :price_currency, :destination_url,
                   :canonical_url, :domain, :attached_product_key, :status,
                   :availability, CAST(:seed_data AS jsonb), :seller_ref, :seed_kind)
                ON CONFLICT (id) DO UPDATE SET
                  external_product_id = EXCLUDED.external_product_id,
                  attached_product_key = EXCLUDED.attached_product_key,
                  destination_url = EXCLUDED.destination_url,
                  canonical_url = EXCLUDED.canonical_url,
                  image_url = EXCLUDED.image_url,
                  price_amount = EXCLUDED.price_amount,
                  status = EXCLUDED.status,
                  availability = EXCLUDED.availability,
                  seed_data = EXCLUDED.seed_data,
                  -- Fresh derivation wins; a NULL re-derivation (unresolvable —
                  -- already logged loudly) never degrades an existing seller.
                  seller_ref = COALESCE(EXCLUDED.seller_ref, external_product_seeds.seller_ref),
                  seed_kind = COALESCE(EXCLUDED.seed_kind, external_product_seeds.seed_kind),
                  updated_at = NOW()
                """,
                {**seed, "seller_ref": seller_ref, "seed_kind": seed_kind},
            )
            counts["seeds"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("insert seed failed for id=%s — %s", seed.get("id"), exc)

    await write_writer_audit_log(audit)
    logger.info("apply_ingest_plan applied: %s", counts)
    return counts

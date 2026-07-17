"""Shared DB executor for the catalog-enrichment ingest plan.

`ingestion.ingest_validated_jsonl` produces a PURE plan (row dicts); this module
executes it against the DB in FK order (merchants → products → [skus → offers] →
seeds) with the same upsert SQL the `run_catalog_enrichment.py` CLI used. Extracted
so the CLI and the programmatic runner share ONE code path (no SQL drift — the
playbook's explicit goal). Behaviour-preserving move of the CLI's apply block.

Two executors share one set of SQL constants:
  - `apply_ingest_plan(..., batch=False)` — the original per-row path, unchanged
    for every existing caller (default byte-for-byte identical behaviour);
  - `apply_ingest_plan(..., batch=True)` — a round-trip-eliminating path that
    upserts each stage in chunked multi-row VALUES statements (services.
    catalog_enrichment_agent.bulk_writer). Same SQL, same guard, same audit; a
    failed chunk replays row-by-row so the per-row skip contract is preserved.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from services.catalog_enrichment_agent.bulk_writer import bulk_upsert
from services.catalog_enrichment_agent.ingestion import AGENT_VERSION
from services.catalog_offer_writer_guard import (
    WriterAuditAccumulator,
    guard_catalog_offer_rows,
    make_batch_id,
    write_writer_audit_log,
)

logger = logging.getLogger("catalog_enrichment_agent.apply")


# --- Stage upsert SQL (ONE source of truth for both the per-row and batched paths).
#     Each is a single-row `INSERT ... VALUES (...) ON CONFLICT <natural key> ...`
#     upsert, so a partially-applied batch + per-row replay can never duplicate a
#     row (bulk_writer idempotency precondition).

_MERCHANT_UPSERT_SQL = """
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
                """

_PDP_UPSERT_SQL = """
                INSERT INTO catalog_products
                  (product_key, merchant_id, platform, source_product_id,
                   pivota_signature_id, pivota_canonical_url, pivota_signature_minted_at,
                   catalog_track, truth_tier, readiness_tier, source_system, source_domain,
                   title, description, brand, product_type, category,
                   category_path, category_kind, category_confidence, category_label_source,
                   canonical_url, image_url, product_payload, tags,
                   price_tier, use_case_tags, lifestyle_tags, demographic,
                   pdp_lifecycle_stage,
                   pdp_scope, pdp_scope_source, pdp_scope_set_at,
                   content_key, gtin)
                VALUES
                  (:product_key, :merchant_id, :platform, :source_product_id,
                   :pivota_signature_id, :pivota_canonical_url, :pivota_signature_minted_at,
                   :catalog_track, :truth_tier, :readiness_tier, :source_system, :source_domain,
                   :title, :description, :brand, :product_type, :category,
                   :category_path, :category_kind, :category_confidence, :category_label_source,
                   :canonical_url, :image_url, CAST(:product_payload AS jsonb),
                   CAST(:tags AS jsonb),
                   :price_tier,
                   CAST(:use_case_tags AS jsonb),
                   CAST(:lifestyle_tags AS jsonb),
                   :demographic,
                   :pdp_lifecycle_stage,
                   :pdp_scope, :pdp_scope_source, NOW(),
                   :content_key, :gtin)
                ON CONFLICT (product_key) DO UPDATE SET
                  pivota_signature_id = COALESCE(catalog_products.pivota_signature_id, EXCLUDED.pivota_signature_id),
                  pivota_canonical_url = COALESCE(catalog_products.pivota_canonical_url, EXCLUDED.pivota_canonical_url),
                  pivota_signature_minted_at = COALESCE(catalog_products.pivota_signature_minted_at, EXCLUDED.pivota_signature_minted_at),
                  category_path = EXCLUDED.category_path,
                  category_kind = COALESCE(EXCLUDED.category_kind, catalog_products.category_kind),
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
                  gtin = COALESCE(EXCLUDED.gtin, catalog_products.gtin),
                  updated_at = NOW()
                """

_SKU_UPSERT_SQL = """
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
                    """

_OFFER_UPSERT_SQL = """
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
                    """

_SEED_UPSERT_SQL = """
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
                """


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


async def _apply_pdp_identity_gate(pdp: Dict[str, Any], *, identity_gate_on: bool) -> bool:
    """Shared pre-insert step for a PDP row (both executors). Mutates `pdp` in
    place: canonicalizes the source barcode into the `gtin` match-attribute column
    (ADR-011 — never folded into content_key) and, when the identity gate is on,
    resolves-or-attaches the content identity. Returns True when the PDP should be
    inserted, False when the identity gate SKIPs it (brand conflict — review
    enqueued)."""
    from services.intake_identity import (
        ACTION_SKIP,
        DOOR_CATALOG_ENRICHMENT,
        canonical_gtin,
        resolve_or_attach_content_identity,
    )

    pdp["gtin"] = canonical_gtin(pdp.get("gtin") or pdp.get("barcode"))
    if not identity_gate_on:
        return True
    # ADR-011 resolve-or-attach (flag-gated). This door was previously UNGUARDED —
    # R1 makes the primitive its required pre-insert step, which also extends the
    # ADR-008/P1.4 brand guard to this door (observed retailer data → a brand
    # conflict SKIPs the mint).
    ident = await resolve_or_attach_content_identity(
        brand=pdp.get("brand"),
        title=pdp.get("title"),
        gtin=pdp.get("gtin"),
        canonical_url=pdp.get("canonical_url"),
        source_product_id=pdp.get("source_product_id"),
        door=DOOR_CATALOG_ENRICHMENT,
        merchant_ctx={
            "merchant_id": pdp.get("merchant_id"),
            "platform": pdp.get("platform"),
            "source_domain": pdp.get("source_domain"),
            "product_key": pdp.get("product_key"),
        },
    )
    if ident.get("action") == ACTION_SKIP:
        logger.info(
            "apply_ingest_plan: identity gate skipped product_key=%s "
            "(brand conflict — review enqueued)", pdp.get("product_key"),
        )
        return False
    pdp["content_key"] = ident.get("content_key") or pdp.get("content_key")
    return True


async def _ensure_singleton_pg(pdp: Dict[str, Any]) -> None:
    """ADR-009 decision 1 (no-fallback): stamp the deterministic SINGLETON
    product_group_id so this enriched product carries a pg (offer path keys on pg
    with zero branching). ON CONFLICT DO NOTHING — never overwrites a real/curated
    group. content_key NULL → pg-NULL + observable log."""
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


def _filter_children_of_skipped(
    skipped_product_keys: set,
    *,
    skus: list,
    offers: list,
    seeds: list,
) -> tuple[list, list, list]:
    """A PDP the identity gate skipped OR whose insert failed must not leave orphan
    child rows (catalog_offers has NO foreign keys — an offer for a missing product
    is a fake offer). Drop its dependent skus/offers/seeds before those stages."""
    if not skipped_product_keys:
        return skus, offers, seeds
    skus = [s for s in skus if s.get("product_key") not in skipped_product_keys]
    offers = [o for o in offers if o.get("product_key") not in skipped_product_keys]
    seeds = [
        s for s in seeds
        if s.get("attached_product_key") not in skipped_product_keys
    ]
    return skus, offers, seeds


async def apply_ingest_plan(
    plan: Dict[str, Any],
    *,
    batch_label: str,
    db: Any = None,
    batch: bool = False,
) -> Dict[str, int]:
    """Execute an ingest plan (from `ingest_validated_jsonl`) against the DB in FK
    order. Returns counts. Per-row failures are logged and skipped (never abort the
    batch); the offer write goes through `guard_catalog_offer_rows`.

    `batch=True` selects the round-trip-eliminating executor (identical SQL, guard,
    and audit; chunked multi-row upserts with per-row fallback). Default is False —
    byte-for-byte the legacy per-row path for every existing caller."""
    from db.database import database as _global_db

    database = db or _global_db
    if not getattr(database, "is_connected", False):
        await database.connect()

    if batch:
        return await _apply_ingest_plan_batched(plan, batch_label=batch_label, database=database)

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
            await database.execute(_MERCHANT_UPSERT_SQL, merchant)
            counts["merchants"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("insert merchant failed for merchant_id=%s — %s", merchant.get("merchant_id"), exc)

    from services.intake_identity import (
        DOOR_CATALOG_ENRICHMENT,
        intake_identity_enabled,
    )

    identity_gate_on = intake_identity_enabled(DOOR_CATALOG_ENRICHMENT)
    counts["pdps_skipped_identity"] = 0
    skipped_product_keys: set = set()

    # 2. catalog_products — UPSERT by product_key.
    for pdp in pdps:
        if not await _apply_pdp_identity_gate(pdp, identity_gate_on=identity_gate_on):
            counts["pdps_skipped_identity"] += 1
            skipped_product_keys.add(pdp.get("product_key"))
            continue
        try:
            await database.execute(_PDP_UPSERT_SQL, pdp)
            counts["pdps"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("insert pdp failed for product_key=%s — %s", pdp.get("product_key"), exc)

        await _ensure_singleton_pg(pdp)

    skus, offers, seeds = _filter_children_of_skipped(
        skipped_product_keys, skus=skus, offers=offers, seeds=seeds
    )

    async with database.transaction():
        # 3. catalog_skus — INSERT one synthetic 'canonical' SKU per PDP.
        for sku in skus:
            try:
                await database.execute(_SKU_UPSERT_SQL, sku)
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
                await database.execute(_OFFER_UPSERT_SQL, offer)
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
                _SEED_UPSERT_SQL,
                {**seed, "seller_ref": seller_ref, "seed_kind": seed_kind},
            )
            counts["seeds"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("insert seed failed for id=%s — %s", seed.get("id"), exc)

    await write_writer_audit_log(audit)
    logger.info("apply_ingest_plan applied: %s", counts)
    return counts


async def _apply_ingest_plan_batched(
    plan: Dict[str, Any],
    *,
    batch_label: str,
    database: Any,
) -> Dict[str, int]:
    """Round-trip-eliminating executor. Same SQL constants, same offer guard, same
    WriterAuditAccumulator as the per-row path; each stage upserts in chunked
    multi-row VALUES statements with a per-row replay fallback (bulk_writer).

    Unlike the per-row path this does NOT wrap skus+offers in one explicit
    transaction: each multi-row chunk is atomic on its own, and dropping the outer
    transaction is what lets a failed chunk replay row-by-row without a poisoned
    (aborted) Postgres transaction. Every statement is an idempotent ON CONFLICT
    upsert, so a partial commit + replay heals on re-run rather than duplicating."""
    pdps = plan.get("pdps") or []
    skus = plan.get("skus") or []
    merchants = plan.get("merchants") or []
    offers = plan.get("offers") or []
    seeds = plan.get("seeds") or []
    audit_reasons = plan.get("audit_reasons") or {}

    counts: Dict[str, int] = {
        "merchants": 0, "pdps": 0, "skus": 0, "offers": 0, "seeds": 0,
        "offers_skipped": 0, "pdps_skipped_identity": 0, "pdps_skipped_insert": 0,
        "products_fully_skipped": 0, "seeds_skipped_derivation": 0,
    }
    audit = WriterAuditAccumulator(
        writer_name=AGENT_VERSION,
        batch_id=make_batch_id(AGENT_VERSION, batch_label),
    )
    audit.record_info(audit_reasons)

    # 1. catalog_merchants.
    counts["merchants"], _, _ = await bulk_upsert(
        database, _MERCHANT_UPSERT_SQL, merchants, label="merchants"
    )

    from services.intake_identity import (
        DOOR_CATALOG_ENRICHMENT,
        intake_identity_enabled,
    )

    identity_gate_on = intake_identity_enabled(DOOR_CATALOG_ENRICHMENT)
    skipped_product_keys: set = set()

    # 2. catalog_products — run the per-row identity gate first (it may SKIP rows
    #    and may itself round-trip when enabled), then bulk-upsert the survivors.
    insertable_pdps = []
    for pdp in pdps:
        if not await _apply_pdp_identity_gate(pdp, identity_gate_on=identity_gate_on):
            counts["pdps_skipped_identity"] += 1
            skipped_product_keys.add(pdp.get("product_key"))
            continue
        insertable_pdps.append(pdp)

    counts["pdps"], counts["pdps_skipped_insert"], pdp_skipped_rows = await bulk_upsert(
        database, _PDP_UPSERT_SQL, insertable_pdps, label="pdps"
    )
    # Orphan prevention: a PDP whose insert failed joins the skip set so its
    # children are excluded from every later stage (no fake offers/skus/seeds).
    failed_pdp_keys = {r.get("product_key") for r in pdp_skipped_rows}
    skipped_product_keys |= failed_pdp_keys
    inserted_pdps = [p for p in insertable_pdps if p.get("product_key") not in failed_pdp_keys]

    # Singleton product_group memberships — only for PDPs that actually landed and
    # carry a content_key (no side effects for skipped rows). ON CONFLICT DO NOTHING.
    from services.product_group_autogrouper import (
        _UPSERT_SINGLETON_MEMBER_SQL,
        make_singleton_product_group_id,
    )

    singleton_rows = [
        {
            "product_group_id": make_singleton_product_group_id(str(p.get("content_key")).strip()),
            "merchant_id": str(p.get("merchant_id") or ""),
            "platform": str(p.get("platform") or ""),
            "platform_product_id": str(p.get("source_product_id") or ""),
        }
        for p in inserted_pdps if (p.get("content_key") or "").strip()
    ]
    try:
        await bulk_upsert(database, _UPSERT_SINGLETON_MEMBER_SQL, singleton_rows, label="pg_singleton")
    except Exception as exc:  # noqa: BLE001 — best-effort, mirrors per-row path
        logger.warning("batched singleton pg mint failed (best-effort): %s", str(exc)[:200])

    if skipped_product_keys:
        logger.info(
            "apply_ingest_plan(batch): %d product(s) fully skipped (children excluded): %s",
            len(skipped_product_keys), sorted(k for k in skipped_product_keys if k),
        )
    counts["products_fully_skipped"] = len(skipped_product_keys)

    skus, offers, seeds = _filter_children_of_skipped(
        skipped_product_keys, skus=skus, offers=offers, seeds=seeds
    )

    # 3. catalog_skus.
    counts["skus"], _, _ = await bulk_upsert(database, _SKU_UPSERT_SQL, skus, label="skus")

    # 4. catalog_offers — SAME guard + audit as the per-row path.
    accepted_offers, skip_reasons, _rejected_offers = await guard_catalog_offer_rows(offers)
    if skip_reasons:
        audit.record_skips(skip_reasons)
        counts["offers_skipped"] = sum(skip_reasons.values())
    applied_offers, offers_insert_skipped, _osr = await bulk_upsert(
        database, _OFFER_UPSERT_SQL, accepted_offers, label="offers"
    )
    counts["offers"] = applied_offers
    counts["offers_skipped_insert"] = offers_insert_skipped
    audit.record_applied(applied_offers)

    # 5. external_product_seeds — per-row seller derivation (ADR-009 D3), then bulk.
    seed_rows = []
    for seed in seeds:
        try:
            seller_ref, seed_kind = await _derive_seed_seller_for_plan_row(seed)
        except Exception as exc:  # noqa: BLE001 — same skip semantics as the per-row
            # path: one seed's derivation failure must not abort the apply AFTER
            # pdps/skus/offers are committed, nor lose the writer-audit row below.
            logger.exception(
                "seed seller derivation failed for id=%s — seed skipped: %s",
                seed.get("id"), exc,
            )
            counts["seeds_skipped_derivation"] += 1
            continue
        seed_rows.append({**seed, "seller_ref": seller_ref, "seed_kind": seed_kind})
    counts["seeds"], _, _ = await bulk_upsert(database, _SEED_UPSERT_SQL, seed_rows, label="seeds")

    await write_writer_audit_log(audit)
    logger.info("apply_ingest_plan(batch) applied: %s", counts)
    return counts

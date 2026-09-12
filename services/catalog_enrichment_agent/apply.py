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
from services.catalog_enrichment_agent.ingestion import AGENT_VERSION, derive_offer_id
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
                   content_key, gtin, rating_value, rating_count)
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
                   :content_key, :gtin, :rating_value, :rating_count)
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
                  -- Review signal: a fresh value wins, but a NULL re-derivation
                  -- (page dropped its aggregateRating) never blanks a captured one.
                  rating_value = COALESCE(EXCLUDED.rating_value, catalog_products.rating_value),
                  rating_count = COALESCE(EXCLUDED.rating_count, catalog_products.rating_count),
                  updated_at = NOW()
                """

# THE ARBITER IS THE IDENTITY INDEX, NOT THE PK. catalog_skus has TWO unique
# constraints — the PK `sku_key` and `idx_catalog_skus_source_identity_v2
# (merchant_id, platform, product_key, source_variant_id)` (migration 123) — and
# Postgres infers ONE; it does not fall through to the other. This upsert named
# the PK, so a re-ingest of a product whose variant already exists under
# `services/catalog_variant_promoter`'s spelling of the SAME identity
# (`<pk>::v::<vid>` vs this lane's `<pk>::v:<vid>`) inserted a rival row, the
# identity index raised 23505, and the row was logged-and-skipped: the SKU was
# never refreshed and its offers pointed at a key that does not exist. 4,286
# promoter rows were adopted by the 2026-09-08 variant-identity backfill and now
# carry live offers, so this is a live production case, not a hypothetical.
#
# The DO UPDATE deliberately touches NO identity column: not `sku_key`, not
# `product_key`, not `merchant_id`/`platform`/`source_variant_id`. Renaming a
# primary key here would orphan every catalog_offers row keyed on the old one
# (catalog_offers has no FK to catch it). `_adopt_existing_sku_identities` is
# what makes the planned row agree with the row already holding the identity,
# before we ever get here.
#
# Keep prose OUT of the span between `VALUES` and `ON CONFLICT`:
# `bulk_writer.split_upsert_sql` partitions on exactly those two markers, so a
# comment placed there is swallowed into the VALUES tuple and breaks the bulk
# path. `CAST(... AS jsonb)` rather than `::jsonb` for the same family of reason
# — a `::` cast reads as a bind param to the multi-row rewriter.
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
                    ON CONFLICT (merchant_id, platform, product_key, source_variant_id)
                    DO UPDATE SET
                      source_domain = EXCLUDED.source_domain,
                      barcode = EXCLUDED.barcode,
                      title = EXCLUDED.title,
                      image_url = EXCLUDED.image_url,
                      ingredient_ids = EXCLUDED.ingredient_ids,
                      -- MERGE, never replace. The row we land on may be one the
                      -- variant-identity backfill stamped with `variant_id_provenance`
                      -- / `source_system`, or one the promoter stamped with its own
                      -- provenance; `EXCLUDED.sku_payload` would erase both. COALESCE
                      -- because `NULL || jsonb` is NULL and the column is nullable.
                      sku_payload = COALESCE(catalog_skus.sku_payload, CAST('{}' AS jsonb))
                                    || EXCLUDED.sku_payload,
                      -- `readiness_tier` IS INSERT-ONLY HERE, deliberately, and it
                      -- stays that way now that the promoter's DO UPDATE moves the
                      -- tier UPWARD. This lane's plan rows carry 'referral_only',
                      -- the FLOOR of that ladder, and the row we land on may be one
                      -- the promoter or the variant-identity backfill wrote as
                      -- 'commerce_ready'. Adding `readiness_tier = EXCLUDED...`
                      -- would DOWNGRADE a purchasable SKU on every content re-sync;
                      -- adding the promoter's upward-only CASE would be a no-op,
                      -- because a floor value can never advance anything. Dropping
                      -- the column entirely is the honest spelling of both: a tier
                      -- is promoted by the lane that can prove the checkout, never
                      -- by a description refresh.
                      updated_at = NOW()
                    """

#: Resolve planned SKU identities against the rows that already hold them, in ONE
#: round trip. Keyed on `product_key` (`idx_catalog_skus_product_key`) rather than
#: a row-constructor `IN`, because the 4-tuple is matched in Python afterwards and
#: this shape is one the PREPARE gate can plan. Every SKU a plan writes names a
#: product_key, and a product's SKU count is small, so the over-read is bounded.
#:
#: `suppressed_at IS NULL` IS LOAD-BEARING. A suppressed row is one a withdrawal
#: took out of supply; adopting its key would resurrect it under a fresh title and
#: hang live offers off it. Excluding it here means the planned row is never
#: rewritten onto it — and `_adopt_existing_sku_identities` then sees the identity
#: as unheld, which is exactly the state `_SKU_SUPPRESSED_IDENTITY_SQL` below is
#: read to explain (the identity index covers suppressed rows too, so the INSERT
#: could not have landed anyway).
_SKU_IDENTITY_LOOKUP_SQL = """
                SELECT sku_key, merchant_id, platform, product_key, source_variant_id
                FROM catalog_skus
                WHERE product_key = ANY(:product_keys)
                  AND suppressed_at IS NULL
                """

#: The MIRROR of the lookup above, from the key side: which planned `sku_key`s are
#: already held, and under WHICH identity tuple. Needed because the two unique
#: constraints fail in opposite directions — the identity lookup finds the row that
#: holds our tuple, this one finds the row that holds our primary key while
#: describing a different tuple (a legacy `source_variant_id = 'default'`, or a
#: `catalog_skus.merchant_id` that drifted from its product's). That row is the one
#: `_SKU_IDENTITY_HEAL_SQL` repairs. Top-level literal with a `= ANY(:sku_keys)`
#: predicate so the repo PREPARE gate can plan it.
_SKU_KEY_HOLDER_LOOKUP_SQL = """
                SELECT sku_key, merchant_id, platform, product_key, source_variant_id
                FROM catalog_skus
                WHERE sku_key = ANY(:sku_keys)
                  AND suppressed_at IS NULL
                """

#: The rows the two lookups above deliberately cannot see. THIS IS THE GUARD, not a
#: classifier: it is the ONLY thing standing between a planned row and the
#: suppressed row holding its identity. Delete it and the INSERT does not fail —
#: `ON CONFLICT (merchant_id, platform, product_key, source_variant_id) DO UPDATE`
#: lands ON the suppressed row, refreshing a SKU a withdrawal took out of supply:
#: title, payload and `updated_at` rewritten, `suppressed_at` left in place, and the
#: whole thing counted in `skus` as a successful write (measured with this lookup
#: stubbed out: `skus: 1`, title replaced). Nothing downstream reports it.
#: (When the suppressed row holds our KEY rather than our identity the INSERT does
#: fail — a PK collision the identity arbiter cannot absorb — but that is the other
#: half of the case, not the whole of it.) A planned row caught here is counted
#: `skus_skipped_suppressed_identity` and its offers are dropped.
_SKU_SUPPRESSED_IDENTITY_SQL = """
                SELECT sku_key, merchant_id, platform, product_key, source_variant_id
                FROM catalog_skus
                WHERE product_key = ANY(:product_keys)
                  AND suppressed_at IS NOT NULL
                """

#: Heal ONE column of a stored row's identity — its `source_variant_id` — in place,
#: keeping its primary key. This is the drift the heal exists for: a legacy
#: `source_variant_id = 'default'` written before variant ids were captured, on the
#: row INGESTION's own key names. The stored row keeps its `sku_key`, so the live
#: `catalog_offers` rows keyed on it (there is no FK) stay attached to a row that
#: exists — the whole reason this is an UPDATE rather than a re-key.
#:
#: WHAT THIS STATEMENT DELIBERATELY DOES NOT TOUCH: `merchant_id` and `platform`.
#: A CONTENT RE-SYNC IS NOT ENTITLED TO DECIDE THEM, and the plan's copies of them
#: are not the pinned values the previous revision of this comment claimed:
#: `_prepare_seller_of_record` step 1 pins the plan's merchant to the existing
#: `catalog_products` row, and then step 3 OVERRIDES it whenever a VERIFIED
#: `brand_claims` tenant exists for the domain (the live claimed-attach path,
#: flowerbeauty.com) — while `_PDP_UPSERT_SQL` never moves
#: `catalog_products.merchant_id`. So on that path the plan's merchant differs from
#: the product's BY CONSTRUCTION, this heal fired on exactly that disagreement, and
#: an unbounded `SET merchant_id` silently moved the stored SKU to the claimed tenant
#: — splitting it from its own product. `platform` was pinned by nothing at all: a
#: shopify SKU could be re-pointed at external_seed by a re-ingest.
#:
#: `AND product_key = :product_key` IS THE OTHER HALF. `_SKU_KEY_HOLDER_LOOKUP_SQL`
#: finds the holder of our key by `sku_key` ALONE, so the row it returns may sit
#: under a DIFFERENT product; without this predicate the heal stole it.
#: `RETURNING sku_key` is how the caller learns whether a row was actually repaired:
#: `databases` + asyncpg returns no rowcount from `execute()`, so a silent no-match
#: would otherwise be counted as a heal.
_SKU_IDENTITY_HEAL_SQL = """
                UPDATE catalog_skus
                SET source_variant_id = :source_variant_id,
                    updated_at = NOW()
                WHERE sku_key = :sku_key
                  AND product_key = :product_key
                RETURNING sku_key
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
                  -- REFRESHED BECAUSE `seed_data` IS. `seed_data = EXCLUDED.seed_data` below
                  -- rewrites the nested `variants[].currency`, so leaving this column at its
                  -- first-written value splits the row against its own JSON --
                  -- `external_seed_audit.detect_price_currency_mismatch` compares exactly those
                  -- two, `price_currency_mismatch` is a BLOCKER anomaly, and a blocked seed makes
                  -- `_build_external_seed_product` return None. Measured: first ingest with an
                  -- unreadable /meta.json writes USD, the next successful one writes SGD variants,
                  -- and the seed leaves the agent surface -- unrepairable, since no other lane
                  -- writes this column.
                  --
                  -- The cost is the opposite direction: a flaky read can write USD over a proved
                  -- SGD. That is TRANSIENT and self-heals on the next good ingest, where a
                  -- split-brain row is permanent. Preferring the recoverable failure is the whole
                  -- of the trade; a per-row conditional cannot live in this arm anyway, because
                  -- `bulk_writer.split_upsert_sql` suffixes VALUES binds per row while this tail
                  -- is shared.
                  price_currency = EXCLUDED.price_currency,
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


async def _apply_pdp_identity_gate(
    pdp: Dict[str, Any], *, identity_gate_on: bool,
    group_targets: Optional[Dict[str, str]] = None,
) -> bool:
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
            "strict_group_resolution": True,
        },
    )
    # The shared resolver serves legacy doors that may return an error-shaped
    # MINT. This primary writer cannot publish that substituted identity.
    from services.intake_identity import ACTION_ATTACH, ACTION_FLAG, ACTION_MINT
    evidence = ident.get("evidence") if isinstance(ident, dict) else None
    detail = evidence.get("evidence") if isinstance(evidence, dict) else None
    failed = isinstance(detail, dict) and detail.get("reason") == "error"
    action = ident.get("action") if isinstance(ident, dict) else None
    content_key = ident.get("content_key") if isinstance(ident, dict) else None
    if (failed or action not in {ACTION_SKIP, ACTION_ATTACH, ACTION_FLAG, ACTION_MINT}
            or (action != ACTION_SKIP and (not isinstance(content_key, str) or not content_key.strip()))):
        logger.error("apply_ingest_plan: identity resolution incomplete for product_key=%s; refusing PDP and children",
                     pdp.get("product_key"))
        return False
    if action == ACTION_SKIP:
        logger.info(
            "apply_ingest_plan: identity gate skipped product_key=%s "
            "(brand conflict — review enqueued)", pdp.get("product_key"),
        )
        return False
    if str(pdp.get("product_key") or "").startswith("ext:retailer:") and group_targets is not None:
        target = ident.get("product_group_id")
        if not isinstance(target, str) or not target.strip():
            logger.error("primary retailer identity returned no group for product_key=%s", pdp.get("product_key"))
            return False
        group_targets[pdp["product_key"]] = target
    pdp["content_key"] = content_key
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


async def _ensure_primary_retailer_group(
    pdp: Dict[str, Any], *, database: Any, target: Optional[str] = None,
) -> bool:
    """Persist primary retailer group evidence on the writer's own DB, or refuse.

    Existing membership is never overwritten. A selected resolver's group must
    match what was persisted; flag-off callers accept an existing curated group.
    """
    from services.product_group_autogrouper import (
        _UPSERT_SINGLETON_MEMBER_SQL, make_singleton_product_group_id,
    )

    try:
        group_id = target or make_singleton_product_group_id(pdp.get("content_key"))
        params = {
            "product_group_id": group_id,
            "merchant_id": str(pdp.get("merchant_id") or ""),
            "platform": str(pdp.get("platform") or ""),
            "platform_product_id": str(pdp.get("source_product_id") or ""),
        }
        await database.execute(_UPSERT_SINGLETON_MEMBER_SQL, params)
        stored = await database.fetch_one(
            """SELECT product_group_id FROM product_group_members
               WHERE merchant_id=:merchant_id AND platform=:platform
                 AND platform_product_id=:platform_product_id""",
            {k: v for k, v in params.items() if k != "product_group_id"},
        )
        actual = str(dict(stored).get("product_group_id") or "").strip() if stored else ""
        if not actual or (target and actual != target):
            raise ValueError("primary group membership missing or disagrees with resolved identity")
        return True
    except Exception as exc:  # noqa: BLE001 — counted refusal, never a success substitute
        logger.error("primary retailer group persistence failed for product_key=%s: %s",
                     pdp.get("product_key"), str(exc)[:200])
        return False


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


async def _adopt_existing_sku_identities(
    skus: list,
    offers: list,
    *,
    database: Any,
) -> tuple[list, list, Dict[str, int]]:
    """Reconcile every planned catalog_skus row with the rows that ALREADY hold its
    key or its identity, BEFORE the upsert runs. Returns
    `(skus, offers, counts)` — the lists are FILTERED, never the caller's originals.

    catalog_skus is keyed twice: by `sku_key` (PK) and by the identity tuple
    (merchant_id, platform, product_key, source_variant_id) —
    `idx_catalog_skus_source_identity_v2`. Postgres INFERS one arbiter from an
    `ON CONFLICT` clause and never falls through to the other, so whichever
    constraint the statement does not name is a hard 23505. Both directions are
    live: two writers spell the same identity with different keys (this lane's
    `ingestion.derive_variant_sku_key` gives `<pk>::v:<vid>`,
    `services/catalog_variant_promoter._derive_sku_key` gives `<pk>::v::<vid>`),
    and stored rows carry tuples that have since drifted from their product's
    (a legacy `source_variant_id = 'default'`, a `merchant_id` re-resolved by W2).

    FIVE OUTCOMES, in this precedence:

    (c) the plan's key is held by a row with a DIFFERENT tuple **and** the plan's
        identity is held by ANOTHER row. Two real rows, and no move satisfies both
        constraints: adopting the identity holder would leave the key holder still
        claiming a key this product derives, healing the key holder's tuple would
        collide with the identity holder. Counted `skus_identity_conflict`, logged
        with both rows, and REFUSED — the only outcome that does not guess which of
        two real rows the plan means.
    (a) the identity is held by a live row under a DIFFERENT key → adopt that key.
        Adoption, not renaming the stored row, is the only safe direction:
        catalog_offers has no foreign key to catalog_skus, so a `sku_key` rewrite
        would silently orphan the live offers hanging off the 4,286 rows the
        2026-09-08 variant-identity backfill adopted.
    (d) the identity (or the key) is held by a SUPPRESSED row. Writing onto a row a
        withdrawal took out of supply would resurrect it under a fresh title and
        hang live offers off it. THERE ARE TWO GUARDS AND THEY CLOSE DIFFERENT
        DOORS: `AND suppressed_at IS NULL` in `_SKU_IDENTITY_LOOKUP_SQL` keeps a
        suppressed row from ever being ADOPTED at (a), and
        `_SKU_SUPPRESSED_IDENTITY_SQL` is THE GUARD ON THE WRITE — not a
        classifier. Remove it and the planned row goes to the upsert, whose
        `ON CONFLICT (merchant_id, platform, product_key, source_variant_id)
        DO UPDATE` lands ON the suppressed row: a successful write, counted in
        `skus`, that rewrites a withdrawn SKU's title, payload and `updated_at`
        while leaving `suppressed_at` in place. (The claim that "both unique constraints cover suppressed rows so the
        INSERT cannot land" is true only when the suppressed row holds our KEY —
        that one is a PK collision the identity arbiter cannot absorb. When it
        holds our IDENTITY the INSERT lands very happily.) The outcome is
        `skus_skipped_suppressed_identity`, and its offers are dropped.
    (b0) the identity is unheld and the plan's key is held by a live row UNDER THIS
        PRODUCT that agrees on `platform` and `source_variant_id` and differs ONLY
        on `merchant_id` → THE PLANNED **SKU** FOLLOWS THE STORED ONE. The planned
        SKU's merchant is re-pointed at the stored row's, so the upsert resolves
        through the identity index onto that very row and refreshes it. Counted
        `skus_adopted_stored_merchant`. NOTHING ABOUT THE STORED ROW IS MOVED —
        this is a re-point of the PLAN, not of the database.

        This is the same reasoning `_heal_drifted_sku_identity` states and it has to
        reach the WRITE, not just the heal: on the live claimed-attach path
        (flowerbeauty.com) `_prepare_seller_of_record` step 3 rewrites the plan's
        `merchant_id` to the verified `brand_claims` tenant while `_PDP_UPSERT_SQL`
        never moves `catalog_products.merchant_id`, so the plan and the stored SKU
        disagree about the merchant BY CONSTRUCTION, on every nightly run, for ever.
        If that disagreement is "manufactured at apply time, not evidence the stored
        row is wrong" — and it is — then refusing the write is not conservatism, it
        is a permanent refusal: the row 23505s on the PK, is counted
        `skus_identity_conflict`, its offer is dropped, and the SKU's title, image,
        payload and its offer's price and availability stop refreshing FOREVER,
        with only a log line. Before the identity arbiter, `ON CONFLICT (sku_key)
        DO UPDATE` refreshed exactly this row for free and left its merchant alone;
        adopting the stored merchant restores that outcome by the one move that is
        consistent with the heal's own premise.

        THE ADOPTION IS SKU-ONLY, AND THAT BOUND IS THE ADR-009 D2 ONE. The two
        tables do different things here:

          - `catalog_skus`: the plan's merchant is re-pointed, and the write lands
            on a row that ALREADY EXISTS under that merchant. No row is created
            there. This is rule 1 ("existing rows win") one table further down, so
            the tripwire `_prepare_seller_of_record` step 2 arms — it refuses the
            CREATION of a product/sku row under the banned bucket — has nothing to
            fire on.
          - `catalog_offers`: NOT re-pointed. The offer keeps the per-brand seller
            `_prepare_seller_of_record` produced (`merch_obs_…`), because
            `_OFFER_UPSERT_SQL` conflicts on `offer_id` alone and `merchant_id` is
            an INSERT column — so an offer re-pointed at the stored merchant is a
            BRAND-NEW `catalog_offers` row under it. On the sibling case below the
            stored merchant IS the sentinel, so re-pointing minted a fresh
            `external_seed` offer on every mirror refresh: exactly the write
            ADR-009 D2 bans ("the shared `external_seed` bucket is banned";
            `seller_identity.ensure_observed_seller_of_record` raises rather than
            mint it, and `scripts/verify_seller_rekey.orphan_failures` reports a
            product-scoped sentinel row as `orphan_residue:catalog_offers=N` the
            moment `catalog_products` is clean). A counter that grows a bucket a
            backfill is draining is not conservatism, it is a self-sustaining
            sentinel.

        CONSEQUENCE, STATED: a SKU may therefore be filed under one merchant while
        its own offers are filed under another. Checked at round 5 — NO reader
        joins `catalog_offers.merchant_id` to `catalog_skus.merchant_id`, or to
        `catalog_products.merchant_id`. Offer readers join on `product_key` /
        `sku_key` and take the merchant from the offer row alone
        (`pivot_query_service`, `agent_pdp_view_assembler`,
        `payment_offer_evidence_service`, `catalog_invariant_checks`,
        `routes/agent_shop_gateway`, `routes/employee_products`;
        `merchant_catalog_listing_fallback_service` filters on the PRODUCT's
        merchant and never reads the offer's). Two readers scope by the SKU's own
        merchant — `agent_center_bd_report_service` (the representative SKU for a
        BD report) and `routes/audit_runs_routes` (audit targets) — so a canonical
        row parked under the sentinel is invisible to the brand tenant in both;
        that is the pre-existing state of those rows, not something this branch
        changes, and `backfill_seller_of_record` is the lane that re-keys them.
        The one query that reads an offer's merchant against another table is
        `scripts/repair_orphan_shopify_offers` (`products_cache.merchant_id =
        o.merchant_id`), which is scoped to `source_system='shopify_products_sync'`
        and so never sees a Path C offer. Counted `offers_kept_plan_seller_on_adoption`
        so the divergence is visible in the log line rather than inferred.

        The sibling case is a `<pk>::canonical` row
        `scripts/repair_external_seed_offer_mainline.py` wrote under
        `merchant_id='external_seed'` while Path C's plan carries the per-brand
        `merch_obs_…` for the same product and platform: the SKU adopts the
        sentinel row that exists, its offer stays on `merch_obs_…`.
    (b) the identity is unheld and the plan's key is held by a live row UNDER THIS
        PRODUCT whose `source_variant_id` has drifted → re-point that one column,
        in place, keeping the key (`_SKU_IDENTITY_HEAL_SQL`). The case this exists
        for is the legacy `source_variant_id = 'default'`: without it the row 23505s
        on INSERT, is counted-and-skipped FOREVER, and its offers are written onto a
        stale row — precisely what `ON CONFLICT (sku_key) DO UPDATE` used to refresh
        for free.

        THE HEAL DOES NOT MOVE `merchant_id` OR `platform`, and does not reach a row
        under another product_key. The plan's merchant is NOT the pinned truth the
        first cut of this branch assumed: `_prepare_seller_of_record` step 1 pins it
        to the `catalog_products` row, and step 3 then OVERRIDES it for a VERIFIED
        `brand_claims` tenant while the product upsert leaves
        `catalog_products.merchant_id` where it was. On that live claimed-attach
        path the plan's merchant differs from the product's by construction, which
        is exactly the disagreement this branch triggers on — so an unbounded heal
        moved stored SKUs onto the claimed tenant, splitting them from their own
        product, on a run that reported nothing but `skus_identity_healed`. A row
        that disagrees on merchant or platform is therefore LEFT ALONE and refused
        downstream (`skus_identity_conflict`, offers dropped), which is a state a
        human can see.

    THE OFFER ID LANDS ON THE BACKFILL'S OWN. Both writers derive it from the same
    triple: `derive_offer_id(product_key, sku_key, destination)`. Ingestion stores
    that destination in `source_ref` (`ingestion._build_offer_inserts`), and
    `scripts/backfill_variant_identity_skus.py` derives its offer id from
    `(product_key, written_key, destination)` where `written_key` is the ADOPTED
    key. Re-keying to the adopted key with the offer's own `source_ref` therefore
    reproduces the backfill's offer_id exactly, so the offer UPSERTs that row
    instead of standing a second, differently-keyed offer beside it.

    An offer whose SKU was refused is DROPPED (`offers_dropped_for_refused_sku`):
    catalog_offers has no FK, so an offer naming a sku_key we did not write is a
    fake offer, not a pending one.

    Best-effort by construction: a failed lookup logs and adopts nothing, which
    leaves the pre-existing behaviour (the upsert's own conflict handling plus the
    executors' refused-row filter) intact.
    """
    counts = {
        "skus_adopted_existing_identity": 0,
        "skus_adopted_stored_merchant": 0,
        "offers_rekeyed_to_adopted_sku": 0,
        "offers_kept_plan_seller_on_adoption": 0,
        "skus_identity_healed": 0,
        "skus_identity_conflict": 0,
        "skus_skipped_suppressed_identity": 0,
        "skus_deduped_same_identity": 0,
        "skus_deduped_after_adoption": 0,
        "offers_dropped_for_refused_sku": 0,
        "offers_deduped_after_rekey": 0,
    }
    if not skus:
        return skus, offers, counts

    #: planned sku_key -> the key the row will actually be written under. Chained,
    #: because a de-duplicated row's survivor may itself go on to adopt another key.
    remap: Dict[str, str] = {}
    #: planned sku_key -> the merchant_id the SKU will actually be written under,
    #: for the (b0) rows that follow their stored SKU's merchant. READ ONLY TO
    #: COUNT: the offers of such a row deliberately do NOT follow it (see (b0) —
    #: an offer re-pointed at the stored merchant is a NEW `catalog_offers` row
    #: under it, and on the sibling case that merchant is ADR-009 D2's banned
    #: bucket). This map is what makes the resulting SKU/offer seller divergence a
    #: reported number instead of an inference.
    merchant_adoptions: Dict[str, str] = {}
    #: planned sku_keys whose row will NOT be written at all.
    refused: set = set()

    # 1. DE-DUPLICATE BY IDENTITY. Two planned rows carrying the same tuple are one
    #    SKU: the second silently UPDATEs the first through the identity arbiter, so
    #    `counts["skus"]` would report two writes where one row exists. Keep the
    #    first, count the rest, and point the loser's offers at the survivor's key
    #    (same identity == same row, so those offers are not orphans).
    deduped: list = []
    first_key_for_identity: Dict[tuple, str] = {}
    for sku in skus:
        identity = _identity_tuple(sku)
        planned_key = str(sku.get("sku_key") or "")
        kept_key = first_key_for_identity.get(identity)
        if kept_key is None:
            first_key_for_identity[identity] = planned_key
            deduped.append(sku)
            continue
        counts["skus_deduped_same_identity"] += 1
        if planned_key and planned_key != kept_key:
            remap[planned_key] = kept_key
        logger.info(
            "apply_ingest_plan: two planned SKUs share identity (merchant_id=%s, "
            "platform=%s, product_key=%s, source_variant_id=%s) — keeping sku_key=%s, "
            "dropping the duplicate planned %s (its offers follow the survivor)",
            sku.get("merchant_id"), sku.get("platform"), sku.get("product_key"),
            sku.get("source_variant_id"), kept_key, planned_key,
        )
    skus = deduped

    product_keys = sorted({
        str(row.get("product_key") or "") for row in skus if row.get("product_key")
    })
    planned_keys = sorted({
        str(row.get("sku_key") or "") for row in skus if row.get("sku_key")
    })
    if not product_keys:
        return skus, _resolve_offer_keys(offers, remap, merchant_adoptions, refused, counts), counts

    try:
        identity_rows = await database.fetch_all(
            _SKU_IDENTITY_LOOKUP_SQL, {"product_keys": product_keys}
        )
        key_rows = await database.fetch_all(
            _SKU_KEY_HOLDER_LOOKUP_SQL, {"sku_keys": planned_keys}
        )
        suppressed_rows = await database.fetch_all(
            _SKU_SUPPRESSED_IDENTITY_SQL, {"product_keys": product_keys}
        )
    except Exception as exc:  # noqa: BLE001 — the upsert still has its own conflict handling
        logger.warning(
            "sku identity pre-resolve failed for %d product_key(s) — writing planned "
            "keys unchanged: %s", len(product_keys), str(exc)[:200],
        )
        return skus, _resolve_offer_keys(offers, remap, merchant_adoptions, refused, counts), counts

    held_by_identity: Dict[tuple, str] = {}
    for row in identity_rows or []:
        data = dict(row)
        held_by_identity[_identity_tuple(data)] = str(data.get("sku_key") or "")
    identity_of_key: Dict[str, tuple] = {}
    for row in key_rows or []:
        data = dict(row)
        identity_of_key[str(data.get("sku_key") or "")] = _identity_tuple(data)
    suppressed_identities: set = set()
    suppressed_keys: set = set()
    for row in suppressed_rows or []:
        data = dict(row)
        suppressed_identities.add(_identity_tuple(data))
        suppressed_keys.add(str(data.get("sku_key") or ""))

    kept: list = []
    for sku in skus:
        planned_key = str(sku.get("sku_key") or "")
        identity = _identity_tuple(sku)
        held_key = held_by_identity.get(identity)
        holder_identity = identity_of_key.get(planned_key)
        key_taken_by_another = (
            holder_identity is not None and holder_identity != identity
        )

        # (c) both constraints point at DIFFERENT existing rows.
        if held_key and held_key != planned_key and key_taken_by_another:
            counts["skus_identity_conflict"] += 1
            refused.add(planned_key)
            logger.error(
                "catalog_skus row refused (identity conflict): planned sku_key=%s is "
                "held by identity=%s while this row's identity (merchant_id=%s, "
                "platform=%s, product_key=%s, source_variant_id=%s) is held by "
                "sku_key=%s — two existing rows, no move satisfies both unique "
                "constraints; row skipped, its offers dropped",
                planned_key, holder_identity, sku.get("merchant_id"),
                sku.get("platform"), sku.get("product_key"),
                sku.get("source_variant_id"), held_key,
            )
            continue

        # (a) the identity is already held, under another key — adopt it.
        if held_key:
            if held_key != planned_key:
                logger.info(
                    "apply_ingest_plan: identity (merchant_id=%s, platform=%s, "
                    "product_key=%s, source_variant_id=%s) is already held by "
                    "sku_key=%s — adopting it instead of inserting the planned %s",
                    sku.get("merchant_id"), sku.get("platform"),
                    sku.get("product_key"), sku.get("source_variant_id"),
                    held_key, planned_key,
                )
                sku["sku_key"] = held_key
                remap[planned_key] = held_key
                counts["skus_adopted_existing_identity"] += 1
            kept.append((planned_key, sku))
            continue

        # (d) the identity looked UNHELD only because a SUPPRESSED row holds it (or
        #     holds our key). THIS BRANCH IS THE GUARD ON THE WRITE: with the
        #     identity held by a suppressed row the upsert does NOT fail, it
        #     DO UPDATEs that row through the identity arbiter — the withdrawn row's
        #     content is rewritten and counted in `skus` as a success. `_SKU_IDENTITY_LOOKUP_SQL`'s own
        #     `suppressed_at IS NULL` closes a DIFFERENT door — it keeps the
        #     suppressed row from being ADOPTED at (a) — which is why this branch
        #     sits below the adoption: `held_key` can only ever name a live row.
        if identity in suppressed_identities or planned_key in suppressed_keys:
            counts["skus_skipped_suppressed_identity"] += 1
            refused.add(planned_key)
            logger.warning(
                "catalog_skus row refused (suppressed row holds it): planned "
                "sku_key=%s, identity=(merchant_id=%s, platform=%s, product_key=%s, "
                "source_variant_id=%s) — a suppressed row holds that key or identity "
                "and both unique constraints cover suppressed rows; row skipped, its "
                "offers dropped rather than hung off a withdrawn SKU",
                planned_key, sku.get("merchant_id"), sku.get("platform"),
                sku.get("product_key"), sku.get("source_variant_id"),
            )
            continue

        if key_taken_by_another:
            # (b0) the holder is THIS product's row and the ONLY thing it disagrees
            #      about is the merchant — the disagreement `_prepare_seller_of_record`
            #      step 3 manufactures on every claimed-attach run. The planned SKU
            #      follows the stored row. Nothing about the stored row moves; the
            #      re-pointed plan simply resolves through the identity index onto
            #      it, so the content re-sync lands instead of 23505ing on the PK
            #      for ever. THE OFFERS DO NOT FOLLOW — an offer under the stored
            #      merchant would be a NEW row there, which on the canonical case
            #      is the ADR-009 D2 sentinel bucket.
            stored_merchant = _stored_merchant_to_adopt(sku, holder_identity)
            if stored_merchant is not None:
                logger.info(
                    "apply_ingest_plan: sku_key=%s is stored under merchant_id=%s "
                    "while this plan carries %s — same product_key=%s, platform=%s "
                    "and source_variant_id=%s, so the disagreement is the one "
                    "_prepare_seller_of_record manufactures at apply time, not "
                    "evidence the stored row is wrong. The PLANNED SKU adopts the "
                    "stored merchant and refreshes that row; the stored row's "
                    "merchant_id is NOT moved, and its offers keep the plan's "
                    "per-brand seller",
                    planned_key, stored_merchant, sku.get("merchant_id"),
                    sku.get("product_key"), sku.get("platform"),
                    sku.get("source_variant_id"),
                )
                sku["merchant_id"] = stored_merchant
                merchant_adoptions[planned_key] = stored_merchant
                counts["skus_adopted_stored_merchant"] += 1
                kept.append((planned_key, sku))
                continue

            # (b) the identity is unheld and our key is held by a row whose
            #     source_variant_id has drifted — re-point THAT ONE COLUMN. A holder
            #     that disagrees on merchant_id/platform, or that sits under another
            #     product, is left alone (see `_heal_drifted_sku_identity`) and the
            #     planned row is refused by the upsert instead.
            healed = await _heal_drifted_sku_identity(
                sku, planned_key, holder_identity, database=database
            )
            if healed:
                counts["skus_identity_healed"] += 1
        kept.append((planned_key, sku))

    # 3. DE-DUPLICATE AGAIN, ON THE RESOLVED KEY. Step 1 collapsed rows sharing the
    #    identity the plan CARRIED — but (a) and (b0) both rewrite that identity
    #    afterwards, in opposite directions: one row adopts the stored KEY, another
    #    adopts the stored MERCHANT, and two rows step 1 saw as distinct come out
    #    naming the SAME stored row. Left alone the executor upserts that row twice
    #    and `counts["skus"]` reports two writes where one row exists — the same lie
    #    step 1 exists to prevent, one resolution later. The loser's offers follow
    #    the survivor's key through `remap`, exactly as at step 1.
    #
    #    NOT REACHABLE FROM `ingest_validated_jsonl` TODAY: its plan carries one
    #    merchant per product_key (rule 1 pins it), so no two planned rows can
    #    differ on `merchant_id` alone and (b0) cannot fire beside (a). That makes
    #    this a bound on a resolution step, not a fix for a live count — which is
    #    why it is pinned by a test that builds the convergence directly rather
    #    than asserted to be impossible.
    deduped_kept: list = []
    survivor_keys: set = set()
    for planned_key, sku in kept:
        written_key = str(sku.get("sku_key") or "")
        if written_key and written_key in survivor_keys:
            counts["skus_deduped_after_adoption"] += 1
            if planned_key and planned_key != written_key:
                remap[planned_key] = written_key
            logger.info(
                "apply_ingest_plan: planned sku_key=%s resolved onto sku_key=%s, "
                "which another planned row in this batch also resolved onto "
                "(identity: merchant_id=%s, platform=%s, product_key=%s, "
                "source_variant_id=%s) — one stored row is one write; the "
                "duplicate is dropped and its offers follow the survivor",
                planned_key, written_key, sku.get("merchant_id"),
                sku.get("platform"), sku.get("product_key"),
                sku.get("source_variant_id"),
            )
            continue
        if written_key:
            survivor_keys.add(written_key)
        deduped_kept.append(sku)

    return (
        deduped_kept,
        _resolve_offer_keys(offers, remap, merchant_adoptions, refused, counts),
        counts,
    )


def _identity_tuple(row: Dict[str, Any]) -> tuple:
    """The 4 columns `idx_catalog_skus_source_identity_v2` is built on, stringified
    so a planned row (Python str) and a fetched row compare equal."""
    return (
        str(row.get("merchant_id") or ""),
        str(row.get("platform") or ""),
        str(row.get("product_key") or ""),
        str(row.get("source_variant_id") or ""),
    )


def _stored_merchant_to_adopt(
    sku: Dict[str, Any], stored_identity: tuple
) -> Optional[str]:
    """The stored row's `merchant_id`, when following it is the ONLY move needed.

    Returns the merchant to re-point the PLANNED SKU at (never its offers — see
    branch (b0) and `_resolve_offer_keys`) when the row already
    holding the plan's `sku_key` sits under THIS product_key, agrees on `platform`
    and on `source_variant_id`, and differs from the plan on `merchant_id` alone.
    `None` in every other shape — anything else needs a second decision (which
    variant, which product, which platform) that a content re-sync is not entitled
    to make, and those keep falling through to the heal or to the refusal.

    WHY FOLLOWING IS RIGHT HERE, and refusing is not. The disagreement this admits
    is one this lane MANUFACTURES: `_prepare_seller_of_record` step 3 rewrites the
    plan's `merchant_id` to a VERIFIED `brand_claims` tenant while
    `_PDP_UPSERT_SQL` never moves `catalog_products.merchant_id`, so on the live
    claimed-attach path the plan's merchant differs from the stored SKU's on EVERY
    run, by construction, until the R3 migration moves the products.
    `_heal_drifted_sku_identity` already says so and declines to move the stored
    row for it — but declining there sent the row to the upsert, where the identity
    index is free, the PK is not, and the 23505 is a PERMANENT refusal: content and
    offer frozen, one log line, no end state. Following the stored merchant is the
    only move that keeps the heal's premise (the stored row is not wrong) and still
    writes: the re-pointed plan resolves through the identity index onto that exact
    row, whose `merchant_id` the DO UPDATE does not touch.

    Bounded on purpose. `_SKU_KEY_HOLDER_LOOKUP_SQL` matches on `sku_key` ALONE, so
    the holder it returns can belong to a different product — the `product_key`
    check is what stops this adopting a foreign row's merchant, the same bound
    `_SKU_IDENTITY_HEAL_SQL` carries. `platform` and `source_variant_id` must agree
    because a row differing on those is a different variant or a different lane, not
    the same SKU under another seller."""
    stored_merchant, stored_platform, stored_product, stored_variant = stored_identity
    if stored_product != str(sku.get("product_key") or ""):
        return None
    if stored_platform != str(sku.get("platform") or ""):
        return None
    if stored_variant != str(sku.get("source_variant_id") or ""):
        return None
    if not stored_merchant or stored_merchant == str(sku.get("merchant_id") or ""):
        return None
    return stored_merchant


async def _heal_drifted_sku_identity(
    sku: Dict[str, Any],
    planned_key: str,
    stored_identity: tuple,
    *,
    database: Any,
) -> bool:
    """Re-point a stored row's `source_variant_id` at the plan's, keeping its key.

    SCOPE, deliberately one column. The drift this repairs is the legacy
    `source_variant_id = 'default'` on the row ingestion's own key names. It does
    NOT repair a `merchant_id` or a `platform` disagreement: the plan's merchant is
    NOT pinned to `catalog_products` on the claimed-attach path (see
    `_SKU_IDENTITY_HEAL_SQL`), so "the plan and the stored row disagree about the
    merchant" is a state this lane MANUFACTURES at apply time, not evidence the
    stored row is wrong. A row that disagrees on either of those columns is left
    exactly as it is and falls through to the upsert, where the PK collision is
    counted `skus_identity_conflict` and its offers are dropped — refused, not
    silently rewritten.

    Best-effort: a failed UPDATE leaves the planned row alone, so the upsert's own
    23505 handling (counted, skipped, offers dropped) still applies — the same
    outcome as before this healing existed."""
    planned_variant_id = str(sku.get("source_variant_id") or "")
    if stored_identity[3] == planned_variant_id:
        # The only column this heal may write already agrees; whatever else the
        # stored row disagrees about is not ours to decide.
        logger.warning(
            "catalog_skus identity NOT healed: sku_key=%s carries %s and the plan "
            "says (merchant_id=%s, platform=%s, product_key=%s, source_variant_id=%s)"
            " — they agree on source_variant_id, and merchant_id/platform are not a "
            "content re-sync's to move; leaving the row to the upsert's own conflict "
            "handling",
            planned_key, stored_identity, sku.get("merchant_id"),
            sku.get("platform"), sku.get("product_key"), planned_variant_id,
        )
        return False
    try:
        healed_row = await database.fetch_one(
            _SKU_IDENTITY_HEAL_SQL,
            {
                "sku_key": planned_key,
                "product_key": sku.get("product_key"),
                "source_variant_id": sku.get("source_variant_id"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "catalog_skus identity heal failed for sku_key=%s (stored identity %s "
            "-> planned (merchant_id=%s, platform=%s, product_key=%s, "
            "source_variant_id=%s)) — leaving the row to the upsert's own conflict "
            "handling: %s",
            planned_key, stored_identity, sku.get("merchant_id"),
            sku.get("platform"), sku.get("product_key"),
            sku.get("source_variant_id"), str(exc)[:200],
        )
        return False
    if healed_row is None:
        # The key holder is not under this product — `_SKU_KEY_HOLDER_LOOKUP_SQL`
        # matches on `sku_key` alone, so it can name a row belonging to a different
        # product. Not ours to move; the upsert refuses the planned row instead.
        logger.warning(
            "catalog_skus identity NOT healed: sku_key=%s is held by %s, which is "
            "not a row under product_key=%s — the heal is bounded to this product; "
            "leaving the row to the upsert's own conflict handling",
            planned_key, stored_identity, sku.get("product_key"),
        )
        return False
    logger.warning(
        "catalog_skus source_variant_id healed: sku_key=%s carried %s, its "
        "source_variant_id re-pointed to the plan's %r (merchant_id and platform "
        "left as stored — a content re-sync does not decide them). The key is kept "
        "so live offers stay attached",
        planned_key, stored_identity, planned_variant_id,
    )
    return True


def _resolve_offer_keys(
    offers: list,
    remap: Dict[str, str],
    merchant_adoptions: Dict[str, str],
    refused: set,
    counts: Dict[str, int],
) -> list:
    """Point every offer at the KEY its SKU was actually written under, drop the
    offers of SKUs that were not written at all, and count — never follow — a (b0)
    merchant adoption.

    THE KEY IS FOLLOWED; THE MERCHANT IS NOT. A `sku_key` remap has to reach the
    offers or they name a row that does not exist (catalog_offers has no FK). The
    (b0) MERCHANT adoption is the opposite case: `_OFFER_UPSERT_SQL` conflicts on
    `offer_id` alone and never updates `merchant_id`, so re-pointing an offer at
    the stored SKU's merchant does not move an existing row — it INSERTS a new
    `catalog_offers` row under that merchant. On the `<pk>::canonical` sibling the
    stored merchant is `external_seed`, ADR-009 D2's banned bucket, and that write
    minted a fresh sentinel-bucket offer on every mirror refresh
    (`verify_seller_rekey.orphan_failures` reports exactly this as
    `orphan_residue:catalog_offers=N`). So offers keep the per-brand seller
    `_prepare_seller_of_record` produced, and the divergence between a SKU's
    merchant and its offers' is REPORTED
    (`offers_kept_plan_seller_on_adoption`) rather than written away."""
    if not remap and not merchant_adoptions and not refused:
        return offers
    kept: list = []
    for offer in offers:
        planned_key = str(offer.get("sku_key") or "")
        target = planned_key
        seen: set = set()
        while target in remap and target not in seen:
            seen.add(target)
            target = remap[target]
        if target in refused or planned_key in refused:
            counts["offers_dropped_for_refused_sku"] += 1
            logger.warning(
                "catalog_offers row dropped: its SKU (sku_key=%s) was not written "
                "— an offer naming a sku_key that does not exist is a fake offer",
                planned_key,
            )
            continue
        if target != planned_key:
            offer["sku_key"] = target
            offer["offer_id"] = derive_offer_id(
                str(offer.get("product_key") or ""),
                target,
                str(offer.get("source_ref") or ""),
            )
            counts["offers_rekeyed_to_adopted_sku"] += 1
        # (b0): the offer does NOT follow its SKU's adopted merchant — see this
        # function's docstring. Counted so the divergence is legible.
        adopted_merchant = merchant_adoptions.get(planned_key)
        if adopted_merchant and str(offer.get("merchant_id") or "") != adopted_merchant:
            counts["offers_kept_plan_seller_on_adoption"] += 1
            logger.info(
                "catalog_offers row keeps its plan seller: sku_key=%s adopted the "
                "stored merchant_id=%s while this offer stays under %s — "
                "_OFFER_UPSERT_SQL conflicts on offer_id and never updates "
                "merchant_id, so re-pointing it would CREATE a row under the "
                "stored merchant (ADR-009 D2 bans that for the 'external_seed' "
                "bucket). No reader joins an offer's merchant to its SKU's",
                planned_key, adopted_merchant, offer.get("merchant_id"),
            )
        kept.append(offer)
    return _dedupe_offers_by_id(kept, counts)


def _dedupe_offers_by_id(offers: list, counts: Dict[str, int]) -> list:
    """ONE ROW PER `offer_id`, after the re-keying above.

    `ingest_validated_jsonl` de-duplicates its offer rows by `offer_id` — but that
    happens BEFORE this function re-derives the id from the ADOPTED `sku_key`. Two
    planned SKUs that were de-duplicated onto one identity, or that adopted the
    same stored key, carry offers whose destinations (`source_ref`) are the same,
    so `derive_offer_id(product_key, adopted_key, destination)` collapses them onto
    one id AFTER the plan's own dedupe has run. Left alone, the per-row executor
    upserts the same id twice and counts two offers where one row exists, and the
    bulk executor's multi-row VALUES raises 21000 ("ON CONFLICT DO UPDATE command
    cannot affect row a second time") and falls back to a row-by-row replay that
    reports the same inflated number. Keep the first, count the rest."""
    by_id: Dict[str, Any] = {}
    dropped = 0
    for offer in offers:
        offer_id = str(offer.get("offer_id") or "")
        if not offer_id:
            # No id to collapse on; leave it to the writer's own error handling.
            by_id[f"__no_id__{len(by_id)}"] = offer
            continue
        if offer_id in by_id:
            dropped += 1
            logger.info(
                "catalog_offers row dropped as a duplicate: offer_id=%s is already "
                "carried by this plan after re-keying (sku_key=%s, source_ref=%s) "
                "— one destination on one SKU is one offer",
                offer_id, offer.get("sku_key"), offer.get("source_ref"),
            )
            continue
        by_id[offer_id] = offer
    if dropped:
        counts["offers_deduped_after_rekey"] = (
            counts.get("offers_deduped_after_rekey", 0) + dropped
        )
    return list(by_id.values())


def _drop_offers_of_refused_skus(
    offers: list, refused_sku_keys: set, counts: Dict[str, int]
) -> list:
    """Post-write filter for the SKUs the pre-resolve could not foresee (a race, a
    suppressed row under another product's key, any write failure at all). Same
    rule as `_filter_children_of_skipped` applies to a skipped PDP: catalog_offers
    has NO foreign key, so an offer whose SKU was refused is an orphan the database
    will never catch."""
    if not refused_sku_keys:
        return offers
    kept = [o for o in offers if str(o.get("sku_key") or "") not in refused_sku_keys]
    dropped = len(offers) - len(kept)
    if dropped:
        counts["offers_dropped_for_refused_sku"] = (
            counts.get("offers_dropped_for_refused_sku", 0) + dropped
        )
        logger.warning(
            "catalog_offers: %d offer(s) dropped because their SKU was refused by "
            "the write (sku_key(s)=%s)", dropped, sorted(refused_sku_keys),
        )
    return kept


def _is_unique_violation(exc: BaseException) -> bool:
    """True for a Postgres unique violation (SQLSTATE 23505).

    Matched the way `routes/billing_routes._is_unique_violation` does — on the
    driver's `sqlstate`/`pgcode`, with the class name as fallback, NEVER on the
    message text (a text match would swallow every other constraint failure and
    report it as an identity conflict). The cause/context chain is walked because
    a wrapper can hide the driver error.
    """
    seen: set = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if code == "23505":
            return True
        if "uniqueviolation" in type(cur).__name__.lower():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _note_sku_write_failure(
    exc: BaseException, row: Dict[str, Any], counts: Dict[str, int]
) -> None:
    """Classify ONE failed catalog_skus upsert; never raises, never aborts a batch.

    The identity arbiter plus `_adopt_existing_sku_identities` closes the common
    collision, but the table's OTHER unique constraint is still reachable from the
    opposite direction: the same `sku_key` under a DIFFERENT identity tuple — a
    product whose merchant_id was re-resolved (W2 remapping, a claimed-attach)
    keeps its derived key while its tuple moves. That row cannot be written
    without deciding which of two real rows wins, so it is counted and logged with
    BOTH keys rather than guessed at.
    """
    if _is_unique_violation(exc):
        counts["skus_identity_conflict"] = counts.get("skus_identity_conflict", 0) + 1
        logger.error(
            "catalog_skus upsert refused (unique violation, SQLSTATE 23505): "
            "sku_key=%s vs identity=(merchant_id=%s, platform=%s, product_key=%s, "
            "source_variant_id=%s) — the key and the identity tuple name different "
            "rows; row skipped, batch continues: %s",
            row.get("sku_key"), row.get("merchant_id"), row.get("platform"),
            row.get("product_key"), row.get("source_variant_id"), str(exc)[:200],
        )
        return
    logger.exception("insert sku failed for sku_key=%s — %s", row.get("sku_key"), exc)


async def _apply_inci_rows(
    inci_rows: list,
    *,
    database: Any,
    skipped_product_keys: set,
) -> Dict[str, int]:
    """Write each captured INCI list into beauty_sku_ingredients via the canonical
    INCI intake (source-precedence + verified-actives + serving recompute). Runs
    AFTER products + skus land so the intake can resolve the product's canonical
    SKU. INCI is OPTIONAL: a row for a skipped/failed product is dropped, a
    non-INCI blob is rejected by the intake, and any per-row failure is logged and
    counted — never aborts the apply."""
    counts = {"inci_written": 0, "inci_skipped": 0}
    if not inci_rows:
        return counts
    from services.canonical_inci_intake import ingest_canonical_inci

    for row in inci_rows:
        pk = row.get("product_key")
        if not pk or pk in skipped_product_keys:
            counts["inci_skipped"] += 1
            continue
        try:
            res = await ingest_canonical_inci(
                pk,
                str(row.get("raw_inci") or ""),
                str(row.get("source") or "reseller_listing"),
                db=database,
            )
            if res.get("status") == "ok" and res.get("written_skus"):
                counts["inci_written"] += 1
            else:
                counts["inci_skipped"] += 1
        except Exception as exc:  # noqa: BLE001 — INCI is best-effort; products/offers already landed
            counts["inci_skipped"] += 1
            logger.warning("apply_ingest_plan INCI write failed for product_key=%s — %s",
                           pk, str(exc)[:200])
    return counts


_ENSURE_MERCHANT_INSERT_SQL = """
                INSERT INTO catalog_merchants
                  (merchant_id, merchant_name, primary_platform, status,
                   source_system, source_ref, metadata_json)
                VALUES
                  (:merchant_id, :merchant_name, :primary_platform, :status,
                   :source_system, :source_ref, CAST(:metadata_json AS jsonb))
                ON CONFLICT (merchant_id) DO NOTHING
                """


async def _prepare_seller_of_record(plan: Dict[str, Any], database: Any) -> Dict[str, Any]:
    """W2 apply-time seller handling, applied to BOTH executors.

    1. EXISTING ROWS WIN (derive from the row): the product upsert never
       updates merchant_id, so for a product_key that already exists the plan
       rows are remapped to the DB row's actual merchant — legacy sentinel
       included. Anything else writes group-membership singletons and observed
       merchant rows for an ownership the catalog does not actually have
       (phantom rows the first re-run over the legacy population would have
       minted at scale). Legacy rows migrate in R3, with parity — never as a
       side effect of a content re-sync.
    2. TRIPWIRE (ADR-009 D2, write boundary): refuse the plan if any NEW
       product/sku row would be created under the banned 'external_seed'
       bucket. Existing sentinel rows remapped by rule 1 are not new writes.
    3. `_ensure_only` merchant rows (the observed sellers of record):
       - attach beats mint: a VERIFIED brand_claims tenant for the registrable
         domain takes the rows — but only when that tenant already has a
         catalog_merchants row; serving surfaces INNER-JOIN it, so remapping
         onto a missing row would silently hide the products. Missing ->
         attach is DEFERRED loudly and the observed identity stands.

    STEP 3 OVERRIDES STEP 1, AND `catalog_products` DOES NOT FOLLOW. A claimed
    attach rewrites `merchant_id` on the plan's pdps AND skus, on top of the pin
    rule 1 just applied — while `_PDP_UPSERT_SQL` never updates
    `catalog_products.merchant_id` (that is rule 1's whole premise). So on this
    path a plan row's merchant is NOT the merchant of the product row it names, by
    construction, until the R3 migration moves the products. Nothing downstream may
    read a plan-vs-stored merchant disagreement as evidence that the STORED row is
    wrong: `_heal_drifted_sku_identity` did, and silently moved live SKUs onto the
    claimed tenant. Live case: flowerbeauty.com.
       - otherwise insert-if-missing (ON CONFLICT DO NOTHING). Never the
         clobbering upsert: its `status = EXCLUDED.status` would downgrade a
         merchant that has graduated beyond 'observed'. A transient insert
         failure is logged and skipped (the plan contract is per-row
         fail-soft); the merchant heals on the next run.
    """
    from db.brand_claims import STATUS_VERIFIED
    from services.seller_identity import BANNED_BUCKET_MERCHANT_ID, etld1

    pdps = plan.get("pdps") or []
    skus = plan.get("skus") or []

    # 1. Existing rows win.
    product_keys = sorted({str(r.get("product_key") or "") for r in pdps if r.get("product_key")})
    existing_by_key: Dict[str, str] = {}
    if product_keys:
        rows = await database.fetch_all(
            "SELECT product_key, merchant_id FROM catalog_products WHERE product_key = ANY(:keys)",
            {"keys": product_keys},
        )
        for row in rows or []:
            data = dict(row)
            existing_by_key[str(data.get("product_key") or "")] = str(data.get("merchant_id") or "")
    planned_by_key: Dict[str, str] = {}
    for row in list(pdps) + list(skus):
        key = str(row.get("product_key") or "")
        existing_merchant = existing_by_key.get(key)
        if existing_merchant:
            planned_by_key.setdefault(key, str(row.get("merchant_id") or ""))
            row["merchant_id"] = existing_merchant
            row["_existing_row"] = True

    # 2. Tripwire on NEW writes only.
    for row in list(pdps) + list(skus):
        if row.pop("_existing_row", False):
            continue
        if str(row.get("merchant_id") or "") == BANNED_BUCKET_MERCHANT_ID:
            raise RuntimeError(
                "ADR-009 D2 violation: refusing to CREATE a product/sku row "
                f"under the banned '{BANNED_BUCKET_MERCHANT_ID}' bucket "
                f"(product_key={row.get('product_key')!r})"
            )

    # 3. Observed seller rows.
    referenced = {str(r.get("merchant_id") or "") for r in list(pdps) + list(skus)}
    merchants = plan.get("merchants") or []
    passthrough: list = []
    claim_memo: Dict[str, Optional[str]] = {}
    for merchant in merchants:
        if not merchant.get("_ensure_only"):
            passthrough.append(merchant)
            continue
        observed_id = str(merchant.get("merchant_id") or "")
        if observed_id not in referenced:
            continue  # every row that named it was remapped to an existing merchant
        registrable = str(merchant.get("source_ref") or "")
        claimed = None
        if registrable:
            if registrable in claim_memo:
                claimed = claim_memo[registrable]
            else:
                # Server-side registrable match against VERIFIED claims — the
                # database handle THIS plan runs on (never the module-global,
                # which diverges under db overrides), and no Python-side scan
                # cap (the old LIMIT-200 scan silently missed claim #201).
                # Column names are brand_claims' REAL ones (`verification_status`,
                # `verified_at`; db/brand_claims.py). Until 2026-09-04 this read
                # `status`/`updated_at`, neither of which exists, so the
                # best-effort except below swallowed a `column does not exist`
                # on EVERY apply and no verified claim ever attached
                # (prod: catalog-curated-brand-onboard-xlv56, flowerbeauty.com).
                # tests/test_w2_claimed_attach_query_postgres.py pins it.
                try:
                    row = await database.fetch_one(
                        """
                        SELECT merchant_id FROM brand_claims
                        WHERE verification_status = :verified
                          AND (
                            lower(coalesce(brand_domain, '')) = :reg
                            OR lower(coalesce(brand_domain, '')) LIKE '%.' || :reg
                          )
                        ORDER BY verified_at DESC NULLS LAST, created_at DESC
                        LIMIT 1
                        """,
                        {"reg": registrable, "verified": STATUS_VERIFIED},
                    )
                    claimed = str(dict(row).get("merchant_id") or "").strip() or None if row else None
                except Exception as exc:  # noqa: BLE001 — attach is best-effort; mint is the honest state
                    logger.warning("W2 claimed-attach lookup failed for %s: %s", registrable, str(exc)[:200])
                    claimed = None
                claim_memo[registrable] = claimed
        if claimed and claimed != observed_id:
            claimed_exists = await database.fetch_one(
                "SELECT 1 FROM catalog_merchants WHERE merchant_id = :mid",
                {"mid": claimed},
            )
            if claimed_exists:
                for row in list(pdps) + list(skus):
                    if row.get("merchant_id") == observed_id:
                        row["merchant_id"] = claimed
                continue  # tenant identity attaches; no observed row is written
            logger.warning(
                "W2 claimed-attach DEFERRED for %s -> %s: claimed merchant has no "
                "catalog_merchants row (serving INNER-JOINs it); observed identity stands",
                registrable, claimed,
            )
        params = {k: v for k, v in merchant.items() if k != "_ensure_only"}
        try:
            await database.execute(_ENSURE_MERCHANT_INSERT_SQL, params)
        except Exception as exc:  # noqa: BLE001 — per-row fail-soft, heals next run
            logger.warning("W2 observed-merchant insert failed for %s: %s", observed_id, str(exc)[:200])
    plan = dict(plan)
    plan["merchants"] = passthrough
    return plan


async def _refuse_parallel_retailer_listings(plan: Dict[str, Any], database: Any) -> None:
    """A new listing key must not silently leave an older same-URL row eligible.

    The reviewed cohort migration owns retiring old children and seed rows. Both
    executors refuse before any merchant or catalog write when that work remains.
    """
    from services.catalog_enrichment_agent.ingestion import retailer_listing_identity

    listings = {}
    for pdp in plan.get("pdps") or []:
        if str(pdp.get("product_key") or "").startswith("ext:retailer:"):
            identity = retailer_listing_identity(pdp.get("source_domain"), pdp.get("canonical_url"))
            listings[identity] = pdp["product_key"]
    if not listings:
        return
    rows = await database.fetch_all(
        """
        SELECT product_key, source_domain, canonical_url FROM catalog_products
        WHERE lower(split_part(regexp_replace(canonical_url,
                             '^https?://(www[.])?', '', 'i'), '/', 1)) = ANY(:hosts)
        """, {"hosts": sorted({listing.split("/", 1)[0] for listing in listings})},
    )
    from urllib.parse import urlsplit

    for row in rows or []:
        row = dict(row)
        # The candidate's URL owns this comparison. Missing source metadata on
        # an unrelated old listing must not block every product on its host.
        url = row.get("canonical_url") or ""
        identity = retailer_listing_identity(urlsplit(url).hostname, url)
        if identity in listings and row.get("product_key") != listings[identity]:
            raise ValueError(
                "retailer_listing_migration_required: existing product "
                f"{row.get('product_key')} owns {identity}; migrate/rekey or remove its full legacy chain before onboarding"
            )


async def apply_ingest_plan(
    plan: Dict[str, Any],
    *,
    batch_label: str,
    db: Any = None,
    batch: bool = False,
    primary_readiness: bool = False,
) -> Dict[str, Any]:
    """Persist an ingest plan, optionally handing curated rows to serving policy.

    Curated CLI/queue callers select primary_readiness explicitly. Every other
    existing door retains the original counts and best-effort persistence path.
    A failed handoff leaves its persisted counts on the typed exception so an
    operator can retry the same identities without calling a partial run done.
    """
    from db.database import database as global_db
    from services.catalog_enrichment_agent.primary_ingestion import require_primary_plan, require_primary_apply

    preflight = require_primary_plan(plan) if primary_readiness else None
    counts = await _apply_ingest_plan(plan, batch_label=batch_label, db=db, batch=batch)
    if not primary_readiness:
        return counts
    from services.catalog_enrichment_agent.primary_readiness import (
        PrimaryReadinessIncomplete, materialize_primary_readiness,
    )
    try:
        require_primary_apply(preflight, counts)
        if int(counts.get("seeds") or 0) != len(plan.get("seeds") or []):
            raise ValueError("incomplete_primary_seed_writes")
    except Exception as exc:
        report = {"status": "failed", "failed_stage": "persistence", "error": str(exc)[:300]}
        raise PrimaryReadinessIncomplete(report, counts) from exc
    try:
        counts["primary_readiness"] = await materialize_primary_readiness(plan, db=db or global_db)
    except PrimaryReadinessIncomplete as exc:
        exc.persisted_counts = dict(counts)
        raise
    return counts


async def _apply_ingest_plan(
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

    await _refuse_parallel_retailer_listings(plan, database)
    plan = await _prepare_seller_of_record(plan, database)

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
    group_targets: Dict[str, str] = {}
    counts["product_groups_failed"] = 0
    counts["pdps_skipped_identity"] = 0
    skipped_product_keys: set = set()

    # 2. catalog_products — UPSERT by product_key.
    for pdp in pdps:
        if not await _apply_pdp_identity_gate(pdp, identity_gate_on=identity_gate_on, group_targets=group_targets):
            counts["pdps_skipped_identity"] += 1
            skipped_product_keys.add(pdp.get("product_key"))
            continue
        try:
            await database.execute(_PDP_UPSERT_SQL, pdp)
            counts["pdps"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("insert pdp failed for product_key=%s — %s", pdp.get("product_key"), exc)

        if str(pdp.get("product_key") or "").startswith("ext:retailer:"):
            if not await _ensure_primary_retailer_group(
                pdp, database=database, target=group_targets.get(pdp["product_key"]),
            ):
                counts["product_groups_failed"] += 1
                skipped_product_keys.add(pdp.get("product_key"))
        else:
            await _ensure_singleton_pg(pdp)

    skus, offers, seeds = _filter_children_of_skipped(
        skipped_product_keys, skus=skus, offers=offers, seeds=seeds
    )

    # 2b. Reconcile every planned SKU with the rows that already hold its key or
    #     its identity (adopt / heal / refuse), and re-key or drop its offers,
    #     BEFORE the upserts run.
    counts["skus_identity_conflict"] = 0
    counts["offers_dropped_for_refused_sku"] = 0
    skus, offers, adoption_counts = await _adopt_existing_sku_identities(
        skus, offers, database=database
    )
    counts.update(adoption_counts)

    async with database.transaction():
        # 3. catalog_skus — INSERT one synthetic 'canonical' SKU per PDP.
        refused_sku_keys: set = set()
        for sku in skus:
            try:
                # SAVEPOINT per row. The residual dual-unique trap raises 23505,
                # and an error inside a Postgres transaction ABORTS it — every
                # later statement (the whole offers stage) would then fail with
                # 25P02, so "logged and skipped" was only ever true off Postgres.
                # The nested transaction rolls back this row alone.
                async with database.transaction():
                    await database.execute(_SKU_UPSERT_SQL, sku)
                counts["skus"] += 1
            except Exception as exc:  # noqa: BLE001
                _note_sku_write_failure(exc, sku, counts)
                refused_sku_keys.add(str(sku.get("sku_key") or ""))

        # A SKU that was refused must not leave an offer behind. catalog_offers has
        # NO foreign key to catalog_skus, so an offer naming a sku_key we did not
        # write is not a pending offer, it is a fake one — the same orphan rule
        # `_filter_children_of_skipped` applies to the children of a skipped PDP.
        offers = _drop_offers_of_refused_skus(offers, refused_sku_keys, counts)

        accepted_offers, skip_reasons, _rejected_offers = await guard_catalog_offer_rows(offers)
        if skip_reasons:
            audit.record_skips(skip_reasons)
            counts["offers_skipped"] = sum(skip_reasons.values())

        # 4. catalog_offers — INSERT one row per validated retailer offer.
        for offer in accepted_offers:
            try:
                # SAVEPOINT per row, for the SAME reason the SKU loop above has
                # one. This loop runs INSIDE the transaction opened at the top of
                # this stage; an error inside a Postgres transaction ABORTS it, so
                # without the nested transaction one bad offer turned every later
                # statement into a 25P02 and degraded the enclosing COMMIT to a
                # ROLLBACK — throwing away the SKUs and the offers that had already
                # succeeded, while `counts` went on reporting them as written. The
                # nested transaction rolls back this offer alone.
                async with database.transaction():
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

    # 6. beauty_sku_ingredients — optional INCI capture (after skus exist).
    counts.update(
        await _apply_inci_rows(
            plan.get("incis") or [],
            database=database,
            skipped_product_keys=skipped_product_keys,
        )
    )

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
    group_targets: Dict[str, str] = {}
    counts["product_groups_failed"] = 0
    skipped_product_keys: set = set()

    # 2. catalog_products — run the per-row identity gate first (it may SKIP rows
    #    and may itself round-trip when enabled), then bulk-upsert the survivors.
    insertable_pdps = []
    for pdp in pdps:
        if not await _apply_pdp_identity_gate(pdp, identity_gate_on=identity_gate_on, group_targets=group_targets):
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

    for pdp in inserted_pdps:
        if str(pdp.get("product_key") or "").startswith("ext:retailer:"):
            if not await _ensure_primary_retailer_group(
                pdp, database=database, target=group_targets.get(pdp["product_key"]),
            ):
                counts["product_groups_failed"] += 1
                skipped_product_keys.add(pdp.get("product_key"))

    singleton_rows = [
        {
            "product_group_id": make_singleton_product_group_id(str(p.get("content_key")).strip()),
            "merchant_id": str(p.get("merchant_id") or ""),
            "platform": str(p.get("platform") or ""),
            "platform_product_id": str(p.get("source_product_id") or ""),
        }
        for p in inserted_pdps if (p.get("content_key") or "").strip()
        and not str(p.get("product_key") or "").startswith("ext:retailer:")
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

    # 3. catalog_skus — same identity adoption as the per-row path (ONE helper), then
    #    the same upsert. `bulk_upsert` classifies a failed multi-row chunk as a data
    #    error (a 23505 is not a transport error) and replays the chunk row by row, so
    #    only the genuinely-conflicting row is skipped; `on_row_error` is where that
    #    row's SQLSTATE gets classified and counted.
    counts["skus_identity_conflict"] = 0
    counts["offers_dropped_for_refused_sku"] = 0
    skus, offers, adoption_counts = await _adopt_existing_sku_identities(
        skus, offers, database=database
    )
    counts.update(adoption_counts)
    counts["skus"], _, sku_skipped_rows = await bulk_upsert(
        database, _SKU_UPSERT_SQL, skus, label="skus",
        on_row_error=lambda row, exc: _note_sku_write_failure(exc, row, counts),
    )
    # `bulk_upsert`'s THIRD return is the orphan guard — the same use the PDP stage
    # above makes of it via `_filter_children_of_skipped`. A SKU the replay skipped
    # was not written, so its offers would name a sku_key that does not exist.
    offers = _drop_offers_of_refused_skus(
        offers,
        {str(r.get("sku_key") or "") for r in sku_skipped_rows},
        counts,
    )

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

    # 6. beauty_sku_ingredients — optional INCI capture (after skus exist). Uses
    #    the same per-row canonical intake as the non-batched path (precedence +
    #    verified actives + serving recompute); best-effort, never aborts.
    counts.update(
        await _apply_inci_rows(
            plan.get("incis") or [],
            database=database,
            skipped_product_keys=skipped_product_keys,
        )
    )

    await write_writer_audit_log(audit)
    logger.info("apply_ingest_plan(batch) applied: %s", counts)
    return counts

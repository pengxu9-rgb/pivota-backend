"""Convergence Phase 1.6 — external offers → persisted `catalog_offers`.

External redirect offers live in `external_product_seeds` and are served from
there today (services.external_seed_search → services.pivot_query_service builds
an in-memory OfferNode per seed). The convergence target is ONE offer table:
`catalog_offers`. The decision (plan P1.6) is to PERSIST — dual-write each
external offer into `catalog_offers` now, keep serving from seeds until Phase 2,
and reconcile drift nightly.

The 15-minute external-seed materialization job
(jobs/external_seed_catalog_materialization_job → scripts.mirror_external_seeds_to
_catalog_products) already writes the full canonical chain (products + skus +
offers) for seeds that have NO mirror yet. Two gaps remain, which this module
closes with a SINGLE offer-projection function reused everywhere:

  1. update lag — once a seed is mirrored the batch never revisits it, so a later
     price / availability edit does not reach its catalog_offers row. `sync_offer
     _for_seed` re-projects one seed's offer on demand.
  2. drift / missing / orphan — scripts/reconcile_external_seed_offers.py compares
     the two tables and repairs via this same function (the nightly reconciliation).

`catalog_offers` requires a product_key (→ catalog_products). This module only
PROJECTS THE OFFER: it never creates the product/sku chain (that stays owned by
the mirror). If a seed has no mirror product yet, `sync_offer_for_seed` is a
no-op and the batch job creates the whole chain within its cadence.

The offer identity + field mapping is deterministic and byte-identical to the
mirror's historical write (extracted here so both paths cannot drift): merchant
sentinel `external_seed`, sku `<product_key>::canonical`, hashed offer_id, and
the honest `external_referral / observed / referral_only` triple with
`offer_mode='redirect'`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, Optional

from db.database import database
from services.catalog_sync_service import make_catalog_product_key

logger = logging.getLogger(__name__)

# Sentinel merchant + platform for employee-managed external seeds (matches the
# mirror). product_key = prod::external_seed::external_seed::<external_product_id>.
MIRROR_MERCHANT_ID = "external_seed"
MIRROR_PLATFORM = "external_seed"

# The external offer's intrinsic tier triple + shape. An external seed is an
# OBSERVED, redirect-fulfilled offer — never first-party, never native checkout.
OFFER_CATALOG_TRACK = "external_referral"
OFFER_TRUTH_TIER = "observed"
OFFER_READINESS_TIER = "referral_only"
OFFER_MODE = "redirect"
OFFER_CHANNEL = "external_referral"
OFFER_SOURCE_SYSTEM = "external_product_seeds_mirror_v1"
OFFER_PRICE_CONFIDENCE = Decimal("0.6")

SKU_SUFFIX = "::canonical"
OFFER_ID_PREFIX = "offer:external_seed:"


def dual_write_enabled() -> bool:
    """Flag: run the synchronous seed→offer dual-write on seed writes.

    Default ON — the projection is inert to live serving (the pivot lane that
    reads catalog_offers is flag-off; the mainline serves external seeds
    directly), so it only keeps the persisted mirror fresh. Off makes every
    `sync_offer_for_seed` call a no-op for an instant kill-switch.
    """
    return os.getenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def mirror_product_key(external_product_id: str) -> str:
    """The catalog_products.product_key the mirror assigns to a seed."""
    return make_catalog_product_key(
        MIRROR_MERCHANT_ID, MIRROR_PLATFORM, str(external_product_id)
    )


def derive_mirror_sku_key(product_key: str) -> str:
    """One canonical SKU per mirrored product (`<product_key>::canonical`)."""
    return f"{product_key}{SKU_SUFFIX}"


def derive_mirror_offer_id(product_key: str) -> str:
    """Deterministic offer id keyed off product_key. Hashed so long
    external_product_id values stay within the offer_id column length."""
    digest = hashlib.sha256(product_key.encode("utf-8")).hexdigest()[:32]
    return f"{OFFER_ID_PREFIX}{digest}"


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def upsert_catalog_offer_from_seed_row(
    product_key: str,
    row_dict: Dict[str, Any],
    merchant_id: str = MIRROR_MERCHANT_ID,
) -> None:
    """Write / refresh the canonical offer row carrying price + currency +
    availability for one external seed. `price_amount` is mapped 1:1 to all
    three pricing columns (the seed carries only the displayed retailer price).

    Idempotent on offer_id: ON CONFLICT refreshes the mutable price/availability
    fields. This is the single source of truth for the seed→offer projection —
    both the mirror script and the reconciliation script call it.
    """
    sku_key = derive_mirror_sku_key(product_key)
    offer_id = derive_mirror_offer_id(product_key)
    raw_price = row_dict.get("price_amount")
    try:
        list_price_value = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        list_price_value = None
    offer_payload = {
        "source": OFFER_SOURCE_SYSTEM,
        "destination_url": row_dict.get("destination_url"),
        "canonical_url": row_dict.get("canonical_url"),
        "domain": row_dict.get("domain"),
        "external_seed_id": row_dict.get("id"),
        "market": row_dict.get("market"),
    }
    await database.execute(
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
          currency = EXCLUDED.currency,
          list_price = EXCLUDED.list_price,
          merchant_effective_price = EXCLUDED.merchant_effective_price,
          estimated_best_price = EXCLUDED.estimated_best_price,
          price_confidence = EXCLUDED.price_confidence,
          offer_payload = EXCLUDED.offer_payload,
          updated_at = NOW()
        """,
        {
            "offer_id": offer_id,
            "sku_key": sku_key,
            "product_key": product_key,
            "merchant_id": merchant_id,
            "catalog_track": OFFER_CATALOG_TRACK,
            "truth_tier": OFFER_TRUTH_TIER,
            "readiness_tier": OFFER_READINESS_TIER,
            "offer_mode": OFFER_MODE,
            "channel": OFFER_CHANNEL,
            "availability": row_dict.get("availability"),
            "inventory_quantity": None,
            "currency": row_dict.get("price_currency") or "USD",
            "list_price": list_price_value,
            "merchant_effective_price": list_price_value,
            "estimated_best_price": list_price_value,
            "price_confidence": (
                str(OFFER_PRICE_CONFIDENCE) if list_price_value is not None else None
            ),
            "source_system": OFFER_SOURCE_SYSTEM,
            "source_ref": row_dict.get("id"),
            "offer_payload": json.dumps(
                offer_payload, ensure_ascii=False, default=_json_default
            ),
        },
    )


_SEED_OFFER_COLUMNS = (
    "id, external_product_id, destination_url, canonical_url, domain, "
    "price_amount, price_currency, availability, market"
)


async def sync_offer_for_seed(seed_id: str) -> Dict[str, Any]:
    """Re-project one external seed's catalog_offers row from its current state.

    Best-effort + idempotent + NEVER raises — it rides on seed-write paths and
    must not break them. No-op (skipped) when the flag is off, the seed is gone,
    it has no external_product_id, or its mirror product doesn't exist yet (the
    materialization job owns product creation). Returns a small status dict.
    """
    if not seed_id:
        return {"seed_id": seed_id, "status": "no_seed_id"}
    if not dual_write_enabled():
        return {"seed_id": seed_id, "status": "disabled"}
    try:
        row = await database.fetch_one(
            f"SELECT {_SEED_OFFER_COLUMNS} FROM external_product_seeds "
            "WHERE id = :seed_id",
            {"seed_id": seed_id},
        )
        if not row:
            return {"seed_id": seed_id, "status": "seed_missing"}
        seed = dict(row)
        external_product_id = seed.get("external_product_id")
        if not external_product_id:
            return {"seed_id": seed_id, "status": "no_external_product_id"}

        product_key = mirror_product_key(external_product_id)
        exists = await database.fetch_val(
            "SELECT 1 FROM catalog_products WHERE product_key = :pk",
            {"pk": product_key},
        )
        if not exists:
            # The mirror hasn't materialized this seed's product yet; the batch
            # job will create the full chain (product + sku + offer). Skip.
            return {"seed_id": seed_id, "status": "no_mirror_product",
                    "product_key": product_key}

        await upsert_catalog_offer_from_seed_row(product_key, seed)
        return {"seed_id": seed_id, "status": "synced", "product_key": product_key}
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "event": "external_offer_dual_write_failed",
            "seed_id": seed_id,
            "error": str(exc),
        })
        return {"seed_id": seed_id, "status": "error", "error": str(exc)}

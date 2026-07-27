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
from services.offer_seller_identity import host_from_url, is_known_retailer

logger = logging.getLogger(__name__)

# The mirror row is located by its provenance link to the seed, NOT by
# reconstructing a product_key. Since ADR-009 D2 the mirror mints a PER-BRAND
# observed seller (merch_obs_<digest>) and keys the product under it, so the old
# `prod::external_seed::external_seed::<ext_id>` assumption never matches a real
# row. The mirror stamps catalog_products.source_ref = external_product_seeds.id
# with this source_system — that pair is the stable seed→product link.
MIRROR_SOURCE_SYSTEM = "external_product_seeds_mirror_v1"

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

    Default OFF — genuine dark launch (slice 3 ships inert). The projection is
    inert to live serving anyway (the pivot lane reading catalog_offers is
    flag-off; the mainline serves seeds directly), but a default-ON write hook
    is not "dark" — flip EXTERNAL_OFFER_DUAL_WRITE_ENABLED=1 to start populating,
    co-gated with the Phase-2 pivot cutover.
    """
    return os.getenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


async def resolve_mirror_product(seed_id: str) -> Optional[Dict[str, str]]:
    """Locate the seed's existing mirror product by provenance
    (catalog_products.source_ref = seed_id under the mirror source_system) and
    return its real {product_key, merchant_id}.

    This is the fix for the per-brand seller keying (ADR-009 D2): we NEVER
    reconstruct the product_key from a sentinel merchant — we read the row the
    mirror actually wrote, so the offer attaches under the correct observed
    seller (never the ADR-009-banned 'external_seed' bucket). Returns None when
    the mirror hasn't materialized the product yet (the batch job owns creation).
    """
    row = await database.fetch_one(
        """
        SELECT product_key, merchant_id
        FROM catalog_products
        WHERE source_ref = :seed_id AND source_system = :src
        ORDER BY updated_at DESC NULLS LAST
        LIMIT 1
        """,
        {"seed_id": str(seed_id), "src": MIRROR_SOURCE_SYSTEM},
    )
    if not row:
        return None
    data = dict(row)
    pk = str(data.get("product_key") or "").strip()
    mid = str(data.get("merchant_id") or "").strip()
    if not pk or not mid:
        return None
    return {"product_key": pk, "merchant_id": mid}


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


def _clean_domain(value: Any) -> Optional[str]:
    """Seed domain → the value `catalog_offers.source_domain` should carry.

    Deliberately MINIMAL: trim, lowercase, empty→None. It does NOT strip `www.`
    and does NOT parse URLs, because the consumers key on this column joined
    against `external_product_seeds.domain` — the audit and the currency backfill
    both read `coalesce(offer.source_domain, seed.domain)` as ONE key and group by
    it. Normalising here but not there would split a single storefront into two
    report rows and hide it under the per-domain `--min-offers` floor. Real seed
    domains in prod include `www.jurlique.com`, so the `www.` is load-bearing for
    that join.

    Host normalisation belongs at the point of use, and already lives there:
    `services.storefront_currency.normalize_domain` strips `www.` when building
    the `/meta.json` URL. Store what the seed said; normalise when fetching.
    """
    text = str(value or "").strip().lower()
    return text or None


async def upsert_catalog_offer_from_seed_row(
    product_key: str,
    row_dict: Dict[str, Any],
    *,
    merchant_id: str,
) -> None:
    """Write / refresh the canonical offer row carrying price + currency +
    availability for one external seed. `price_amount` is mapped 1:1 to all
    three pricing columns (the seed carries only the displayed retailer price).

    Idempotent on offer_id: ON CONFLICT refreshes the mutable price/availability
    fields. This is the single source of truth for the seed→offer projection —
    both the mirror script and the reconciliation script call it.

    `merchant_id` is REQUIRED and must be the seed's real observed seller (from
    resolve_mirror_product) — never the 'external_seed' sentinel, which ADR-009
    D2 bans and the mirror refuses.
    """
    if not merchant_id or merchant_id == "external_seed":
        raise ValueError(
            f"external_offer_dual_write: refusing offer write under merchant_id="
            f"{merchant_id!r} (must be the seed's observed seller; product_key={product_key})"
        )
    sku_key = derive_mirror_sku_key(product_key)
    offer_id = derive_mirror_offer_id(product_key)
    # Offer typing by SELLER IDENTITY (Fix Plan C), not just the ingest lane. A
    # crawl seed with seed_kind='self' is the brand selling its OWN product on its
    # OWN storefront (D2C) -> brand_direct / first-party. But we also honour the
    # domain: a KNOWN-retailer host (ulta.com …) is always 'retailer' even if the
    # seed was mislabelled 'self', and a self-seed keeps brand_direct only when the
    # domain isn't a retailer. `is_first_party` marks brand-ownership of the offer
    # and is orthogonal to the referral fulfillment tier above — an external
    # self-seed is still redirect-fulfilled.
    evidence_domain = (
        str(row_dict.get("domain") or "").strip()
        or host_from_url(row_dict.get("canonical_url"))
        or host_from_url(row_dict.get("destination_url"))
    )
    is_self_seed = str(row_dict.get("seed_kind") or "").strip().lower() == "self"
    if evidence_domain and is_known_retailer(evidence_domain):
        # Retailer host is authoritative that this is a third-party offer.
        offer_type_value = "retailer"
        is_first_party_value = False
    elif is_self_seed:
        offer_type_value = "brand_direct"
        is_first_party_value = True
    else:
        offer_type_value = None
        is_first_party_value = False
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
           catalog_track, truth_tier, readiness_tier,
           offer_type, is_first_party, offer_mode,
           channel, availability, inventory_quantity, currency,
           list_price, merchant_effective_price, estimated_best_price,
           price_confidence, source_system, source_ref, source_domain,
           offer_payload)
        VALUES
          (:offer_id, :sku_key, :product_key, :merchant_id,
           :catalog_track, :truth_tier, :readiness_tier,
           :offer_type, :is_first_party, :offer_mode,
           :channel, :availability, :inventory_quantity, :currency,
           :list_price, :merchant_effective_price, :estimated_best_price,
           :price_confidence, :source_system, :source_ref, :source_domain,
           CAST(:offer_payload AS jsonb))
        ON CONFLICT (offer_id) DO UPDATE SET
          -- A KNOWN-retailer host is AUTHORITATIVE third-party evidence, so it
          -- corrects a wrongly-stored value (demotes a bad brand_direct/first-party):
          -- when EXCLUDED.offer_type='retailer' it wins. Otherwise we only FILL a
          -- NULL offer_type (COALESCE) and keep is_first_party sticky — a brand_direct
          -- or unknown never clobbers what onboard already set. Real corrections
          -- away from retailer are the backfill's job, not this ingest upsert.
          offer_type = CASE
            WHEN EXCLUDED.offer_type = 'retailer' THEN 'retailer'
            ELSE COALESCE(catalog_offers.offer_type, EXCLUDED.offer_type)
          END,
          is_first_party = CASE
            WHEN EXCLUDED.offer_type = 'retailer' THEN FALSE
            ELSE catalog_offers.is_first_party OR EXCLUDED.is_first_party
          END,
          availability = EXCLUDED.availability,
          inventory_quantity = EXCLUDED.inventory_quantity,
          -- FILL-ONLY (COALESCE keeps the stored value): this closes the audit
          -- blind spot for new/refreshed rows without letting a re-seed clobber a
          -- domain some other pass resolved more authoritatively. See below for
          -- why writing it at all is the fix.
          source_domain = COALESCE(catalog_offers.source_domain, EXCLUDED.source_domain),
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
            "offer_type": offer_type_value,
            "is_first_party": is_first_party_value,
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
            # THE AUDIT BLIND SPOT, closed at the writer. This path never wrote
            # source_domain, so its offers were invisible to
            # scripts/audit_offer_currency.py — which scans live source_domains
            # against each storefront's /meta.json. MEASURED on prod 2026-07-27:
            # 4,718 of 18,809 live offers carry no source_domain, and 2,727 of
            # those cannot be reached even through the backfill's attached-seed
            # fallback. Those 2,727 are currency-unauditable.
            #
            # The value was already in hand — `domain` is in _SEED_OFFER_COLUMNS
            # and is written into offer_payload two lines above — so this is
            # provenance we were fetching, using, and then discarding at the
            # column that the audit actually reads. No new network call, no new
            # query: the only reason it was missing is that nobody wired it.
            #
            # Deliberately NOT derived: currency stays as the seed reports it.
            # Resolving the store's real currency needs a live /meta.json fetch,
            # and putting the network in this write path is the design prior
            # reviews blocked (this function rides seed-write paths and must
            # never raise). The founder chose the enrichment-pass design instead
            # — scripts/backfill_offer_market_currency.py + its weekly cron —
            # and writing source_domain here is precisely what lets that pass key
            # on a real domain rather than guess through a seed join.
            "source_domain": _clean_domain(row_dict.get("domain")),
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

        mirror = await resolve_mirror_product(seed_id)
        if not mirror:
            # The mirror hasn't materialized this seed's product yet (or under a
            # different provenance); the batch job owns product creation. Skip.
            return {"seed_id": seed_id, "status": "no_mirror_product"}

        product_key = mirror["product_key"]
        await upsert_catalog_offer_from_seed_row(
            product_key, seed, merchant_id=mirror["merchant_id"]
        )
        return {"seed_id": seed_id, "status": "synced", "product_key": product_key}
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "event": "external_offer_dual_write_failed",
            "seed_id": seed_id,
            "error": str(exc),
        })
        return {"seed_id": seed_id, "status": "error", "error": str(exc)}

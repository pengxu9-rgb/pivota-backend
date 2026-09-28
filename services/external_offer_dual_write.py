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
import time
from decimal import Decimal
from typing import Any, Dict, Optional

from db.database import database
from services.offer_seller_identity import host_from_url, is_known_retailer
from services.storefront_currency import normalize_domain, plausible_domain

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

# THE WRITER'S OWN VOCABULARY, exported so a consumer never has to guess it. `sync_offer_for_seed`
# returns exactly one of: no_seed_id, disabled, seed_missing, no_external_product_id,
# no_mirror_product, synced, error -- and, for an attached seed with no mirror
# (`sync_attached_listing_offers`): no_listing_offer, listing_offer_suppressed,
# ambiguous_listing_seller, currency_not_read, listing_offer_not_written. Only `synced` means a row was written; its
# `target` says which (`mirror` or `attached`).
#
# This exists because the refresh hook first hardcoded {"synced","inserted","updated","ok"} —
# three of which this function cannot emit, and "ok" is what its own tests stubbed. Deleting
# "synced" from that set would have left projections_written at 0 every night while writes were
# landing, degrading the run and failing the job nightly. Import these instead of restating them.
OFFER_SYNC_WRITTEN_STATUSES = frozenset({"synced"})
OFFER_SYNC_ERROR_STATUSES = frozenset({"error"})
# NOTHING TO WRITE, AND NOTHING WRONG. The seed has no offer row this writer may touch: no mirror
# product (unattached, not materialised yet) or, for an attached seed, no live offer on the
# canonical for the listing it reads (none, or every one suppressed by a human or a quarantine). A night of these is the shape of the catalogue, not a failed write, so
# the batch leaves them out of "a projection that should have written wrote nothing" -- on 09-27
# `no_mirror_product` 2,220/2,220 failed the job while every one of them was this.
OFFER_SYNC_STRUCTURAL_SKIP_STATUSES = frozenset(
    {"no_mirror_product", "no_listing_offer", "listing_offer_suppressed"}
)
OFFER_CHANNEL = "external_referral"
OFFER_SOURCE_SYSTEM = "external_product_seeds_mirror_v1"
OFFER_PRICE_CONFIDENCE = Decimal("0.6")

SKU_SUFFIX = "::canonical"
OFFER_ID_PREFIX = "offer:external_seed:"


def dual_write_enabled() -> bool:
    """Flag: run the synchronous seed→offer dual-write on seed writes.

    Default OFF.

    THE "inert to live serving" CLAIM THIS DOCSTRING USED TO MAKE IS FALSE, and believing it
    cost roughly a quarter of the catalogue its price accuracy. `catalog_offers` IS read on two
    live surfaces: the PDP, via `agent_pdp_view_assembler.fetch_offers_for_keys` →
    `normalize_offer`, and the index's `serving_eligible.has_price` gate, via
    `index_pipeline_state_service` → `priced_offer_sql`. Only the search/offers lane reads the
    seed directly. So with this flag off, the nightly external-referral refresh updated the seed
    and the PDP kept quoting whatever `catalog_offers` was mirrored with — measured on prod
    2026-09-06, **1,321 of 5,316 live products (25%) showed a different price on the PDP than in
    search**, 917 of them with a seed read inside 7 days.

    It is still default OFF because arming it is a deploy-time decision (it adds a write to
    every seed-write path), but "dark" now means "not yet armed", NOT "harmless to leave off".
    Flip EXTERNAL_OFFER_DUAL_WRITE_ENABLED=1 on web, worker and the
    external-referral-refresh job together — arming it on only some of them leaves exactly the
    split-brain above.
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


# ── price_checked_at (migration 246) is never a precondition for writing a price ────────────────
#
# Prod boots skip db/migrations; the column arrives by schema_guard's heal, which takes the table's
# lock under a 500ms lock_timeout and waits for the next boot when a bulk writer holds it. A write
# that named a missing column would fail UndefinedColumn, `sync_offer_for_seed` would swallow it as
# status=error, and prices would silently stop moving. So each writer asks first and, while the
# column is absent, runs the statement it ran before 246, which is derived from the one below by
# cutting exactly the stamp (`_without_price_check`), so the two cannot drift.
PRICE_CHECK_COLUMN_SQL = """
SELECT EXISTS (
  SELECT 1 FROM pg_attribute
  WHERE attrelid = to_regclass('catalog_offers')
    AND attname = 'price_checked_at'
    AND attnum > 0
    AND NOT attisdropped
) AS present
"""
# Absent is re-asked at most this often; present is final (the column is never dropped in service).
PRICE_CHECK_RECHECK_SECONDS = 60.0
_price_check_column: Dict[str, Any] = {"present": False, "checked": None}


async def price_check_column_present() -> bool:
    """Does catalog_offers carry price_checked_at yet? Never raises; a failed read is "absent"."""
    if _price_check_column["present"]:
        return True
    now = time.monotonic()
    checked = _price_check_column["checked"]
    if checked is not None and now - checked < PRICE_CHECK_RECHECK_SECONDS:
        return False
    _price_check_column["checked"] = now
    try:
        row = await database.fetch_one(PRICE_CHECK_COLUMN_SQL)
        present = bool(row and row["present"])
    except Exception:  # noqa: BLE001
        present = False
    _price_check_column["present"] = present
    return present


def _without_price_check(sql: str, cuts: tuple) -> str:
    """`sql` with each of `cuts` (old, new) applied exactly once, and no price_checked_at left."""
    for old, new in cuts:
        if sql.count(old) != 1:
            raise RuntimeError(f"statement changed shape; update its price_checked_at cut: {old[:60]!r}")
        sql = sql.replace(old, new)
    if "price_checked_at" in sql or ":price_read" in sql:
        raise RuntimeError("price_checked_at survived the cut")
    return sql


MIRROR_OFFER_UPSERT_SQL = """
        INSERT INTO catalog_offers
          (offer_id, sku_key, product_key, merchant_id,
           catalog_track, truth_tier, readiness_tier,
           offer_type, is_first_party, offer_mode,
           channel, availability, inventory_quantity, currency,
           list_price, merchant_effective_price, estimated_best_price,
           price_confidence, source_system, source_ref, source_domain,
           offer_payload, price_checked_at)
        VALUES
          (:offer_id, :sku_key, :product_key, :merchant_id,
           :catalog_track, :truth_tier, :readiness_tier,
           :offer_type, :is_first_party, :offer_mode,
           :channel, :availability, :inventory_quantity, :currency,
           :list_price, :merchant_effective_price, :estimated_best_price,
           :price_confidence, :source_system, :source_ref, :source_domain,
           CAST(:offer_payload AS jsonb),
           CASE WHEN CAST(:price_read AS BOOLEAN) THEN NOW() END)
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
          currency = EXCLUDED.currency,
          list_price = EXCLUDED.list_price,
          merchant_effective_price = EXCLUDED.merchant_effective_price,
          estimated_best_price = EXCLUDED.estimated_best_price,
          price_confidence = EXCLUDED.price_confidence,
          -- Fill-only: a source_domain already on the row (ingest-written or
          -- audit-backfilled) is never clobbered — same correct-only posture as
          -- the currency backfill. A later seed-domain edit is the audit's job
          -- to reconcile, not this ingest upsert's.
          source_domain = COALESCE(catalog_offers.source_domain, EXCLUDED.source_domain),
          offer_payload = EXCLUDED.offer_payload,
          price_checked_at = CASE
            WHEN CAST(:price_read AS BOOLEAN) THEN NOW()
            ELSE catalog_offers.price_checked_at
          END,
          updated_at = NOW()
"""
MIRROR_OFFER_UPSERT_SQL_WITHOUT_PRICE_CHECK = _without_price_check(MIRROR_OFFER_UPSERT_SQL, (
    ("offer_payload, price_checked_at)", "offer_payload)"),
    ("CAST(:offer_payload AS jsonb),\n           CASE WHEN CAST(:price_read AS BOOLEAN) THEN NOW() END)",
     "CAST(:offer_payload AS jsonb))"),
    ("          price_checked_at = CASE\n            WHEN CAST(:price_read AS BOOLEAN) THEN NOW()\n"
     "            ELSE catalog_offers.price_checked_at\n          END,\n", ""),
))


async def upsert_catalog_offer_from_seed_row(
    product_key: str,
    row_dict: Dict[str, Any],
    *,
    merchant_id: str,
    price_read: bool = False,
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

    `price_read` is the caller vouching that the seed's price and currency were
    just read from the served page (see `sync_offer_for_seed`), and only then is
    `price_checked_at` stamped. Otherwise the row keeps its stamp, and migration
    246's trigger forgets it if this write moves the price.
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
    # source_domain makes the offer visible to the domain-keyed machinery
    # (scripts/audit_offer_currency.py's scan keys on this column, and the
    # trust upserter's `domain`-quarantine matching reaches offers through it
    # directly rather than via its eps.domain fallback). It was never written
    # on this path, which produced the 4,705-offer audit blind spot — every
    # mirrored offer was invisible to the weekly currency audit. Each seed
    # field is tried independently: a junk `domain` value ('N/A') must not
    # shadow a perfectly good canonical/destination URL host, and a host that
    # fails the plausibility gate is dropped rather than written forever
    # (the ON CONFLICT below is fill-only, so junk would be permanent). None
    # when the seed carries no domain evidence — never fabricated.
    source_domain_value = None
    for candidate in (
        row_dict.get("domain"),
        row_dict.get("canonical_url"),
        row_dict.get("destination_url"),
    ):
        host = normalize_domain(candidate)
        if plausible_domain(host):
            source_domain_value = host
            break
    with_check = await price_check_column_present()
    await database.execute(
        MIRROR_OFFER_UPSERT_SQL if with_check else MIRROR_OFFER_UPSERT_SQL_WITHOUT_PRICE_CHECK,
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
            "source_domain": source_domain_value,
            "offer_payload": json.dumps(
                offer_payload, ensure_ascii=False, default=_json_default
            ),
            # Only the statement that names price_checked_at binds it.
            **({"price_read": bool(price_read)} if with_check else {}),
        },
    )


_SEED_OFFER_COLUMNS = (
    "id, external_product_id, destination_url, canonical_url, domain, "
    "price_amount, price_currency, availability, market, attached_product_key, "
    # Only what the attached lane reads of seed_data, never the whole document.
    "seed_data -> 'variants' AS seed_variants, "
    "seed_data -> 'snapshot' -> 'variants' AS snapshot_variants, "
    "seed_data -> 'snapshot' -> 'variant_refresh' ->> 'status' AS variant_refresh_status"
)

# ── the attached lane ────────────────────────────────────────────────────────────────────────────
#
# THE GAP. `sync_offer_for_seed` found a seed's offer only through its MIRROR product
# (catalog_products.source_ref = seed id). The 13,114 served seeds the enrichment agent attached to
# its own `ext:` canonical have none -- that product's source_ref is the agent's -- so every
# refresh of them ended `no_mirror_product` (09-27: 2,220 of 2,220 origin reads) while the canonical
# kept the price the agent captured at ingest. The PDP (`agent_pdp_view`) and the serving price
# gate read that row. PIVOTA-Agent #2215 is one of them: seed S$28.80 re-read, offer S$28.20 served.
#
# THE ROWS. A price belongs to a LISTING (one seller's page), not to a product and not to a seller:
# one seller can list 30 ml and 50 ml on two URLs of the same canonical. So the rows this seed may
# write are the canonical's offers for the seed's own destination -- `source_ref` is the listing URL
# (enrichment, `ingestion._build_offer_inserts`) or the seed id (the mirror and the variant
# backfill), and `offer_payload` carries the same pair. Measured 2026-09-28 over 19,808 served
# attached seeds: 19,782 have such rows and 26 have none; 18,278 have exactly one product-level
# (`<pk>::canonical`) row. Seller keying (ADR-009 D1: offer = product x seller) is the row's own:
# product_key, merchant_id, offer_id and sku_key are never written, only the price on rows that
# already name this listing, and a listing whose rows disagree on the seller writes nothing.
#
# WHICH PRICE. The product-level row takes the seed's price; a variant row takes the seed variant's
# OWN price (`ingestion.variant_own_price`, never the product's), matched on the merchant's variant
# id (`catalog_skus.source_variant_id`), and only when the refresh re-read every stored variant
# (`snapshot.variant_refresh.status == 'all_re_read'`). A sibling the page did not list keeps its
# row and its clock.
#
# CURRENCY = MARKET. Nothing here writes `currency` or `market`: a row takes a number only in the
# currency it already declares, so this lane can never create or move a market/currency pair (the
# at-rest check in services/catalog_invariant_checks owns rows that disagree). A seed read in USD
# never lands on a GBP row -- 17 served product-level rows are exactly that and are refused.
#
# WHO MAY ASK, and what each source vouches for (`ATTACHED_PRICE_SOURCES`):
#   * `refresh`: the nightly/per-seed refresh, after a fetch that read the served product and
#     re-read its price. Every row is priced, the variants only when all were re-read. It must
#     also have READ the currency (`evidence.price_currency_source == 'page'`).
#     `resolve_external_offer` used to substitute the market's currency when the page named
#     none ('market_default'), and a refresh of a USD seed then accepted a KRW number as
#     dollars. It now stores no price at all ('unread'); older snapshots still carry
#     'market_default'. Anything but 'page' is refused whole.
#   * `employee_edit`: an employee changed the price or currency on the PATCH route. Only the
#     product-level row moves; the variants in the seed are whatever the last refresh left, so
#     they are not the employee's claim.
# Anyone else (seed_data_writer's merge, the mirror reconciler) rewrites seed_data without a
# price read and never reaches this lane: `updated_at = NOW()` would claim a read nobody made.
#
# PRICE SANITY. This lane writes the PDP's price, and the read that feeds it was lossy: the old
# `_parse_price` kept digits and dots only, so a page's "28,80" became 2880 (utils/crawled_price
# now reads it right or refuses it). The band stays as a backstop -- and it also means a row
# ALREADY holding such a 100x price refuses the corrected read; that is a correction-pass job,
# not the refresh's. A refresh-sourced price outside [current / R, current * R] of the row it would replace
# (R = EXTERNAL_OFFER_PROJECTION_MAX_PRICE_RATIO, default 3) is refused, counted and logged for a
# human. An employee's edit is not bounded: correcting exactly such a 100x row is what it is for.
ATTACHED_PRICE_SOURCES = frozenset({"refresh", "employee_edit"})


def price_was_read(source: Optional[str], currency_read: bool) -> bool:
    """Does the caller vouch for a price AND currency read now, so `price_checked_at` may say so?

    The attached lane's own rule, for the mirror row too: an employee's edit, or a refresh whose
    page named the currency. A refresh that fell back to the market's currency read a number but
    not what it is in, and the seed_data merges and the reconciler pass no source at all."""
    return source == "employee_edit" or (source == "refresh" and bool(currency_read))
_DEFAULT_MAX_PRICE_RATIO = 3.0


def max_price_ratio() -> float:
    raw = (os.getenv("EXTERNAL_OFFER_PROJECTION_MAX_PRICE_RATIO") or "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_MAX_PRICE_RATIO
    except ValueError:
        return _DEFAULT_MAX_PRICE_RATIO
    # A ratio at or under 1 would refuse every change; treat it as a typo, not a policy.
    return value if value > 1 else _DEFAULT_MAX_PRICE_RATIO


ATTACHED_LISTING_OFFERS_SQL = """
SELECT o.offer_id, o.sku_key, o.merchant_id, o.currency, o.source_ref,
       o.list_price, o.merchant_effective_price,
       o.offer_payload ->> 'destination_url' AS payload_destination_url,
       o.offer_payload ->> 'external_seed_id' AS payload_seed_id,
       o.suppressed_at IS NOT NULL AS suppressed,
       sk.source_variant_id
FROM catalog_offers o
LEFT JOIN catalog_skus sk ON sk.sku_key = o.sku_key
WHERE o.product_key = :product_key
"""

# Guarded on what the row was when read: a concurrent writer that moved the currency or suppressed
# the offer since wins, and the UPDATE matches nothing.
ATTACHED_LISTING_OFFER_UPDATE_SQL = """
UPDATE catalog_offers
SET list_price = :price,
    merchant_effective_price = :price,
    estimated_best_price = :price,
    -- Every write this lane makes is a vouched read (ATTACHED_PRICE_SOURCES), so it dates the price.
    price_checked_at = NOW(),
    updated_at = NOW()
WHERE offer_id = :offer_id
  AND upper(trim(coalesce(currency, ''))) = :currency
  AND suppressed_at IS NULL
RETURNING offer_id
"""
ATTACHED_LISTING_OFFER_UPDATE_SQL_WITHOUT_PRICE_CHECK = _without_price_check(ATTACHED_LISTING_OFFER_UPDATE_SQL, (
    ("    -- Every write this lane makes is a vouched read (ATTACHED_PRICE_SOURCES), so it dates the price.\n"
     "    price_checked_at = NOW(),\n", ""),
))


def _json_value(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _served_seed_variants(seed: Dict[str, Any]) -> list:
    """The variant list the builder serves: top-level `variants`, else `snapshot.variants`
    (the same container rule the refresh reconciles, routes/employee_products)."""
    top = _json_value(seed.get("seed_variants"))
    if isinstance(top, list):
        return [v for v in top if isinstance(v, dict)]
    snap = _json_value(seed.get("snapshot_variants"))
    if isinstance(snap, list):
        return [v for v in snap if isinstance(v, dict)]
    return []


def is_listing_offer(offer: Dict[str, Any], seed: Dict[str, Any]) -> bool:
    """Is this canonical offer row the seed's own listing? An exact id or a same-destination URL
    (services/seed_served_url, the refresh's own rule), never a host or a seller alone."""
    from services.seed_served_url import same_destination

    seed_id = str(seed.get("id") or "")
    if seed_id and seed_id in (offer.get("source_ref"), offer.get("payload_seed_id")):
        return True
    dest = seed.get("destination_url")
    return bool(dest) and any(
        candidate and same_destination(candidate, dest)
        for candidate in (offer.get("source_ref"), offer.get("payload_destination_url"))
    )


def _current_price(offer: Dict[str, Any]) -> Optional[float]:
    for key in ("merchant_effective_price", "list_price"):
        price = _positive_price(offer.get(key))
        if price is not None:
            return price
    return None


def plan_attached_listing_offer_writes(
    seed: Dict[str, Any], offers: list, *, source: str = "refresh", currency_read: bool = False,
    max_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    """Pure: which of the canonical's offer rows take which price. No IO, so every refusal is
    testable on the rows the SELECT returns.

    Returns {"status", "writes": [{offer_id, price, currency}], "skips": {reason: n},
    "refused": [row detail for review]}. Status is `no_listing_offer` / `listing_offer_suppressed`
    (nothing this seed may write: structural), `ambiguous_listing_seller`, `currency_not_read`,
    or `planned` (possibly with zero writes; the caller reports why).
    """
    from services.catalog_enrichment_agent.ingestion import variant_own_price

    product_key = str(seed.get("attached_product_key") or "")
    listing = [o for o in offers if is_listing_offer(o, seed)]
    if not listing:
        return {"status": "no_listing_offer", "writes": [], "skips": {}, "refused": []}
    live = [o for o in listing if not o.get("suppressed")]
    if not live:
        return {"status": "listing_offer_suppressed", "writes": [], "skips": {}, "refused": []}
    sellers = {str(o.get("merchant_id") or "") for o in live}
    if len(sellers) != 1 or "" in sellers or "external_seed" in sellers:
        # One listing is one seller. Rows that disagree (or name ADR-009's banned bucket) are not
        # a row this lane can vouch for.
        return {"status": "ambiguous_listing_seller", "writes": [], "skips": {}, "refused": []}
    if source == "refresh" and not currency_read:
        # The page named no currency and the reader filled in the market's. Not a reading.
        return {"status": "currency_not_read", "writes": [], "skips": {}, "refused": []}

    ratio = max_ratio if max_ratio is not None else max_price_ratio()
    currency = str(seed.get("price_currency") or "").strip().upper()
    all_variants_re_read = (
        source == "refresh" and str(seed.get("variant_refresh_status") or "") == "all_re_read"
    )
    variant_prices: Dict[str, Optional[float]] = {}
    for variant in _served_seed_variants(seed):
        vid = str(variant.get("variant_id") or variant.get("id") or "").strip()[:128]
        if vid:
            variant_prices.setdefault(vid, variant_own_price(variant))

    writes = []
    refused = []
    skips: Dict[str, int] = {}

    def _skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    for offer in live:
        if not currency or str(offer.get("currency") or "").strip().upper() != currency:
            _skip("currency_mismatch")
            continue
        if offer.get("sku_key") == f"{product_key}{SKU_SUFFIX}":
            price = _positive_price(seed.get("price_amount"))
            if price is None:
                _skip("no_seed_price")
                continue
        else:
            vid = str(offer.get("source_variant_id") or "").strip()
            if not vid or vid not in variant_prices:
                _skip("variant_not_on_seed")
                continue
            if not all_variants_re_read:
                _skip("variant_not_re_read" if source == "refresh" else "variant_not_edited")
                continue
            price = variant_prices[vid]
            if price is None:
                _skip("no_variant_price")
                continue
        current = _current_price(offer)
        if source == "refresh" and current is not None and not (current / ratio <= price <= current * ratio):
            _skip("price_ratio_out_of_bounds")
            refused.append({"offer_id": offer["offer_id"], "sku_key": offer.get("sku_key"),
                            "current": current, "read": price, "currency": currency})
            continue
        writes.append({"offer_id": offer["offer_id"], "price": price, "currency": currency})
    return {"status": "planned", "writes": writes, "skips": skips, "refused": refused}


def _positive_price(value: Any) -> Optional[float]:
    try:
        price = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return price if price is not None and price > 0 else None


async def sync_attached_listing_offers(
    seed: Dict[str, Any], *, source: str, currency_read: bool = False
) -> Dict[str, Any]:
    """Write a seed's vouched price onto its listing's offer rows on the attached canonical.

    `synced` (target `attached`) when at least one row was written. Otherwise the plan's status,
    or `listing_offer_not_written` with the per-row reasons. Raises nothing the caller does not
    already catch (`sync_offer_for_seed`)."""
    seed_id = seed.get("id")
    product_key = str(seed.get("attached_product_key") or "")
    rows = await database.fetch_all(ATTACHED_LISTING_OFFERS_SQL, {"product_key": product_key})
    plan = plan_attached_listing_offer_writes(
        seed, [dict(r) for r in rows or []], source=source, currency_read=currency_read
    )
    if plan["status"] == "currency_not_read":
        logger.warning({
            "event": "external_offer_attached_refused",
            "reason": "currency_not_read",
            "seed_id": seed_id,
            "product_key": product_key,
        })
    for refused in plan.get("refused") or []:
        # WARNING, not INFO: prod drops module-logger INFO. One line per refused row, for review.
        logger.warning({
            "event": "external_offer_attached_refused",
            "reason": "price_ratio_out_of_bounds",
            "seed_id": seed_id,
            "product_key": product_key,
            **refused,
        })
    if plan["status"] != "planned":
        return {"seed_id": seed_id, "status": plan["status"], "product_key": product_key}
    written = 0
    skips = dict(plan["skips"])
    update_sql = (
        ATTACHED_LISTING_OFFER_UPDATE_SQL
        if await price_check_column_present()
        else ATTACHED_LISTING_OFFER_UPDATE_SQL_WITHOUT_PRICE_CHECK
    )
    for write in plan["writes"]:
        # RETURNING, because `execute` reports no row count: a row the guard no longer matches
        # (currency moved, offer suppressed since the read) is a skip, not a write.
        if await database.fetch_one(update_sql, write):
            written += 1
        else:
            skips["changed_since_read"] = skips.get("changed_since_read", 0) + 1
    result = {
        "seed_id": seed_id,
        "product_key": product_key,
        "target": "attached",
        "offers_written": written,
        "offer_skips": skips,
    }
    if written:
        return {**result, "status": "synced"}
    return {**result, "status": "listing_offer_not_written"}


async def sync_offer_for_seed(
    seed_id: str, *, attached_price_source: Optional[str] = None, currency_read: bool = False
) -> Dict[str, Any]:
    """Re-project one external seed's catalog_offers row from its current state.

    Best-effort + idempotent + NEVER raises — it rides on seed-write paths and
    must not break them. No-op (skipped) when the flag is off, the seed is gone,
    it has no external_product_id, or it has neither a mirror product (the
    materialization job owns product creation) nor an attached canonical. A seed
    with a mirror upserts the mirror's offer; an attached seed without one prices
    its listing's existing rows on the canonical (`sync_attached_listing_offers`).
    Returns a small status dict.

    `attached_price_source` is the caller vouching that the seed's price is
    CURRENT, and how: `refresh` (re-read from the served page this run;
    `currency_read` says the page named the currency) or `employee_edit` (an
    employee set the price). See ATTACHED_PRICE_SOURCES. The attached lane
    stamps the listing's rows `updated_at` and `price_checked_at` with NOW(), and
    the mirror upsert stamps `price_checked_at` on the same vouching
    (`price_was_read`). A caller that merely
    rewrote seed_data (seed_data_writer's merge, the mirror reconciler) would
    claim a freshness nobody earned, so those pass nothing and keep the
    mirror-only behaviour: `no_mirror_product` for an attached seed.
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
            if attached_price_source in ATTACHED_PRICE_SOURCES and seed.get("attached_product_key"):
                # Attached to a canonical another lane built (the enrichment agent's `ext:`
                # products): there is no mirror to upsert, but the canonical carries this
                # listing's offer rows, and those are what the PDP reads.
                return await sync_attached_listing_offers(
                    seed, source=attached_price_source, currency_read=currency_read
                )
            # The mirror hasn't materialized this seed's product yet (or under a
            # different provenance); the batch job owns product creation. Skip.
            return {"seed_id": seed_id, "status": "no_mirror_product"}

        product_key = mirror["product_key"]
        await upsert_catalog_offer_from_seed_row(
            product_key, seed, merchant_id=mirror["merchant_id"],
            price_read=price_was_read(attached_price_source, currency_read),
        )
        return {"seed_id": seed_id, "status": "synced", "product_key": product_key, "target": "mirror"}
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "event": "external_offer_dual_write_failed",
            "seed_id": seed_id,
            "error": str(exc),
        })
        return {"seed_id": seed_id, "status": "error", "error": str(exc)}

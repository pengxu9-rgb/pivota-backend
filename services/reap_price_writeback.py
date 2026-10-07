"""A Reap quote's live price corrects OUR catalog, so a buyer told "the price is now X" can buy at X.

WHY (owner decision 2026-10-06, PR 2 of the price witness). On `price_changed` the agent re-reads
`get_product` and re-creates with `expected_unit_price_minor = X`. The gateway compares X with the
CATALOG price, and the backend prices the purchase from the catalog (`_load_cart_link_item`).
While the catalog still says the old price, the re-create is refused, and re-creating at the old
price is refused by the next quote: a dead end until the nightly refresh happens to re-read the
product. This pass closes that loop from evidence the purchase step already recorded
(`live_unit_price_minor`, mig 258). Nothing here talks to the network.

WHAT MAY BE WRITTEN (the owner's rules):

  * An INCREASE is written on the quote alone. Reap's quote echoes no variant, but it reads the
    cart URL WE built, which names exactly one Shopify variant, and its quote id is bound to our
    request. The worst case is a buyer asked to approve a higher price.
  * A DECREASE is written only when our own independent store read of exactly this variant says
    the same price (`reap_price_corroboration.independent_unit_price`, the rule `_settle_price_change`
    applies at approval). A cheaper price on Reap's word alone is how a substituted variant would
    start passing the exact-subtotal check silently.
  * ENRICHMENT-lane offers are written only when our fresh enrichment proof of the variant already
    says the same price, in either direction: the purchase route requires the listing's offers to
    EQUAL that proof (`enrichment_offer_price_ok`), so an offer moved past the proof would make the
    product unbuyable. The proof table is NEVER written from a quote: corroboration depends on it
    being our own read.

WHAT IS WRITTEN, per lane -- exactly where the two price readers look:

  * MIRROR (`external_product_seeds_mirror_v1`): the seller's offers on the variant's own skus (the
    route's `_cart_sku_choice`) and, when the storefront proof shows one live variant, on the
    `::canonical` placeholder -- the rows `_load_cart_link_item` prices from; and the seed's own
    variant price in `seed_data` (+ `price_amount` when the seed lists only this variant) -- what
    the gateway's `get_product` and create check read.
  * ENRICHMENT: the listing's own offers on the proof's sku spellings (the route's selection).

GUARDS (review of #2519):
  * THE ROUTE MUST BE PRICING THIS VARIANT, before and after: the route's own loader is run and
    its variant must be the purchase's (a placeholder whose proof now names another shade is not
    this observation's row). Mirror rows it refuses are left alone; enrichment rows may only be
    refused as `row_price_stale` (offers behind our proof -- the case this heals).
  * NEWEST QUOTE WINS, by quote time (`observed_at`), never `updated_at`; a seed that already holds
    a write from a newer quote (`snapshot.price_writeback.observed_at`) or a crawl after the quote
    (`last_crawled_at`) is left alone.
  * COMPARE-AND-SET IN SQL: offers on the old price in integer minor units, the seed on the
    document as read; any row that moved rolls the whole write back (`raced`).
  * CHECKED AT THE SINK: after the write the route must price the purchase at the live price, else
    `written_not_effective`.

DIAL `REAP_AGENTIC_PRICE_WRITEBACK` = off (default) | shadow (decide and log, write nothing) | on.
Run from the poll job (jobs/reap_agentic_purchase_poll.py) after its own work, bounded.

LATENCY. The gateway caches mirror product detail per instance (PRODUCT_DETAIL_CACHE, 10 min), so
`get_product` can show the old price for up to that long after a write.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import db.reap_agentic_ledger as ledger
import services.reap_price_corroboration as corroboration
from db.database import IS_POSTGRES, database
from services.reap_webhooks import minor_to_major

logger = logging.getLogger(__name__)

REAP_AGENTIC_PRICE_WRITEBACK_ENV = "REAP_AGENTIC_PRICE_WRITEBACK"
WRITEBACK_MODES = ("off", "shadow", "on")
#: How far back an observation is still worth acting on. After this the nightly refresh owns it.
WINDOW = timedelta(hours=72)
#: Observations acted on per pass, newest QUOTE first; one per (product, variant).
BATCH_LIMIT = 20
#: Rows read to find them. `updated_at` (the SQL bound) moves on every claim and transition, so the
#: newest quote is chosen in Python from a wider read, never from SQL order.
SCAN_LIMIT = 200
MIRROR_SOURCE_SYSTEM = "external_product_seeds_mirror_v1"
#: Where each write came from, recorded on the seed for review and for the next reader.
SOURCE = "reap_quote"


def writeback_mode() -> str:
    value = (os.getenv(REAP_AGENTIC_PRICE_WRITEBACK_ENV) or "").strip().lower()
    return value if value in WRITEBACK_MODES else "off"


# ── the observations ─────────────────────────────────────────────────────────────────────────

#: Cart-link purchases whose quote named a live price we did not have: refused `price_changed`
#: (the buyer must re-confirm), or continued on a corroborated lower price (a rebind).
#: `updated_at >= since` is only a COARSE bound: it is never earlier than the quote, so it drops
#: nothing inside the window; the window and the order are applied to the quote time in Python.
_OBSERVATIONS_SQL = """
    SELECT id, product_key, variant_key, cart_url, item_source, merchant_domain, market_country,
           currency, quantity, our_price_minor, live_unit_price_minor, live_price_stage,
           price_rebound_to_minor, price_corroborated_at, preflight_checked_at, terminal_at,
           state_entered_at, updated_at
      FROM reap_agentic_purchases
     WHERE item_source = 'cart_link'
       AND live_unit_price_minor IS NOT NULL AND live_price_stage IS NOT NULL
       AND ((state = 'refused' AND refusal_reason = 'price_changed')
            OR price_rebound_to_minor IS NOT NULL)
       AND updated_at >= :since
     ORDER BY updated_at DESC, id
     LIMIT :limit
"""


def observed_at(row: Mapping[str, Any]) -> Optional[datetime]:
    """When the quote that named this price was TAKEN (never `updated_at`, which every claim moves):
      * preflight stage -- the witness's own stamp, `preflight_checked_at`;
      * approval stage, a corroborated rebind that continued -- `price_corroborated_at`, written in
        the same step as the quote (the row's later transitions are not the quote);
      * approval stage, refused -- `terminal_at`, the transition that quote caused."""
    if row.get("live_price_stage") == "preflight":
        value = row.get("preflight_checked_at")
    elif row.get("price_rebound_to_minor") is not None:
        value = row.get("price_corroborated_at")
    else:
        value = row.get("terminal_at")
    return corroboration._aware(value)


async def observations(*, now: datetime, limit: int = BATCH_LIMIT) -> List[Dict[str, Any]]:
    """The newest QUOTE per (product, variant) taken inside the window, newest first."""
    since = now - WINDOW
    rows = await database.fetch_all(
        _OBSERVATIONS_SQL, {"since": ledger._bind_dt(since), "limit": SCAN_LIMIT}
    )
    dated = []
    for row in rows:
        row = dict(row)
        observed = observed_at(row)
        if observed is None or observed < since or observed > now:
            continue
        dated.append((observed, str(row["id"]), row))
    dated.sort(key=lambda item: (item[0], item[1]), reverse=True)
    seen, newest = set(), []
    for _observed, _id, row in dated:
        key = (row.get("product_key"), corroboration.purchase_variant_id(row))
        if key in seen:
            continue
        seen.add(key)
        newest.append(row)
    return newest[:limit]


# ── the decision ─────────────────────────────────────────────────────────────────────────────


def _major(minor: int, currency: str) -> float:
    """The catalog columns' unit (major, binary float -- what the seed->offer projection binds)."""
    return float(minor_to_major(int(minor), currency))


def _same_price(value: Any, minor: int, currency: str) -> bool:
    return ledger.amount_minor_or_none(_decimal_text(value), currency) == minor


def _decimal_text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        # A binary float read back from a REAL column: its shortest repr is the stored decimal.
        return repr(value)
    return str(value)


async def _lane(product_key: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    import routes.agent_commerce_reap as route

    product = await database.fetch_one(
        "SELECT product_key, source_system, source_ref, merchant_id, content_key "
        "FROM catalog_products WHERE product_key = :pk", {"pk": product_key}
    )
    if product is None:
        return None, None
    product = dict(product)
    system = str(product.get("source_system") or "")
    if system == MIRROR_SOURCE_SYSTEM:
        return "mirror", product
    if system == route.ENRICHMENT_SOURCE_SYSTEM:
        return "enrichment", product
    return None, product


async def _priced_now(
    lane: str, row: Mapping[str, Any], *, sku_key: Optional[str]
) -> Tuple[Optional[int], str, Optional[str]]:
    """What the PURCHASE ROUTE would price this purchase at now: `(minor, reason, variant_id)`.

    The route's own loader, read-only. `reason` is its refusal code when it refuses; `variant_id`
    is the Shopify variant the route would buy -- a write is only for THIS purchase's variant."""
    import routes.agent_commerce_reap as route
    import services.reap_agentic_purchase as svc

    try:
        if lane == "enrichment":
            product = await database.fetch_one(
                route._CART_ENRICHMENT_PRODUCT_SQL,
                {"product_key": row["product_key"], "source_system": route.ENRICHMENT_SOURCE_SYSTEM},
            )
            if product is None:
                return None, "row_not_found", None
            facts, _seller, variant, _kind = await route._load_enrichment_cart_link_item(
                product=dict(product), merchant_host=str(row["merchant_domain"]),
                variant_key=sku_key, market_country=str(row["market_country"]),
            )
        else:
            facts, _seller, variant, _kind = await route._load_cart_link_item(
                merchant_domain=str(row["merchant_domain"]), product_key=str(row["product_key"]),
                variant_key=None, market_country=str(row["market_country"]),
            )
    except svc.PurchaseRefused as refused:
        return None, str(refused.reason), None
    return int(facts["our_price_minor"]), "priced", (str(variant) if variant else None)


# ── mirror ───────────────────────────────────────────────────────────────────────────────────

_MIRROR_SEED_SQL = """
    SELECT id, seed_data, price_amount, last_crawled_at
      FROM external_product_seeds
     WHERE id = :seed_id AND status = 'active' AND attached_product_key = :product_key
       AND upper(market) = :market
"""

#: This seller's usable offers on one sku in the market's currency -- the route's own filters
#: (`_CART_OFFER_SQL`), with the offer id so it can be written.
_MIRROR_OFFERS_SQL = """
    SELECT o.offer_id,
           CAST(coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price)
                AS TEXT) AS price
      FROM catalog_offers o
     WHERE o.product_key = :product_key AND o.sku_key = :sku_key
       AND o.merchant_id = :merchant_id
       AND o.suppression_reason IS NULL AND o.suppressed_at IS NULL
       AND coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) IS NOT NULL
       AND lower(coalesce(o.availability, 'unknown')) NOT IN
           ('out_of_stock', 'sold_out', 'unavailable')
       AND upper(trim(coalesce(o.currency, ''))) = :market_currency
     ORDER BY o.offer_id
     LIMIT 20
"""

#: COMPARE-AND-SET IN SQL: the row must still carry the old price, compared in integer minor units
#: (`:scale` = 10^exponent of the currency), never a float equality. No RETURNING row = it moved.
_WRITE_OFFER_SQL = """
    UPDATE catalog_offers
       SET list_price = :price, merchant_effective_price = :price, estimated_best_price = :price,
           updated_at = CURRENT_TIMESTAMP
     WHERE offer_id = :offer_id
       AND upper(trim(coalesce(currency, ''))) = :currency
       AND suppression_reason IS NULL AND suppressed_at IS NULL
       AND ROUND(coalesce(merchant_effective_price, estimated_best_price, list_price) * :scale) = :old_minor
    RETURNING offer_id
"""

#: COMPARE-AND-SET on the whole document as read: a crawl (or any writer) that changed `seed_data`
#: since the read wins, and this write lands nowhere. `updated_at` is deliberately NOT bumped: the
#: gateway serves the seed of one `external_product_id` by `updated_at DESC` across markets, so a
#: bump here could change WHICH market's seed it serves. The provenance is in
#: `snapshot.price_writeback` instead.
_WRITE_SEED_SQL = """
    UPDATE external_product_seeds
       SET seed_data = CAST(:seed_data AS jsonb), price_amount = :price_amount
     WHERE id = :id AND status = 'active' AND seed_data = CAST(:old_seed_data AS jsonb)
       AND price_amount IS NOT DISTINCT FROM :old_price_amount
    RETURNING id
"""
_WRITE_SEED_SQL_SQLITE = """
    UPDATE external_product_seeds
       SET seed_data = :seed_data, price_amount = :price_amount
     WHERE id = :id AND status = 'active' AND seed_data = :old_seed_data
       AND price_amount IS :old_price_amount
    RETURNING id
"""


_READ_OFFER_PAYLOAD_SQL = "SELECT offer_payload FROM catalog_offers WHERE offer_id = :offer_id"

_WRITE_OFFER_PAYLOAD_SQL = """
    UPDATE catalog_offers SET offer_payload = CAST(:payload AS jsonb)
     WHERE offer_id = :offer_id AND offer_payload = CAST(:old_payload AS jsonb)
    RETURNING offer_id
"""
_WRITE_OFFER_PAYLOAD_SQL_SQLITE = """
    UPDATE catalog_offers SET offer_payload = :payload
     WHERE offer_id = :offer_id AND offer_payload = :old_payload
    RETURNING offer_id
"""


class _Raced(Exception):
    """A row moved between the read and the write: roll the whole write back."""

_VARIANT_ID_KEYS = ("shopify_variant_id", "variant_id", "id")
_VARIANT_PRICE_KEYS = ("price", "price_amount")


def _variant_id_of(variant: Mapping[str, Any]) -> Optional[str]:
    ids = set()
    for key in _VARIANT_ID_KEYS:
        text = str(variant.get(key) or "").strip()
        if text:
            ids.add(text.rsplit("/", 1)[-1])
    return ids.pop() if len(ids) == 1 else None


#: Where the catalog keeps DISPLAY copies of a price beside the column the purchase route reads:
#: the gateway serves `commerce_facts_v1.regional_price` (and its `agent_safe_commerce_facts.price`
#: copy, built by PIVOTA-Agent src/commerce/commerceFacts.js) to agents with the product, and an
#: offer's payload carries `price` / `price_amount`. Named explicitly -- never "any key that looks
#: like a price".
_FACT_BLOCKS = (("commerce_facts_v1", "regional_price"), ("commerce_facts", "regional_price"),
                ("agent_safe_commerce_facts", "price"))


def _price_like(value: Any, minor: int, currency: str) -> bool:
    return value not in (None, "") and _same_price(value, minor, currency)


def _as_type_of(original: Any, major: Decimal) -> Any:
    return str(major) if isinstance(original, str) else float(major)


def move_price_facts(
    holder: Dict[str, Any], *, old_minor: int, new_minor: int, currency: str,
    observed: Optional[datetime], top_keys: Iterable[str] = (),
) -> int:
    """Pure, in place: move the display copies of the price in `holder` from the old price to the
    new one. Returns how many fields moved.

    Only a copy that still says the OLD price moves; one already at the new price, or at some third
    price (another writer's fact), is left as it is -- these are display facts, not the price the
    purchase is checked against, so they never refuse the write. `display_raw` keeps its own
    formatting with the number replaced; `captured_at` becomes the quote's time, the moment this
    price was observed."""
    old_major, new_major = minor_to_major(int(old_minor), currency), minor_to_major(int(new_minor), currency)
    moved = 0
    for key in top_keys:
        if _price_like(holder.get(key), old_minor, currency):
            holder[key] = _as_type_of(holder[key], new_major)
            moved += 1
    for outer, inner in _FACT_BLOCKS:
        block = holder.get(outer)
        price = block.get(inner) if isinstance(block, dict) else None
        if not isinstance(price, dict) or not _price_like(price.get("amount"), old_minor, currency):
            continue
        price["amount"] = _as_type_of(price["amount"], new_major)
        raw = price.get("display_raw")
        if isinstance(raw, str) and raw.strip():
            replaced = raw.replace(str(old_major), str(new_major))
            price["display_raw"] = replaced if replaced != raw else str(new_major)
        if observed is not None and "captured_at" in price:
            price["captured_at"] = observed.isoformat()
        moved += 1
    return moved


def listed_variant_ids(seed_data: Any) -> set:
    """The variant ids the seed lists (`variants`, `snapshot.variants`)."""
    document = corroboration._seed_document(seed_data) or {}
    snapshot = document.get("snapshot") if isinstance(document.get("snapshot"), dict) else {}
    lists = [v for v in (document.get("variants"), snapshot.get("variants")) if isinstance(v, list)]
    return {(_variant_id_of(v) if isinstance(v, dict) else None) for vs in lists for v in vs}


def plan_seed_write(
    seed_data: Any, *, variant_id: str, old_minor: int, new_minor: int, currency: str,
    purchase_id: str, observed: Optional[datetime], sole_live_variant: bool = False,
) -> Tuple[Optional[Dict[str, Any]], bool, str]:
    """Pure: `(new_seed_data | None, product_level, reason)`.

    Every listed copy of THIS variant (`variants`, `snapshot.variants`) takes the new price, and
    only where its price is the old one (a copy already at the new price is left as it is: an
    earlier write). `product_level` -- the seed's own price -- moves when the seed lists this one
    variant and nothing else, OR when `sole_live_variant` (the placeholder offer is being written:
    the storefront proof shows this as the one live variant, so its price IS the product's, and
    the seed->offer projection re-copies `price_amount` onto that placeholder -- leaving it behind
    would undo the write; review of #2519, round 2). A copy at some third price, or a variant the
    seed does not list, writes nothing (`seed_price_unexpected` / `variant_not_on_seed`)."""
    document = corroboration._seed_document(seed_data)
    if document is None:
        return None, False, "seed_unreadable"
    document = json.loads(json.dumps(document))
    snapshot = document.get("snapshot") if isinstance(document.get("snapshot"), dict) else {}
    lists = [v for v in (document.get("variants"), snapshot.get("variants")) if isinstance(v, list)]
    new_major = minor_to_major(int(new_minor), currency)
    touched = 0
    for variants in lists:
        for variant in variants:
            if not isinstance(variant, dict) or _variant_id_of(variant) != variant_id:
                continue
            for key in _VARIANT_PRICE_KEYS:
                if key not in variant or variant[key] in (None, ""):
                    continue
                if _same_price(variant[key], new_minor, currency):
                    touched += 1
                    continue
                if not _same_price(variant[key], old_minor, currency):
                    return None, False, "seed_price_unexpected"
                variant[key] = str(new_major) if isinstance(variant[key], str) else float(new_major)
                touched += 1
    if not touched:
        return None, False, "variant_not_on_seed"
    listed = {(_variant_id_of(v) if isinstance(v, dict) else None) for vs in lists for v in vs}
    product_level = listed == {variant_id} or bool(sole_live_variant)
    if product_level:
        for holder in (snapshot, document):
            if holder.get("price_amount") in (None, "") or _same_price(holder["price_amount"], new_minor, currency):
                continue
            if not _same_price(holder["price_amount"], old_minor, currency):
                # The product-level price is the projection's source for the placeholder: moving
                # the offer but not a holder at some third price would be undone (R2-2).
                return None, False, "seed_price_unexpected"
            holder["price_amount"] = (str(new_major) if isinstance(holder["price_amount"], str)
                                      else float(new_major))
        for holder in (snapshot, document):
            move_price_facts(holder, old_minor=old_minor, new_minor=new_minor, currency=currency,
                             observed=observed)
    snapshot["price_writeback"] = {
        "source": SOURCE, "purchase_id": purchase_id, "from_minor": int(old_minor),
        "to_minor": int(new_minor), "currency": currency,
        "observed_at": observed.isoformat() if observed else None,
    }
    document["snapshot"] = snapshot
    return document, product_level, "planned"


async def _mirror_offer_targets(row: Mapping[str, Any], product: Mapping[str, Any], seed_data: Any,
                                variant_id: str, *, old: int, new: int,
                                currency: str) -> Tuple[List[Dict[str, Any]], str, bool]:
    """The offers to write: `(offers, reason, placeholder_written)`.

    What `_load_cart_link_item` PRICES from: the variant's own skus' offers whenever there is one,
    the `::canonical` placeholder only when there is none and the storefront proof shows one live
    variant. Under that sole-variant proof the placeholder IS this variant's product-level price,
    so beside real-sku offers it is written too when it still says the old (or already the new)
    price -- else the PDP view keeps showing the old price (review of #2519 round 2, R2-1); a
    placeholder at some third price is left as it is and never blocks the write (P5)."""
    import routes.agent_commerce_reap as route
    import services.reap_agentic_purchase as svc

    product_key = str(row["product_key"])
    skus = [dict(r) for r in await database.fetch_all(
        route._CART_PRODUCT_SKUS_SQL, {"product_key": product_key})]
    try:
        sku_variant, candidates, placeholder = route._cart_sku_choice(skus, product_key)
    except svc.PurchaseRefused:
        return [], "sku_ambiguous", False
    if candidates and sku_variant != variant_id:
        return [], "sku_names_another_variant", False

    async def _offers(sku_key: str) -> List[Dict[str, Any]]:
        return [dict(o) for o in await database.fetch_all(_MIRROR_OFFERS_SQL, {
            "product_key": product_key, "sku_key": sku_key,
            "merchant_id": str(product.get("merchant_id") or ""),
            "market_currency": str(row["currency"]).upper(),
        })]

    offers: List[Dict[str, Any]] = []
    for candidate in candidates:
        if placeholder and candidate["sku_key"] == placeholder["sku_key"]:
            continue
        offers.extend(await _offers(candidate["sku_key"]))
    sole = bool(placeholder) and route._proof_live_variant_count(
        corroboration._seed_document(seed_data)) == 1
    # The placeholder is a projection of the seed's product-level price (MIRROR_OFFER_UPSERT_SQL
    # copies `price_amount` onto it). That price moves when the storefront shows one live variant OR
    # when the seed lists only this variant, so in both cases the placeholder moves with it -- else
    # the product view aggregates the old price (staging, 2026-10-07: Judydoll lists one of 8 live
    # shades; its placeholder stayed at 12.99 beside the written 13.99).
    seed_is_this_variant = bool(placeholder) and listed_variant_ids(seed_data) == {variant_id}
    if not (sole or seed_is_this_variant):
        return offers, "planned", False
    on_placeholder = await _offers(placeholder["sku_key"])
    if not offers:
        # Priced from the placeholder only under a sole proof (the route's rule).
        return (on_placeholder, "planned", bool(on_placeholder)) if sole else ([], "planned", False)
    beside = [o for o in on_placeholder
              if _same_price(o.get("price"), old, currency) or _same_price(o.get("price"), new, currency)]
    return offers + beside, "planned", bool(beside)


# ── enrichment ───────────────────────────────────────────────────────────────────────────────


async def _enrichment_offer_targets(row: Mapping[str, Any], sku_keys: Iterable[str]) -> List[Dict[str, Any]]:
    """The listing's own offers on these skus, selected exactly as the purchase route does."""
    import routes.agent_commerce_reap as route

    product = await database.fetch_one(
        route._CART_ENRICHMENT_PRODUCT_SQL,
        {"product_key": row["product_key"], "source_system": route.ENRICHMENT_SOURCE_SYSTEM},
    )
    page = route.storefront_page(dict(product).get("canonical_url")) if product is not None else None
    if page is None:
        return []
    shop_host, handle = page
    offers: List[Dict[str, Any]] = []
    for sku_key in sorted(set(sku_keys)):
        found = [dict(o) for o in await database.fetch_all(route._CART_ENRICHMENT_OFFERS_SQL, {
            "product_key": row["product_key"], "sku_key": sku_key,
            "source_system": route.ENRICHMENT_SOURCE_SYSTEM,
            "seed_merchant_prefix": route._ENRICHMENT_OFFER_MERCHANT_PREFIX,
            "seed_merchant_prefix_len": len(route._ENRICHMENT_OFFER_MERCHANT_PREFIX),
        })]
        offers.extend(o for o in found if route._enrichment_listing_offer(o, shop_host, handle))
    return offers


# ── one observation ──────────────────────────────────────────────────────────────────────────


def _scale(currency: str) -> int:
    return int(1 / minor_to_major(1, currency))


async def _write_offer_payload(offer_id: str, *, old: int, new: int, currency: str,
                               observed: Optional[datetime]) -> None:
    """The offer's payload copies of the price (`move_price_facts`), compare-and-set on the payload
    as read inside the same transaction; a payload that moved raises `_Raced`."""
    found = await database.fetch_one(_READ_OFFER_PAYLOAD_SQL, {"offer_id": offer_id})
    raw = found["offer_payload"] if found is not None else None
    payload = corroboration._seed_document(raw)
    if payload is None:
        return
    payload = json.loads(json.dumps(payload))
    if not move_price_facts(payload, old_minor=old, new_minor=new, currency=currency,
                            observed=observed, top_keys=("price", "price_amount")):
        return
    written = await database.fetch_one(
        _WRITE_OFFER_PAYLOAD_SQL if IS_POSTGRES else _WRITE_OFFER_PAYLOAD_SQL_SQLITE,
        {"offer_id": offer_id, "payload": json.dumps(payload), "old_payload": _raw_json(raw)})
    if written is None:
        raise _Raced("offer_payload")


async def _write_offers(offers: List[Mapping[str, Any]], *, old: int, new: int, currency: str,
                        any_prior: bool = False, observed: Optional[datetime] = None) -> Tuple[int, str]:
    """Each UPDATE compares, IN SQL, the price that offer was READ at; one that lands nowhere raises
    `_Raced` (roll back). Mirror (`any_prior` False): every target must have been read at the old
    (or already the new) price. Enrichment (`any_prior` True): every target is on a sku whose OWN
    fresh proof says the new price, so a prior third price is a stale offer our read corrects
    (review of #2519 round 2, R2-3)."""
    if not offers:
        return 0, "no_offer"
    # EVERY refusal comes BEFORE the first UPDATE: a return after one would commit it (the caller's
    # transaction rolls back only on an exception). Review of #2520, F1.
    if any(ledger.amount_minor_or_none(_decimal_text(o.get("price")), currency) is None for o in offers):
        return 0, "offer_price_unreadable"
    if not any_prior and any(
            not _same_price(o.get("price"), old, currency) and not _same_price(o.get("price"), new, currency)
            for o in offers):
        return 0, "offer_price_unexpected"
    written = 0
    for offer in offers:
        if _same_price(offer.get("price"), new, currency):
            continue
        read_minor = ledger.amount_minor_or_none(_decimal_text(offer.get("price")), currency)
        found = await database.fetch_one(_WRITE_OFFER_SQL, {
            "price": _major(new, currency), "offer_id": offer["offer_id"], "currency": currency,
            "scale": _scale(currency), "old_minor": int(read_minor)})
        if found is None:
            raise _Raced("offer")
        await _write_offer_payload(offer["offer_id"], old=int(read_minor), new=new, currency=currency,
                                   observed=observed)
        written += 1
    return written, "planned"


def _raw_json(value: Any) -> str:
    """The document exactly as read, for the compare-and-set (`databases` returns jsonb as text)."""
    return value if isinstance(value, str) else json.dumps(value)


#: Purchases this process already logged a shadow decision for (one WARNING each, not one a tick).
_SHADOW_LOGGED: set = set()


async def apply_one(row: Mapping[str, Any], *, mode: str, now: datetime) -> str:
    """Decide (and in `on`, write) one observation. Returns its outcome code."""
    variant_id = corroboration.purchase_variant_id(row)
    if variant_id is None:
        return "not_cart_link_variant"
    currency = str(row.get("currency") or "").strip().upper()
    live, ours = row.get("live_unit_price_minor"), row.get("our_price_minor")
    if not currency or not isinstance(live, int) or not isinstance(ours, int) or live <= 0 or live == ours:
        return "no_change"
    lane, product = await _lane(str(row["product_key"]))
    if lane is None or product is None:
        return "unsupported_lane"
    observed = observed_at(row)
    max_age = _corroboration_max_age()

    proof_skus: List[str] = []
    if lane == "enrichment":
        from db import reap_price_witness as witness

        proofs = await witness.enrichment_proofs_for_variant(str(row["product_key"]), variant_id)
        agrees = corroboration.enrichment_unit_price(
            proofs, variant_id=variant_id, shop_host=str(row["merchant_domain"]).lower(),
            currency=currency, now=now, max_age=max_age,
        )
        if agrees != live:
            return "proof_disagrees"
        # Only the skus whose OWN proof row corroborates (fresh, ok, same host and variant).
        proof_skus = sorted({
            str(p["sku_key"]) for p in proofs if p.get("sku_key") and corroboration.enrichment_unit_price(
                [p], variant_id=variant_id, shop_host=str(row["merchant_domain"]).lower(),
                currency=currency, now=now, max_age=max_age) == live})
    elif live < ours:
        found = await corroboration.independent_unit_price(row, now=now, max_age=max_age)
        if found is None or found.unit_price_minor != live:
            return "decrease_unconfirmed"

    named_sku = proof_skus[0] if len(proof_skus) == 1 else None
    # THE ROUTE MUST BE PRICING THIS PURCHASE'S VARIANT. A mirror row the route refuses, or prices
    # for another variant (a placeholder whose proof now names a different shade), is not this
    # observation's row. Enrichment may be refused only as `row_price_stale` -- offers behind our
    # own proof, exactly what this heals; its identity was proven before the price was compared.
    priced, reason, routed = await _priced_now(lane, row, sku_key=named_sku)
    if priced is not None and routed != variant_id:
        return "route_prices_another_variant"
    if priced == live:
        return "already_current"
    if priced is None and not (lane == "enrichment" and reason == "row_price_stale"):
        return "route_refuses"

    if lane == "mirror":
        seed = await database.fetch_one(_MIRROR_SEED_SQL, {
            "seed_id": str(product.get("source_ref") or ""), "product_key": row["product_key"],
            "market": str(row.get("market_country") or "").upper()})
        if seed is None:
            return "no_seed"
        seed = dict(seed)
        crawled = corroboration._aware(seed.get("last_crawled_at"))
        if crawled is not None and (observed is None or crawled > observed):
            return "catalog_read_newer"
        document = corroboration._seed_document(seed.get("seed_data")) or {}
        earlier = (document.get("snapshot") or {}).get("price_writeback") if isinstance(document.get("snapshot"), dict) else None
        earlier_at = corroboration._aware((earlier or {}).get("observed_at")) if isinstance(earlier, dict) else None
        if earlier_at is not None and (observed is None or earlier_at > observed):
            return "catalog_write_newer"
        offers, reason, placeholder_priced = await _mirror_offer_targets(
            row, product, seed.get("seed_data"), variant_id, old=ours, new=live, currency=currency)
        if reason != "planned":
            return reason
        document, product_level, reason = plan_seed_write(
            seed.get("seed_data"), variant_id=variant_id, old_minor=ours, new_minor=live,
            currency=currency, purchase_id=str(row["id"]), observed=observed,
            sole_live_variant=placeholder_priced)
        if document is None:
            return reason
        column = seed.get("price_amount")
        if product_level and column is not None and not (
                _same_price(column, ours, currency) or _same_price(column, live, currency)):
            return "seed_price_unexpected"  # the column the projection copies (R2-2)
    else:
        offers = await _enrichment_offer_targets(row, proof_skus)

    if mode != "on":
        if row["id"] not in _SHADOW_LOGGED:
            _SHADOW_LOGGED.add(row["id"])
            logger.warning(
                "reap_price_writeback: WOULD write purchase=%s product=%s %s %s -> %s offers=%d",
                row["id"], row["product_key"], lane, ours, live, len(offers))
        return "would_write"

    try:
        async with database.transaction():
            written, reason = await _write_offers(offers, old=ours, new=live, currency=currency,
                                                  any_prior=(lane == "enrichment"), observed=observed)
            if reason != "planned":
                return reason  # refused before its first write: nothing to roll back
            if lane == "mirror":
                seed_price = seed.get("price_amount")
                if product_level and seed_price is not None and _same_price(seed_price, ours, currency):
                    seed_price = _major(live, currency)
                found = await database.fetch_one(
                    _WRITE_SEED_SQL if IS_POSTGRES else _WRITE_SEED_SQL_SQLITE, {
                        "id": seed["id"], "seed_data": json.dumps(document),
                        "old_seed_data": _raw_json(seed.get("seed_data")), "price_amount": seed_price,
                        "old_price_amount": seed.get("price_amount")})
                if found is None:
                    raise _Raced("seed")
    except _Raced as raced:
        logger.warning("reap_price_writeback: raced purchase=%s product=%s on %s; nothing written",
                       row["id"], row["product_key"], raced)
        return "raced"
    await _refresh_pdp(lane, product, seed_id=(seed["id"] if lane == "mirror" else None))
    logger.warning(
        "reap_price_writeback: wrote purchase=%s product=%s %s %s -> %s offers=%d",
        row["id"], row["product_key"], lane, ours, live, written)

    after, after_reason, after_variant = await _priced_now(lane, row, sku_key=named_sku)
    if after != live or after_variant != variant_id:
        logger.warning(
            "reap_price_writeback: NOT EFFECTIVE purchase=%s product=%s route now %s (%s)",
            row["id"], row["product_key"], after, after_reason)
        return "written_not_effective"
    return "written"


def _corroboration_max_age() -> timedelta:
    import services.reap_agentic_purchase as svc

    return svc.corroboration_max_age()


async def _refresh_pdp(lane: str, product: Mapping[str, Any], *, seed_id: Optional[str]) -> None:
    """Best-effort: the PDP view is a cache, and a failed rebuild must not undo a price fix."""
    try:
        if lane == "mirror" and seed_id:
            from services.seed_data_writer import refresh_agent_pdp_view_for_seed

            await refresh_agent_pdp_view_for_seed(
                seed_id=seed_id, proposal_id=None, refresh_source="reap_price_writeback")
        elif product.get("content_key"):
            from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key

            await refresh_agent_pdp_view_for_content_key(
                str(product["content_key"]), refresh_source="reap_price_writeback")
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("reap_price_writeback: pdp refresh failed product=%s err=%s",
                       product.get("product_key"), type(exc).__name__)


async def run_writeback_pass(*, now: Optional[datetime] = None, limit: int = BATCH_LIMIT) -> Optional[Dict[str, int]]:
    """One pass. None when the dial is off (nothing read). Never raises for one observation."""
    mode = writeback_mode()
    if mode == "off":
        return None
    now = now or datetime.now(timezone.utc)
    outcomes: Counter = Counter()
    for row in await observations(now=now, limit=limit):
        try:
            outcomes[await apply_one(row, mode=mode, now=now)] += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not end the pass
            outcomes["error"] += 1
            logger.error("reap_price_writeback: purchase=%s error_type=%s",
                         row.get("id"), type(exc).__name__)
    return dict(outcomes)


__all__ = [
    "REAP_AGENTIC_PRICE_WRITEBACK_ENV",
    "apply_one",
    "observations",
    "observed_at",
    "plan_seed_write",
    "run_writeback_pass",
    "writeback_mode",
]

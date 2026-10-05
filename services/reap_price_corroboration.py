"""May a CHANGED Reap quote price be accepted? Only when OUR OWN store read agrees with it.

WHY (owner decision 2026-10-05). The quote's exact items-subtotal check (`verify_quote` (b)) is
the only guard between a buyer and a SUBSTITUTED variant: Reap answers 200 for a substituted
variant and its quote echoes no items, so "Reap now says $30" alone could be the price of some
other shade. A changed price is therefore accepted only when an INDEPENDENT live read of THIS
purchase's own Shopify variant -- one we took from the storefront ourselves -- says the same unit
price, in the same currency, recently.

WHAT COUNTS AS INDEPENDENT. Two writers of ours read Shopify storefronts:

  * ENRICHMENT LANE -- `enrichment_cart_variant_proofs` (db/enrichment_cart_variant_proofs.py,
    written by the proof job). Its contract records `live_price_minor` in the currency the price
    was ACTUALLY read in (`currency`), so it can corroborate. Usable only when: outcome 'ok',
    a known `source`, `variant_id` exactly the purchase's cart-URL variant, available, the same
    storefront host (one `www.` fold), `currency` equal to the purchase currency, a positive
    integer price, and `checked_at` aware, not in the future, at most the freshness window old.
    Every usable row for that variant (sku spellings) must agree on one price.

  * MIRROR / SEED LANE -- `seed_data.snapshot.shopify_cart_proof` and
    `snapshot.shopify_cart_variant_proofs[<id>]`, written ONLY by
    scripts/backfill_shopify_variant_ids.py from one `/products/<handle>.js` fetch.

    THE CURRENCY DECISION: A MIRROR PROOF CORROBORATES ONLY IF THE PROOF ITSELF RECORDS A
    `currency` EQUAL TO THE PURCHASE'S. No writer records one today, so today a mirror proof
    NEVER corroborates. Why not infer it: `products.js` carries no currency; its `price` is in
    whatever PRESENTMENT currency the storefront chose for the crawler's request (Shopify Markets
    localises by IP, and our crawl egress has its own NAT), and it is always x100 -- even for
    zero-decimal currencies -- so for JPY/KRW it is not minor units at all. The seed's
    `price_currency` and the market currency are statements about OTHER numbers (the seed's
    catalog price, the buyer's market); neither says what currency THIS fetch's price was in.
    Accepting on them would be accepting on an assumption, and an assumed currency is exactly
    how a substituted-variant price slips through. If the backfill later records the currency it
    read, this reader starts accepting with no further change.

NOTHING HERE TALKS TO THE NETWORK. Reads only, through db/reap_price_witness.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Mapping, Optional

from db.enrichment_cart_variant_proofs import OUTCOME_OK, PROOF_SOURCES
from services.curated_brand_feed import _same_storefront_host
from services.reap_cart_link import cart_link_line
# The mirror proof's own FETCH rule (https, the shop's own host, the seed's own product URL,
# fresh, not from the future) -- the one the cart-link lane already trusts. Reused, not restated.
# It also pins `source == products_js_v1`, so that rule is not restated below.
from services.shopify_variant_identity import _cart_proof_fetch_is_trusted

ENRICHMENT_SOURCE = "enrichment_proof"
MIRROR_SOURCE = "mirror_proof"


@dataclass(frozen=True)
class Corroboration:
    """An independent live unit price for exactly the purchase's variant."""

    unit_price_minor: int
    source: str
    corroborated_at: datetime


def _aware(value: Any) -> Optional[datetime]:
    """A stored timestamp as an AWARE datetime, or None. SQLite hands back text in the server's
    own format (UTC by construction); Postgres an aware datetime. A naive datetime object is
    refused: the columns are TIMESTAMPTZ, so it did not come from them."""
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else None
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace(" ", "T") if "T" not in text else text)
        except ValueError:
            return None
        if parsed.utcoffset() is None:
            # SQLite's CURRENT_TIMESTAMP text carries no offset and is UTC.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _fresh(checked_at: Optional[datetime], *, now: datetime, max_age: timedelta) -> bool:
    return checked_at is not None and checked_at <= now and now - checked_at <= max_age


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _is_true(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1)


def enrichment_unit_price(
    proofs: Iterable[Mapping[str, Any]],
    *,
    variant_id: str,
    shop_host: str,
    currency: str,
    now: datetime,
    max_age: timedelta,
) -> Optional[int]:
    """The one unit price every USABLE enrichment proof of this variant agrees on, or None."""
    prices = set()
    for proof in proofs:
        if proof.get("outcome") != OUTCOME_OK or proof.get("source") not in PROOF_SOURCES:
            continue
        if str(proof.get("variant_id") or "") != variant_id:
            continue
        if not _is_true(proof.get("available")):
            continue
        if not _same_storefront_host(shop_host, proof.get("shop_host")):
            continue
        if proof.get("currency") != currency:
            continue
        if not _fresh(_aware(proof.get("checked_at")), now=now, max_age=max_age):
            continue
        price = _positive_int(proof.get("live_price_minor"))
        if price is None:
            continue
        prices.add(price)
    return prices.pop() if len(prices) == 1 else None


def _seed_document(seed_data: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(seed_data, str):
        try:
            seed_data = json.loads(seed_data)
        except (TypeError, ValueError):
            return None
    return seed_data if isinstance(seed_data, dict) else None


def mirror_unit_price(
    seed_data: Any,
    *,
    variant_id: str,
    product_urls: List[str],
    shop_domain: str,
    currency: str,
    now: datetime,
    max_age: timedelta,
) -> Optional[int]:
    """The unit price a mirror storefront proof of THIS variant states, or None.

    Candidates: the seed's `shopify_cart_proof` and its `shopify_cart_variant_proofs[<id>]`. A
    candidate counts only when it names this variant, says `available: true`, passes the shared
    fetch rule, is inside the freshness window, carries a positive integer `price_minor` AND a
    `currency` equal to the purchase's -- see THE CURRENCY DECISION in the module docstring. All
    counting candidates must agree.
    """
    document = _seed_document(seed_data)
    snapshot = document.get("snapshot") if document else None
    if not isinstance(snapshot, dict):
        return None
    candidates = []
    proof = snapshot.get("shopify_cart_proof")
    if isinstance(proof, dict):
        candidates.append(proof)
    selected = snapshot.get("shopify_cart_variant_proofs")
    if isinstance(selected, dict) and isinstance(selected.get(variant_id), dict):
        candidates.append(selected[variant_id])
    prices = set()
    for candidate in candidates:
        if str(candidate.get("variant_id") or "") != variant_id:
            continue
        if candidate.get("available") is not True:
            continue
        # THE CURRENCY MUST BE RECORDED ON THE PROOF. Never inferred (module docstring).
        if candidate.get("currency") != currency:
            continue
        if not _cart_proof_fetch_is_trusted(
            candidate, product_urls=product_urls, shop_domain=shop_domain, now=now
        ):
            continue
        if not _fresh(_aware(candidate.get("checked_at")), now=now, max_age=max_age):
            continue
        price = _positive_int(candidate.get("price_minor"))
        if price is None:
            continue
        prices.add(price)
    return prices.pop() if len(prices) == 1 else None


def purchase_variant_id(row: Mapping[str, Any]) -> Optional[str]:
    """The numeric Shopify variant this CART-LINK purchase buys: its own cart URL's one line,
    which must agree with the row's `variant_key` ('shopify:<id>'). None for the variant lane
    (Reap's opaque `var_` handles name no storefront variant) and for any disagreement."""
    if str(row.get("item_source") or "reap_variant") != "cart_link":
        return None
    line = cart_link_line(row.get("cart_url"))
    if line is None:
        return None
    variant_id = str(line[0])
    if str(row.get("variant_key") or "") != "shopify:" + variant_id:
        return None
    return variant_id


async def independent_unit_price(
    row: Mapping[str, Any], *, now: datetime, max_age: timedelta
) -> Optional[Corroboration]:
    """Our own fresh storefront read of exactly this purchase's variant, or None.

    Enrichment proofs first, then the mirror seed. If BOTH lanes yield a price they must agree;
    two of our own reads disagreeing is not corroboration of either.
    """
    from db import reap_price_witness as witness

    variant_id = purchase_variant_id(row)
    product_key = str(row.get("product_key") or "").strip()
    currency = str(row.get("currency") or "").strip().upper()
    shop = str(row.get("merchant_domain") or "").strip().lower()
    if not variant_id or not product_key or not currency or not shop:
        return None
    found = []
    enrichment = enrichment_unit_price(
        await witness.enrichment_proofs_for_variant(product_key, variant_id),
        variant_id=variant_id, shop_host=shop, currency=currency, now=now, max_age=max_age,
    )
    if enrichment is not None:
        found.append(Corroboration(enrichment, ENRICHMENT_SOURCE, now))
    seed = await witness.mirror_seed_for_product(product_key, str(row.get("market_country") or ""))
    if seed is not None:
        mirror = mirror_unit_price(
            seed.get("seed_data"), variant_id=variant_id,
            product_urls=[seed.get("canonical_url") or seed.get("destination_url")],
            shop_domain=shop, currency=currency, now=now, max_age=max_age,
        )
        if mirror is not None:
            found.append(Corroboration(mirror, MIRROR_SOURCE, now))
    if not found or len({c.unit_price_minor for c in found}) != 1:
        return None
    return found[0]


__all__ = [
    "Corroboration",
    "ENRICHMENT_SOURCE",
    "MIRROR_SOURCE",
    "enrichment_unit_price",
    "independent_unit_price",
    "mirror_unit_price",
    "purchase_variant_id",
]

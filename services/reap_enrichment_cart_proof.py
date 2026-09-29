"""Can the Reap cart-link lane buy this ENRICHMENT catalog row? Pure checks, no I/O.

WHAT THIS IS (option 2, PR A, 2026-09-29). A `catalog_enrichment_agent_v1` product names a brand's
own Shopify page (`canonical_url = https://<host>/products/<handle>`) and carries one sku per
variant, `<product_key>::v:<token>`, plus the `<product_key>::canonical` display placeholder. The
cart-link lane (routes/agent_commerce_reap._load_cart_link_item) buys a Shopify variant through a
cart permalink, so it has to know three things the catalog alone cannot vouch for:

  1. WHICH live Shopify variant this sku is  -> `verify_enrichment_cart_proof`, against a fresh
     storefront proof row (db/enrichment_cart_variant_proofs.py, written by PR B's job);
  2. WHO sells it                            -> `derive_enrichment_seller`, the observed seller id
     the repo's own minting functions give this row;
  3. WHAT it costs                           -> `enrichment_offer_price_ok`, the catalog offer
     price, only when it equals the price the proof read live.

INERT IN PR A. Nothing calls these functions yet. PR C wires them into the purchase lane behind a
dark flag; until then no behaviour anywhere changes.

EVERY FUNCTION REFUSES RATHER THAN GUESSES. A wrong answer here buys the wrong shade, bills the
wrong seller, or charges a price nobody quoted. Each refusal is a short stable reason code that
PR C maps to a 409.

THE VARIANT ID OF A SKU IS ITS `source_variant_id`, NOT ITS KEY. The design note read it from
`::v:<id>`, which is wrong for two live shapes:
  * the retailer lane keys a variant `::v:retailer-<sha256[:32]>` (ingestion.derive_variant_sku_key
    with a seller scope), so all 250 bluemercury rows carry the numeric id only in
    `source_variant_id` (e.g. 32903948173387);
  * a key that does not fit 255 characters carries a sha1 digest instead of the id.
So the id is `source_variant_id`; a key that DOES spell `::v:<digits>` must agree with it.
The placeholder is recognised by its key alone (`<product_key>::canonical`): its
`source_variant_id` is the product_key only while that fits 128 characters
(ingestion.canonical_sku_variant_id), so comparing the two misses every longer key.

THE PLACEHOLDER NEVER STANDS IN FOR A VARIANT THE CATALOG KNOWS. It may be bought only when the
product has NO `::v:` sku at all (`catalog_variant_sku_count == 0`, which the caller counts) AND
the handle has exactly one variant. Without the first condition, MAC's 99 folded families pass:
their canonical_url is the parent handle, whose one variant is a "Default Title" stub, while the
placeholder's price is the first shade's -- the cart would buy the stub.

HOSTS ARE COMPARED EXACTLY AFTER ONE `www.` FOLD, the same fold on all three sides (the
canonical_url host, `source_domain`, the proof's `shop_host`), by the repo's existing owner of
that rule, `services.curated_brand_feed._same_storefront_host` (case and a trailing dot are also
folded; a subdomain, a suffix or a lookalike never matches).

THE PROOF CONTRACT -- what PR B's writer must put in db/enrichment_cart_variant_proofs.py, and
what this verifier assumes. A row that breaks any of these must not be written as 'ok'.
  * product_key, sku_key  the catalog row the proof is FOR, exactly. One proof per sku.
  * shop_host   the host the job REQUESTED (the canonical_url host). If the storefront redirects
                to a host that fails `_same_storefront_host` against it, the outcome is not 'ok'
                (e.g. 'host_redirected').
  * handle      Shopify's own `handle` from the response, verbatim; compared exactly with
                `sku_payload.source_handle`, else the canonical_url handle.
  * variant_id  a variant id taken from the storefront response for that handle (bare ASCII
                digits). An 'ok' proof is written only when the sku's own numeric id is FOUND
                among that handle's variants (for the placeholder: the handle's one variant).
  * live_variant_count  ALL variants on the handle, available or not. Sole-variant mode relies on
                it: a 3-shade product with 2 shades sold out has live_variant_count 3, not 1.
  * available   that variant's own `available`.
  * live_price_minor, currency  that variant's price in ISO-4217 MINOR units of the currency the
                price was ACTUALLY read in (not the market's, not an assumed one). `.js` prices
                are x100 for every currency, JPY and KRW included, so the writer converts with
                the currency's exponent (`.js` 440000 JPY -> 4400); `/products.json` prices are
                major-unit strings.
  * source      one of PROOF_SOURCES (the table CHECKs it).
  * checked_at  when the response was read (TIMESTAMPTZ); updated_at is SET EXPLICITLY on every
                write (the column default fires on INSERT only).
  * outcome     'ok', or a refusal the job keeps for itself ('revoked_404', 'variant_gone', ...).
PR C's offer SQL must filter suppressed offers (`suppression_reason IS NULL AND suppressed_at IS
NULL`) and select by listing identity; `enrichment_offer_price_ok` sees only what it is handed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from db.enrichment_cart_variant_proofs import OUTCOME_OK, PROOF_SOURCES
# THE repo's major -> minor conversion for this rail: refuses (None) rather than rounds, knows the
# zero-decimal currencies, and refuses a binary float. The cart-link route prices with it too.
from db.reap_agentic_ledger import amount_minor_or_none
# The one reader of a Shopify variant reference (bare digits or `gid://shopify/ProductVariant/<n>`).
from services.outbound_links_service import extract_shopify_numeric_variant_id
# THE owner of the "same storefront host" rule (one `www.` fold, nothing else). Imported.
from services.curated_brand_feed import _same_storefront_host
# THE owners of the observed seller-of-record id. Imported, never re-implemented.
from services.seller_identity import etld1, make_observed_retailer_id, resolve_seed_seller_identity
# Shopify caps a product at 100 variants.
from services.shopify_variant_identity import MAX_VARIANTS

#: `catalog_products.source_system` of an enrichment row (ingestion.AGENT_VERSION; a test pins it).
ENRICHMENT_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"
#: A retailer-lane listing's product_key prefix (`ext:retailer:<sha256(listing)[:32]>`).
RETAILER_KEY_PREFIX = "ext:retailer:"
#: The display placeholder sku (`<product_key>::canonical`) and the variant infix (`::v:`), as
#: services/catalog_enrichment_agent/ingestion.py spells them (SKU_SUFFIX, VARIANT_SKU_INFIX).
PLACEHOLDER_SUFFIX = "::canonical"
VARIANT_INFIX = "::v:"

#: How old a proof may be. The storefront price and stock move; 72h is the design's bound.
MAX_PROOF_AGE = timedelta(hours=72)

# Success reasons.
SOLE_VARIANT = "sole_variant"
NAMED_VARIANT = "named_variant"

# Refusal reasons of verify_enrichment_cart_proof.
ROW_MALFORMED = "row_malformed"
ROW_NOT_ENRICHMENT = "row_not_enrichment"
SKU_NOT_OF_PRODUCT = "sku_not_of_product"
SKU_KEY_UNRECOGNIZED = "sku_key_unrecognized"
SKU_PAYLOAD_MALFORMED = "sku_payload_malformed"
SKU_VARIANT_UNVERIFIED = "sku_variant_unverified"
SKU_VARIANT_CONTRADICTION = "sku_variant_contradiction"
CANONICAL_URL_UNUSABLE = "canonical_url_unusable"
HOST_MISMATCH = "host_mismatch"
HANDLE_MISMATCH = "handle_mismatch"
PROOF_MISSING = "proof_missing"
PROOF_KEY_MISMATCH = "proof_key_mismatch"
PROOF_SOURCE_UNKNOWN = "proof_source_unknown"
PROOF_OUTCOME_NOT_OK = "proof_outcome_not_ok"
PROOF_MALFORMED = "proof_malformed"
PROOF_FROM_FUTURE = "proof_from_future"
PROOF_STALE = "proof_stale"
PROOF_UNAVAILABLE = "proof_unavailable"
PLACEHOLDER_MULTI_VARIANT = "placeholder_multi_variant"
PLACEHOLDER_HAS_VARIANT_SKUS = "placeholder_has_variant_skus"
VARIANT_MISMATCH = "variant_mismatch"

# Reasons of enrichment_offer_price_ok (the route's own vocabulary, plus row_price_stale).
PRICE_OK = "ok"
ROW_UNPRICED = "row_unpriced"
ROW_PRICE_STALE = "row_price_stale"
ROW_PRICE_AMBIGUOUS = "row_price_ambiguous"
ROW_CURRENCY_MISMATCH = "row_currency_mismatch"

#: A Shopify numeric id: ASCII digits only (`str.isdigit` also admits other scripts' digits).
_NUMERIC_ID = re.compile(r"[0-9]{1,20}")
#: One path segment of a handle. Never empty, never a separator, never whitespace.
_HANDLE = re.compile(r"[^/?#\\\s]+")
_PRODUCT_PATH = re.compile(r"/products/([^/]+)")
_CURRENCY = re.compile(r"[A-Z]{3}")
_GID_PREFIX = "gid://shopify/ProductVariant/"

Verdict = Tuple[bool, Optional[str], str]


def _refuse(reason: str) -> Verdict:
    return False, None, reason


def _mapping(row: Any) -> Optional[Mapping[str, Any]]:
    """`row` if it is a Mapping, else None. A `databases` Record is a Sequence, not a Mapping:
    callers pass `dict(record)`, as the cart-link route already does."""
    return row if isinstance(row, Mapping) else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _numeric_id(value: Any) -> Optional[str]:
    """`value` if it is a string of ASCII digits (a Shopify numeric id), else None."""
    if isinstance(value, str) and _NUMERIC_ID.fullmatch(value):
        return value
    return None


def _strict_variant_id(value: Any) -> Optional[str]:
    """A sku's `source_variant_id` as a Shopify numeric id, STRICTLY: the whole value is ASCII digits
    or exactly `gid://shopify/ProductVariant/<digits>`. The shared reader
    (`extract_shopify_numeric_variant_id`) also strips whitespace and finds a gid anywhere in the
    string; this wrapper keeps its answer only when the value IS one of the two exact spellings."""
    if not isinstance(value, str):
        return None
    numeric = _numeric_id(extract_shopify_numeric_variant_id(value))
    if numeric is None:
        return None
    return numeric if value in (numeric, _GID_PREFIX + numeric) else None


def storefront_page(canonical_url: Any) -> Optional[Tuple[str, str]]:
    """`(host, handle)` of an `https://<host>/products/<handle>` page, or None.

    Refused: any scheme but https; an explicit port; userinfo (`https://brand.com@evil.io/`
    is a page on evil.io); a query or fragment; any path but exactly `/products/<handle>`; a
    handle that is empty, `.js`/`.json`, or carries whitespace or a separator; surrounding
    whitespace. The host is lower case (urlsplit folds it) and is returned exactly otherwise:
    no `www.` folding, no trailing-dot folding, so equality below is exact.
    """
    if not isinstance(canonical_url, str):
        return None
    if any(ch.isspace() or ord(ch) < 0x20 for ch in canonical_url):
        return None
    try:
        parts = urlsplit(canonical_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if not host:
        return None
    if parts.username is not None:
        return None
    if port is not None:
        return None
    if parts.query:
        return None
    if parts.fragment:
        return None
    path = _PRODUCT_PATH.fullmatch(parts.path)
    if path is None:
        return None
    handle = path.group(1)
    if not _HANDLE.fullmatch(handle) or handle.endswith((".js", ".json")):
        return None
    return host, handle


def _sku_payload(value: Any) -> Optional[Mapping[str, Any]]:
    """`sku_payload` as a Mapping ({} when absent), or None when it is present but unreadable.
    asyncpg and SQLite both hand JSON back as text."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, Mapping) else None


def _expected_handle(payload: Mapping[str, Any], canonical_handle: str) -> Optional[str]:
    """The handle the sku lives under: `sku_payload.source_handle` when set (a folded MAC shade is
    its own Shopify product), else the canonical_url's. None when source_handle is unusable."""
    source_handle = payload.get("source_handle")
    if source_handle is None or (isinstance(source_handle, str) and not source_handle.strip()):
        return canonical_handle
    if not isinstance(source_handle, str) or not _HANDLE.fullmatch(source_handle):
        return None
    return source_handle


def _aware(value: Any) -> Optional[datetime]:
    """A stored timestamp as an aware datetime, or None. A naive value is refused, not assumed UTC:
    the column is TIMESTAMPTZ, so a naive value did not come from it."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return None
    return value


def _is_true(value: Any) -> bool:
    """True for a real boolean true, or SQLite's 1. Never for a truthy string."""
    return value is True or (type(value) is int and value == 1)


def _sku_variant(sku_key: str, product_key: str, source_variant_id: Any) -> Tuple[Optional[str], bool]:
    """`(numeric_variant_id, contradiction)` for a non-placeholder sku.

    The id is `source_variant_id` (see the module docstring). A key that spells `::v:<digits>`
    must name the same id; a key with digits and a source_variant_id without is a contradiction
    too, never a fallback to the key.
    """
    stored = _strict_variant_id(source_variant_id)
    token = sku_key[len(product_key) + len(VARIANT_INFIX):]
    keyed = token if _NUMERIC_ID.fullmatch(token) else None
    if keyed is not None and keyed != stored:
        return None, True
    return stored, False


def verify_enrichment_cart_proof(
    product_row: Any,
    sku_row: Any,
    proof_row: Any,
    *,
    catalog_variant_sku_count: int,
    now: datetime,
    max_age: timedelta = MAX_PROOF_AGE,
) -> Verdict:
    """`(ok, shopify_variant_id | None, reason)`: may the cart link buy this sku, and which variant.

    Inputs are the catalog_products row (product_key, source_system, source_domain,
    canonical_url), the catalog_skus row (sku_key, source_variant_id, sku_payload) and the
    enrichment_cart_variant_proofs row for exactly that (product_key, sku_key), or None.
    `catalog_variant_sku_count` is how many LIVE non-placeholder skus (`::v:`) the product has in
    catalog_skus; the caller counts them. It is required so no caller can forget it.

    ACCEPTED in one of two modes; `reason` names which:
      * sole_variant   the proof's handle has exactly one live variant. The `::canonical`
                       placeholder may ONLY use this mode, and only on a product with no
                       `::v:` sku. A real sku must also name the proof's variant (tarte
                       flat blush brush, one variant).
      * named_variant  the handle has several live variants and the sku's own numeric id IS the
                       proof's variant (a MAC shade folded into its own handle, a bluemercury
                       size).
    Both modes require: the row is an enrichment row; the sku belongs to it; the proof is for
    this (product_key, sku_key); canonical_url is `https://<host>/products/<handle>`; the host
    equals `source_domain` and the proof's shop_host after one `www.` fold (no subdomain,
    suffix or lookalike match); the proof's handle equals `sku_payload.source_handle` when set, else the
    canonical_url handle; the proof was read by PR B's job (`source`), its outcome is 'ok', it is
    at most `max_age` old and not from the future; the variant is available.

    `now` must be timezone-aware and `catalog_variant_sku_count` a non-negative int; anything else
    is a caller bug and raises ValueError.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("verify_enrichment_cart_proof: `now` must be a timezone-aware datetime")
    if type(catalog_variant_sku_count) is not int or catalog_variant_sku_count < 0:
        raise ValueError("verify_enrichment_cart_proof: `catalog_variant_sku_count` must be an int >= 0")
    product = _mapping(product_row)
    sku = _mapping(sku_row)
    if product is None or sku is None:
        return _refuse(ROW_MALFORMED)
    if product.get("source_system") != ENRICHMENT_SOURCE_SYSTEM:
        return _refuse(ROW_NOT_ENRICHMENT)
    product_key = _text(product.get("product_key"))
    sku_key = _text(sku.get("sku_key"))
    if not product_key or not sku_key.startswith(product_key + "::"):
        return _refuse(SKU_NOT_OF_PRODUCT)
    placeholder = sku_key == product_key + PLACEHOLDER_SUFFIX
    if not placeholder and not sku_key.startswith(product_key + VARIANT_INFIX):
        return _refuse(SKU_KEY_UNRECOGNIZED)

    proof = _mapping(proof_row)
    if proof is None:
        return _refuse(PROOF_MISSING)
    if proof.get("product_key") != product_key or proof.get("sku_key") != sku_key:
        return _refuse(PROOF_KEY_MISMATCH)

    page = storefront_page(product.get("canonical_url"))
    if page is None:
        return _refuse(CANONICAL_URL_UNUSABLE)
    host, canonical_handle = page
    if not _same_storefront_host(host, _text(product.get("source_domain"))):
        return _refuse(HOST_MISMATCH)
    if not _same_storefront_host(host, _text(proof.get("shop_host"))):
        return _refuse(HOST_MISMATCH)

    payload = _sku_payload(sku.get("sku_payload"))
    if payload is None:
        return _refuse(SKU_PAYLOAD_MALFORMED)
    expected_handle = _expected_handle(payload, canonical_handle)
    if expected_handle is None:
        return _refuse(SKU_PAYLOAD_MALFORMED)
    if proof.get("handle") != expected_handle:
        return _refuse(HANDLE_MISMATCH)

    if proof.get("source") not in PROOF_SOURCES:
        return _refuse(PROOF_SOURCE_UNKNOWN)
    if proof.get("outcome") != OUTCOME_OK:
        return _refuse(PROOF_OUTCOME_NOT_OK)
    checked_at = _aware(proof.get("checked_at"))
    if checked_at is None:
        return _refuse(PROOF_MALFORMED)
    if checked_at > now:
        return _refuse(PROOF_FROM_FUTURE)
    if now - checked_at > max_age:
        return _refuse(PROOF_STALE)
    if not _is_true(proof.get("available")):
        return _refuse(PROOF_UNAVAILABLE)
    live_count = proof.get("live_variant_count")
    if type(live_count) is not int or not 1 <= live_count <= MAX_VARIANTS:
        return _refuse(PROOF_MALFORMED)
    proof_variant = _numeric_id(proof.get("variant_id"))
    if proof_variant is None:
        return _refuse(PROOF_MALFORMED)

    if placeholder:
        # The placeholder names the PRODUCT, not a variant. It may stand for the product's one
        # variant only when the catalog knows no variant of it (else the MAC parent-stub cart) and
        # the handle has exactly one variant, sold out or not (else it is some shade, unnamed).
        if catalog_variant_sku_count != 0:
            return _refuse(PLACEHOLDER_HAS_VARIANT_SKUS)
        if live_count != 1:
            return _refuse(PLACEHOLDER_MULTI_VARIANT)
        return True, proof_variant, SOLE_VARIANT

    sku_variant, contradiction = _sku_variant(sku_key, product_key, sku.get("source_variant_id"))
    if contradiction:
        return _refuse(SKU_VARIANT_CONTRADICTION)
    if sku_variant is None:
        return _refuse(SKU_VARIANT_UNVERIFIED)
    if sku_variant != proof_variant:
        return _refuse(VARIANT_MISMATCH)
    return True, proof_variant, SOLE_VARIANT if live_count == 1 else NAMED_VARIANT


def derive_enrichment_seller(product_row: Any) -> Optional[str]:
    """The observed seller id this enrichment row's seller must be, re-derived; None if underivable.

      * `ext:retailer:` key  -> make_observed_retailer_id(etld1(host)): a retailer is keyed on its
                                domain alone (bluemercury.com, whatever brand the row carries).
                                resolve_seed_seller_identity would NOT give this for a host that is
                                not on the known-retailer list, and bluemercury.com is not.
      * any other key        -> resolve_seed_seller_identity(brand, host)["merchant_id"].
    The host is the canonical_url's (storefront_page), never destination_url or an offer's. The
    CALLER accepts the row only when this equals `product.merchant_id` exactly; the offer's
    `agent_seed::` merchant and the seed's seller_ref are never the seller.
    """
    product = _mapping(product_row)
    if product is None or product.get("source_system") != ENRICHMENT_SOURCE_SYSTEM:
        return None
    page = storefront_page(product.get("canonical_url"))
    if page is None:
        return None
    host = page[0]
    try:
        if _text(product.get("product_key")).startswith(RETAILER_KEY_PREFIX):
            return make_observed_retailer_id(etld1(host))
        brand = product.get("brand")
        return resolve_seed_seller_identity(brand=brand if isinstance(brand, str) else None,
                                            domain=host)["merchant_id"]
    except ValueError:
        return None


def enrichment_offer_price_ok(
    offers: Optional[Iterable[Any]], proof: Any, market_currency: Optional[str],
) -> Verdict:
    """`(ok, price_minor | None, reason)` for the offers PR C's SQL selected for this sku.

    Each offer carries `price` (a MAJOR-unit decimal, as text or Decimal -- `_CART_OFFER_SQL`
    CASTs it to TEXT; a binary float is refused as unusable) and `currency`. An offer is unusable
    when it is not a mapping, its currency is not three letters, or its price does not convert
    exactly to minor units. Then:
      row_unpriced           no usable offer;
      row_price_ambiguous    usable offers disagree on (currency, price), or an unusable offer
                             sits beside usable ones (a price nobody can read is not agreement);
      row_currency_mismatch  the offers' currency, or the proof's, is not the market's;
      row_price_stale        the proof has no live price, or it differs from the offers'.
    Call it after verify_enrichment_cart_proof accepted `proof`.
    """
    market = _text(market_currency).strip().upper()
    usable = set()
    unusable = 0
    for raw in offers or ():
        offer = _mapping(raw)
        if offer is None:
            unusable += 1
            continue
        currency = _text(offer.get("currency")).strip().upper()
        if not _CURRENCY.fullmatch(currency):
            unusable += 1
            continue
        minor = amount_minor_or_none(offer.get("price"), currency)
        if minor is None:
            unusable += 1
            continue
        usable.add((currency, minor))
    if not usable:
        return _refuse(ROW_UNPRICED)
    if unusable:
        return _refuse(ROW_PRICE_AMBIGUOUS)
    if len(usable) > 1:
        return _refuse(ROW_PRICE_AMBIGUOUS)
    ((currency, minor),) = usable
    if currency != market:
        return _refuse(ROW_CURRENCY_MISMATCH)
    proof_row = _mapping(proof)
    if proof_row is None:
        return _refuse(ROW_PRICE_STALE)
    if proof_row.get("currency") != market:
        return _refuse(ROW_CURRENCY_MISMATCH)
    live = proof_row.get("live_price_minor")
    if type(live) is not int or live != minor:
        return _refuse(ROW_PRICE_STALE)
    return True, minor, PRICE_OK


__all__ = [
    "ENRICHMENT_SOURCE_SYSTEM",
    "MAX_PROOF_AGE",
    "PROOF_SOURCES",
    "derive_enrichment_seller",
    "enrichment_offer_price_ok",
    "storefront_page",
    "verify_enrichment_cart_proof",
]

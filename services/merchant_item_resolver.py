"""Find OUR catalog record for an item a caller names by the MERCHANT's own product URL.

WHY. What a buyer pays for is the merchant's own item: one variant of one product on one storefront.
The cart-link lane already takes the merchant's variant as the storefront numbers it (`variant_id`) and
resolves it to our sku itself. What it still demanded was `product_key`: the primary key of OUR record of
what we have observed about that product, an internal id no search card publishes. A card does carry the
merchant's product page (`canonical_url`), so a purchase may name the product by that URL instead, and the
server finds the record.

WHAT THE URL IS, AND IS NOT. A LOOKUP KEY ONLY. It never reaches a cart link, a price, a seller or a
proof: everything the purchase uses after this point is read from OUR record, exactly as for a
`product_key` request. The cart link's host, in particular, is still the seed's own URL host
(`routes.agent_commerce_reap._seed_storefront_host`). So the lane's rule that "no caller URL is
accepted" holds: the caller's URL picks a record, it does not describe one.

HOW. Our record of a served product is the one its seed is ATTACHED to (`attached_product_key`, live).
The 2026-11-07 census (3,000 serving-pool seeds) found every sampled seed attached: 30% to their own
mirror, 70% to an enrichment-ingested product; 92.7% resolved to one record and an unambiguous variant
(US 93.1%). Only the attachment is used, for two reasons:
  * the cart-link lane buys a mirror product only when the seed is attached to it
    (`routes.agent_commerce_reap._CART_SEED_VARIANT_SQL`), so a seed's unattached mirror could never
    complete a purchase, and answering with its key would only move the refusal later;
  * an attachment to a SUPPRESSED product names nothing: the catalog withdrew it, and search does not
    serve such a seed (`SEED_SUPPRESSED_PRODUCT_ANTI_JOIN`).
The seed is found by its storefront (`domain`, indexed) and market, then by its product URL: a SQL `LIKE`
anchored on the path (it must end the stored URL or be followed by `/`, `?` or `#`) narrows the rows, and
the exact comparison is done here, on one normalisation applied to both sides (`normalize_product_url`).

FAILS CLOSED. A seed whose `domain` is NULL or not lowercase is not read (`row_not_found`): the writers
lowercase it, and a seed placed by its URL alone is reachable by `product_key` as before. Paths are
compared case-folded (Shopify handles are lowercase); two pages differing only in case resolve to a
refusal or to a record the buyer's `variant_id` must then match.

EXACTLY ONE RECORD OR A REFUSAL. No seed, or no live attached record: `row_not_found`, the answer the lane
already gives for a product that is not under this domain. More than one distinct record (two seeds of one
URL attached to different products): `product_ambiguous`. Never a pick.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any, List, Optional, Set
from urllib.parse import urlsplit

from db.database import database
from services.reap_agentic_purchase import PurchaseRefused

logger = logging.getLogger(__name__)

#: What the request fingerprint carries for a product named by URL, like `variant_id:<n>` for a variant
#: named by the storefront's id: what the caller SENT, normalised, never the key resolved from today's
#: catalog, so a retry hashes the same and a URL request can never replay a `product_key` one.
PRODUCT_URL_HASH_PREFIX = "product_url:"

#: Longer than any product URL a card carries; a value past it is a caller bug, refused before any read.
MAX_PRODUCT_URL_CHARS = 2048

#: Seeds read for one (storefront, market, path). Production has at most a handful per URL; a read that
#: fills this is not one product, and is refused rather than trusted to have seen every candidate.
MAX_SEED_CANDIDATES = 50

_SEED_SQL = """
    SELECT e.id, e.attached_product_key, e.canonical_url, e.destination_url
      FROM external_product_seeds e
     WHERE e.status = 'active'
       AND e.domain IN (:domain_apex, :domain_www)
       AND upper(e.market) = :market_country
       AND (lower(e.canonical_url) LIKE :path_end ESCAPE '!'
            OR lower(e.canonical_url) LIKE :path_slash ESCAPE '!'
            OR lower(e.canonical_url) LIKE :path_query ESCAPE '!'
            OR lower(e.canonical_url) LIKE :path_fragment ESCAPE '!'
            OR lower(e.destination_url) LIKE :path_end ESCAPE '!'
            OR lower(e.destination_url) LIKE :path_slash ESCAPE '!'
            OR lower(e.destination_url) LIKE :path_query ESCAPE '!'
            OR lower(e.destination_url) LIKE :path_fragment ESCAPE '!')
     ORDER BY e.id
     LIMIT :limit
"""

_LIVE_PRODUCT_SQL = """
    SELECT p.product_key
      FROM catalog_products p
     WHERE p.product_key = :product_key
       AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
"""


def _bare_host(host: str) -> str:
    """Lowercased, trailing dot dropped, ONE leading `www.` removed: the storefront rule this lane uses
    everywhere (`services.curated_brand_feed._same_storefront_host`)."""
    return str(host or "").strip().lower().rstrip(".").removeprefix("www.")


def _normalized(url: Any) -> Optional[str]:
    """`<bare host><path>` for an http(s) URL, path lowercased with trailing slashes and any query or
    fragment dropped; None for anything that is not one. One rule for the caller's URL and the stored
    ones, so the comparison is symmetric by construction."""
    text = str(url or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not host:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if port is not None and port not in (80, 443):
        return None
    path = parts.path.lower().rstrip("/")
    return _bare_host(host) + path


def normalize_product_url(product_url: str, merchant_host: str) -> str:
    """The caller's product URL in the comparison form, or `invalid_request`.

    It must be an http(s) URL on THIS storefront (one `www.` is noise; a subdomain or a lookalike is
    another store) with a product path. The value is never echoed: it is caller-controlled.
    """
    text = str(product_url or "").strip()
    if not text or len(text) > MAX_PRODUCT_URL_CHARS or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in text):
        raise PurchaseRefused("invalid_request", "product_url is not a usable URL")
    normalized = _normalized(text)
    if normalized is None:
        raise PurchaseRefused("invalid_request", "product_url is not an http(s) product URL")
    host = urlsplit(text).hostname or ""
    if _is_ip_literal(host):
        raise PurchaseRefused("invalid_request", "product_url must name the storefront by its host name")
    if _bare_host(host) != _bare_host(merchant_host):
        raise PurchaseRefused("invalid_request", "product_url is not on merchant_domain")
    if normalized == _bare_host(host):
        raise PurchaseRefused("invalid_request", "product_url names no product page")
    return normalized


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _like_escape(text: str) -> str:
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


async def resolve_product_key(*, normalized_url: str, market_country: str) -> str:
    """The ONE live catalog product the seed(s) at this storefront URL and market name, or a refusal.

    `normalized_url` is `normalize_product_url`'s answer. Read-only; runs inside a caller's read-only
    transaction unchanged.
    """
    apex = normalized_url.split("/", 1)[0]
    path = normalized_url[len(apex):]
    rows = await database.fetch_all(
        _SEED_SQL,
        {
            "domain_apex": apex,
            "domain_www": "www." + apex,
            "market_country": str(market_country or "").upper(),
            "path_end": "%" + _like_escape(path),
            "path_slash": "%" + _like_escape(path) + "/%",
            "path_query": "%" + _like_escape(path) + "?%",
            "path_fragment": "%" + _like_escape(path) + "#%",
            "limit": MAX_SEED_CANDIDATES + 1,
        },
    )
    if len(rows) > MAX_SEED_CANDIDATES:
        raise PurchaseRefused("product_ambiguous", "too many catalog seeds for this product URL")
    seeds: List[dict] = [
        dict(r) for r in rows
        if normalized_url in (_normalized(dict(r).get("canonical_url")), _normalized(dict(r).get("destination_url")))
    ]
    keys: Set[str] = set()
    for seed in seeds:
        attached = str(seed.get("attached_product_key") or "").strip()
        if not attached:
            continue  # unattached: nothing the lane could buy (see the module docstring)
        live = await database.fetch_one(_LIVE_PRODUCT_SQL, {"product_key": attached})
        if live is not None:
            keys.add(str(dict(live)["product_key"]))
    if not keys:
        raise PurchaseRefused("row_not_found", "no catalog product for this product URL")
    if len(keys) > 1:
        raise PurchaseRefused("product_ambiguous", "this product URL names more than one catalog product")
    return next(iter(keys))

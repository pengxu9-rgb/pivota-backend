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
The seed is found by its storefront (`domain`, indexed) and market, then by its product page: the
lane's own idea of it, `canonical_url` else `destination_url` (`_seed_storefront_host`), never a
`destination_url` that sits beside a different canonical page. A SQL `LIKE` anchored on the path (it
must end the stored URL or be followed by `/`, `?` or `#`) narrows the rows, and the exact comparison is
done here, on one normalisation applied to both sides (`normalize_product_url`).

THE PAGE NAMES A PRODUCT, NEVER A VARIANT. A URL purchase names its variant by `variant_id`, required
beside `product_url` (the route's selector rule). A variant in the URL is not ignored: every `variant`
key in its query or fragment (decoded, any case) must have exactly `<variant_id>` as its value, or the
request is refused (`check_url_variant`). Otherwise a variant-grain seed (`?variant=A`) would answer a caller's
`?variant=B` with the record that proves only A.

FAILS CLOSED. A seed whose `domain` is NULL or not lowercase is not read (`row_not_found`): the writers
lowercase it, and a seed placed by its URL alone is reachable by `product_key` as before. Paths are
compared case-folded (Shopify handles are lowercase); two pages differing only in case resolve to a
refusal or to a record the buyer's `variant_id` must then match.

EXACTLY ONE RECORD OR A REFUSAL. No seed, or no live attached record: `row_not_found`, the answer the lane
already gives for a product that is not under this domain. More than one distinct record (two seeds of one
URL attached to different products): `product_ambiguous`. Never a pick.
"""
from __future__ import annotations

import html
import ipaddress
import logging
import re
from typing import Any, List, Optional, Set
from urllib.parse import unquote_plus, urlsplit

from db.database import database
from services.reap_agentic_purchase import PurchaseRefused

logger = logging.getLogger(__name__)

#: What the request fingerprint carries for a product named by URL, like `variant_id:<n>` for a variant
#: named by the storefront's id: what the caller SENT, normalised, never the key resolved from today's
#: catalog, so a retry hashes the same and a URL request can never replay a `product_key` one.
#: VERSIONED: the fingerprint depends on `normalize_product_url`, so a later change to the normalisation
#: takes a new tag and leaves in-flight attempts reachable under the old one only by their old spelling
#: (a retry then answers `idempotency_conflict`, never a second purchase).
PRODUCT_URL_HASH_PREFIX = "product_url:v1:"

#: No `product_key` (a catalog key) starts with this: a key that did would share a URL request's fingerprint.
PRODUCT_URL_HASH_ROOT = "product_url:"

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
       AND (lower(trim(coalesce(nullif(e.canonical_url, ''), e.destination_url))) LIKE :path_end ESCAPE '!'
            OR lower(trim(coalesce(nullif(e.canonical_url, ''), e.destination_url))) LIKE :path_slash ESCAPE '!'
            OR lower(trim(coalesce(nullif(e.canonical_url, ''), e.destination_url))) LIKE :path_query ESCAPE '!'
            OR lower(trim(coalesce(nullif(e.canonical_url, ''), e.destination_url))) LIKE :path_fragment ESCAPE '!')
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
    ones, so the comparison is symmetric by construction.

    A stored value is trimmed of SPACES only (what SQL `trim()` removes in both dialects, so the
    candidate read finds every value accepted here). Then the part the comparison keeps -- scheme, host
    and path, everything before the first `?` or `#` -- must be printable ASCII, as the caller's whole
    URL must be: a tab, a newline or a raw non-ASCII character THERE means no caller URL can equal it, so
    it is not a page. The query and fragment are dropped unread, so a stored `.../p?ref=café` IS the
    caller's `.../p`, exactly as the SQL read (which does not look past the path) treats it."""
    text = str(url or "").strip(" ")
    compared = re.split(r"[?#]", text, maxsplit=1)[0]
    if not text or any(not 0x21 <= ord(ch) <= 0x7E for ch in compared):
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

    PRINTABLE ASCII ONLY, never trimmed: a card's URL is ASCII (a non-ASCII page is percent-encoded), so
    anything else -- whitespace, a zero-width or format character, NBSP, a raw unicode letter (whose case
    Python and SQL fold differently) -- is a caller bug, refused before it reaches a bind or a fingerprint.
    """
    text = str(product_url or "")
    if not text or len(text) > MAX_PRODUCT_URL_CHARS or any(not 0x21 <= ord(ch) <= 0x7E for ch in text):
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


#: The query or fragment keys that name a variant (after decoding, case-folded). Shopify reads `variant`.
_VARIANT_KEYS = frozenset({"variant", "variant[]"})

#: Decoding rounds a KEY may need (percent-encoding, `+`, HTML entities) before it is compared with
#: `_VARIANT_KEYS`; past this the key is refused, never matched half-decoded. A VALUE is decoded once,
#: as a store reads it (`check_url_variant`).
_MAX_DECODE_ROUNDS = 4


def _decoded(text: str) -> str:
    """`text` decoded until stable, or `invalid_request` when it is still changing after
    `_MAX_DECODE_ROUNDS` rounds (an encoding this lane will not chase)."""
    for _ in range(_MAX_DECODE_ROUNDS + 1):
        decoded = html.unescape(unquote_plus(text))
        if decoded == text:
            return text
        text = decoded
    raise PurchaseRefused("invalid_request", "product_url is encoded deeper than this lane reads")


def check_url_variant(product_url: str, variant_id: Optional[str]) -> None:
    """Refuse a URL whose query or fragment names a variant other than the request's `variant_id`.

    The request must carry a `variant_id`. The query and the fragment are each read as a parameter list,
    split BEFORE anything is decoded, so an encoded separator inside a value stays part of that value.

    KEYS ARE FOUND STRICTLY, VALUES ARE READ AS A STORE READS THEM. A key is looked for after every `&`
    and every `;` (an older store, and an HTML-escaped `&amp;`, split there), then decoded (`_decoded`)
    and case-folded; a key that is `variant` or `variant[]` must have a value equal to `variant_id`, else
    `invalid_request` ("does not match"). The value is what a current store reads: everything up to the
    next `&` (a `;` after it is part of it, so `<id>;x` is not `<id>`), percent-decoded ONCE (so a
    double-encoded `%2534...` is not `4...`). Another id, leading zeros, a blank, a missing value, `<id>;x`
    and `<id>%3Bx` all fail it. A key that decodes to `variant=...` (an encoded `=`) is refused as
    unreadable. Every other parameter -- `utm_content=variant_a`, `sections=product-variant-picker`, a
    fragment word such as `variant-picker` -- is not a variant key and is ignored, as a store ignores it.
    Call it after `normalize_product_url` has accepted the URL."""
    if variant_id is None:
        raise PurchaseRefused("invalid_request", "a product_url purchase names its variant by variant_id")
    try:
        parts = urlsplit(product_url)
    except ValueError:
        raise PurchaseRefused("invalid_request", "product_url is not a usable URL") from None
    for section in (parts.query, parts.fragment):
        for field in section.split("&"):
            pieces = field.split(";")
            for index, piece in enumerate(pieces):
                raw_key, has_value, raw_value = piece.partition("=")
                name, encoded_equals, _ = _decoded(raw_key).lower().partition("=")
                if name not in _VARIANT_KEYS:
                    continue
                if encoded_equals:
                    raise PurchaseRefused("invalid_request", "product_url's variant is in a form this lane cannot read")
                store_value = ";".join([raw_value, *pieces[index + 1:]])
                if not has_value or unquote_plus(store_value) != variant_id:
                    raise PurchaseRefused("invalid_request", "product_url's variant does not match variant_id")


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
        row for row in (dict(r) for r in rows)
        if _normalized(row.get("canonical_url") or row.get("destination_url")) == normalized_url
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

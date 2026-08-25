"""One vocabulary for product availability, shared by the SEED-LANE readers of it.

SCOPE, stated honestly: this unifies the seed/offer ingestion readers (employee_products,
external_seed_audit, external_offers_service) and the readiness projections. Several
DOWNSTREAM gates still carry their own local literal sets — agent_shop_gateway,
beauty_external_ranking, offer_buyability, offer_classification, product_exposure_service.
Those are denylists on `out_of_stock` and the writers above now emit exactly that canonical
token, so they are consistent today, but they are not yet routed through here.

WHY THIS MODULE EXISTS
----------------------
Availability reaches us as free text from several producers that do not agree with each
other: the crawler writes human strings ("Out of Stock", "Sold Out"), JSON-LD carries
schema.org URLs ("https://schema.org/SoldOut"), some feeds carry an `inventory_status`,
and internal scoring writes canonical tokens. Before this module, four separate call
sites each re-implemented the mapping with a different set of literals, and MEASURED
2026-08-25 they disagreed on 16 of 23 real spellings. Two examples of what that cost:

  - `services/external_offers_service._availability_from_raw` matched only the InStock
    and OutOfStock substrings, so ten of the twelve schema.org ItemAvailability values
    resolved to "unknown" — including SoldOut, and Reserved which schema.org defines as
    "reserved and therefore not available".
  - the readiness denylists omitted the SPACE-separated forms, so a lowercased
    "out of stock" read as IN STOCK.

The same defect shipped in PIVOTA-Agent (#2099): a guard compared one literal that only
one of two producers ever emitted, so it could not fire on the other lane. Normalising at
a single choke point is what stops that recurring — a predicate must never be written
against whichever producer its author happened to be reading.

THE UNKNOWN RULE
----------------
Anything we cannot confidently classify returns None (unknown) — never OUT_OF_STOCK.
A false "out of stock" silently deletes a live, sellable product from a lane; a false
"in stock" builds a cart that dies at checkout. Both are bad, so ambiguity is reported
as ambiguity and the caller decides. States that are orderable-but-not-immediate
(back order, pre-order) and channel-restricted states (in-store only) are deliberately
UNKNOWN rather than forced into a binary they do not fit.
"""

from __future__ import annotations

import re
from typing import Any, Iterator, Optional

IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"

# Bare tokens, after lowercasing, stripping, and collapsing separators (space/underscore/
# hyphen) — so "Out of Stock", "out-of-stock" and "out_of_stock" all arrive here as
# "outofstock" and only one spelling per concept needs listing.
_OUT_OF_STOCK_TOKENS = frozenset({
    "outofstock",
    "soldout",
    "unavailable",
    "notavailable",
    "oos",
    # schema.org: "the item is reserved and therefore not available".
    "reserved",
    # DELIBERATE DEVIATION from the unknown rule below. schema.org says only "the item has
    # been discontinued" — a LIFECYCLE statement, not an inventory one, and retailers do sell
    # through remaining stock of discontinued SKUs. We still treat it as not-servable because
    # a discontinued SKU is a bad serve, but this is a product choice, not an inference from
    # the source's own semantics. Do not "correct" the other entries to match this one.
    "discontinued",
})

_IN_STOCK_TOKENS = frozenset({
    "instock",
    "available",
    "instocknow",
    # Both spellings of ONE concept — stock remains, it is just scarce. They must agree:
    # shipping "limited availability" as in-stock while "limited stock" read as unknown was
    # precisely the split-by-spelling defect this module exists to remove.
    "limitedavailability",
    "limitedstock",
})

# Orderable-but-not-immediate, or channel-restricted. Forcing these into either bucket
# would be a guess; they are reported as unknown on purpose.
#
# This set is checked BEFORE the decisive sets, which makes it a precedence guard rather
# than a behavioural one: today these tokens appear in no other set, so removing the check
# changes nothing and no test can kill it. Its value is that if someone later classifies one
# of these as in/out of stock, ambiguity still wins. That only holds while the sets stay
# disjoint, so the disjointness is pinned by a test rather than left to trust.
_AMBIGUOUS_TOKENS = frozenset({
    "backorder",
    "backordered",
    # WooCommerce's third stock_status value.
    "onbackorder",
    "preorder",
    "presale",
    "madetoorder",
    # CHANNEL statements, orthogonal to stock: a page can say "online only" and be sold out.
    # Both sides of the axis are unknown — treating OnlineOnly as in-stock invented a positive
    # from a signal carrying no inventory information at all, while its mirror image was
    # unknown. One axis must not get two opposite treatments.
    "onlineonly",
    "instoreonly",
})


# Matches ONLY a whole schema.org IRI. A loose `"schema.org/" in text` split also fired on
# any string that merely CONTAINED the substring, so a tracking URL like
# https://example.com/?x=schema.org/InStock resolved to in_stock.
_SCHEMA_ORG_IRI_RE = re.compile(r"^https?://(?:www\.)?schema\.org/([A-Za-z]+)$")


def _candidate_values(value: Any) -> Iterator[Any]:
    """Yield the scalar availability signals carried by a JSON-LD-shaped value.

    JSON-LD legitimately writes an IRI as {"@id": ...} and legitimately allows a list. The
    call sites stringify whatever they receive, so without this a dict or list arrived as its
    repr and was classified by whichever IRI happened to be printed LAST — which meant
    ["OutOfStock", "InStock"] resolved to in_stock.
    """
    if isinstance(value, dict):
        for key in ("@id", "@value", "id", "value", "availability"):
            if key in value:
                yield value[key]
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield item
        return
    yield value


def _canonical_token(value: Any) -> str:
    """Lowercase, unwrap a whole schema.org IRI, and remove separators/punctuation."""
    text = ("" if value is None else str(value)).strip()
    if not text:
        return ""
    iri = _SCHEMA_ORG_IRI_RE.match(text)
    if iri:
        text = iri.group(1)
    return "".join(ch for ch in text.lower() if ch.isalnum())


# Substring-searchable OUT-of-stock phrases, matched against the canonicalised token when
# exact lookup misses. See _normalize_scalar for why this direction — and ONLY this
# direction — falls back to substring matching.
#
# Every entry must be long and distinctive enough that it cannot appear inside an unrelated
# word. "oos" is deliberately NOT here ("choose" contains it), nor is "reserved"
# ("preserved" contains it); both stay exact-token-only.
_OUT_OF_STOCK_PHRASES = (
    "outofstock",
    "soldout",
    "nolongerinstock",
    "nolongeravailable",
    "notinstock",
    "notavailable",
    "unavailable",
    "discontinued",
)

# Used only to detect a self-contradictory phrase, never to conclude in-stock.
_IN_STOCK_PHRASES = ("instock", "available")


def _phrase_verdict(token: str) -> Optional[str]:
    """Substring fallback for OUT-of-stock only, with a contradiction guard.

    The direction is asymmetric on purpose, because the two errors do not cost the same.
    Every serving gate in this repo is a DENYLIST on out_of_stock, so `unknown` is servable:
    failing to notice "Temporarily out of stock" hands a shopper a dead product, while
    failing to notice free-text in-stock costs nothing (it stays servable either way).
    So we search generously for out-of-stock and NEVER infer in-stock from a substring —
    inferring it would fabricate a positive from a phrase that might be negating it
    ("not in stock", "0 in stock").

    The guard: an out-of-stock phrase only wins if, once removed, no in-stock phrase remains.
    That stops "Sold out of the old model, but in stock now" from reading as out of stock —
    the exact false positive that unanchored substring matching produces.
    """
    matched = [phrase for phrase in _OUT_OF_STOCK_PHRASES if phrase in token]
    if not matched:
        return None
    remainder = token
    for phrase in matched:
        remainder = remainder.replace(phrase, "")
    if any(phrase in remainder for phrase in _IN_STOCK_PHRASES):
        return None  # says both things; that is not evidence of either
    return OUT_OF_STOCK


def _normalize_scalar(value: Any) -> Optional[str]:
    # Shopify's products.json carries a BOOLEAN `available`. str(False) is "False", which is
    # not a token, so a boolean false would otherwise read as unknown rather than out of stock.
    if value is True:
        return IN_STOCK
    if value is False:
        return OUT_OF_STOCK
    token = _canonical_token(value)
    if not token:
        return None
    if token in _AMBIGUOUS_TOKENS:
        return None
    if token in _OUT_OF_STOCK_TOKENS:
        return OUT_OF_STOCK
    if token in _IN_STOCK_TOKENS:
        return IN_STOCK
    # Exact lookup missed. Availability also arrives as human prose ("Temporarily out of
    # stock", "Out Of Stock - notify me"), which the previous substring-matching
    # implementation caught and pure exact-token matching does not.
    return _phrase_verdict(token)


def normalize_availability(value: Any) -> Optional[str]:
    """Map any producer's availability signal onto IN_STOCK / OUT_OF_STOCK / None.

    None means "we could not determine it" — an empty, unrecognised, deliberately ambiguous,
    or SELF-CONTRADICTORY signal. It NEVER means out of stock.
    """
    verdicts = {v for v in (_normalize_scalar(item) for item in _candidate_values(value)) if v is not None}
    if len(verdicts) != 1:
        # Zero decisive signals, or two that disagree. A container that says both OutOfStock
        # and InStock is not evidence of either; picking one would be a coin flip on whether
        # a shopper gets a dead cart.
        return None
    return verdicts.pop()


def is_out_of_stock(value: Any) -> bool:
    """True only when the signal AFFIRMATIVELY says out of stock.

    Unknown is not a negative: callers that decline on this must not decline on silence,
    or every producer that does not publish availability gets dropped.
    """
    return normalize_availability(value) == OUT_OF_STOCK


def is_in_stock(value: Any) -> bool:
    """True only when the signal AFFIRMATIVELY says in stock."""
    return normalize_availability(value) == IN_STOCK

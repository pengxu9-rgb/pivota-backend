"""One vocabulary for product availability, shared by every reader of it.

WHY THIS MODULE EXISTS
----------------------
Availability reaches us as free text from several producers that do not agree with each
other: the crawler writes human strings ("Out of Stock", "Sold Out"), JSON-LD carries
schema.org URLs ("https://schema.org/SoldOut"), some feeds carry an `inventory_status`,
and internal scoring writes canonical tokens. Before this module, four separate call
sites each re-implemented the mapping with a different set of literals, and MEASURED
2026-08-25 they disagreed on 16 of 23 real spellings. Two examples of what that cost:

  - `services/external_offers_service._availability_from_raw` matched only the InStock
    and OutOfStock substrings, so `https://schema.org/SoldOut` — an unambiguous
    out-of-stock signal in schema.org's own vocabulary — resolved to "unknown", along
    with Discontinued, BackOrder, PreOrder, LimitedAvailability, OnlineOnly, InStoreOnly.
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

from typing import Any, Optional

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
    "discontinued",
})

_IN_STOCK_TOKENS = frozenset({
    "instock",
    "available",
    "instocknow",
    "limitedavailability",
    "onlineonly",
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
    "preorder",
    "presale",
    "instoreonly",
    "limitedstock",
})


def _canonical_token(value: Any) -> str:
    """Lowercase, drop any schema.org URL prefix, and remove separators/punctuation."""
    text = ("" if value is None else str(value)).strip().lower()
    if not text:
        return ""
    # JSON-LD carries the full IRI: https://schema.org/SoldOut, http://schema.org/InStock.
    if "schema.org/" in text:
        text = text.rsplit("schema.org/", 1)[1]
    return "".join(ch for ch in text if ch.isalnum())


def normalize_availability(value: Any) -> Optional[str]:
    """Map any producer's availability signal onto IN_STOCK / OUT_OF_STOCK / None.

    None means "we could not determine it" — an empty, unrecognised, or deliberately
    ambiguous signal. It NEVER means out of stock.
    """
    token = _canonical_token(value)
    if not token:
        return None
    if token in _AMBIGUOUS_TOKENS:
        return None
    if token in _OUT_OF_STOCK_TOKENS:
        return OUT_OF_STOCK
    if token in _IN_STOCK_TOKENS:
        return IN_STOCK
    return None


def is_out_of_stock(value: Any) -> bool:
    """True only when the signal AFFIRMATIVELY says out of stock.

    Unknown is not a negative: callers that decline on this must not decline on silence,
    or every producer that does not publish availability gets dropped.
    """
    return normalize_availability(value) == OUT_OF_STOCK


def is_in_stock(value: Any) -> bool:
    """True only when the signal AFFIRMATIVELY says in stock."""
    return normalize_availability(value) == IN_STOCK

"""One vocabulary for availability, and the four call sites that must share it.

Context: four separate implementations of this mapping existed, and MEASURED 2026-08-25
they disagreed on 16 of 23 real spellings. The same defect class shipped in PIVOTA-Agent
(#2099): a predicate written against whichever producer its author happened to be reading,
so it could not fire on the other lane.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.availability_vocabulary import (
    IN_STOCK,
    OUT_OF_STOCK,
    is_in_stock,
    is_out_of_stock,
    normalize_availability,
)


@pytest.mark.parametrize(
    "raw",
    [
        "out_of_stock", "out of stock", "Out of Stock", "OUT OF STOCK", "out-of-stock",
        "outofstock", "OutOfStock",
        "sold_out", "sold out", "Sold Out", "soldout", "SoldOut",
        "unavailable", "Unavailable", "not available", "notavailable",
        "oos", "OOS",
        "discontinued", "Discontinued",
        # JSON-LD carries the full IRI, and schema.org's own vocabulary includes SoldOut
        # and Discontinued — the reader that only matched OutOfStock called these unknown.
        "https://schema.org/OutOfStock", "http://schema.org/OutOfStock",
        "https://schema.org/SoldOut", "https://schema.org/Discontinued",
    ],
)
def test_every_out_of_stock_spelling_is_recognised(raw):
    assert normalize_availability(raw) == OUT_OF_STOCK
    assert is_out_of_stock(raw) is True
    assert is_in_stock(raw) is False


@pytest.mark.parametrize(
    "raw",
    [
        "in_stock", "in stock", "In Stock", "IN STOCK", "in-stock", "instock", "InStock",
        "available", "Available",
        "https://schema.org/InStock", "http://schema.org/InStock",
        "https://schema.org/LimitedAvailability", "https://schema.org/OnlineOnly",
    ],
)
def test_every_in_stock_spelling_is_recognised(raw):
    assert normalize_availability(raw) == IN_STOCK
    assert is_in_stock(raw) is True
    assert is_out_of_stock(raw) is False


@pytest.mark.parametrize(
    "raw",
    [
        None, "", "   ", "wibble", "???", 42, [], {},
        # Deliberately ambiguous: orderable-but-not-immediate, or channel-restricted.
        # Forcing these into a binary would be a guess.
        "backorder", "back order", "BackOrder", "https://schema.org/BackOrder",
        "preorder", "pre-order", "https://schema.org/PreOrder", "https://schema.org/PreSale",
        "https://schema.org/InStoreOnly",
    ],
)
def test_unknown_is_never_a_false_negative(raw):
    # A wrong "out of stock" silently deletes a live, sellable product from a lane. Anything
    # we cannot classify must report as unknown and let the caller decide.
    assert normalize_availability(raw) is None
    assert is_out_of_stock(raw) is False
    assert is_in_stock(raw) is False


def test_the_four_call_sites_now_agree_on_every_spelling():
    """The regression that motivated this module: four readers, four different answers."""
    from routes.employee_products import _normalize_seed_availability
    from services.external_seed_audit import normalize_seed_availability
    from services.external_offers_service import _availability_from_raw
    from readiness.summary import _snapshot_variant_agent_push_projection  # noqa: F401  (import-guard)

    spellings = [
        "out_of_stock", "Out of Stock", "out of stock", "OutOfStock", "sold_out",
        "Sold Out", "sold out", "soldout", "unavailable", "oos", "discontinued",
        "https://schema.org/OutOfStock", "https://schema.org/SoldOut",
        "in_stock", "In Stock", "in stock", "instock", "available",
        "https://schema.org/InStock",
    ]
    for raw in spellings:
        verdicts = {
            "employee_products": _normalize_seed_availability(raw),
            "seed_audit": normalize_seed_availability(raw),
            "offers_service": _availability_from_raw(raw),
        }
        assert len(set(verdicts.values())) == 1, f"{raw!r} still splits the readers: {verdicts}"


def test_readiness_denylist_replacement_is_behaviour_preserving():
    """readiness/scoring.py writes only canonical tokens; the swap must not move any of them."""
    previous_denylist = {"out_of_stock", "outofstock", "sold_out", "soldout", "unavailable"}
    for value in ["in_stock", "out_of_stock", "", "unknown", *previous_denylist]:
        assert (value not in previous_denylist) == (not is_out_of_stock(value)), value


def test_schema_org_sold_out_no_longer_reads_as_unknown():
    """The specific reachable defect: JSON-LD SoldOut resolved to 'unknown' before."""
    from services.external_offers_service import _availability_from_raw

    assert _availability_from_raw("https://schema.org/SoldOut") == OUT_OF_STOCK
    assert _availability_from_raw("https://schema.org/Discontinued") == OUT_OF_STOCK
    assert _availability_from_raw("sold out") == OUT_OF_STOCK
    assert _availability_from_raw("unavailable") == OUT_OF_STOCK
    # ...and an unreadable signal is still 'unknown', not a fabricated negative.
    assert _availability_from_raw("wibble") == "unknown"
    assert _availability_from_raw(None) == "unknown"


def test_unrecognised_values_still_pass_through_where_that_contract_exists():
    """employee_products/seed_audit callers read a passthrough as 'unknown'.

    Collapsing those to None here would instead make them read as AVAILABLE at
    routes/employee_products.py, so the passthrough is load-bearing, not cosmetic.
    """
    from routes.employee_products import _normalize_seed_availability
    from services.external_seed_audit import normalize_seed_availability

    assert _normalize_seed_availability("some vendor state") == "some_vendor_state"
    assert normalize_seed_availability("some vendor state") == "some_vendor_state"
    assert _normalize_seed_availability(None) is None
    assert _normalize_seed_availability("  ") is None


def test_ambiguous_tokens_stay_disjoint_from_the_decisive_sets():
    """The ambiguity check is a PRECEDENCE guard, and only works while the sets are disjoint.

    Removing that check is an equivalent mutant today — the tokens appear nowhere else, so it
    has no observable effect and no test can kill it. What this pins instead is the invariant
    it depends on: if someone later adds "backorder" to the in-stock set, ambiguity must still
    win, and they should have to come here and say so deliberately.
    """
    from utils.availability_vocabulary import (
        _AMBIGUOUS_TOKENS,
        _IN_STOCK_TOKENS,
        _OUT_OF_STOCK_TOKENS,
    )

    assert not (_AMBIGUOUS_TOKENS & _IN_STOCK_TOKENS)
    assert not (_AMBIGUOUS_TOKENS & _OUT_OF_STOCK_TOKENS)
    # And the two decisive sets must never overlap each other either.
    assert not (_IN_STOCK_TOKENS & _OUT_OF_STOCK_TOKENS)

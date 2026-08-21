"""The region predicate is the US gate, parameterized — and provably the same SQL.

ADR-024 (PR #1796) Phase 1 item 5 introduces `has_offer_priced_for_region` beside
`_HAS_US_OFFER_EXISTS` with ZERO behavior change. "Zero" is only a claim until
something asserts the two strings are equal, so that assertion is the first test
here: while both names exist they cannot drift, and the day the US case stops
emitting what it emitted, this fails rather than the sitemap quietly halving.
"""
from __future__ import annotations

import pytest

import services.index_pipeline_state_service as ips
from services.priced_offer_sql import priced_offer_exists_sql
from services.region_pricing import (
    REGION_PRICING_CURRENCY,
    has_offer_priced_for_region_sql,
    normalize_region,
    pricing_currency_for_region,
    region_currency_predicate,
)

# The literal the US gate has always been built from — spelled out here on
# purpose, NOT imported, so this test still fails if the module changes it.
US_PREDICATE = "upper(trim(coalesce(co.currency, ''))) = 'USD'"


# --- zero behavior change: the US case is byte-identical --------------------

def test_us_extra_predicate_is_byte_identical():
    assert region_currency_predicate("US") == US_PREDICATE


def test_us_region_sql_is_byte_identical_to_the_hand_spelled_gate():
    expected = priced_offer_exists_sql("cp.product_key", extra_predicate=US_PREDICATE)
    assert has_offer_priced_for_region_sql("cp.product_key", "US") == expected


def test_the_live_constant_is_now_the_us_case_of_the_region_predicate():
    """_HAS_US_OFFER_EXISTS and the helper cannot drift while both names exist."""
    assert ips._HAS_US_OFFER_EXISTS == has_offer_priced_for_region_sql("cp.product_key", "US")


def test_the_live_constant_still_emits_the_string_it_always_emitted():
    """Belt and braces: pin the constant against the literal, not just the helper —
    a mutation in BOTH the helper and the map would otherwise pass the test above."""
    assert ips._HAS_US_OFFER_EXISTS == priced_offer_exists_sql(
        "cp.product_key", extra_predicate=US_PREDICATE
    )


# --- other regions ----------------------------------------------------------

@pytest.mark.parametrize(
    "region,currency",
    [("GB", "GBP"), ("JP", "JPY"), ("FR", "EUR"), ("KR", "KRW"), ("CA", "CAD")],
)
def test_non_us_regions_swap_only_the_currency(region, currency):
    assert region_currency_predicate(region) == (
        f"upper(trim(coalesce(co.currency, ''))) = '{currency}'"
    )
    # ...and the surrounding EXISTS is the US one with that one conjunct swapped
    assert has_offer_priced_for_region_sql("cp.product_key", region) == (
        has_offer_priced_for_region_sql("cp.product_key", "US").replace("'USD'", f"'{currency}'")
    )


def test_eurozone_regions_share_one_currency():
    # FR/HR/FI are three regions, one currency — the map is region-keyed, not
    # currency-keyed, and is not invertible.
    assert {pricing_currency_for_region(r) for r in ("FR", "HR", "FI")} == {"EUR"}


def test_alias_threads_through_to_the_predicate():
    sql = has_offer_priced_for_region_sql("crt.subject_key", "JP", alias="o2")
    assert "upper(trim(coalesce(o2.currency, ''))) = 'JPY'" in sql
    assert "o2.product_key = crt.subject_key" in sql
    assert "co." not in sql


# --- refusals: no default region, ever --------------------------------------

@pytest.mark.parametrize("region", ["DE", "XX", "ZZ"])
def test_unlisted_but_well_formed_region_raises(region):
    """'DE' is a real ISO code we have not measured supply for. It must raise, not
    quietly become USD — a silent fallback answers a foreign buyer with a US-only
    catalog and nothing reports it."""
    with pytest.raises(ValueError) as exc:
        pricing_currency_for_region(region)
    assert "unknown pricing region" in str(exc.value)


@pytest.mark.parametrize("region", ["", None, "usa", "U", "U1", "US;--", "United States", 42])
def test_garbage_region_raises(region):
    with pytest.raises(ValueError):
        pricing_currency_for_region(region)


def test_has_offer_priced_for_region_sql_refuses_an_unknown_region():
    """The refusal reaches the SQL builder too — no half-built query escapes."""
    with pytest.raises(ValueError):
        has_offer_priced_for_region_sql("cp.product_key", "DE")


# --- normalization: case/whitespace only ------------------------------------

def test_case_and_whitespace_are_normalized_not_refused():
    """DOCUMENTED CHOICE: an ISO-3166 code is case-insensitive and arrives
    lower-cased from request fields, so case and surrounding whitespace are
    normalized. Everything else is refused by the lookup."""
    assert normalize_region(" gb ") == "GB"
    assert pricing_currency_for_region("gb") == "GBP"
    assert pricing_currency_for_region(" Jp\n") == "JPY"
    assert has_offer_priced_for_region_sql("cp.product_key", "us") == (
        has_offer_priced_for_region_sql("cp.product_key", "US")
    )


# --- the map itself ---------------------------------------------------------

def test_map_covers_the_measured_supply_regions():
    # the regions ADR-024 measured servable non-USD offers for, plus US.
    assert REGION_PRICING_CURRENCY == {
        "US": "USD", "GB": "GBP", "JP": "JPY", "FR": "EUR", "HR": "EUR",
        "FI": "EUR", "AU": "AUD", "SE": "SEK", "KR": "KRW", "HK": "HKD",
        "SG": "SGD", "CA": "CAD",
    }


def test_map_keys_and_values_are_canonical_codes():
    for region, currency in REGION_PRICING_CURRENCY.items():
        assert len(region) == 2 and region.isalpha() and region.isupper()
        assert len(currency) == 3 and currency.isalpha() and currency.isupper()

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


class TestPricingCurrencyForRegionOrNone:
    """The SOFT accessor (added by the ADR-024 map fold-in) normalizes for
    itself. offer_buyability happens to pre-normalize before calling, so
    without these rows a mutant that drops the accessor's own
    normalize_region call survives every existing test -- any OTHER caller
    passing raw input would then silently get None for a mapped region."""

    def test_normalizes_case_and_whitespace_itself(self):
        from services.region_pricing import pricing_currency_for_region_or_none
        assert pricing_currency_for_region_or_none(" gb ") == "GBP"
        assert pricing_currency_for_region_or_none("jp") == "JPY"

    def test_unmapped_and_garbage_yield_none_never_usd(self):
        from services.region_pricing import pricing_currency_for_region_or_none
        # Mutant killed: defaulting the .get() to 'USD' -- the exact assumption
        # every one of the four currency defects was built on.
        assert pricing_currency_for_region_or_none("DE") is None
        assert pricing_currency_for_region_or_none("") is None
        assert pricing_currency_for_region_or_none("usa") is None


# --- the MULTI-region disjunction, and its zero-change default ---------------

class TestHasOfferPricedForAnyRegion:
    """Serving more than one region must not change what serving ONE region emits.

    The whole safety argument for reading the region list from configuration is
    that the default is not merely equivalent but byte-identical to the string
    the US gate has always emitted. Everything else in this class is downstream
    of that.
    """

    def test_a_single_region_is_byte_identical_to_the_single_region_helper(self):
        from services.region_pricing import has_offer_priced_for_any_region_sql
        assert has_offer_priced_for_any_region_sql("cp.product_key", ["US"]) == (
            has_offer_priced_for_region_sql("cp.product_key", "US")
        )

    def test_the_default_serving_region_still_emits_the_original_us_string(self):
        """The live constant the eligibility SQL interpolates, with no env set."""
        assert ips._HAS_SERVING_REGION_OFFER_EXISTS == priced_offer_exists_sql(
            "cp.product_key", extra_predicate=US_PREDICATE
        )

    def test_no_parens_or_or_appear_in_the_single_region_form(self):
        """A wrapped single region would still be CORRECT SQL and would still
        break the byte-identity the test above depends on. Pin the shape too."""
        from services.region_pricing import has_offer_priced_for_any_region_sql
        sql = has_offer_priced_for_any_region_sql("cp.product_key", ["US"])
        assert " OR " not in sql
        assert not sql.startswith("(")

    def test_two_regions_or_the_two_single_region_predicates(self):
        from services.region_pricing import has_offer_priced_for_any_region_sql
        sql = has_offer_priced_for_any_region_sql("cp.product_key", ["US", "SG"])
        assert sql == (
            "("
            + has_offer_priced_for_region_sql("cp.product_key", "US")
            + " OR "
            + has_offer_priced_for_region_sql("cp.product_key", "SG")
            + ")"
        )
        assert "'USD'" in sql and "'SGD'" in sql

    def test_it_is_a_membership_test_and_never_a_conversion(self):
        """ADR-024 commitment 5. Widening the gate to a second region must not
        smuggle in a rate, a multiplication, or a cross-currency comparison."""
        from services.region_pricing import has_offer_priced_for_any_region_sql
        sql = has_offer_priced_for_any_region_sql("cp.product_key", ["US", "SG", "JP"])
        for forbidden in ("*", "/", "rate", "convert", "fx"):
            assert forbidden not in sql.lower()

    def test_duplicates_collapse_and_order_is_preserved(self):
        from services.region_pricing import has_offer_priced_for_any_region_sql
        assert has_offer_priced_for_any_region_sql("cp.product_key", ["US", "SG", "us"]) == (
            has_offer_priced_for_any_region_sql("cp.product_key", ["US", "SG"])
        )
        # A repeated entry must not re-collapse to the single-region form either.
        assert has_offer_priced_for_any_region_sql("cp.product_key", ["SG", "sg"]) == (
            has_offer_priced_for_region_sql("cp.product_key", "SG")
        )

    def test_an_unknown_region_raises_rather_than_matching_nothing(self):
        from services.region_pricing import has_offer_priced_for_any_region_sql
        with pytest.raises(ValueError, match="unknown pricing region"):
            has_offer_priced_for_any_region_sql("cp.product_key", ["US", "DE"])

    def test_no_regions_at_all_raises_rather_than_taking_the_index_dark(self):
        """An empty list would emit a predicate false for every row — every
        product blocked, and nothing in the output saying why."""
        from services.region_pricing import has_offer_priced_for_any_region_sql
        with pytest.raises(ValueError, match="at least one region"):
            has_offer_priced_for_any_region_sql("cp.product_key", [])


class TestServingPricingRegions:
    """Reading the region list from the environment."""

    def test_unset_means_us_only(self, monkeypatch):
        monkeypatch.delenv(ips._SERVING_REGIONS_ENV, raising=False)
        assert ips.serving_pricing_regions() == ["US"]

    def test_the_sql_constant_is_fixed_at_import_so_the_env_must_be_set_per_process(self, monkeypatch):
        """The predicate is a module-level constant. Setting the env AFTER import changes
        `serving_pricing_regions()` and nothing else — which is exactly why the runbook says
        the variable must be present on every PROCESS that recomputes eligibility (the
        onboarding job, `web`, and the worker's nightly index-health job), not just one.
        A refactor to a lazy read would silently change that deploy contract; this pins it."""
        monkeypatch.setenv(ips._SERVING_REGIONS_ENV, "US,SG")
        assert ips.serving_pricing_regions() == ["US", "SG"]
        assert ips._HAS_SERVING_REGION_OFFER_EXISTS == ips._HAS_US_OFFER_EXISTS

    @pytest.mark.parametrize("blank", ["", "   ", ",", " , "])
    def test_a_blank_setting_is_us_only_not_no_regions(self, monkeypatch, blank):
        """An operator who clears the var, or a deploy that sets it empty, must
        land on today's behaviour — not on a value that blocks every row."""
        monkeypatch.setenv(ips._SERVING_REGIONS_ENV, blank)
        assert ips.serving_pricing_regions() == ["US"]

    def test_it_parses_a_list_and_normalises_each_entry(self, monkeypatch):
        monkeypatch.setenv(ips._SERVING_REGIONS_ENV, " us , sg ")
        assert ips.serving_pricing_regions() == ["US", "SG"]

    def test_the_configured_list_reaches_the_sql(self, monkeypatch):
        """THE SEAM. The parser can be perfect and the builder can be perfect and
        the gate is still US-only if nothing joins them — the constant is built at
        import, so this asserts the composition rather than the module global."""
        from services.region_pricing import has_offer_priced_for_any_region_sql
        monkeypatch.setenv(ips._SERVING_REGIONS_ENV, "US,SG")
        sql = has_offer_priced_for_any_region_sql(
            "cp.product_key", ips.serving_pricing_regions()
        )
        assert "'SGD'" in sql

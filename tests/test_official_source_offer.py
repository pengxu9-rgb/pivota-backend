"""official_source trust signal on OfferNode (decision-grade `trust` dimension)."""
from services.pivot_query_service import _registrable_host, _is_official_brand_source


def test_registrable_host_strips_scheme_www_path():
    assert _registrable_host("https://www.cosrx.com/products/x") == "cosrx.com"
    assert _registrable_host("roundlab.com") == "roundlab.com"
    assert _registrable_host("https://shop.brand.co.uk/p") == "co.uk"  # last two labels
    assert _registrable_host(None) is None
    assert _registrable_host("") is None


def test_official_when_source_domain_matches_canonical_host():
    assert _is_official_brand_source("cosrx.com", "https://www.cosrx.com/products/snail-96") is True
    assert _is_official_brand_source("www.skin1004.com", "https://skin1004.com/products/centella") is True


def test_not_official_for_retailer_mirror_or_missing():
    assert _is_official_brand_source("sephora.com", "https://www.cosrx.com/products/x") is False
    assert _is_official_brand_source(None, "https://cosrx.com/x") is False
    assert _is_official_brand_source("cosrx.com", None) is False


# ---- ADR-019: official_source is seller-derived --------------------------------
# The comparison above is not wrong; its INPUTS were. On an external-seed mirror
# row `catalog_products.canonical_url` and `catalog_offers.source_domain` are both
# written from the SAME seed, so `_is_official_brand_source` asks whether a value
# equals itself. Measured on prod 2026-07-27: true for 2,646 of 2,646 candidate
# rows (100%), including 480 the seller-identity derivation typed `retailer`.

import pytest

from services.pivot_query_service import _build_canonical_offer_node


def _mirror_row(**overrides):
    """An external-seed mirror offer: source_domain and canonical_url agree
    BECAUSE they come from the same seed, not because the seller is the brand."""
    row = {
        "offer_id": "off_1",
        "catalog_track": "external_referral",
        "offer_catalog_track": "external_referral",
        "offer_is_first_party": False,
        "offer_offer_type": "retailer",
        "offer_source_domain": "ulta.com",
        "canonical_url": "https://www.ulta.com/p/cosrx-snail-mucin",
        "merchant_effective_price": "20.00",
        "currency": "USD",
    }
    row.update(overrides)
    return row


def _node(row, monkeypatch, *, flag):
    if flag:
        monkeypatch.setenv("OFFICIAL_SOURCE_SELLER_DERIVED", "true")
    else:
        monkeypatch.delenv("OFFICIAL_SOURCE_SELLER_DERIVED", raising=False)
    return _build_canonical_offer_node(row, [])


def test_external_referral_tautology_is_dead_even_with_flag_off(monkeypatch):
    """This test used to PIN the defect (flag-off => True, 'the lie: a ulta.com
    retailer offer') so the flag flip would be provably a change. That pin became
    untenable the moment source_domain started being stamped on every mirror
    offer (the domain-less-offer audit work): the false-positive cohort would
    have grown from 2,646 rows to the entire external_referral lane. The lane is
    now excluded from the legacy disjunct in code — its two hosts come from the
    SAME seed record and can never be independent evidence."""
    node = _node(_mirror_row(), monkeypatch, flag=False)
    assert node.official_source is False


def test_retailer_mirror_is_not_official_when_seller_derived(monkeypatch):
    node = _node(_mirror_row(), monkeypatch, flag=True)
    assert node.official_source is False


def test_unknown_seller_mirror_is_not_official_even_with_flag_off(monkeypatch):
    """The derivation returns NULL offer_type when it has no positive evidence —
    'do not guess'. The read path must not manufacture a positive claim from it.
    This was the larger prod cohort: 2,166 of the 2,646."""
    row = _mirror_row(offer_offer_type=None, offer_source_domain="somebrand.com",
                      canonical_url="https://somebrand.com/p/x")
    assert _node(row, monkeypatch, flag=False).official_source is False


def test_first_party_mirror_row_stays_official(monkeypatch):
    """A genuine self-seed (the brand's own storefront) is official via its
    STORED seller identity — the carve-out must not demote it."""
    row = _mirror_row(offer_is_first_party=True, offer_offer_type="brand_direct")
    assert _node(row, monkeypatch, flag=False).official_source is True
    assert _node(row, monkeypatch, flag=True).official_source is True


def test_legacy_disjunct_survives_for_independent_lanes(monkeypatch):
    """internal_merchant rows keep today's flag-off behaviour: their
    source_domain (merchant ingest) and canonical_url are independently sourced,
    which is the comparison _is_official_brand_source exists for. Only the
    same-record external_referral lane is carved out; retiring THIS path stays
    the ADR-019 flag's job."""
    row = _mirror_row(catalog_track="internal_merchant",
                      offer_catalog_track="internal_merchant",
                      offer_offer_type=None,
                      offer_source_domain="cosrx.com",
                      canonical_url="https://www.cosrx.com/products/snail-96")
    assert _node(row, monkeypatch, flag=False).official_source is True
    assert _node(row, monkeypatch, flag=True).official_source is False


def test_brand_direct_stays_official_under_both_flags(monkeypatch):
    """The change must cost nothing real. A brand_direct offer is is_first_party
    (the derivation sets them together), so it is official either way — this is
    why the flip has no legitimate blast radius."""
    row = _mirror_row(offer_offer_type="brand_direct", offer_is_first_party=True,
                      offer_source_domain="cosrx.com",
                      canonical_url="https://cosrx.com/products/snail-96")
    assert _node(row, monkeypatch, flag=False).official_source is True
    assert _node(row, monkeypatch, flag=True).official_source is True


# Flag-parsing tests use an internal_merchant fixture: since the external_referral
# lane was carved out of the legacy disjunct (its answer is now flag-independent),
# only a lane where the disjunct still applies can make the flag's two states
# observable — a mirror-row fixture here would pass vacuously with the flag
# parsing entirely broken.
def _independent_row(**overrides):
    row = _mirror_row(catalog_track="internal_merchant",
                      offer_catalog_track="internal_merchant",
                      offer_offer_type=None,
                      offer_source_domain="cosrx.com",
                      canonical_url="https://www.cosrx.com/products/snail-96")
    row.update(overrides)
    return row


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_flag_accepts_the_repo_standard_truthy_values(monkeypatch, value):
    monkeypatch.setenv("OFFICIAL_SOURCE_SELLER_DERIVED", value)
    assert _build_canonical_offer_node(_independent_row(), []).official_source is False


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "maybe"])
def test_flag_is_off_for_anything_else(monkeypatch, value):
    monkeypatch.setenv("OFFICIAL_SOURCE_SELLER_DERIVED", value)
    assert _build_canonical_offer_node(_independent_row(), []).official_source is True


def test_official_source_equals_is_first_party_when_seller_derived(monkeypatch):
    """The whole decision in one assertion: no independent derivation survives on
    the read path. If someone reintroduces a URL comparison, this fails."""
    monkeypatch.setenv("OFFICIAL_SOURCE_SELLER_DERIVED", "1")
    for first_party in (True, False):
        for src, canon in (("ulta.com", "https://ulta.com/p"),
                           ("cosrx.com", "https://cosrx.com/p"),
                           (None, None),
                           ("a.com", "https://b.com/p")):
            node = _build_canonical_offer_node(
                _mirror_row(offer_is_first_party=first_party,
                            offer_source_domain=src, canonical_url=canon), [])
            assert node.official_source is first_party


def test_the_fix_applies_to_EVERY_track_not_just_the_offending_one(monkeypatch):
    """The universality of the fix, which nothing else pins.

    Review mutation: gating the seller-derived branch on
    `catalog_track == "external_referral"` (or `!= "internal_merchant"`) passed
    the entire file, because every other fixture here is external_referral. That
    scoping is precisely the option ADR-019 considered and REJECTED — leaving a
    disjunct whose only live cohort is empty means dead code that reads as
    load-bearing, and the next person adding a track has to rediscover why.

    So: an internal_merchant row whose source_domain and canonical_url agree must
    ALSO take its answer from is_first_party alone. Note this is observable only
    with is_first_party=False, which no live internal_merchant row has today —
    the point is that the RULE holds, not that the data happens to.
    """
    row = _mirror_row(
        catalog_track="internal_merchant",
        offer_catalog_track="internal_merchant",
        offer_is_first_party=False,
        offer_offer_type=None,
        offer_source_domain="somebrand.com",
        canonical_url="https://somebrand.com/products/x",
    )
    # Both directions, per the repo rule that a gate must be shown to answer both
    # ways: OFF leaves this track untouched, ON applies the fix to it too.
    monkeypatch.delenv("OFFICIAL_SOURCE_SELLER_DERIVED", raising=False)
    assert _build_canonical_offer_node(row, []).official_source is True
    monkeypatch.setenv("OFFICIAL_SOURCE_SELLER_DERIVED", "1")
    assert _build_canonical_offer_node(row, []).official_source is False


def test_flag_tolerates_surrounding_whitespace(monkeypatch):
    """`.strip()` was untested. A value pasted from a dashboard often carries it.
    Uses the independent-lane fixture for the same reason as the parsing tests
    above — a mirror row is False under both flag states now."""
    monkeypatch.setenv("OFFICIAL_SOURCE_SELLER_DERIVED", "  true  ")
    assert _build_canonical_offer_node(_independent_row(), []).official_source is False

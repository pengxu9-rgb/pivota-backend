"""services/payment_orchestration/a2a_markets.py: which markets the A2A rail takes, and their currency.

Pinned here: unset means the US only (the rail as before); the env is the whole list, and a code outside the table
turns nothing on; every market's currency is the market registry's; KR and TW are priced in their own currency
without joining the Reap lane's market map; NO address rule of ours in any market (a region or postcode given is
sent as written, neither is ever required: the store platform's checkout and Visa judge the address); and the
router takes a currency only while its market is on.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.reap_agentic_purchase as reap_svc  # noqa: E402
from services import market_registry  # noqa: E402
from services.payment_orchestration import a2a_markets as markets  # noqa: E402
from services.payment_orchestration import a2a_shipping_address as shipping  # noqa: E402
from services.payment_orchestration import router as rail_router  # noqa: E402

EMAIL = "pat@example.test"
#: One valid address per market (placeholder people and streets).
VALID = {
    "US": {"region": "TX", "postalCode": "78701"},
    "CA": {"region": "ON", "postalCode": "K1A 0B1"},
    "AU": {"region": "NSW", "postalCode": "2000"},
    "NZ": {"region": "AUK", "postalCode": "1010"},
    "SG": {"postalCode": "018989"},
    "JP": {"region": "13", "postalCode": "100-0001"},
    "KR": {"region": "11", "postalCode": "04524"},
    "TW": {"region": "TPE", "postalCode": "100"},
    "HK": {},  # no region, no postcode
}


def _address(country, **over):
    base = {"firstName": "Pat", "lastName": "Example", "phone": "+15125550100", "addressLine1": "1 Example St",
            "city": "Example City", "country": country, **VALID[country]}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


@pytest.fixture(autouse=True)
def _no_markets_env(monkeypatch):
    monkeypatch.delenv(markets.MARKETS_ENV, raising=False)


@pytest.mark.parametrize("raw,expected", [
    (None, {"US"}), ("", {"US"}), ("   ", {"US"}), ("US", {"US"}), ("us, jp", {"US", "JP"}), ("CA", {"CA"}),
    ("CA,XX,GB", {"CA"}), ("XX", set()), ("U S", set()), ("US,HK", {"US", "HK"}),
])
def test_unset_is_the_us_only_and_the_env_is_the_whole_list(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv(markets.MARKETS_ENV, raw)
    assert markets.enabled_markets() == frozenset(expected)


def test_every_market_is_the_registrys_with_its_currency():
    assert set(markets.MARKETS) == {"US", "CA", "AU", "NZ", "SG", "JP", "KR", "TW", "HK"}
    for market in markets.MARKETS:
        assert markets.currency_for(market) == market_registry.currency_for(market), market
    assert markets.currency_for("GB") is None and markets.currency_for("HK") == "HKD"
    assert market_registry.market("TW")["status"] == "off"  # listed for its currency, served nowhere


def test_the_reap_lanes_markets_are_unchanged_and_only_the_a2a_rail_prices_korea_and_taiwan():
    import routes.agent_commerce_reap as reap_routes

    assert set(reap_routes._MARKET_CURRENCY) == {"US", "CA", "GB", "AU", "SG", "HK", "JP", "NZ"}
    for market in reap_routes._MARKET_CURRENCY:  # a caller that names no currency is priced as before
        assert reap_routes._cart_market_currency(market, None) == reap_routes._MARKET_CURRENCY[market]
    assert reap_routes._cart_market_currency("KR", None) is None
    assert reap_routes._cart_market_currency("KR", markets.currency_for("KR")) == "KRW"
    assert reap_routes._cart_market_currency("TW", markets.currency_for("TW")) == "TWD"
    assert reap_routes._cart_market_currency("US", "CAD") == "CAD"  # a caller's currency always wins


def test_the_enabled_currencies_follow_the_markets(monkeypatch):
    assert markets.enabled_currencies() == frozenset({"USD"})
    monkeypatch.setenv(markets.MARKETS_ENV, "US,CA,AU,NZ,SG,JP,KR,TW,HK")
    assert markets.enabled_currencies() == frozenset({"USD", "CAD", "AUD", "NZD", "SGD", "JPY", "KRW", "TWD", "HKD"})


@pytest.mark.parametrize("country,over", [
    # Values our own per-market rules used to refuse: each is the buyer's, sent as written.
    ("US", {"region": "PR"}), ("US", {"region": "Texas"}), ("US", {"region": "US-TX"}), ("US", {"region": "AE"}),
    ("US", {"postalCode": "787"}), ("US", {"postalCode": "787011234"}), ("US", {"region": "ON", "postalCode": "K1A 0B1"}),
    ("CA", {"postalCode": "K1A0B"}), ("CA", {"region": "ZZ"}), ("AU", {"region": "NS"}), ("NZ", {"region": "ZZZ"}),
    ("SG", {"postalCode": "18989"}), ("JP", {"region": "48"}), ("JP", {"region": "Tokyo"}), ("KR", {"region": "99"}),
    ("TW", {"region": "TPQ"}), ("HK", {"region": "Kowloon"}), ("HK", {"postalCode": "000000"}),
])
def test_no_market_has_an_address_rule_of_ours(monkeypatch, country, over):
    monkeypatch.setenv(markets.MARKETS_ENV, ",".join(markets.MARKETS))
    email, address, errors = _submit(country, **over)
    assert errors == {}, errors
    for key, value in over.items():
        assert address[key] == value  # as written: not upper-cased, not reformatted
    assert {k: v for k, v in shipping.door_address(_address(country, **over)).items() if k in over} == over


@pytest.mark.parametrize("country", [*sorted(VALID), "GB", None])
def test_fields_missing_never_asks_for_a_region_or_a_postcode(country):
    address = {"country": country} if country else {}
    missing = shipping.fields_missing(EMAIL, address)
    assert "region" not in missing and "postalCode" not in missing  # the store platform's to require
    assert missing == [f for f in ("firstName", "lastName", "phone", "addressLine1", "city", "country")
                       if f != "country" or country is None]
    complete = _address(country) if country in VALID else None
    if complete is not None:
        bare = {k: v for k, v in complete.items() if k not in ("region", "postalCode")}
        assert shipping.fields_missing(EMAIL, bare) == [] and shipping.incomplete_address(bare) is None


def _submit(country, **over):
    return shipping.read_submission({"email": EMAIL, **_address(country, **over)}, email_required=True,
                                    address_required=True)


def test_the_address_write_takes_a_market_only_while_it_is_on_and_says_only_that(monkeypatch):
    email, address, errors = _submit("JP")
    assert (email, address, errors) == (None, None, {"country": "unsupported"})
    monkeypatch.setenv(markets.MARKETS_ENV, "US,JP,SG,CA")
    email, address, errors = _submit("JP")
    assert errors == {} and email == EMAIL and (address["region"], address["postalCode"]) == ("13", "100-0001")
    assert _submit("SG")[2] == {} and _submit("CA", region=None)[2] == {}
    lower = shipping.read_submission({"email": EMAIL, **_address("JP"), "country": "jp"}, email_required=True,
                                     address_required=True)
    assert lower[1]["country"] == "JP"  # the country code is read in any case
    assert _submit("US", region="x" * 101)[2] == {"region": "too_long"}  # a length cap only, never a format


def test_the_door_reads_an_address_as_given():
    assert shipping.door_address(_address("CA", postalCode="k1a 0b1", region="on")) == {
        **_address("CA"), "postalCode": "k1a 0b1", "region": "on"}
    assert shipping.door_address(_address("NZ", region=None))["country"] == "NZ"
    assert "region" not in shipping.door_address(_address("JP", region=None))
    with pytest.raises(reap_svc.PurchaseRefused) as refused:
        shipping.door_address(_address("JP", city=None))  # the relay's own fields stay required
    assert refused.value.reason == "invalid_address"


@pytest.fixture
def _a2a_router(monkeypatch):
    from services.payment_orchestration import a2a_rail

    monkeypatch.setattr(a2a_rail, "is_enabled", lambda: True)
    monkeypatch.setenv(rail_router.A2A_RAIL_CREATE_ENABLED_ENV, "1")
    monkeypatch.setenv(rail_router.A2A_RAIL_PILOT_AGENT_IDS_ENV, "agent_a2a")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, "shop.example:JP,shop.example:US")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000,JPY:30000")


def _choice(**over):
    args = dict(agent_id="agent_a2a", merchant_domain="shop.example", market_country="JP", item_source="cart_link",
                currency="JPY", worker_alive=True)
    args.update(over)
    return rail_router.choose_rail(**args)


@pytest.mark.usefixtures("_a2a_router")
def test_the_router_takes_a_currency_only_while_its_market_is_on(monkeypatch):
    assert _choice().dropped == {"a2a": "currency_unsupported"}
    assert _choice(preferred="a2a").refusal == "currency_unsupported"
    monkeypatch.setenv(markets.MARKETS_ENV, "US,JP")
    assert _choice().rail == "a2a"
    monkeypatch.setenv(markets.MARKETS_ENV, "JP")
    assert _choice(market_country="US", currency="USD").dropped == {"a2a": "currency_unsupported"}


def test_an_unknown_market_code_is_logged_once_and_turns_nothing_on(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(markets, "_WARNED", set())
    monkeypatch.setenv(markets.MARKETS_ENV, "USA")
    with caplog.at_level(logging.WARNING, logger=markets.__name__):
        assert markets.enabled_markets() == frozenset()
        assert markets.enabled_markets() == frozenset()
    lines = [r.getMessage() for r in caplog.records if "not in the table" in r.getMessage()]
    assert len(lines) == 1 and "USA" in lines[0]


def test_the_market_of_a_currency_and_the_address_pairing():
    assert markets.market_for_currency("cad") == "CA" and markets.market_for_currency("EUR") is None
    assert markets.address_matches_currency({"country": "ca"}, "CAD")
    assert not markets.address_matches_currency({"country": "US"}, "CAD")
    assert not markets.address_matches_currency({}, "USD") and not markets.address_matches_currency(None, "USD")


def test_the_address_write_with_a_market_takes_only_that_country(monkeypatch):
    monkeypatch.setenv(markets.MARKETS_ENV, "US,CA")
    body = {"email": EMAIL, **_address("US")}
    assert shipping.read_submission(body, email_required=True, address_required=True, market="CA")[2] == {
        "country": "unsupported"}
    assert shipping.read_submission(body, email_required=True, address_required=True, market=None)[2] == {
        "country": "unsupported"}  # a currency with no one market: every country refused
    assert shipping.read_submission(body, email_required=True, address_required=True, market="US")[2] == {}


def test_a_markets_value_that_names_nothing_is_logged(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(markets, "_WARNED", set())
    monkeypatch.setenv(markets.MARKETS_ENV, ",")
    with caplog.at_level(logging.WARNING, logger=markets.__name__):
        assert markets.enabled_markets() == frozenset()
    assert any("names no market" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("_a2a_router")
def test_a_canadian_purchase_shown_in_dollars_never_takes_the_rail(monkeypatch):
    monkeypatch.setenv(markets.MARKETS_ENV, "US,CA")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, "shop.example:CA,shop.example:US")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000,CAD:50000")
    assert _choice(market_country="CA", currency="USD").dropped == {"a2a": "currency_market_mismatch"}
    assert _choice(market_country="CA", currency="USD", preferred="a2a").refusal == "currency_market_mismatch"
    assert _choice(market_country="CA", currency="CAD").rail == "a2a"


def test_no_market_asks_for_a_region_or_postcode_and_the_relay_sends_neither_without_one(monkeypatch):
    from services.payment_orchestration import a2a_rail

    assert "postalCode" not in shipping.REQUIRED_ADDRESS_FIELDS and "region" not in shipping.REQUIRED_ADDRESS_FIELDS
    monkeypatch.setenv(markets.MARKETS_ENV, "US,JP")
    for country in ("US", "JP"):
        email, address, errors = _submit(country, postalCode=None)
        assert errors == {} and "postalCode" not in address, country
    monkeypatch.setenv(markets.MARKETS_ENV, "US,HK")
    email, address, errors = _submit("HK")
    assert errors == {} and email == EMAIL and "postalCode" not in address and address["country"] == "HK"
    assert shipping.fields_missing(EMAIL, address) == [] and shipping.incomplete_address(address) is None
    assert _submit("HK", region="HK")[2] == {}
    assert shipping.door_address(_address("HK"))["country"] == "HK"
    hk = {"email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+85221234567", "country": "HK",
          "address1": "1 Example Road", "city": "Kowloon"}
    assert a2a_rail.checkout_shipping(hk) == hk  # no zip_code, nothing invented for it
    assert a2a_rail.checkout_shipping({**hk, "zip_code": "000000"})["zip_code"] == "000000"  # the buyer's, as given
    us = {**hk, "country": "US", "state": "TX"}
    assert a2a_rail.checkout_shipping(us) == us  # no zip_code in any market unless one was given
    assert a2a_rail.checkout_shipping({**us, "zip_code": "78701"})["zip_code"] == "78701"
    no_state = {k: v for k, v in us.items() if k != "state"}
    assert a2a_rail.checkout_shipping(no_state) == no_state  # no state required either, the US included
    assert a2a_rail.checkout_shipping({k: v for k, v in hk.items() if k != "country"}) is None

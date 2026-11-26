"""services/payment_orchestration/a2a_markets.py: which markets the A2A rail takes, and each one's address rules.

Pinned here: unset means the US only (the rail as before); the env is the whole list, and a code outside the table
turns nothing on; every market's currency is the market registry's; Hong Kong is not in the table; KR and TW are
priced in their own currency without joining the Reap lane's market map; each market's
region and postcode rules, as the door and the address write read them; and the router takes a currency only
while its market is on.
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
    ("CA,XX,HK,GB", {"CA"}), ("XX", set()), ("U S", set()),
])
def test_unset_is_the_us_only_and_the_env_is_the_whole_list(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv(markets.MARKETS_ENV, raw)
    assert markets.enabled_markets() == frozenset(expected)


def test_every_market_is_the_registrys_with_its_currency_and_hong_kong_is_not_in_the_table():
    assert set(markets.MARKETS) == {"US", "CA", "AU", "NZ", "SG", "JP", "KR", "TW"}
    for market in markets.MARKETS:
        assert markets.currency_for(market) == market_registry.currency_for(market), market
    for market in ("HK", "GB"):
        assert markets.currency_for(market) is None, market
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
    monkeypatch.setenv(markets.MARKETS_ENV, "US,CA,AU,NZ,SG,JP,KR,TW")
    assert markets.enabled_currencies() == frozenset({"USD", "CAD", "AUD", "NZD", "SGD", "JPY", "KRW", "TWD"})


@pytest.mark.parametrize("country", sorted(VALID))
def test_a_valid_address_passes_its_markets_rules(country):
    assert markets.address_errors(_address(country)) == {}


@pytest.mark.parametrize("country,over,errors", [
    ("US", {"region": "PR"}, {"region": "invalid"}),
    ("US", {"postalCode": "787"}, {"postalCode": "invalid"}),
    ("CA", {"region": None}, {"region": "required"}),
    ("CA", {"postalCode": "K1A0B"}, {"postalCode": "invalid"}),
    ("CA", {"postalCode": "D1A 0B1"}, {"postalCode": "invalid"}),  # no Canadian postcode starts with D
    ("AU", {"region": "NS"}, {"region": "invalid"}),
    ("AU", {"postalCode": "200"}, {"postalCode": "invalid"}),
    ("NZ", {"region": None}, {}),
    ("NZ", {"region": "ZZZ"}, {"region": "invalid"}),
    ("SG", {"region": "SG"}, {"region": "invalid"}),  # Singapore has no region
    ("SG", {"postalCode": "18989"}, {"postalCode": "invalid"}),
    ("JP", {"region": "48"}, {"region": "invalid"}),
    ("JP", {"region": None}, {"region": "required"}),
    ("JP", {"postalCode": "1000001"}, {}),
    ("JP", {"postalCode": None}, {"postalCode": "required"}),
    ("KR", {"region": None}, {}),
    ("KR", {"region": "51"}, {}),
    ("KR", {"region": "99"}, {"region": "invalid"}),
    ("KR", {"postalCode": "045240"}, {"postalCode": "invalid"}),
    ("TW", {"postalCode": "10048"}, {}),
    ("TW", {"postalCode": "1004"}, {"postalCode": "invalid"}),
    ("TW", {"region": "TPQ"}, {"region": "invalid"}),
    ("TW", {"postalCode": None}, {"postalCode": "required"}),
])
def test_each_markets_region_and_postcode_rules(country, over, errors):
    assert dict(markets.address_errors(_address(country, **over))) == errors


@pytest.mark.parametrize("country,region_listed", [
    ("US", True), ("CA", True), ("AU", True), ("JP", True), ("NZ", False), ("SG", False), ("KR", False), ("TW", False),
    ("HK", True), (None, True),
])
def test_fields_missing_asks_for_a_region_only_where_the_market_requires_one(country, region_listed):
    address = {"country": country} if country else {}
    missing = shipping.fields_missing(EMAIL, address)
    assert ("region" in missing) is region_listed
    assert "postalCode" in missing and (country is not None or "country" in missing)
    complete = _address(country) if country in VALID else None
    if complete is not None:
        assert shipping.fields_missing(EMAIL, {k: v for k, v in complete.items() if k != "region"}) == (
            ["region"] if region_listed else [])
        assert shipping.incomplete_address({k: v for k, v in complete.items() if k != "region"}) == (
            {k: v for k, v in complete.items() if k != "region"} if region_listed else None)


def _submit(country, **over):
    return shipping.read_submission({"email": EMAIL, **_address(country, **over)}, email_required=True,
                                    address_required=True)


def test_the_address_write_takes_a_market_only_while_it_is_on_and_says_only_that(monkeypatch):
    email, address, errors = _submit("JP")
    assert (email, address, errors) == (None, None, {"country": "unsupported"})
    monkeypatch.setenv(markets.MARKETS_ENV, "US,JP,SG,CA")
    email, address, errors = _submit("JP")
    assert errors == {} and email == EMAIL and (address["region"], address["postalCode"]) == ("13", "100-0001")
    assert _submit("JP", region="99")[2] == {"region": "invalid"}
    assert _submit("SG")[2] == {} and _submit("SG", region="SG")[2] == {"region": "invalid"}
    # Read as the door reads it: the postcode upper-cased.
    assert _submit("CA", postalCode="k1a 0b1")[1]["postalCode"] == "K1A 0B1"


def test_the_door_reads_an_address_by_its_markets_rules():
    assert shipping.door_address(_address("CA", postalCode="k1a 0b1", region="on")) == {
        **_address("CA"), "postalCode": "K1A 0B1"}
    with pytest.raises(reap_svc.PurchaseRefused) as refused:
        shipping.door_address(_address("JP", region="48"))
    assert refused.value.reason == "invalid_address"
    with pytest.raises(reap_svc.PurchaseRefused):
        shipping.door_address(_address("SG", region="SG"))
    assert shipping.door_address(_address("NZ", region=None))["country"] == "NZ"


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

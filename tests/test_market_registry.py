"""ADR-026 P1 step 1: the market registry exists, validates, and EQUALS every constant it will replace.

Parity, not a switch: each test pins one hand-kept list or map against its registry accessor, so the
registry cannot drift from the code while both exist, and swapping a constant for its accessor later is a
no-op. When a constant is switched to read the registry, its parity test here becomes trivially true.
"""

import json

import pytest

from services import market_registry as reg


def test_registry_loads_and_has_a_served_market():
    assert "US" in reg.served_markets()
    assert set(reg.served_markets()) == {"US", "SG"}


def test_region_pricing_map_is_the_registry_pricing_regions():
    from services.region_pricing import REGION_PRICING_CURRENCY
    assert dict(REGION_PRICING_CURRENCY) == reg.pricing_region_currency()


def test_acquisition_markets_match():
    from services.region_pricing import ACQUISITION_MARKETS
    assert set(ACQUISITION_MARKETS) == set(reg.acquisition_markets())


def test_ingest_markets_match():
    from services.retailer_ingest.pipeline import INGEST_MARKETS, SERVED_INGEST_MARKETS
    assert set(INGEST_MARKETS) == set(reg.ingest_markets())
    assert set(SERVED_INGEST_MARKETS) == set(reg.ingest_markets()) - set(reg.acquisition_markets())


def test_capture_markets_match():
    from services.retailer_ingest.shopify_markets import CAPTURE_MARKETS
    assert set(CAPTURE_MARKETS) == set(reg.capture_markets())


def test_reap_currency_map_agrees_with_the_registry():
    from routes.agent_commerce_reap import _MARKET_CURRENCY
    for market, currency in _MARKET_CURRENCY.items():
        assert reg.currency_for(market) == currency, market


def test_unknown_market_raises_never_falls_back():
    with pytest.raises(ValueError, match="unknown market"):
        reg.market("ZZ")
    with pytest.raises(ValueError):
        reg.currency_for("")
    assert reg.market_or_none("ZZ") is None
    assert reg.currency_for(" sg ") == "SGD"


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["markets"]["US"].update(status="live"), "status"),
    (lambda d: d["markets"]["US"].update(currency="usd"), "ISO-4217"),
    (lambda d: d["markets"].update(USA=dict(d["markets"]["US"])), "alpha-2"),
    (lambda d: d["markets"]["GB"].update(status="served"), "ingest"),
    (lambda d: d["markets"]["GB"].update(capture=True), "capture"),
    (lambda d: d["markets"]["SG"].pop("locales"), "fields"),
    (lambda d: [e.update(status="off") for e in d["markets"].values()], "served"),
])
def test_validation_refuses_a_broken_registry(mutate, message):
    doc = json.loads(reg.REGISTRY_PATH.read_text())
    mutate(doc)
    with pytest.raises(ValueError, match=message):
        reg._validate(doc)


def test_gateway_projection_is_stable_json():
    first = reg.gateway_projection_json()
    assert first == reg.gateway_projection_json()
    parsed = json.loads(first)
    assert list(parsed["markets"]) == sorted(parsed["markets"])
    assert parsed["markets"]["SG"]["currency"] == "SGD"


def test_generator_writes_and_checks(tmp_path):
    from scripts import generate_market_registry_json as gen
    target = tmp_path / "market_registry.generated.json"
    assert gen.main([str(target)]) == 0
    assert gen.main(["--check", str(target)]) == 0
    target.write_text(target.read_text().replace('"SGD"', '"XXX"'))
    assert gen.main(["--check", str(target)]) == 1

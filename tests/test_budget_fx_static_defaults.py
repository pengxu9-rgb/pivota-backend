"""ADR-026 P0: the budget FX static fallback covers every served market's currency.

Without SGD, a budget bound for an SGD offer was unresolvable whenever the stored snapshot and the
latest-rate API both missed. The static table only translates a budget; it never prices an offer.
"""

from routes import agent_shop_gateway as gw


def test_static_table_covers_served_market_currencies():
    for currency in ("USD", "SGD", "AUD"):
        assert gw._DEFAULT_BUDGET_FX_USD_RATES.get(currency, 0) > 0, currency


def test_usd_budget_translates_to_sgd_and_aud_through_the_static_fallback(monkeypatch):
    monkeypatch.setattr(gw, "_BUDGET_FX_STATIC_FALLBACK_ENABLED", True)
    monkeypatch.setattr(gw, "_BUDGET_FX_USD_RATES", dict(gw._DEFAULT_BUDGET_FX_USD_RATES))
    rate, source = gw._lookup_budget_fx_static_rate("USD", "SGD")
    assert source is not None
    assert 1.1 < rate < 1.5  # USD 1 buys more than 1 SGD
    rate, _ = gw._lookup_budget_fx_static_rate("AUD", "SGD")
    assert 0.8 < rate < 1.0  # AUD 1 buys a little under 1 SGD

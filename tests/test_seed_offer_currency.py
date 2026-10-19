"""services/seed_offer_currency: the seed's own currency, and the reader kill switch.

The SQL predicate is exercised on real Postgres in
tests/test_agent_pdp_view_seed_projected_offer_currency_postgres.py; these pin the Python half the
writers use and the switch's default.
"""

import pytest

from services import seed_offer_currency as m


@pytest.mark.parametrize("seed, expected", [
    ({"price_currency": "USD", "seed_data": {"snapshot": {"price_currency": "USD"}}}, "USD"),
    ({"price_currency": " usd ", "seed_data": {}}, "USD"),                                    # column alone
    ({"price_currency": None, "seed_data": {"snapshot": {"price_currency": "SGD"}}}, "SGD"),  # page read alone
    ({"price_currency": "USD", "seed_data": {"snapshot": {"price_currency": "EUR"}}}, None),  # disagree: unknown
    ({"price_currency": "AUD", "snapshot_price_currency": "AUD"}, "AUD"),                     # the sync path's column
    ({"price_currency": "USD", "snapshot_price_currency": "QAR"}, None),
    ({"price_currency": "SGD", "seed_data": {"price_currency": "USD", "snapshot": {"price_currency": "SGD"}}}, "SGD"),
    ({"price_currency": None, "seed_data": {}}, None),
])
def test_seed_currency_is_known_only_when_column_and_page_read_agree(seed, expected):
    assert m.seed_currency_of(seed) == expected


def test_the_reader_guard_is_on_unless_switched_off(monkeypatch):
    monkeypatch.delenv(m.SEED_PROJECTED_OFFER_CURRENCY_GUARD_FLAG, raising=False)
    assert m.seed_projected_offer_guard_and_sql().startswith("AND (coalesce(o.source_system")
    for off in ("off", "0", "false", "No", " disabled "):
        monkeypatch.setenv(m.SEED_PROJECTED_OFFER_CURRENCY_GUARD_FLAG, off)
        assert m.seed_projected_offer_guard_and_sql() == ""
    monkeypatch.setenv(m.SEED_PROJECTED_OFFER_CURRENCY_GUARD_FLAG, "on")
    assert m.seed_projected_offer_guard_and_sql() != ""

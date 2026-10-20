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
    # A payload that hides the page read states none, as in the SQL (seed_currency_sql).
    ({"price_currency": "USD", "seed_data": "not json"}, None),
    ({"price_currency": "USD", "seed_data": [1, 2]}, None),
    ({"price_currency": "USD", "seed_data": 7}, None),
    ({"price_currency": "USD", "seed_data": {"snapshot": '{"price_currency": "USD"}'}}, None),  # snapshot as a JSON string
    ({"price_currency": "USD", "seed_data": '{"snapshot": {"price_amount": "1"}}'}, "USD"),  # jsonb as driver text
    ({"price_currency": "USD", "seed_data": None}, "USD"),
    # A stated non-string currency is text to `#>>` ('0', 'false', '["USD"]'): it disagrees, never falls back.
    ({"price_currency": "USD", "seed_data": {"snapshot": {"price_currency": 0}}}, None),
    ({"price_currency": "USD", "seed_data": {"snapshot": {"price_currency": False}}}, None),
    ({"price_currency": "USD", "seed_data": {"snapshot": {"price_currency": ["USD"]}}}, None),
    # trim() strips SPACES only, as in SQL: a tab or newline keeps the codes apart.
    ({"price_currency": "\tUSD", "seed_data": {"snapshot": {"price_currency": "USD"}}}, None),
    ({"price_currency": "USD\n", "seed_data": {"snapshot": {"price_currency": "USD"}}}, None),
    ({"price_currency": " usd ", "seed_data": {"snapshot": {"price_currency": "USD"}}}, "USD"),
    # The SQL's own answer, when the caller projected it, is authoritative.
    ({"price_currency": "USD", "snapshot_price_currency": None, "seed_currency": None}, None),
    ({"price_currency": "USD", "snapshot_price_currency": None, "seed_currency": "USD"}, "USD"),
])
def test_seed_currency_is_known_only_when_column_and_page_read_agree(seed, expected):
    assert m.seed_currency_of(seed) == expected


def test_the_reader_guard_is_on_unless_switched_off(monkeypatch):
    monkeypatch.delenv(m.SEED_PROJECTED_OFFER_CURRENCY_GUARD_FLAG, raising=False)
    assert m.seed_projected_offer_guard_and_sql().lstrip().startswith("AND (coalesce(o.source_system")
    # Interpolated at the end of the previous line: the prefix is the newline and indent.
    assert m.seed_projected_offer_guard_and_sql(prefix=" ").startswith(" AND (")
    for off in ("off", "0", "false", "No", " disabled "):
        monkeypatch.setenv(m.SEED_PROJECTED_OFFER_CURRENCY_GUARD_FLAG, off)
        assert m.seed_projected_offer_guard_and_sql() == ""
    monkeypatch.setenv(m.SEED_PROJECTED_OFFER_CURRENCY_GUARD_FLAG, "on")
    assert m.seed_projected_offer_guard_and_sql() != ""


_PROJECTED_OVER_STORED = [
    # stored: column USD, page read AUD (states none). A projection on the row says USD.
    ({"status": "active", "price_currency": "USD", "seed_data": {"snapshot": {"price_currency": "AUD"}},
      "seed_currency": "USD", "snapshot_price_currency": "USD"}, None),
    # stored: USD agreeing. A projection on the row says none.
    ({"status": "active", "price_currency": "USD", "seed_data": {"snapshot": {"price_currency": "USD"}},
      "seed_currency": None, "snapshot_price_currency": "AUD"}, "USD"),
]


@pytest.mark.parametrize("seed, stored", _PROJECTED_OVER_STORED)
def test_the_purchase_path_reads_only_what_is_stored(seed, stored):
    """PR controller review of #2657: stored_seed_currency is seed_currency_of over price_currency and
    seed_data ALONE. A row that also carries a projected `seed_currency` / `snapshot_price_currency`
    (the writer's sync projection) must not let them stand in for what is stored."""
    assert m.stored_seed_currency(seed) == stored
    offer = {"source_system": "variant_identity_backfill_v1", "currency": "USD"}
    assert m.offer_priced_in_seed_currency(offer, [seed]) is (stored == "USD")

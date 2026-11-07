"""services/seed_serving: a seed row's own currency, the market it prices, and whether it is served.

The partition (`external_product_seeds.market`) is never the answer: SGD, AUD and JPY seeds sit in the
'US' partition on purpose (ingestion.SEED_PARTITION_MARKET). The SQL form runs here on SQLite (the
dialect-neutral CASE) and in tests/test_seed_serving_postgres.py on Postgres (the full jsonb form),
over the same SEED_ROWS, and both must give the Python answers.
"""

from __future__ import annotations

import sqlite3

import pytest

from services import seed_serving as m
from services.region_pricing import REGION_PRICING_CURRENCY, market_for_pricing_currency
from services.seed_offer_currency import currency_code

# (id, partition, price_currency column, seed_data, expected currency, expected market).
# Shared with tests/test_seed_serving_postgres.py, which stores them and runs seed_row_market_sql.
SEED_ROWS = [
    ("usd_in_us", "US", "USD", {"snapshot": {"price_currency": "USD"}}, "USD", "US"),
    # seed_data.price_currency is the writers' old "USD" default, not evidence.
    ("sgd_in_us", "US", "SGD", {"price_currency": "USD", "snapshot": {"price_currency": "SGD"}}, "SGD", "SG"),
    ("usd_in_sg", "SG", "USD", None, "USD", "US"),
    ("aud_in_us", "US", "AUD", {"snapshot": {"price_currency": "AUD"}}, "AUD", "AU"),
    ("jpy_in_us", "US", "JPY", {}, "JPY", "JP"),
    ("gbp_in_us", "US", "GBP", None, "GBP", "GB"),
    # EUR prices several markets: no market, never a default.
    ("eur_in_us", "US", "EUR", {"snapshot": {"price_currency": "EUR"}}, "EUR", None),
    ("padded_lower", "US", " sgd ", {"snapshot": {"price_currency": "Sgd"}}, "SGD", "SG"),
    # Column and page read disagree: the row states no currency.
    ("usd_col_sgd_page", "US", "USD", {"snapshot": {"price_currency": "SGD"}}, None, None),
    ("page_only", "US", None, {"snapshot": {"price_currency": "SGD"}}, "SGD", "SG"),
    ("blank_column", "US", "", None, None, None),
    ("null_everything", "US", None, None, None, None),
    ("unknown_code", "US", "XYZ", None, "XYZ", None),
    # A snapshot that is not an object hides the page read: the column alone does not stand.
    ("snapshot_not_object", "US", "USD", {"snapshot": "SGD"}, None, None),
]


def _row(rid, partition, column, seed_data):
    return {"id": rid, "market": partition, "price_currency": column, "seed_data": seed_data}


@pytest.mark.parametrize("rid, partition, column, seed_data, currency, market", SEED_ROWS, ids=[r[0] for r in SEED_ROWS])
def test_the_row_currency_and_market_come_from_the_row_currency(rid, partition, column, seed_data, currency, market):
    row = _row(rid, partition, column, seed_data)
    assert m.seed_row_currency(row) == currency
    assert m.seed_row_market(row) == market


def test_the_market_map():
    for code, market in (("USD", "US"), ("SGD", "SG"), ("AUD", "AU"), ("JPY", "JP")):
        assert m.seed_row_market({"price_currency": code}) == market
    assert m.seed_row_market({"price_currency": "EUR"}) is None
    assert m.seed_row_market({"price_currency": " usd "}) == "US"
    assert m.seed_row_market({"price_currency": None}) is None
    assert m.seed_row_market({"price_currency": " "}) is None
    assert m.seed_row_market({}) is None


def test_a_projected_seed_currency_wins_over_the_column():
    """The SQL's own answer (seed_currency_sql AS seed_currency) is authoritative when projected."""
    row = {"market": "US", "price_currency": "USD", "seed_currency": "SGD"}
    assert m.seed_row_currency(row) == "SGD"
    assert m.seed_row_market(row) == "SG"
    # ...including when the SQL said the row states none.
    assert m.seed_row_market({"market": "US", "price_currency": "USD", "seed_currency": None}) is None


# ---------------------------------------------------------------------------------------------------------------
# The partition is ignored. A mutant that reads row["market"] must fail these.
# ---------------------------------------------------------------------------------------------------------------

_PARTITION_CASES = [
    ({"market": "US", "price_currency": "SGD"}, "SG"),
    ({"market": "SG", "price_currency": "USD"}, "US"),
    ({"market": "US", "price_currency": "AUD"}, "AU"),
    ({"market": "US", "price_currency": None}, None),
    ({"market": "US", "price_currency": "EUR"}, None),
]


def _partition_failures(seed_row_market):
    return [(row, want, seed_row_market(row)) for row, want in _PARTITION_CASES if seed_row_market(row) != want]


def test_the_partition_is_ignored():
    assert _partition_failures(m.seed_row_market) == []


@pytest.mark.parametrize(
    "mutant",
    [
        lambda row: str(row.get("market") or "").strip().upper() or None,  # reads the partition
        lambda row: m.seed_row_market(row) or str(row.get("market") or "") or None,  # partition as the fallback
        lambda row: market_for_pricing_currency(row.get("price_currency")) or row.get("market"),
    ],
    ids=["partition", "partition_fallback", "currency_then_partition"],
)
def test_a_partition_reading_mutant_fails(mutant):
    assert _partition_failures(mutant), "a seed_row_market that reads row['market'] must fail the partition cases"


# ---------------------------------------------------------------------------------------------------------------
# Served
# ---------------------------------------------------------------------------------------------------------------


def test_seed_row_served_against_a_given_set():
    served = {"USD", "SGD"}
    assert m.seed_row_served({"market": "US", "price_currency": "SGD"}, served=served) is True
    assert m.seed_row_served({"market": "US", "price_currency": "USD"}, served=served) is True
    assert m.seed_row_served({"market": "US", "price_currency": "AUD"}, served=served) is False
    assert m.seed_row_served({"market": "US", "price_currency": None}, served=served) is False
    # The partition does not serve a row: an SG partition with no currency is not SGD.
    assert m.seed_row_served({"market": "SG", "price_currency": None}, served=served) is False
    # Codes in the set normalise; blanks serve nothing.
    assert m.seed_row_served({"price_currency": "SGD"}, served=[" sgd "]) is True
    assert m.seed_row_served({"price_currency": ""}, served=["", " ", None]) is False


@pytest.mark.parametrize("code", ["USD", "SGD", "AUD", "JPY", "EUR", None, ""])
def test_an_empty_served_set_serves_nothing(code):
    assert m.seed_row_served({"market": "US", "price_currency": code}, served=frozenset()) is False


def test_seed_row_served_defaults_to_the_served_regions(monkeypatch):
    import services.index_pipeline_state_service as ips

    monkeypatch.setattr(ips, "serving_pricing_regions", lambda: ["US"])
    assert m.seed_row_served({"market": "US", "price_currency": "USD"}) is True
    assert m.seed_row_served({"market": "US", "price_currency": "SGD"}) is False
    monkeypatch.setattr(ips, "serving_pricing_regions", lambda: ["US", "SG"])
    assert m.seed_row_served({"market": "US", "price_currency": "SGD"}) is True

    def _boom():
        raise ValueError("registry did not load")

    monkeypatch.setattr(ips, "serving_pricing_regions", _boom)
    assert m.seed_row_served({"market": "US", "price_currency": "USD"}) is False


def test_seed_row_served_for_a_buyer_market():
    sgd = {"market": "US", "price_currency": "SGD"}
    usd = {"market": "SG", "price_currency": "USD"}
    assert m.seed_row_served_for(sgd, "SG") is True
    assert m.seed_row_served_for(sgd, " sg ") is True
    assert m.seed_row_served_for(sgd, "US") is False
    assert m.seed_row_served_for(usd, "US") is True
    assert m.seed_row_served_for(usd, "SG") is False
    # The existing buyer rule (seed_serving_currency) reads a missing market as the default serving market.
    assert m.seed_row_served_for(usd, None) is True
    assert m.seed_row_served_for(sgd, None) is False
    # A market with no known currency, and a row with none, are never served.
    assert m.seed_row_served_for(usd, "DE") is False
    assert m.seed_row_served_for({"market": "US", "price_currency": None}, "US") is False
    assert m.seed_row_served_for({"market": "US", "price_currency": "USD", "seed_data": {"snapshot": {"price_currency": "SGD"}}}, "US") is False


def test_served_currencies_moved_and_is_re_exported():
    import services.external_referral_readiness as readiness

    assert readiness.served_region_currencies is m.served_currencies


def test_served_currencies_follow_the_serving_regions(monkeypatch):
    monkeypatch.setenv("PIVOTA_SERVING_PRICING_REGIONS", " sg , us ")
    assert m.served_currencies() == frozenset({"USD", "SGD"})


# ---------------------------------------------------------------------------------------------------------------
# The SQL form
# ---------------------------------------------------------------------------------------------------------------


def test_the_sql_is_generated_from_the_pricing_table(monkeypatch):
    sql = m.market_for_currency_sql("x")
    singles = {c: mk for c, mk in ((c, market_for_pricing_currency(c)) for c in set(REGION_PRICING_CURRENCY.values())) if mk}
    for currency, market in singles.items():
        assert f"WHEN '{currency}' THEN '{market}'" in sql
    assert "'EUR'" not in sql
    assert sql.endswith("ELSE NULL END)")
    # A market added to the table is added to the SQL, with no second spelling.
    monkeypatch.setitem(REGION_PRICING_CURRENCY, "NZ", "NZD")
    assert "WHEN 'NZD' THEN 'NZ'" in m.market_for_currency_sql("x")
    # And a currency that becomes shared drops out.
    monkeypatch.setitem(REGION_PRICING_CURRENCY, "XS", "SGD")
    assert "'SGD'" not in m.market_for_currency_sql("x")


def test_seed_row_market_sql_wraps_the_seed_currency_sql():
    from services.seed_offer_currency import seed_currency_sql

    sql = m.seed_row_market_sql("eps")
    assert sql.startswith(f"(CASE {seed_currency_sql('eps')} WHEN ")
    # One reference to the currency expression: seed_data is detoasted once per row.
    assert sql.count("seed_data#>>") == seed_currency_sql("eps").count("seed_data#>>")


def test_the_sql_case_on_sqlite_matches_python():
    """The CASE runs on SQLite over the normalised column; the full jsonb form runs on Postgres
    (tests/test_seed_serving_postgres.py). Both are compared with the Python answer for the same rows."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE eps (id TEXT PRIMARY KEY, market TEXT, price_currency TEXT)")
        conn.executemany("INSERT INTO eps VALUES (?, ?, ?)", [(r[0], r[1], r[2]) for r in SEED_ROWS])
        expr = m.market_for_currency_sql("nullif(upper(trim(eps.price_currency)), '')")
        got = dict(conn.execute(f"SELECT id, {expr} FROM eps").fetchall())
    finally:
        conn.close()
    want = {r[0]: market_for_pricing_currency(currency_code(r[2])) for r in SEED_ROWS}
    assert got == want
    # And for the rows whose currency is the column alone, that is seed_row_market.
    for rid, partition, column, seed_data, _currency, market in SEED_ROWS:
        if seed_data is None and column is not None:
            assert got[rid] == market == m.seed_row_market(_row(rid, partition, column, None))


def test_the_module_imports_no_routes():
    import subprocess
    import sys

    code = (
        "import sys; import services.seed_serving; "
        "bad = sorted(n for n in sys.modules if n == 'main' or n.startswith('routes')); "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    assert proc.returncode == 0, proc.stdout + proc.stderr

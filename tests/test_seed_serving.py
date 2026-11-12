"""services/seed_serving: a seed row's own currency, the market it prices, and whether it is served.

The partition (`external_product_seeds.market`) is never the answer: SGD, AUD and JPY seeds sit in the
'US' partition on purpose (ingestion.SEED_PARTITION_MARKET). The SQL form runs here on SQLite (the
dialect-neutral CASE) and in tests/test_seed_serving_postgres.py on Postgres (the full jsonb form),
over the same SEED_ROWS, and both must give the Python answers.

AU and JP are the market registry's acquisition markets (ingested, never served): an AUD or JPY seed has no
market and is never served, refused by name ('acquisition_market:AU').
"""

from __future__ import annotations

import sqlite3

import pytest

from services import market_registry
from services import seed_serving as m
from services.region_pricing import ACQUISITION_MARKETS, REGION_PRICING_CURRENCY, market_for_pricing_currency
from services.seed_offer_currency import currency_code

# (id, partition, price_currency column, seed_data, expected currency, expected market).
# Shared with tests/test_seed_serving_postgres.py, which stores them and runs seed_row_market_sql.
SEED_ROWS = [
    ("usd_in_us", "US", "USD", {"snapshot": {"price_currency": "USD"}}, "USD", "US"),
    # seed_data.price_currency is the writers' old "USD" default, not evidence.
    ("sgd_in_us", "US", "SGD", {"price_currency": "USD", "snapshot": {"price_currency": "SGD"}}, "SGD", "SG"),
    ("usd_in_sg", "SG", "USD", None, "USD", "US"),
    # AU and JP are acquisition markets: refused by name, so no market.
    ("aud_in_us", "US", "AUD", {"snapshot": {"price_currency": "AUD"}}, "AUD", None),
    ("jpy_in_us", "US", "JPY", {}, "JPY", None),
    ("aud_in_au", "AU", "AUD", None, "AUD", None),
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
    for code, market in (("USD", "US"), ("SGD", "SG"), ("GBP", "GB")):
        assert m.seed_row_market({"price_currency": code}) == market
    for code in ("AUD", "JPY", " jpy "):
        assert m.seed_row_market({"price_currency": code}) is None
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
    ({"market": "US", "price_currency": "GBP"}, "GB"),
    ({"market": "US", "price_currency": "AUD"}, None),
    ({"market": "AU", "price_currency": "AUD"}, None),
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
# Acquisition markets (AU, JP) are refused by name, from the market registry
# ---------------------------------------------------------------------------------------------------------------


def test_the_refused_markets_are_the_registrys_acquisition_markets():
    assert m.refused_seed_markets() == frozenset(market_registry.acquisition_markets()) == frozenset({"AU", "JP"})
    # region_pricing's constant says the same (tests/test_market_registry.py keeps the two in parity).
    assert m.refused_seed_markets() == frozenset(ACQUISITION_MARKETS)
    # No served market is refused.
    assert not m.refused_seed_markets() & set(market_registry.served_markets())


@pytest.mark.parametrize(
    "column, refusal",
    [("AUD", "acquisition_market:AU"), ("JPY", "acquisition_market:JP"), (" jpy ", "acquisition_market:JP"),
     ("USD", None), ("SGD", None), ("GBP", None), ("EUR", None), ("XYZ", None), (None, None), ("", None)],
)
def test_an_acquisition_market_is_refused_by_name(column, refusal):
    row = {"market": "US", "price_currency": column}
    assert m.seed_row_market_refusal(row) == refusal
    if refusal is not None:
        assert m.seed_row_market(row) is None


def test_a_refusal_needs_the_row_currency_not_the_partition():
    # An AU partition with a USD row is a US row; an SGD page read over an AUD-less column is SG.
    assert m.seed_row_market_refusal({"market": "AU", "price_currency": "USD"}) is None
    assert m.seed_row_market({"market": "AU", "price_currency": "USD"}) == "US"
    assert m.seed_row_market_refusal({"market": "JP", "price_currency": None}) is None


@pytest.mark.parametrize(
    "mutant",
    [
        lambda row: market_for_pricing_currency(m.seed_row_currency(row)),  # no refusal at all
        lambda row: m.seed_row_market(row) or (None if m.seed_row_currency(row) != "AUD" else "AU"),  # JP only
    ],
    ids=["no_refusal", "refuses_jp_only"],
)
def test_a_seed_row_market_that_labels_au_or_jp_fails(mutant):
    rows = [{"price_currency": "AUD"}, {"price_currency": "JPY"}]
    assert any(mutant(r) is not None for r in rows)
    assert all(m.seed_row_market(r) is None for r in rows)


def test_the_registry_decides_what_is_refused(monkeypatch):
    """Serving AU in the registry stops refusing it; making GB an acquisition market starts. Nothing is spelled
    in seed_serving."""
    monkeypatch.setitem(market_registry.MARKETS, "AU", {**market_registry.MARKETS["AU"], "status": "served"})
    monkeypatch.setitem(market_registry.MARKETS, "GB", {**market_registry.MARKETS["GB"], "status": "acquisition"})
    assert m.refused_seed_markets() == frozenset({"JP", "GB"})
    assert m.seed_row_market({"price_currency": "AUD"}) == "AU"
    assert m.seed_row_market({"price_currency": "GBP"}) is None
    assert m.seed_row_market_refusal({"price_currency": "GBP"}) == "acquisition_market:GB"
    sql = m.market_for_currency_sql("x")
    assert "WHEN 'AUD' THEN 'AU'" in sql and "'GBP'" not in sql and "'JPY'" not in sql


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
    # An acquisition market's currency is never served, even when a caller's set names it.
    assert m.seed_row_served({"market": "US", "price_currency": "AUD"}, served={"AUD", "USD"}) is False
    assert m.seed_row_served({"market": "US", "price_currency": "JPY"}, served=["jpy"]) is False


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
    # An acquisition buyer market is refused by name, though region_pricing prices it.
    assert m.seed_row_served_for({"market": "US", "price_currency": "AUD"}, "AU") is False
    assert m.seed_row_served_for({"market": "US", "price_currency": "JPY"}, " jp ") is False
    # So is an `off` one, in its own currency too.
    assert m.seed_row_served_for({"market": "US", "price_currency": "GBP"}, "GB") is False


_BUYER_MARKETS = ["US", "SG", "GB", "gb", "DE", None, "", "FR", "CA", "SE", "KR", "HK", "AU", "JP", "ZZ", "EU-DE"]
_ROW_CURRENCIES = ["USD", "SGD", "GBP", "EUR", "CAD", "SEK", "KRW", "HKD", "AUD", "JPY", "XYZ", None]
_REFUSAL = {"US": None, "SG": None, "GB": "market_not_served:GB", "DE": "unknown_market:DE",
            "FR": "market_not_served:FR", "CA": "market_not_served:CA", "SE": "market_not_served:SE",
            "KR": "market_not_served:KR", "HK": "market_not_served:HK", "AU": "market_not_served:AU",
            "JP": "market_not_served:JP", "ZZ": "unknown_market:ZZ", "EU-DE": "unknown_market:EU-DE"}


@pytest.mark.parametrize("buyer", _BUYER_MARKETS, ids=[repr(b) for b in _BUYER_MARKETS])
def test_the_priced_path_rule_for_every_buyer_market(buyer):
    """US/SG/GB/DE/none and every other listed or unknown market: a seed is served only to a SERVED buyer
    market (US, SG: the registry's served markets), and only in that market's registry currency. Every other
    buyer market is served nothing and refused by name, even GB, FR, CA, SE, KR and HK, whose own currencies
    region_pricing prices. A missing market is the default serving market, US (the existing buyer rule)."""
    code = market_registry.normalize_market(buyer) or "US"
    served_markets = set(market_registry.served_markets())
    assert served_markets == {"US", "SG"}
    wanted = market_registry.currency_for(code) if code in served_markets else None
    served = {c for c in _ROW_CURRENCIES if m.seed_row_served_for({"market": "US", "price_currency": c}, buyer)}
    assert served == ({wanted} if wanted else set())
    assert served == {"US": {"USD"}, "SG": {"SGD"}}.get(code, set())
    assert m.seed_buyer_market_refusal(buyer) == _REFUSAL[code]


def test_a_narrowed_serving_list_refuses_the_market_it_drops(monkeypatch):
    monkeypatch.setenv("PIVOTA_SERVING_PRICING_REGIONS", "US")
    sgd = {"market": "US", "price_currency": "SGD"}
    assert m.seed_row_served_for(sgd, "SG") is False
    assert m.seed_buyer_market_refusal("SG") == "market_not_served:SG"
    assert m.seed_row_served_for({"price_currency": "USD"}, "US") is True


def test_no_buyer_market_is_served_when_the_serving_list_does_not_load(monkeypatch):
    import services.index_pipeline_state_service as ips

    def _boom():
        raise ValueError("registry did not load")

    monkeypatch.setattr(ips, "serving_pricing_regions", _boom)
    for buyer, currency in (("US", "USD"), (None, "USD"), ("SG", "SGD")):
        assert m.seed_row_served_for({"price_currency": currency}, buyer) is False
    assert m.seed_buyer_market_refusal("US") == "market_not_served:US"


@pytest.mark.parametrize(
    "mutant",
    [
        # The previous delta: a denylist of the acquisition markets only.
        lambda row, buyer: (market_registry.normalize_market(buyer) not in m.refused_seed_markets()
                            and m.seed_row_currency(row) is not None
                            and m.seed_row_currency(row) == m.seed_serving_currency(buyer)),
        # The currency rule alone.
        lambda row, buyer: m.seed_row_currency(row) is not None and m.seed_row_currency(row) == m.seed_serving_currency(buyer),
    ],
    ids=["acquisition_denylist", "currency_only"],
)
def test_a_buyer_rule_that_serves_an_off_market_fails(mutant):
    cases = [({"price_currency": "GBP"}, "GB"), ({"price_currency": "CAD"}, "CA"), ({"price_currency": "KRW"}, "KR")]
    assert any(mutant(row, buyer) for row, buyer in cases)
    assert not any(m.seed_row_served_for(row, buyer) for row, buyer in cases)


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
    singles = {c: mk for c, mk in ((c, market_for_pricing_currency(c)) for c in set(REGION_PRICING_CURRENCY.values()))
               if mk and mk not in m.refused_seed_markets()}
    assert {"USD", "SGD", "GBP"} <= set(singles)
    for currency, market in singles.items():
        assert f"WHEN '{currency}' THEN '{market}'" in sql
    assert "'EUR'" not in sql
    # The acquisition markets are refused in SQL as in Python.
    assert "'AUD'" not in sql and "'JPY'" not in sql and "'AU'" not in sql and "'JP'" not in sql
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
    want = {r[0]: m.seed_row_market({"price_currency": currency_code(r[2])}) for r in SEED_ROWS}
    assert got == want
    assert got["aud_in_us"] is None and got["jpy_in_us"] is None and got["gbp_in_us"] == "GB"
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

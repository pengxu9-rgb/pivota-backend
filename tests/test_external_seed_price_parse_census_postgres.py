"""The price-parse census (scripts/ops/external_seed_price_parse_census.py): its signals, and its SQL
on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_external_seed_price_parse_census_postgres.py

The pure tests run everywhere. The Postgres test gets the census one real run per sitting on the
2-vCPU primary right: the freshness census's served-row SQL with extra JSONB projections spliced
in, `= ANY($1::text[])`, the pg_stat guard, all inside a READ ONLY transaction.

ISOLATION as in test_external_seed_freshness_census_postgres.py: a per-process scratch schema with
the real migrations, dropped at teardown.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from utils.crawled_price import parse_crawled_price
from utils.money import ZERO_DECIMAL_CURRENCIES

_ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
needs_pg = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL; see the module docstring")


def _census():
    spec = importlib.util.spec_from_file_location(
        "external_seed_price_parse_census", _ROOT / "scripts/ops/external_seed_price_parse_census.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _flags(amount, currency="EUR", peer=None, variant=None, raw=()):
    return _census().price_flags(
        amount=amount, currency=currency, peer_median=peer, variant_median=variant,
        raw_texts=list(raw), zero_decimal=ZERO_DECIMAL_CURRENCIES, parse=parse_crawled_price,
    )


# ------------------------------------------------------------------------------------ pure signals

def test_the_old_parsers_shapes_are_strong_signals() -> None:
    assert "peer_x100" in _flags(2880.0, peer=28.8)  # "28,80" read as 2880
    assert "peer_div1000" in _flags(1.23456, peer=1234.56)  # "1.234,56" read as 1.23456
    assert "variant_x100" in _flags(2880.0, variant=28.8)
    assert "excess_decimals" in _flags(1.23456)
    assert "excess_decimals" in _flags(2.4, currency="JPY")
    assert "raw_comma_x100" in _flags(2880.0, raw=["28,80 €"])


def test_a_correct_price_raises_nothing_strong() -> None:
    census = _census()
    for flags in (
        _flags(28.8, peer=29.5, variant=28.8, raw=["28,80 €"]),
        _flags(2400.0, currency="JPY", peer=2400.0),
        _flags(1234.56, peer=1200.0),
    ):
        assert not set(flags) & census.STRONG, flags


def test_weak_signals_stay_weak() -> None:
    census = _census()
    flags = _flags(2880.0)  # no peers, no variants, no text: only its shape
    assert "cents_integer" in flags and "above_ceiling" in flags
    assert not set(flags) & census.STRONG
    assert "peer_outlier" in _flags(300.0, peer=30.0)
    assert "peer_outlier" not in _flags(2880.0, peer=28.8), "the x100 band is its own flag"


def test_a_raw_text_must_match_exactly_a_hundredth() -> None:
    assert "raw_comma_x100" not in _flags(2881.0, raw=["28,80"])
    assert "raw_comma_x100" not in _flags(28.8, raw=["28,80"])


@pytest.mark.parametrize("text", ["28,80", "28,80 €", "1.234,56", "2 400,00 €", "49,9", "28.80", "1,234", "n/a", "2,400.00"])
def test_the_pre_merge_fallback_agrees_with_the_parser_on_the_shape_it_answers(text) -> None:
    """Before the fix merges, the image has no utils.crawled_price and the census uses a
    stand-in for the raw-text signal. It may refuse more, never read differently."""
    fallback = _census()._comma_decimal_only(text).amount
    if fallback is not None:
        assert fallback == pytest.approx(parse_crawled_price(text).amount)
    if text in ("28,80", "28,80 €", "1.234,56", "2 400,00 €", "49,9"):
        assert fallback is not None, text


def test_currency_flags() -> None:
    census = _census()
    assert census.currency_flags(market="FR", currency="EUR", host="x.de", variant_currencies=[]) == []
    assert census.currency_flags(market="US", currency="USD", host="shop.de", variant_currencies=[None]) == [
        "default_shaped", "uncorroborated", "tld_mismatch"
    ]
    assert census.currency_flags(market="US", currency="USD", host="shop.com", variant_currencies=["EUR"]) == [
        "default_shaped", "contradicted"
    ]
    assert census.currency_flags(market="US", currency="USD", host="shop.com", variant_currencies=["USD"]) == [
        "default_shaped"
    ]
    assert census.currency_flags(market="JP", currency="JPY", host="shop.co.uk", variant_currencies=[]) == [
        "default_shaped", "uncorroborated", "tld_mismatch"
    ]


def test_the_program_is_safe_to_pass_inline() -> None:
    """run_oneoff_job.sh picks gcloud's --args delimiter from a fixed list starting with the
    at-sign; the launcher passes this program inline."""
    assert chr(64) not in (_ROOT / "scripts/ops/external_seed_price_parse_census.py").read_text()


# ------------------------------------------------------------------------------------ Postgres

_MIGRATIONS = _ROOT / "db/migrations"
_TABLE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "200_external_seed_destination_liveness.sql",
    "202_external_seed_content_freshness.sql",
    "134_catalog_source_quarantine.sql",
    "058_catalog_core.sql",
    "135_catalog_product_sku_stale_suppression.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"seed_price_census_test_{os.getpid()}"


def _url() -> str:
    return "postgresql://" + DATABASE_URL[len("postgres://"):] if DATABASE_URL.startswith("postgres://") else DATABASE_URL


@pytest.fixture()
async def scoped_db():
    import databases

    from db.sql_migrations import split_statements

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r}: throwaway only")
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        for name in _TABLE_MIGRATIONS:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                await scoped.execute(statement)
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


async def _seed(db, sid, *, host, market="FR", price=28.8, currency="EUR", title="serum",
                attached=None, seed_data=None):
    await db.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, market, destination_url,"
        " domain, title, price_amount, price_currency, availability, status, seed_data,"
        " attached_product_key)"
        " VALUES (:id, :id, :market, :url, :host, :title, :price, :currency, 'in_stock', 'active',"
        " CAST(:seed_data AS JSONB), :attached)",
        {"id": sid, "market": market, "url": f"https://{host}/products/{sid}", "host": host,
         "title": title, "price": price, "currency": currency,
         "seed_data": json.dumps(seed_data or {}), "attached": attached},
    )


@needs_pg
@pytest.mark.asyncio
async def test_the_census_runs_read_only_on_postgres(scoped_db):
    import asyncpg

    db = scoped_db
    # An EU host whose meta price was read x100; its JSON-LD variant was read right.
    # Two variants in ANOTHER currency are not comparable and must not move its variant median.
    await _seed(db, "eu_bad", host="beaute.example.de", price=2880.0,
                seed_data={"variants": [{"price_amount": 28.8, "price_currency": "EUR"},
                                        {"price_amount": 2880, "price_currency": "USD"},
                                        {"price_amount": 2880, "price_currency": "USD"}]})
    # A same-title row on the SAME host was read the same wrong way: not a peer, or a misparsed
    # host would vouch for itself.
    await _seed(db, "eu_bad_twin", host="beaute.example.de", price=2880.0, title="serum")
    # The same product elsewhere, read right: a peer by title on another host.
    await _seed(db, "eu_twin", host="other.example.fr", price=29.0, title="Serum")
    # An attached pair: one misread /1000, its sibling and a merchant offer read right.
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title)"
        " VALUES ('p1', 'm', 'p', 'p1', 't')"
    )
    await _seed(db, "att_bad", host="a.example.de", price=1.23456, title="cream", attached="p1")
    await _seed(db, "att_ok", host="b.example.de", price=1234.56, title="cream", attached="p1")
    await db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency, merchant_effective_price)"
        " VALUES ('o1', 'k1', 'p1', 'm', 'EUR', 1230), ('o2', 'k2', 'p1', 'external_seed', 'EUR', 1.23)"
    )
    # A stored price TEXT that proves the x100.
    await _seed(db, "raw_bad", host="c.example.fi", price=4990.0, title="toner",
                seed_data={"price": "49,90 €"})
    # A US-partition seed on a .de host with a USD price and no variant currency: likely invented.
    await _seed(db, "usd_de", host="d.example.de", market="US", price=28.8, currency="USD", title="mask")
    # ...three more on the same host: the probe sample keeps at most SAMPLE_PER_HOST of its URLs.
    for i in (2, 3, 4):
        await _seed(db, f"usd_de_{i}", host="d.example.de", market="US", price=20.0 + i, currency="USD",
                    title=f"mask {i}")
    # Served in JP, read right -- but JPY is also what the old code invented for a JP page with no
    # currency, so it is default-shaped: the probe has to look. Its product has one JPY offer (fed
    # by its read) and one USD offer (not).
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title)"
        " VALUES ('p_jp', 'm', 'p', 'p_jp', 't')"
    )
    await _seed(db, "jp_ok", host="e.example.jp", market="JP", price=2400.0, currency="JPY", title="lotion",
                attached="p_jp")
    await db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency, merchant_effective_price)"
        " VALUES ('o3', 'k3', 'p_jp', 'm', 'JPY', 2400), ('o4', 'k4', 'p_jp', 'm2', 'USD', 20)"
    )
    # Not served (the market has no currency: DE), so not counted at all.
    await _seed(db, "de_unserved", host="f.example.de", market="DE", price=2880.0, title="gel")

    census = _census()
    conn = await asyncpg.connect(_url(), server_settings={"search_path": _SCHEMA})
    try:
        summary, suspects, sample = await census.collect(conn)
    finally:
        await conn.close()

    by_id = {s["id"]: s for s in suspects}
    assert set(by_id) == {"eu_bad", "eu_bad_twin", "att_bad", "raw_bad", "usd_de", "usd_de_2", "usd_de_3", "usd_de_4"}
    assert by_id["eu_bad"]["peers"] == 1 and by_id["eu_bad"]["peer_median"] == 29.0
    assert {"variant_x100", "peer_x100"} <= set(by_id["eu_bad"]["flags"])
    assert by_id["eu_bad"]["variant_median"] == 28.8
    assert {"peer_div1000", "excess_decimals"} <= set(by_id["att_bad"]["flags"])
    # the external_seed merchant's offer (projected from the seed) is not a peer
    assert by_id["att_bad"]["peers"] == 2
    assert "raw_comma_x100" in by_id["raw_bad"]["flags"]
    assert {"default_shaped", "tld_mismatch", "uncorroborated"} <= set(by_id["usd_de"]["flags"])

    totals = summary["totals"]
    assert totals["served"] == 11
    assert totals["suspect"] == 4
    assert totals["currency_likely_invented"] == 4
    assert summary["by_market"]["FR"]["suspect"] == 4
    assert "DE" not in summary["by_market"]
    assert (summary["top_hosts"][0]["host"], summary["top_hosts"][0]["suspect"]) == ("beaute.example.de", 2)
    assert "beaute.example.de" in "\n".join(census.render(summary))

    de_urls = sample[0].pop("urls")
    assert len(de_urls) == census.SAMPLE_PER_HOST == 3
    assert set(de_urls) <= {f"https://d.example.de/products/{i}" for i in ("usd_de", "usd_de_2", "usd_de_3", "usd_de_4")}
    assert sample == [
        {"host": "d.example.de", "market": "US", "served": 4, "default_shaped": 4,
         "default_shaped_attached_offers": 0},
        {"host": "e.example.jp", "market": "JP", "served": 1, "default_shaped": 1,
         "default_shaped_attached_offers": 1, "urls": ["https://e.example.jp/products/jp_ok"]},
    ]
    assert totals["default_shaped"] == 5 and totals["default_shaped_attached_offers"] == 1


@needs_pg
@pytest.mark.asyncio
async def test_the_census_refuses_while_another_copy_runs(scoped_db):
    import asyncpg

    census = _census()
    other = await asyncpg.connect(_url(), server_settings={"application_name": census.APPLICATION_NAME})
    conn = await asyncpg.connect(_url(), server_settings={"search_path": _SCHEMA})
    try:
        with pytest.raises(SystemExit, match="other_runs=1"):
            await census.collect(conn)
    finally:
        await conn.close()
        await other.close()


@needs_pg
@pytest.mark.asyncio
async def test_the_probe_takes_its_sample_through_the_census_read(scoped_db, capsys):
    """`external_seed_currency_probe.py --from-db`: the census's own read (READ ONLY, its guards),
    printed under the line cap, and the connection closed before anything is fetched."""
    import asyncpg

    await _seed(scoped_db, "usd_x", host="x.example.com", market="US", price=20.0, currency="USD", title="mask")
    spec = importlib.util.spec_from_file_location(
        "external_seed_currency_probe", _ROOT / "scripts/ops/external_seed_currency_probe.py"
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    sep = "&" if "?" in _url() else "?"
    sample = await probe.sample_from_db(f"{_url()}{sep}search_path={_SCHEMA}")
    assert sample == [{"host": "x.example.com", "market": "US", "served": 1, "default_shaped": 1,
                       "default_shaped_attached_offers": 0, "urls": ["https://x.example.com/products/usd_x"]}]
    out = capsys.readouterr().out.splitlines()
    assert any(ln.startswith("CENSUS_JSON ") for ln in out)
    assert any(ln.startswith("CURRENCY_SAMPLE_JSON_PART 1/1 ") for ln in out)
    assert max(len(ln) for ln in out) < 90_000
    # nothing left open: no session of the census's application_name survives the call
    conn = await asyncpg.connect(_url())
    try:
        left = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'external_seed_price_parse_census'"
        )
    finally:
        await conn.close()
    assert left == 0


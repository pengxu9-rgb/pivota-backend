"""The mirror offer's market is its currency's, and the upsert keeps refreshing a re-stamped row.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_mirror_offer_market_from_currency_postgres.py

external_product_seeds.market is the 'US' serving partition for SG, JP and AU stores' seeds too
(catalog_enrichment_agent.ingestion SEED_PARTITION_MARKET). The mirror offer writer copied it onto
the offer, so JPY, SGD and AUD offers were stamped market 'US'. It now declares the market the
currency prices (seed_offer_currency.seed_offer_market). On the production dialect, this file pins:

  * a new offer off a JPY seed in the 'US' partition is written market 'JP';
  * an offer stored by the old writer (JPY at market 'US') is NOT refreshed by the new writer: the
    upsert's DO UPDATE runs only where the stored market equals the one written, so it freezes
    until it is re-stamped (and no second row appears: the offer id is the product's, not the market's);
  * once re-stamped to 'JP' it refreshes again;
  * a USD seed writes and refreshes market 'US', as before;
  * an EUR seed writes nothing and leaves the stored row alone.

ISOLATION as in test_offer_price_checked_at_postgres.py: a per-process scratch schema, the real
migrations applied through a connection whose search_path is that schema alone, dropped at teardown.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_BASE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "202_external_seed_content_freshness.sql",
    "058_catalog_core.sql",
    "132_catalog_offer_suppression_writer_audit.sql",
    "133_catalog_source_domain.sql",
    "135_catalog_product_sku_stale_suppression.sql",
    "149_catalog_offers_agent_decision_fields.sql",
    "246_catalog_offers_price_checked_at.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"mirror_offer_market_test_{os.getpid()}"

SELLER = "merch_obs_offer_market"
PK = f"prod::external_seed::{SELLER}::ext_offer_market"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


async def _apply(db, name: str) -> None:
    from db.sql_migrations import split_statements

    for statement in split_statements((_MIGRATIONS / name).read_text()):
        if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
            continue
        await db.execute(statement)


@pytest.fixture(autouse=True)
def _fresh_price_check_probe(monkeypatch):
    """The dual-write's column probe caches per process; each test's schema is asked afresh."""
    from services import external_offer_dual_write as mod

    monkeypatch.setattr(mod, "_price_check_column", {"present": False, "checked": None})


@pytest.fixture()
async def db():
    import databases

    _assert_throwaway_database()
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        for name in _BASE_MIGRATIONS:
            await _apply(scoped, name)
        # The mirror writer requires a live product and its canonical SKU.
        await scoped.execute(
            "INSERT INTO catalog_products(product_key,merchant_id,platform,source_product_id,title)"
            " VALUES(:pk,:seller,'external_seed','ext_offer_market','market fixture')",
            {"pk": PK, "seller": SELLER},
        )
        await scoped.execute(
            "INSERT INTO catalog_skus(sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title)"
            " VALUES(:sku,:pk,:seller,'external_seed','ext_offer_market','canonical','market fixture')",
            {"sku": PK + "::canonical", "pk": PK, "seller": SELLER},
        )
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


def _seed(price, currency):
    return {
        "id": "seed_offer_market", "external_product_id": "ext_offer_market",
        "destination_url": "https://store.example/products/item", "domain": "store.example",
        "price_amount": price, "price_currency": currency, "availability": "in_stock",
        # the serving partition every seed sits in, whatever its store's market
        "market": "US",
    }


async def _write(monkeypatch, db, price, currency):
    from services import external_offer_dual_write as mod

    monkeypatch.setattr(mod, "database", db)
    return await mod.upsert_catalog_offer_from_seed_row(PK, _seed(price, currency), merchant_id=SELLER)


async def _offers(db):
    rows = await db.fetch_all(
        "SELECT offer_id, market, currency, list_price FROM catalog_offers WHERE product_key = :pk ORDER BY offer_id",
        {"pk": PK},
    )
    return [(r["market"], r["currency"], float(r["list_price"])) for r in rows]


async def test_a_jpy_seed_in_the_us_partition_writes_and_refreshes_market_jp(db, monkeypatch):
    from services import external_offer_dual_write as mod

    assert await _write(monkeypatch, db, 1800, "JPY") == mod.MIRROR_OFFER_WRITTEN
    assert await _offers(db) == [("JP", "JPY", 1800.0)]
    assert await _write(monkeypatch, db, 1650, "JPY") == mod.MIRROR_OFFER_WRITTEN
    assert await _offers(db) == [("JP", "JPY", 1650.0)]


async def test_an_old_us_stamp_freezes_until_re_stamped_then_refreshes(db, monkeypatch):
    from services import external_offer_dual_write as mod

    await _write(monkeypatch, db, 1800, "JPY")
    # The shape the old writer left in prod: the same offer, stamped with the seed's partition.
    await db.execute("UPDATE catalog_offers SET market = 'US' WHERE product_key = :pk", {"pk": PK})

    # Frozen: not refreshed, and no second row beside it (the offer id is the product's). The writer says so.
    assert await _write(monkeypatch, db, 1650, "JPY") == mod.MIRROR_OFFER_FROZEN_MARKET_MISMATCH
    assert await _offers(db) == [("US", "JPY", 1800.0)]

    # The re-stamp (market := the currency's market) ...
    await db.execute(
        "UPDATE catalog_offers SET market = 'JP' WHERE offer_id = :oid AND market = 'US' AND upper(currency) = 'JPY'",
        {"oid": mod.derive_mirror_offer_id(PK)},
    )
    # ... and the next write refreshes it again.
    assert await _write(monkeypatch, db, 1650, "JPY") == mod.MIRROR_OFFER_WRITTEN
    assert await _offers(db) == [("JP", "JPY", 1650.0)]


async def test_a_suppressed_row_is_held_not_frozen(db, monkeypatch):
    from services import external_offer_dual_write as mod

    await _write(monkeypatch, db, 1800, "JPY")
    await db.execute(
        "UPDATE catalog_offers SET suppressed_at = NOW(), suppression_reason = 'fixture' WHERE product_key = :pk",
        {"pk": PK},
    )
    assert await _write(monkeypatch, db, 1650, "JPY") == mod.MIRROR_OFFER_HELD
    assert await _offers(db) == [("JP", "JPY", 1800.0)]


@pytest.mark.parametrize("hold_sql", [
    "UPDATE catalog_offers SET suppressed_at = NOW(), suppression_reason = 'fixture' WHERE product_key = :pk",
    "UPDATE catalog_offers SET offer_payload = CAST('{\"price_repair\": {}}' AS jsonb) WHERE product_key = :pk",
])
async def test_a_held_row_the_old_writer_also_stamped_us_is_held_not_frozen(db, monkeypatch, hold_sql):
    """A hold outranks the mismatch: re-stamping a suppressed or price-repaired row would not make it refresh,
    so it is not counted with the rows to re-stamp (PR controller review of #2701)."""
    from services import external_offer_dual_write as mod

    await _write(monkeypatch, db, 1800, "JPY")
    await db.execute("UPDATE catalog_offers SET market = 'US' WHERE product_key = :pk", {"pk": PK})
    await db.execute(hold_sql, {"pk": PK})
    assert await _write(monkeypatch, db, 1650, "JPY") == mod.MIRROR_OFFER_HELD
    assert await _offers(db) == [("US", "JPY", 1800.0)]


async def test_a_refresh_of_a_frozen_row_reports_it_and_is_not_synced(db, monkeypatch):
    """sync_offer_for_seed, end to end on the real upsert: a frozen row is `frozen_market_mismatch`, not `synced`."""
    from services import external_offer_dual_write as mod

    await _write(monkeypatch, db, 1800, "JPY")
    await db.execute("UPDATE catalog_offers SET market = 'US' WHERE product_key = :pk", {"pk": PK})
    await db.execute(
        "INSERT INTO external_product_seeds(id, external_product_id, destination_url, domain, price_amount, "
        "price_currency, availability, market, status) VALUES('seed_offer_market', 'ext_offer_market', "
        "'https://store.example/products/item', 'store.example', 1650, 'JPY', 'in_stock', 'US', 'active')"
    )

    async def _mirror(seed_id):
        return {"product_key": PK, "merchant_id": SELLER}

    monkeypatch.setattr(mod, "resolve_mirror_product", _mirror)
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    result = await mod.sync_offer_for_seed("seed_offer_market")
    assert result["status"] == "frozen_market_mismatch"
    assert result["status"] not in mod.OFFER_SYNC_WRITTEN_STATUSES
    assert await _offers(db) == [("US", "JPY", 1800.0)]


async def test_a_usd_seed_writes_and_refreshes_market_us(db, monkeypatch):
    await _write(monkeypatch, db, 24.0, "USD")
    await _write(monkeypatch, db, 22.0, "USD")
    assert await _offers(db) == [("US", "USD", 22.0)]


async def test_an_eur_seed_writes_nothing_and_leaves_the_stored_row_alone(db, monkeypatch):
    from services.seed_offer_currency import SeedOfferCurrencyRefused

    with pytest.raises(SeedOfferCurrencyRefused):
        await _write(monkeypatch, db, 30.0, "EUR")
    assert await _offers(db) == []

    await _write(monkeypatch, db, 24.0, "USD")
    with pytest.raises(SeedOfferCurrencyRefused):
        await _write(monkeypatch, db, 30.0, "EUR")
    assert await _offers(db) == [("US", "USD", 24.0)]

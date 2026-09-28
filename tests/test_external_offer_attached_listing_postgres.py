"""The attached lane's offer read and guarded UPDATE, executed on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_external_offer_attached_listing_postgres.py

tests/test_external_offer_attached_listing.py proves the plan on dicts shaped like the producers'
rows. This file proves what a fake cannot: the seed SELECT's jsonb projections, the offer read's
jsonb `->>` and its catalog_skus join, and the UPDATE's currency/suppression guard and RETURNING,
all through `sync_offer_for_seed` on real Postgres against the tables the real migrations build.

ISOLATION. The dialect gate runs every file against ONE database, so this file never touches
`public`: a per-process scratch schema, the real migrations applied there through a connection
whose search_path is that schema ALONE, dropped at teardown.
"""

from __future__ import annotations

import json
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
_TABLE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "058_catalog_core.sql",
    "132_catalog_offer_suppression_writer_audit.sql",
    "135_catalog_product_sku_stale_suppression.sql",
    "246_catalog_offers_price_checked_at.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"attached_listing_offer_test_{os.getpid()}"

PK = "ext:missha-pdrn-peel-shot::3595c15f"
DEST = "https://missha.us/products/pdrn-peel-shot"
SEED_ID = "seed:catalog_enrichment_agent_v1:2bed56bf52daa7d4"
SELLER = "agent_seed::missha"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


@pytest.fixture()
async def scoped_db():
    import databases

    from db.sql_migrations import split_statements

    _assert_throwaway_database()
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


async def _load(db, *, variant_refresh_status="all_re_read"):
    seed_data = {
        "variants": [
            {"variant_id": "47761881301179", "price_amount": 22.7},
            {"variant_id": "47761881301180", "price_amount": 38.0},
        ],
        "snapshot": {"variant_refresh": {"status": variant_refresh_status}},
    }
    await db.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, market, destination_url,"
        " domain, title, price_amount, price_currency, status, attached_product_key, seed_data)"
        " VALUES (:id, 'missha:2bed56bf', 'US', :dest, 'missha.us', 'PDRN Peel Shot', 22.7, 'USD',"
        "         'active', :pk, CAST(:sd AS jsonb))",
        {"id": SEED_ID, "dest": DEST, "pk": PK, "sd": json.dumps(seed_data)},
    )
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,"
        " title, catalog_track, truth_tier, readiness_tier, source_system, source_ref)"
        " VALUES (:pk, :m, 'external_seed', :pk, 'PDRN Peel Shot', 'external_referral',"
        "         'observed', 'referral_only', 'catalog_enrichment_agent_v1', 'agent-run-1')",
        {"pk": PK, "m": SELLER},
    )
    for sku, vid in ((f"{PK}::canonical", "canonical"), (f"{PK}::v:47761881301179", "47761881301179"),
                     (f"{PK}::v:47761881301180", "47761881301180"), (f"{PK}::v:999", "999")):
        await db.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,"
            " source_product_id, source_variant_id, title, currency)"
            " VALUES (:sk, :pk, :m, 'external_seed', :pk, :vid, 'x', 'USD')",
            {"sk": sku, "pk": PK, "m": SELLER, "vid": vid},
        )

    async def offer(oid, sku, price, *, merchant=SELLER, dest=DEST, currency="USD", suppressed=False):
        await db.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
            " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
            " currency, list_price, merchant_effective_price, estimated_best_price,"
            " source_system, source_ref, offer_payload, suppressed_at, updated_at)"
            " VALUES (:oid, :sk, :pk, :m, 'external_referral', 'observed', 'referral_only',"
            "         'external_referral', 'default', 'in_stock', :cur, :p, :p, :p,"
            "         'catalog_enrichment_agent_v1', :dest, CAST(:payload AS jsonb),"
            "         CASE WHEN :sup THEN NOW() END, TIMESTAMP '2026-09-12 01:37:40')",
            {"oid": oid, "sk": sku, "pk": PK, "m": merchant, "cur": currency, "p": price,
             "dest": dest, "payload": json.dumps({"destination_url": dest}), "sup": suppressed},
        )

    await offer("of_canon", f"{PK}::canonical", 29.00)
    await offer("of_v1", f"{PK}::v:47761881301179", 29.00)
    await offer("of_v2", f"{PK}::v:47761881301180", 40.00)
    await offer("of_v999", f"{PK}::v:999", 11.00)                  # not on the seed
    await offer("of_ulta", f"{PK}::canonical", 31.00, merchant="agent_seed::retailer::ulta.com",
                dest="https://www.ulta.com/p/pdrn")                 # another seller's listing


async def _offers(db):
    rows = await db.fetch_all(
        "SELECT offer_id, list_price, merchant_effective_price, estimated_best_price, currency,"
        " updated_at > TIMESTAMP '2026-09-12 01:37:40' AS touched,"
        " price_checked_at IS NOT NULL AS checked FROM catalog_offers"
    )
    return {r["offer_id"]: dict(r) for r in rows}


async def _sync(monkeypatch, db):
    from services import external_offer_dual_write as mod

    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    monkeypatch.setattr(mod, "database", db)
    return await mod.sync_offer_for_seed(SEED_ID, attached_price_source="refresh", currency_read=True)


async def test_the_listing_rows_take_the_re_read_prices_and_nothing_else_moves(scoped_db, monkeypatch):
    await _load(scoped_db)
    result = await _sync(monkeypatch, scoped_db)

    assert result["status"] == "synced" and result["target"] == "attached", result
    assert result["offers_written"] == 3
    assert result["offer_skips"] == {"variant_not_on_seed": 1}
    rows = await _offers(scoped_db)
    for oid, price in (("of_canon", 22.7), ("of_v1", 22.7), ("of_v2", 38.0)):
        row = rows[oid]
        assert float(row["list_price"]) == price, oid
        assert float(row["merchant_effective_price"]) == price
        assert float(row["estimated_best_price"]) == price
        assert row["currency"] == "USD" and row["touched"], oid
        # The read dates the price it wrote (mig 246), which is what the gateway's as-of reads.
        assert row["checked"], oid
    for oid, price in (("of_v999", 11.0), ("of_ulta", 31.0)):
        assert float(rows[oid]["list_price"]) == price and not rows[oid]["touched"], oid
        assert not rows[oid]["checked"], oid


async def test_variant_rows_hold_until_every_variant_was_re_read(scoped_db, monkeypatch):
    await _load(scoped_db, variant_refresh_status="not_all_re_read")
    result = await _sync(monkeypatch, scoped_db)
    assert result["offers_written"] == 1
    rows = await _offers(scoped_db)
    assert float(rows["of_canon"]["list_price"]) == 22.7
    assert float(rows["of_v2"]["list_price"]) == 40.0 and not rows["of_v2"]["touched"]
    assert rows["of_canon"]["checked"] and not rows["of_v2"]["checked"]


async def test_the_guard_refuses_a_row_whose_currency_moved_after_the_read(scoped_db, monkeypatch):
    """The UPDATE re-checks currency and suppression: a concurrent writer wins, and RETURNING
    tells the lane it wrote nothing."""
    from services import external_offer_dual_write as mod

    await _load(scoped_db)
    await scoped_db.execute("UPDATE catalog_offers SET currency = 'GBP' WHERE offer_id = 'of_canon'")
    row = await scoped_db.fetch_one(
        mod.ATTACHED_LISTING_OFFER_UPDATE_SQL, {"offer_id": "of_canon", "price": 22.7, "currency": "USD"}
    )
    assert row is None
    await scoped_db.execute("UPDATE catalog_offers SET currency = 'USD', suppressed_at = NOW() WHERE offer_id = 'of_canon'")
    row = await scoped_db.fetch_one(
        mod.ATTACHED_LISTING_OFFER_UPDATE_SQL, {"offer_id": "of_canon", "price": 22.7, "currency": "USD"}
    )
    assert row is None
    assert float((await _offers(scoped_db))["of_canon"]["list_price"]) == 29.0

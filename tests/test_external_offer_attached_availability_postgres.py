"""The attached lane's stock write (EXTERNAL_OFFER_ATTACHED_AVAILABILITY_WRITE_ENABLED) on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_external_offer_attached_availability_postgres.py

tests/test_external_offer_attached_availability.py proves the plan and the binds on fakes. This file proves the
guarded UPDATE itself: `availability = COALESCE(CAST(:availability AS VARCHAR), availability)` writes the stated
stock on the product-level row only, keeps every other row's (and every row's with the flag off), in both
statement shapes (with and without migration 246's price_checked_at).

ISOLATION: a per-process scratch schema, the real migrations applied there, dropped at teardown.
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
    "133_catalog_source_domain.sql",
    "135_catalog_product_sku_stale_suppression.sql",
    "149_catalog_offers_agent_decision_fields.sql",
    "246_catalog_offers_price_checked_at.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"attached_availability_test_{os.getpid()}"

PK = "ext:sample-body-cream::0a1b2c3d"
DEST = "https://brand.example/products/body-cream"
SEED_ID = "seed:catalog_enrichment_agent_v1:0a1b2c3d4e5f6071"
SELLER = "agent_seed::brand-example"


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


async def _load(db):
    seed_data = {
        "variants": [
            {"variant_id": "40000000000001", "price_amount": 29.2},
            {"variant_id": "40000000000002", "price_amount": 41.0},
        ],
        "snapshot": {"variant_refresh": {"status": "all_re_read"}},
    }
    await db.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, market, destination_url,"
        " domain, title, price_amount, price_currency, availability, status, attached_product_key, seed_data)"
        " VALUES (:id, 'brand:0a1b2c3d', 'US', :dest, 'brand.example', 'Body Cream', 29.2, 'USD',"
        "         'in_stock', 'active', :pk, CAST(:sd AS jsonb))",
        {"id": SEED_ID, "dest": DEST, "pk": PK, "sd": json.dumps(seed_data)},
    )
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,"
        " title, catalog_track, truth_tier, readiness_tier, source_system, source_ref)"
        " VALUES (:pk, :m, 'external_seed', :pk, 'Body Cream', 'external_referral',"
        "         'observed', 'referral_only', 'catalog_enrichment_agent_v1', 'agent-run-1')",
        {"pk": PK, "m": SELLER},
    )
    for sku, vid in ((f"{PK}::canonical", "canonical"), (f"{PK}::v:40000000000001", "40000000000001"),
                     (f"{PK}::v:40000000000002", "40000000000002")):
        await db.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,"
            " source_product_id, source_variant_id, title, currency)"
            " VALUES (:sk, :pk, :m, 'external_seed', :pk, :vid, 'x', 'USD')",
            {"sk": sku, "pk": PK, "m": SELLER, "vid": vid},
        )

    async def offer(oid, sku, price, *, merchant=SELLER, dest=DEST, availability="unknown"):
        await db.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
            " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
            " currency, list_price, merchant_effective_price, estimated_best_price,"
            " source_system, source_ref, offer_payload, updated_at)"
            " VALUES (:oid, :sk, :pk, :m, 'external_referral', 'observed', 'referral_only',"
            "         'external_referral', 'default', :av, 'USD', :p, :p, :p,"
            "         'catalog_enrichment_agent_v1', :dest, CAST(:payload AS jsonb),"
            "         TIMESTAMP '2026-09-12 01:37:40')",
            {"oid": oid, "sk": sku, "pk": PK, "m": merchant, "p": price, "av": availability,
             "dest": dest, "payload": json.dumps({"destination_url": dest})},
        )

    await offer("of_canon", f"{PK}::canonical", 29.00)
    await offer("of_v1", f"{PK}::v:40000000000001", 29.00)
    await offer("of_v2", f"{PK}::v:40000000000002", 40.00, availability="out_of_stock")
    await offer("of_other", f"{PK}::canonical", 31.00, merchant="agent_seed::retailer::shop.example",
                dest="https://shop.example/p/cream")


async def _stock(db):
    rows = await db.fetch_all("SELECT offer_id, availability, list_price FROM catalog_offers")
    return {r["offer_id"]: (r["availability"], float(r["list_price"])) for r in rows}


async def _sync(monkeypatch, db, *, flag, stated, price_check_present, source="refresh"):
    from services import external_offer_dual_write as mod

    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    if flag:
        monkeypatch.setenv(mod.ATTACHED_AVAILABILITY_WRITE_ENV, "1")
    else:
        monkeypatch.delenv(mod.ATTACHED_AVAILABILITY_WRITE_ENV, raising=False)
    monkeypatch.setattr(mod, "database", db)
    monkeypatch.setattr(mod, "_price_check_column", {"present": price_check_present, "checked": None})
    if not price_check_present:
        # Answer "absent" without asking, so the WITHOUT_PRICE_CHECK statement is the one that runs.
        import time
        mod._price_check_column["checked"] = time.monotonic()
    return await mod.sync_offer_for_seed(
        SEED_ID, attached_price_source=source, currency_read=True, availability_stated=stated)


@pytest.mark.parametrize("price_check_present", [True, False])
async def test_the_stated_stock_lands_on_the_product_level_row_only(scoped_db, monkeypatch, price_check_present):
    await _load(scoped_db)
    result = await _sync(monkeypatch, scoped_db, flag=True, stated="in_stock",
                         price_check_present=price_check_present)
    assert result["status"] == "synced" and result["offers_written"] == 3
    stock = await _stock(scoped_db)
    assert stock["of_canon"] == ("in_stock", 29.2), "unknown -> in_stock, with the re-read price"
    assert stock["of_v1"] == ("unknown", 29.2), "a variant row is re-priced but keeps its stock"
    assert stock["of_v2"] == ("out_of_stock", 41.0)
    assert stock["of_other"] == ("unknown", 31.0), "another seller's listing is never written"


@pytest.mark.parametrize("price_check_present", [True, False])
async def test_an_out_of_stock_statement_is_written_too(scoped_db, monkeypatch, price_check_present):
    await _load(scoped_db)
    await scoped_db.execute("UPDATE catalog_offers SET availability = 'in_stock' WHERE offer_id = 'of_canon'")
    await _sync(monkeypatch, scoped_db, flag=True, stated="out_of_stock", price_check_present=price_check_present)
    assert (await _stock(scoped_db))["of_canon"][0] == "out_of_stock"


@pytest.mark.parametrize("flag, stated, source", [
    (False, "in_stock", "refresh"),       # flag off: byte-for-byte the old write
    (True, None, "refresh"),              # the page stated nothing this run
    (True, "unknown", "refresh"),
    (True, "in_stock", "employee_edit"),  # a person vouching for a price says nothing about stock
])
async def test_otherwise_every_row_keeps_its_stock(scoped_db, monkeypatch, flag, stated, source):
    await _load(scoped_db)
    before = {k: v[0] for k, v in (await _stock(scoped_db)).items()}
    result = await _sync(monkeypatch, scoped_db, flag=flag, stated=stated, price_check_present=True, source=source)
    assert result["status"] == "synced"
    after = await _stock(scoped_db)
    assert {k: v[0] for k, v in after.items()} == before
    assert after["of_canon"][1] == 29.2, "the price write itself is unchanged"


# ── the marker (P2-2 of the be#2806 review): a rollback must find exactly the rows the writer touched ──────────

async def _payloads(db):
    rows = await db.fetch_all("SELECT offer_id, offer_payload::text AS p, availability FROM catalog_offers")
    return {r["offer_id"]: (json.loads(r["p"]) if r["p"] else None, r["availability"]) for r in rows}


@pytest.mark.parametrize("price_check_present", [True, False])
async def test_every_row_given_a_stock_carries_the_marker_and_only_those(scoped_db, monkeypatch, price_check_present):
    await _load(scoped_db)
    result = await _sync(monkeypatch, scoped_db, flag=True, stated="in_stock", price_check_present=price_check_present)
    assert result["availability_written"] == {"in_stock": 1}
    p = await _payloads(scoped_db)
    marker = p["of_canon"][0]["availability_read"]
    assert {k: marker[k] for k in ("source", "value", "previous")} == {
        "source": "refresh", "value": "in_stock", "previous": "unknown"}
    assert marker["at"], "when it was written"
    assert p["of_canon"][0]["price_read_source"] == "refresh", "the price marker is still written"
    for oid in ("of_v1", "of_v2", "of_other"):
        assert "availability_read" not in (p[oid][0] or {}), oid
    marked = await scoped_db.fetch_all("SELECT offer_id FROM catalog_offers WHERE offer_payload ? 'availability_read'")
    assert [r["offer_id"] for r in marked] == ["of_canon"], "the rollback query finds exactly the written row"


async def test_previous_is_the_stock_before_the_FIRST_write(scoped_db, monkeypatch):
    await _load(scoped_db)
    await _sync(monkeypatch, scoped_db, flag=True, stated="in_stock", price_check_present=True)
    await _sync(monkeypatch, scoped_db, flag=True, stated="out_of_stock", price_check_present=True)
    payload, availability = (await _payloads(scoped_db))["of_canon"]
    assert availability == "out_of_stock"
    assert payload["availability_read"]["value"] == "out_of_stock"
    assert payload["availability_read"]["previous"] == "unknown", "a rollback restores the pre-writer stock"


@pytest.mark.parametrize("flag, stated", [(False, "in_stock"), (True, None), (True, "unknown")])
async def test_a_run_that_writes_no_stock_writes_no_marker(scoped_db, monkeypatch, flag, stated):
    await _load(scoped_db)
    result = await _sync(monkeypatch, scoped_db, flag=flag, stated=stated, price_check_present=True)
    assert "availability_written" not in result
    marked = await scoped_db.fetch_all("SELECT offer_id FROM catalog_offers WHERE offer_payload ? 'availability_read'")
    assert marked == []
    payload = (await _payloads(scoped_db))["of_canon"][0]
    assert set(payload) == {"destination_url", "price_read_source"}, "the payload is what it was before this PR"


async def test_a_payload_that_cannot_hold_the_marker_takes_no_stock(scoped_db, monkeypatch):
    """A scalar payload passes through untouched (jsonb_set would raise): the stock is not written either,
    so a stock without its marker never exists, and the run counts it as skipped."""
    await _load(scoped_db)
    await scoped_db.execute("UPDATE catalog_offers SET offer_payload = CAST('\"legacy\"' AS jsonb) WHERE offer_id = 'of_canon'")
    # The listing still matches on source_ref; only its payload is a legacy scalar.
    result = await _sync(monkeypatch, scoped_db, flag=True, stated="in_stock", price_check_present=True)
    payload, availability = (await _payloads(scoped_db))["of_canon"]
    assert payload == "legacy" and availability == "unknown"
    assert "availability_written" not in result and result.get("availability_skipped") == 1

"""The unconfirmed-dead hide on the production dialect, through the real readers that decide whether it is reversible
and what a buyer sees.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_external_seed_dead_unconfirmed_hide_postgres.py

* THE QUEUE (be#2725 review P1). The mirror back-links each seed's `attached_product_key` to its own mirror product,
  which is exactly what the hide suppresses, and the seed lane's suppressed-product anti-join drops such a seed from
  the refresh queue. The sweep cannot read those hosts' catalogues, so the refresh is the only lane that can lift the
  hide. Through the real `get_external_referral_refresh_candidate_seed_ids`: a seed hidden under this reason stays a
  candidate, a live read lifts it, and a seed suppressed for any other reason still leaves the queue.
* THE CARD (design R3). Two sellers on one content_key, one of them hidden: the PDP view row is rebuilt through the
  real assembler and upsert and read back through the PDP route's own SELECT. The card stays served with the other
  seller's offer, and the lift restores both.

Each test runs in its own scratch schema, alone on the search_path (the dialect gate shares one database).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL (the production-dialect gate)")

_MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    return "postgresql://" + DATABASE_URL[len("postgres://"):] if DATABASE_URL.startswith("postgres://") else DATABASE_URL


# --------------------------------------------------------------------------- the refresh queue

_QUEUE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "200_external_seed_destination_liveness.sql",
    "202_external_seed_content_freshness.sql",
    "266_external_seed_destination_corroborated_dead_at.sql",
    "279_external_seed_destination_dead_404_since.sql",
    "058_catalog_core.sql",
    "132_catalog_offer_suppression_writer_audit.sql",
    "135_catalog_product_sku_stale_suppression.sql",
    "134_catalog_source_quarantine.sql",
)


@pytest.fixture()
async def queue_db():
    import databases

    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    schema = f"unconfirmed_queue_{uuid.uuid4().hex[:8]}"
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"CREATE SCHEMA {schema}")
    scoped = databases.Database(_url(), server_settings={"search_path": schema})
    await scoped.connect()
    try:
        for name in _QUEUE_MIGRATIONS:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                await scoped.execute(statement)
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.disconnect()


async def _seed_with_mirror(db, seed_id: str) -> str:
    from services.external_offer_dual_write import MIRROR_SOURCE_SYSTEM

    key = f"seed|m|{seed_id}"
    await db.execute(
        "INSERT INTO external_product_seeds (id, market, destination_url, attached_product_key, price_currency)"
        " VALUES (:id, 'US', :u, :k, 'USD')",
        {"id": seed_id, "u": f"https://brand.example/shop/{seed_id}.html", "k": key})
    # The mirror product, back-linked as the seed's attached product (the mirror's own back-link).
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, source_system,"
        " source_ref) VALUES (:k, 'm', 'external_seed', :id, 't', :src, :id)",
        {"k": key, "id": seed_id, "src": MIRROR_SOURCE_SYSTEM})
    return key


@pytest.mark.asyncio
async def test_a_hidden_seed_stays_in_the_refresh_queue_and_its_live_read_lifts_the_hide(queue_db, monkeypatch):
    import services.external_referral_readiness as err
    from services import destination_dead_suppression
    from services import external_seed_destination_liveness as liveness

    for mod in (err, liveness):
        monkeypatch.setattr(mod, "database", queue_db)
    monkeypatch.setenv(liveness.UNCONFIRMED_HIDE_ENV, "on")
    db = queue_db
    dead_key = await _seed_with_mirror(db, "eps_dead")
    await _seed_with_mirror(db, "eps_alive")
    pending_key = await _seed_with_mirror(db, "eps_pending")
    other_key = await _seed_with_mirror(db, "eps_other")
    await destination_dead_suppression.suppress_pending(
        [pending_key], reason=liveness.PENDING_SUPPRESSION_REASON, stamp=NOW, db=db)
    await db.execute("UPDATE catalog_products SET suppressed_at = :at, suppression_reason = 'duplicate_offer'"
                     " WHERE product_key = :k", {"at": NOW, "k": other_key})

    before = await err.get_external_referral_refresh_candidate_seed_ids(limit=50)
    assert set(before) == {"eps_dead", "eps_alive"}, "any other suppression still leaves the queue"

    hard_404 = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, "http_404")
    await liveness.record_destination_observation("eps_dead", hard_404, now=NOW)
    second = await liveness.record_destination_observation("eps_dead", hard_404, now=NOW + timedelta(hours=25))
    assert second.get("unconfirmed_hide_candidate") is True
    by_host: dict = {}
    liveness.count_unconfirmed_observation(by_host, "brand.example", "eps_dead", second)
    summary = await liveness.apply_unconfirmed_hides(by_host, mode="on", now=NOW + timedelta(hours=25))
    assert summary["totals"]["hidden_products"] == 1
    hidden = await db.fetch_one("SELECT suppressed_at, suppression_reason FROM catalog_products WHERE product_key = :k",
                                {"k": dead_key})
    assert hidden["suppression_reason"] == liveness.UNCONFIRMED_DEAD_REASON

    # THE FIX: still a candidate, so the next refresh observes it.
    during = await err.get_external_referral_refresh_candidate_seed_ids(limit=50)
    assert set(during) == {"eps_dead", "eps_alive"}

    alive = liveness.DestinationObservation(liveness.VERDICT_LIVE, 200, None)
    lifted = await liveness.record_destination_observation("eps_dead", alive, now=NOW + timedelta(hours=30))
    assert lifted["unconfirmed_lifted"] == 1
    back = await db.fetch_one("SELECT suppressed_at, suppression_reason FROM catalog_products WHERE product_key = :k",
                              {"k": dead_key})
    assert back["suppressed_at"] is None and back["suppression_reason"] is None
    after = await err.get_external_referral_refresh_candidate_seed_ids(limit=50)
    assert set(after) == {"eps_dead", "eps_alive"}


# --------------------------------------------------------------------------- the card (two sellers, one content_key)

_SCHEMA = f"unconfirmed_card_{uuid.uuid4().hex[:8]}"
BRAND = "Example Brand"


def _pk(name: str) -> str:
    return "ext:retailer:" + hashlib.sha256(name.encode()).hexdigest()[:32]


def _seller(host: str):
    from services.seller_identity import resolve_seed_seller_identity

    return resolve_seed_seller_identity(brand=BRAND, domain=host)


@pytest.fixture(scope="module")
def card_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    _assert_throwaway_database()
    admin = create_engine(_url())
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
    engine = create_engine(_url(), connect_args={"options": f"-csearch_path={_SCHEMA}"})
    try:
        metadata.create_all(engine, tables=[metadata.tables[t] for t in (
            "catalog_products", "catalog_offers", "catalog_merchants", "catalog_skus", "agent_pdp_view")])
        with engine.begin() as c:
            for f in ("044_external_product_seeds.sql", "045_product_groups.sql",
                      "134_catalog_source_quarantine.sql", "179_identity_resolution_d2.sql",
                      "269_agent_pdp_view_seller_count.sql"):
                c.exec_driver_sql((_MIGRATIONS / f).read_text())
            # Read by the assembler's source-quarantine anti-join; only the columns it reads.
            c.execute(text("CREATE TABLE pdp_identity_listing (product_id text, merchant_id text)"))
            c.execute(text(
                "CREATE TABLE merchant_stores (store_id text PRIMARY KEY, merchant_id text, platform text,"
                " domain text, status text, is_primary boolean, last_sync timestamptz, created_at timestamptz)"))
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{_SCHEMA}" CASCADE'))
        admin.dispose()


def _listing(c, name, *, ck, host, price, group, primary=False, source_system="catalog_enrichment_agent_v1",
             source_ref=None):
    from sqlalchemy import text

    pk, merchant, url = _pk(name), _seller(host)["merchant_id"], f"https://{host}/products/{name}"
    c.execute(text(
        "INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status)"
        " VALUES (:m, :m, 'external_seed', 'observed') ON CONFLICT DO NOTHING"), {"m": merchant})
    c.execute(text(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
        " content_key, pivota_signature_id, canonical_url, catalog_track, truth_tier, readiness_tier,"
        " pdp_scope, pdp_lifecycle_stage, source_system, source_ref, updated_at)"
        " VALUES (:pk,:m,'external_seed',:pk,'Example Barrier Cream 50ml',:brand,:ck,:sig,:url,'citation','primary',"
        "         'referral_only','multi_merchant_canonical','published',:src,:ref,NOW())"
    ), {"pk": pk, "m": merchant, "brand": BRAND, "ck": ck, "sig": "sig_" + hashlib.sha256(pk.encode()).hexdigest()[:24],
        "url": url, "src": source_system, "ref": source_ref})
    c.execute(text(
        "INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id, is_primary)"
        " VALUES (:g, :m, 'external_seed', :pk, :primary)"), {"g": group, "m": merchant, "pk": pk, "primary": primary})
    sku = f"{pk}::v:1"
    c.execute(text(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, source_variant_id,"
        " title, currency, updated_at) VALUES (:sk,:pk,:m,'external_seed',:pk,'1','x','USD',NOW())"),
        {"sk": sku, "pk": pk, "m": merchant})
    c.execute(text(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,"
        " readiness_tier, offer_mode, channel, availability, market, currency, list_price, merchant_effective_price,"
        " offer_type, is_first_party, source_ref, offer_payload, updated_at)"
        " VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only','redirect','external_referral',"
        "         'in_stock','US','USD',:p,:p,'retailer',false,:url,cast(:payload AS jsonb),NOW())"
    ), {"oid": f"of_{name}", "sk": sku, "pk": pk, "m": merchant, "p": price, "url": url,
        "payload": json.dumps({"destination_url": url})})
    return pk


def _run(monkeypatch, work):
    import databases

    import db.product_enrichment as enrichment
    import services.outcome_aggregation_service as outcomes
    from services import agent_pdp_view_assembler as assembler

    monkeypatch.setattr(enrichment, "get_enrichments_for_products", AsyncMock(return_value={}))
    monkeypatch.setattr(outcomes, "seller_trust_bulk", AsyncMock(return_value={}))
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    assembler.reset_seller_count_probe()
    assembler.reset_market_prices_probe()

    async def go():
        db = databases.Database(_url(), server_settings={"search_path": _SCHEMA})
        await db.connect()
        try:
            return await work(db)
        finally:
            await db.disconnect()

    try:
        return asyncio.run(go())
    finally:
        assembler.reset_seller_count_probe()
        assembler.reset_market_prices_probe()


async def _served(db, ck):
    """Rebuild the view row through the real assembler and upsert, then read it as the PDP route does."""
    from routes import agent_pdp_v1
    from services import agent_pdp_view_assembler as assembler

    built = await assembler.build_agent_pdp_view_row(ck, refresh_source="test", db=db)
    if built is None:
        return None, None
    await assembler.execute_agent_pdp_view_upsert(db, built)
    stored = await db.fetch_one(agent_pdp_v1.BYPASS_SELECT_BY_CONTENT_KEY_SQL, {"id": ck})
    row = agent_pdp_v1._row_to_dict(stored)
    return row, agent_pdp_v1._build_response(row)


def _offers_text(payload) -> str:
    [offers] = [m for m in payload["modules"] if m["type"] == "offers"]
    return json.dumps(offers["data"].get("offers") or [], default=str)


def test_a_two_seller_card_stays_served_by_the_other_seller_and_the_lift_restores_both(card_engine, monkeypatch):
    from services import destination_dead_suppression
    from services import external_seed_destination_liveness as liveness
    from services.external_offer_dual_write import MIRROR_SOURCE_SYSTEM
    from sqlalchemy import text

    with card_engine.begin() as c:
        for t in ("agent_pdp_view", "catalog_offers", "catalog_skus", "catalog_products", "catalog_merchants",
                  "product_group_members"):
            c.execute(text(f"DELETE FROM {t}"))
        ours = _listing(c, "ours", ck="ck_two", host="shop-one.example", price=25, group="pg_two", primary=True,
                        source_system=MIRROR_SOURCE_SYSTEM, source_ref="eps_ours")
        _listing(c, "theirs", ck="ck_two", host="shop-two.example", price=28, group="pg_two")

    async def work(db):
        row0, payload0 = await _served(db, "ck_two")
        # What `apply_unconfirmed_hides` writes after its re-check: this seed's mirror product and its offers.
        moved = await destination_dead_suppression.suppress_pending(
            [ours], reason=liveness.UNCONFIRMED_DEAD_REASON, stamp=NOW, db=db)
        row1, payload1 = await _served(db, "ck_two")
        await destination_dead_suppression.lift_pending([ours], reason=liveness.UNCONFIRMED_DEAD_REASON, db=db)
        row2, payload2 = await _served(db, "ck_two")
        return row0, payload0, moved, row1, payload1, row2, payload2

    row0, payload0, moved, row1, payload1, row2, payload2 = _run(monkeypatch, work)
    assert moved["products"] == [ours] and moved["offers"] == ["of_ours"]
    assert (row0["offer_count"], row0["seller_count"]) == (2, 2)
    # Hidden: the card is still served, by the other seller alone, at its price.
    assert row1 is not None and payload1 is not None
    assert (row1["offer_count"], row1["seller_count"]) == (1, 1)
    assert float(row1["price_min"]) == 28.0
    assert "shop-two.example" in _offers_text(payload1) and "shop-one.example" not in _offers_text(payload1)
    assert "shop-one.example" in _offers_text(payload0)
    # Lifted: both again.
    assert (row2["offer_count"], row2["seller_count"]) == (2, 2)
    assert float(row2["price_min"]) == 25.0
    assert "shop-one.example" in _offers_text(payload2) and "shop-two.example" in _offers_text(payload2)

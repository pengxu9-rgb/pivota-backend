"""offer_count / price_min drop the placeholder twins, and seller_count (migration 269) counts
sellers -- on POSTGRES, through the real assembler, upsert and PDP route.

THE DEFECT (read-only prod census, 2026-10-19). Every reader of agent_pdp_view.offer_count read it as
a seller count: routes/agent_pdp_v1 scoped a row `multi_merchant_canonical` at offer_count > 1,
pivota-agent-ui marks a card multi-seller (and drops its savings badge) at offers_count > 1. But a
retailer row carries its per-variant offer AND its `<product_key>::canonical` placeholder to the same
page, so one store with one size counted 2. Of 26,763 rows, 18,927 had offer_count > 1 and 1,845
have more than one seller; dropping the twins takes 11,164 to 1, and 5,918 stay above 1 on one
store's shades -- so the scope reads seller_count. A twin's price was the headline on 632 rows.

The rows below are the retailer lane's shape (`ext:` keys, `<pk>::v:<id>` and `<pk>::canonical`
SKUs, observed sellers minted by services/seller_identity, product_group_members). Nothing hands an
offer list to a function: build_agent_pdp_view_row reads them with its own SQL,
execute_agent_pdp_view_upsert writes the row, and the route's own SELECT reads it back into
_build_response.

Runs in its own scratch schema, alone on the search_path. agent_pdp_view is built from the db.catalog
Table, which does not carry seller_count -- every environment's shape before migration 269.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)"
)

_SCHEMA = f"apv_seller_count_{uuid.uuid4().hex[:8]}"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
_MIGRATION_269 = _MIGRATIONS / "269_agent_pdp_view_seller_count.sql"
_DOWN_269 = _MIGRATIONS / "down" / "269_agent_pdp_view_seller_count_down.sql"
BRAND = "O HUI"


def _pk(name: str) -> str:
    return "ext:retailer:" + hashlib.sha256(name.encode()).hexdigest()[:32]


def _seller(host: str):
    from services.seller_identity import resolve_seed_seller_identity

    return resolve_seed_seller_identity(brand=BRAND, domain=host)


@pytest.fixture(scope="module")
def schema_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    admin = create_engine(DATABASE_URL)
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={_SCHEMA}"})
    try:
        metadata.create_all(engine, tables=[metadata.tables[t] for t in (
            "catalog_products", "catalog_offers", "catalog_merchants", "catalog_skus", "agent_pdp_view")])
        with engine.begin() as c:
            for f in ("044_external_product_seeds.sql", "045_product_groups.sql",
                      "134_catalog_source_quarantine.sql", "179_identity_resolution_d2.sql"):
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


def _insert_product(c, *, pk, ck, merchant, url, title, group=None, primary=False):
    from sqlalchemy import text

    c.execute(text(
        "INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status)"
        " VALUES (:m, :m, 'external_seed', 'observed') ON CONFLICT DO NOTHING"), {"m": merchant})
    c.execute(text(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
        " content_key, pivota_signature_id, canonical_url, catalog_track, truth_tier, readiness_tier,"
        " pdp_scope, pdp_lifecycle_stage, source_system, updated_at)"
        " VALUES (:pk,:m,'external_seed',:pk,:title,:brand,:ck,:sig,:url,'citation','primary',"
        "         'referral_only','multi_merchant_canonical','published','catalog_enrichment_agent_v1',NOW())"
    ), {"pk": pk, "m": merchant, "title": title, "brand": BRAND, "ck": ck,
        "sig": "sig_" + hashlib.sha256(pk.encode()).hexdigest()[:24], "url": url})
    if group:
        c.execute(text(
            "INSERT INTO product_group_members (product_group_id, merchant_id, platform,"
            " platform_product_id, is_primary) VALUES (:g, :m, 'external_seed', :pk, :primary)"
        ), {"g": group, "m": merchant, "pk": pk, "primary": primary})


def _insert_offer(c, *, offer_id, sku, pk, merchant, price, url, market="US", currency="USD"):
    from sqlalchemy import text

    c.execute(text(
        "INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status)"
        " VALUES (:m, :m, 'external_seed', 'observed') ON CONFLICT DO NOTHING"), {"m": merchant})
    c.execute(text(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,"
        " source_variant_id, title, currency, updated_at)"
        " VALUES (:sk,:pk,:m,'external_seed',:pk,:vid,'x',:cur,NOW()) ON CONFLICT DO NOTHING"
    ), {"sk": sku, "pk": pk, "m": merchant, "vid": sku.rsplit(":", 1)[-1], "cur": currency})
    c.execute(text(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,"
        " readiness_tier, offer_mode, channel, availability, market, currency, list_price,"
        " merchant_effective_price, offer_type, is_first_party, source_ref, offer_payload, updated_at)"
        " VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only','redirect',"
        "         'external_referral','in_stock',:market,:cur,:p,:p,'retailer',false,:url,"
        "         cast(:payload AS jsonb), NOW())"
    ), {"oid": offer_id, "sk": sku, "pk": pk, "m": merchant, "market": market, "cur": currency,
        "p": price, "url": url, "payload": json.dumps({"destination_url": url})})


def _listing(c, name, *, ck, host, price, group, primary=False, twin=True, title="O HUI Eye Cream 25ml"):
    """One store's listing: its per-variant offer and, as the retailer lane writes it, the
    product-level placeholder twin to the same page at the same price."""
    pk, merchant, url = _pk(name), _seller(host)["merchant_id"], f"https://{host}/products/{name}"
    _insert_product(c, pk=pk, ck=ck, merchant=merchant, url=url, title=title, group=group, primary=primary)
    _insert_offer(c, offer_id=f"of_{name}_v", sku=f"{pk}::v:1", pk=pk, merchant=merchant, price=price, url=url)
    if twin:
        _insert_offer(c, offer_id=f"of_{name}_c", sku=f"{pk}::canonical", pk=pk, merchant=merchant,
                      price=price, url=url)
    return pk, merchant, url


def _seed(engine):
    from sqlalchemy import text

    with engine.begin() as c:
        for t in ("agent_pdp_view", "catalog_offers", "catalog_skus", "catalog_products", "catalog_merchants",
                  "product_group_members"):
            c.execute(text(f"DELETE FROM {t}"))

        # ONE STORE, ONE SIZE, its twin pair: the 11,164 rows that read as two sellers.
        _listing(c, "solo", ck="ck_solo", host="kbeautymakeup.com", price=38, group="pg_solo", primary=True)

        # ONE STORE, SEVEN SHADES and the product-level placeholder at $19, a price no shade has:
        # seven offers, one seller (the 5,918), and the headline was the placeholder's $19.
        pk, merchant, url = _pk("shades"), _seller("kbeautymakeup.com")["merchant_id"], \
            "https://kbeautymakeup.com/products/o-hui-lip"
        _insert_product(c, pk=pk, ck="ck_shades", merchant=merchant, url=url, title="O HUI Lip",
                        group="pg_shades", primary=True)
        for n in range(7):
            _insert_offer(c, offer_id=f"of_shade_{n}", sku=f"{pk}::v:{n}", pk=pk, merchant=merchant,
                          price=20 + n, url=url)
        _insert_offer(c, offer_id="of_shade_c", sku=f"{pk}::canonical", pk=pk, merchant=merchant, price=19, url=url)

        # TWO STORES, a twin pair each: two offers, two sellers, multi-merchant.
        _listing(c, "pair_a", ck="ck_pair", host="kbeautymakeup.com", price=38, group="pg_pair", primary=True)
        _listing(c, "pair_b", ck="ck_pair", host="jolse.com", price=41, group="pg_pair")

        # ANOTHER RETAILER'S OFFER HUNG ON A PRODUCT as `<pk>::canonical` (scripts/attach_retailer_offer.py):
        # that retailer's only offer, not a twin of the row's own variant offer.
        pk, _, url = _listing(c, "attached", ck="ck_attached", host="stylekorean.com", price=52,
                              group="pg_attached", primary=True, twin=False)
        _insert_offer(c, offer_id="of_attached_other", sku=f"{pk}::canonical", pk=pk,
                      merchant=_seller("koolseoul.com")["merchant_id"], price=49,
                      url="https://koolseoul.com/products/o-hui-eye")

        # PER CURRENCY: store 1 prices US/USD (with its twin) and carries an SG/SGD placeholder with no SGD
        # variant beside it (its only SGD offer: kept); store 2 prices SG/SGD with its twin.
        pk1, m1, url1 = _listing(c, "multi_1", ck="ck_multi", host="nanasbeautyholic.com", price=42,
                                 group="pg_multi", primary=True)
        _insert_offer(c, offer_id="of_multi_1_sgd", sku=f"{pk1}::canonical", pk=pk1, merchant=m1, price=55,
                      url=url1, market="SG", currency="SGD")
        pk2, m2, url2 = _pk("multi_2"), _seller("cocomo.sg")["merchant_id"], "https://cocomo.sg/products/o-hui"
        _insert_product(c, pk=pk2, ck="ck_multi", merchant=m2, url=url2, title="O HUI Eye Cream 25ml",
                        group="pg_multi")
        for oid, sku in (("of_multi_2_v", f"{pk2}::v:1"), ("of_multi_2_c", f"{pk2}::canonical")):
            _insert_offer(c, offer_id=oid, sku=sku, pk=pk2, merchant=m2, price=50, url=url2,
                          market="SG", currency="SGD")


def _set_column(engine, present: bool):
    from sqlalchemy import text

    with engine.begin() as c:
        if present:
            c.exec_driver_sql(_MIGRATION_269.read_text())
        else:
            c.exec_driver_sql(_DOWN_269.read_text())
            assert c.execute(text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = :s"
                " AND table_name = 'agent_pdp_view' AND column_name = 'seller_count'"), {"s": _SCHEMA}).scalar() == 0


def _run(monkeypatch, work):
    """Run `work(db)` against the scratch schema, with the overlays the assembler reads stubbed (copy,
    seller trust) and both column probes reset."""
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
        db = databases.Database(DATABASE_URL, server_settings={"search_path": _SCHEMA})
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


async def _rebuild(db, ck):
    from services import agent_pdp_view_assembler as assembler

    row = await assembler.build_agent_pdp_view_row(ck, refresh_source="test", db=db)
    await assembler.execute_agent_pdp_view_upsert(db, row)
    return row


async def _served(db, ck, buyer_market=None):
    """What the PDP route serves for the stored row: its own SELECT, then _build_response."""
    from routes import agent_pdp_v1

    stored = await db.fetch_one(agent_pdp_v1.BYPASS_SELECT_BY_CONTENT_KEY_SQL, {"id": ck})
    row = agent_pdp_v1._row_to_dict(stored)
    if buyer_market:
        row["market_prices"] = json.loads(await db.fetch_val(
            "SELECT market_prices::text FROM agent_pdp_view WHERE content_key = :ck", {"ck": ck}))
    return row, agent_pdp_v1._build_response(row, buyer_market=buyer_market)


def _scope(payload):
    [canonical] = [m for m in payload["modules"] if m["type"] == "canonical"]
    return canonical["data"].get("canonical_scope")


def _counts(payload):
    [canonical] = [m for m in payload["modules"] if m["type"] == "canonical"]
    [offers] = [m for m in payload["modules"] if m["type"] == "offers"]
    pdp = canonical["data"]["pdp_payload"]
    triples = {(pdp["offers_count"], pdp["sellers_count"]),
               (offers["data"]["offers_count"], offers["data"]["sellers_count"]),
               (payload["offers_count"], payload["sellers_count"])}
    assert len(triples) == 1, triples
    return triples.pop()


# ------------------------------------------------------------------ with migration 269

def test_one_store_with_one_twin_pair_is_one_offer_one_seller_and_not_multi_merchant(schema_engine, monkeypatch):
    _seed(schema_engine)
    _set_column(schema_engine, True)

    async def work(db):
        built = await _rebuild(db, "ck_solo")
        return built, await _served(db, "ck_solo")

    built, (row, payload) = _run(monkeypatch, work)
    # Was offer_count 2 and multi_merchant_canonical.
    assert (built["offer_count"], built["seller_count"]) == (1, 1)
    assert (row["offer_count"], row["seller_count"]) == (1, 1)
    assert _counts(payload) == (1, 1)
    assert row["product_group_id"] == "pg_solo" and _scope(payload) is None
    usd = built["market_prices"]["currencies"]["USD"]
    assert (usd["offer_count"], usd["seller_count"], usd["price_min"]) == (1, 1, 38.0)


def test_one_store_with_seven_shades_is_seven_offers_one_seller_and_its_headline_is_a_shades_price(
    schema_engine, monkeypatch
):
    _seed(schema_engine)
    _set_column(schema_engine, True)

    async def work(db):
        await _rebuild(db, "ck_shades")
        return await _served(db, "ck_shades")

    row, payload = _run(monkeypatch, work)
    # Was 8 offers and a $19 headline: the placeholder's price, which no shade has.
    assert (row["offer_count"], row["seller_count"]) == (7, 1)
    assert (float(row["price_min"]), float(row["price_max"])) == (20.0, 26.0)
    assert payload["modules"][0]["data"]["pdp_payload"]["product"]["price"]["current"]["amount"] == 20
    assert _counts(payload) == (7, 1)
    # Several offers, one seller: not multi-merchant (offer_count alone said it was).
    assert _scope(payload) is None


def test_two_stores_are_two_sellers_and_multi_merchant(schema_engine, monkeypatch):
    _seed(schema_engine)
    _set_column(schema_engine, True)

    async def work(db):
        await _rebuild(db, "ck_pair")
        return await _served(db, "ck_pair")

    row, payload = _run(monkeypatch, work)
    assert (row["offer_count"], row["seller_count"]) == (2, 2)  # was 4 offers
    assert _counts(payload) == (2, 2)
    assert _scope(payload) == "multi_merchant_canonical"


def test_another_retailers_placeholder_on_the_row_is_its_offer_not_a_twin(schema_engine, monkeypatch):
    _seed(schema_engine)
    _set_column(schema_engine, True)

    async def work(db):
        await _rebuild(db, "ck_attached")
        return await _served(db, "ck_attached")

    row, payload = _run(monkeypatch, work)
    assert (row["offer_count"], row["seller_count"]) == (2, 2)
    assert (float(row["price_min"]), float(row["price_max"])) == (49.0, 52.0)
    assert _scope(payload) == "multi_merchant_canonical"


def test_each_currency_counts_its_own_offers_and_sellers_and_the_sg_buyer_reads_them(schema_engine, monkeypatch):
    _seed(schema_engine)
    _set_column(schema_engine, True)

    async def work(db):
        built = await _rebuild(db, "ck_multi")
        return built, await _served(db, "ck_multi", buyer_market="SG")

    built, (row, payload) = _run(monkeypatch, work)
    # Kept: store 1's USD variant and its lone SGD placeholder, store 2's SGD variant.
    assert (built["offer_count"], built["seller_count"]) == (3, 2)
    currencies = built["market_prices"]["currencies"]
    assert {code: (e["offer_count"], e["seller_count"], e["price_min"], e["price_max"])
            for code, e in currencies.items()} == {"USD": (1, 1, 42.0, 42.0), "SGD": (2, 2, 50.0, 55.0)}
    assert all(o["currency"] == code for code, e in currencies.items() for o in e["offers"])
    # The SG buyer's view is the SGD entry's, never the row's all-currency count.
    assert _counts(payload) == (2, 2)
    assert _scope(payload) == "multi_merchant_canonical"


# ------------------------------------------------------------------ before migration 269, and the rollout

def test_before_the_migration_the_refresh_still_writes_and_the_route_falls_back_to_offer_count(
    schema_engine, monkeypatch
):
    """Prod deploys skip db/migrations/: until 269 (or schema_guard's heal) lands, the refresh must not
    fail and the route must not fail. The upsert leaves seller_count out, the SELECT reads it as NULL,
    sellers_count is null (unknown) and the scope keeps its pre-269 reading of offer_count. Applied
    and rebuilt, the same row reads one seller; the backfill scope finds it before and not after."""
    import scripts.backfill_agent_pdp_view as backfill
    from services import agent_pdp_view_assembler as assembler

    _seed(schema_engine)
    _set_column(schema_engine, False)

    async def before(db):
        assert await assembler.agent_pdp_view_has_seller_count(db) is False
        built = await _rebuild(db, "ck_shades")
        assert built["seller_count"] == 1  # assembled; just not written
        return await _served(db, "ck_shades")

    row, payload = _run(monkeypatch, before)
    assert row["offer_count"] == 7 and row["seller_count"] is None
    assert _counts(payload) == (7, None)
    assert _scope(payload) == "multi_merchant_canonical"

    _set_column(schema_engine, True)
    _set_column(schema_engine, True)  # idempotent

    async def missing_keys(db):
        sql, params = backfill.build_content_key_query(scope="seller_count_missing", limit=10, offset=0)
        return [r["content_key"] for r in await db.fetch_all(sql, params)]

    async def after(db):
        assert await assembler.agent_pdp_view_has_seller_count(db) is True
        assert await missing_keys(db) == ["ck_shades"]
        await _rebuild(db, "ck_shades")
        assert await missing_keys(db) == []
        return await _served(db, "ck_shades")

    row, payload = _run(monkeypatch, after)
    assert row["seller_count"] == 1 and _counts(payload) == (7, 1) and _scope(payload) is None


def test_a_column_dropped_after_the_probe_falls_back_on_the_write(schema_engine, monkeypatch):
    from services import agent_pdp_view_assembler as assembler

    _seed(schema_engine)
    _set_column(schema_engine, True)

    async def work(db):
        assert await assembler.agent_pdp_view_has_seller_count(db) is True
        _set_column(schema_engine, False)
        await _rebuild(db, "ck_pair")  # the probe still says present; the write recovers
        assert assembler._seller_count_probe["present"] is False
        return await db.fetch_val("SELECT offer_count FROM agent_pdp_view WHERE content_key = 'ck_pair'")

    assert _run(monkeypatch, work) == 2

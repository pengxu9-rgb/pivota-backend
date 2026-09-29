"""scripts/restamp_offer_market.py on real PostgreSQL: plan, apply, drift, revert.

The rows are shaped like prod's (census 2026-09-29): jsmbeauty.sg offers stamped market 'US' in SGD, some
reaching their host only through the product URL (no source_domain), one suppressed. Pinned here: which rows
the plan takes and leaves (a USD sibling, another host), that a would-be duplicate shelf refuses the run, that
any drift since the plan writes nothing, and that revert puts every row back and refuses twice.
Isolated schema built from the production model; never run against production.
"""
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)


@pytest.fixture
async def catalog(monkeypatch):
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.database as dbmod
    import db.catalog  # noqa: F401 -- registers the catalog tables on the shared metadata
    from scripts import restamp_offer_market as tool

    refreshed = []

    async def refresh_after(db, m, *, source):
        refreshed.append((source, sorted({o["content_key"] for o in m["offers"] if o.get("content_key")})))
        return {"refreshed": 0}
    monkeypatch.setattr(tool, "refresh_after", refresh_after)

    url = os.environ["DATABASE_URL"]
    schema = "restamp_offer_market_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    database = None
    try:
        for name in ("catalog_products", "catalog_offers"):
            await admin.execute(str(sa.schema.CreateTable(dbmod.metadata.tables[name]).compile(
                dialect=postgresql.dialect())))
        # Not on the shared metadata: migration 179's shape.
        await admin.execute("CREATE TABLE identity_resolution_events (id BIGSERIAL PRIMARY KEY, proposal_id TEXT, "
                            "action TEXT NOT NULL, run_id TEXT NOT NULL, detail JSONB, "
                            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        database = databases.Database(url, server_settings={"search_path": schema})
        await database.connect()
        yield database, admin, tool, refreshed
    finally:
        if database is not None:
            await database.disconnect()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _product(admin, key, url, ck):
    await admin.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url,
                                         content_key)
           VALUES ($1, 'm_sg', 'external_seed', 'shopify:1', 'Cushion', $2, $3)""", key, url, ck)


async def _offer(admin, offer_id, key, *, market="US", currency="SGD", source_domain=None, suppressed=False,
                 sku=None, channel="default"):
    await admin.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, channel, market, currency,
                                       source_domain, suppressed_at)
           VALUES ($1, $2, $3, 'm_sg', $4, $5, $6, $7, $8)""",
        offer_id, sku or f"sku:{offer_id}", key, channel, market, currency, source_domain,
        datetime(2026, 9, 1, tzinfo=timezone.utc) if suppressed else None)


async def _seed_rows(admin):
    await _product(admin, "ext:jsm-cushion::1", "https://www.jsmbeauty.sg/products/cushion", "ck_cushion")
    await _product(admin, "ext:jsm-tint::2", "https://jsmbeauty.sg/products/tint", "ck_tint")
    await _product(admin, "ext:other::3", "https://cocomo.sg/products/x", "ck_other")
    await _offer(admin, "o_url_only", "ext:jsm-cushion::1")                               # host from the product URL
    await _offer(admin, "o_bare_domain", "ext:jsm-tint::2", source_domain="www.jsmbeauty.sg")  # a bare host
    await _offer(admin, "o_suppressed", "ext:jsm-tint::2", suppressed=True)
    await _offer(admin, "o_usd_sibling", "ext:jsm-tint::2", currency="USD")                # Shopify-Markets US sibling
    await _offer(admin, "o_other_host", "ext:other::3")                                    # not a named host


async def _markets(admin):
    return {r["offer_id"]: r["market"] for r in await admin.fetch("SELECT offer_id, market FROM catalog_offers")}


async def test_the_plan_takes_only_named_hosts_sgd_rows_suppressed_included(catalog):
    database, admin, tool, _ = catalog
    await _seed_rows(admin)
    before = await _markets(admin)
    p = await tool.plan(database, ["https://www.JSMBEAUTY.sg/"], "us", "sg")
    assert [o["offer_id"] for o in p["offers"]] == ["o_bare_domain", "o_suppressed", "o_url_only"]
    assert p["currency"] == "SGD" and p["collisions"] == []
    assert {(r["market"], r["currency"]): r["n"] for r in p["left_alone"]} == {("US", "USD"): 1}
    s = tool.summary(p)
    assert s["by_host"] == {"jsmbeauty.sg live": 2, "jsmbeauty.sg suppressed": 1} and s["content_keys"] == 2
    assert await _markets(admin) == before  # the plan writes nothing


async def test_apply_restamps_records_and_revert_restores(catalog):
    database, admin, tool, refreshed = catalog
    await _seed_rows(admin)
    out = await tool.apply(database, await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))
    assert out["moved"] == 3
    after = await _markets(admin)
    assert {k: after[k] for k in ("o_url_only", "o_bare_domain", "o_suppressed")} == dict.fromkeys(
        ("o_url_only", "o_bare_domain", "o_suppressed"), "SG")
    assert (after["o_usd_sibling"], after["o_other_host"]) == ("US", "US")
    actions = [r["action"] for r in await admin.fetch(
        "SELECT action FROM identity_resolution_events WHERE run_id = $1 ORDER BY id", out["run_id"])]
    assert actions == [tool.MANIFEST_ACTION, tool.APPLIED_ACTION]
    assert refreshed == [(out["run_id"], ["ck_cushion", "ck_tint"])]

    back = await tool.revert(database, out["run_id"])
    assert back["reverted"] == 3 and set((await _markets(admin)).values()) == {"US"}
    with pytest.raises(SystemExit, match="already reverted"):
        await tool.revert(database, out["run_id"])


async def test_a_would_be_duplicate_shelf_refuses_the_whole_run(catalog):
    database, admin, tool, _ = catalog
    await _seed_rows(admin)
    await _offer(admin, "o_live_sg", "ext:jsm-cushion::1", market="SG", sku="sku:o_url_only")
    before = await _markets(admin)
    p = await tool.plan(database, ["jsmbeauty.sg"], "US", "SG")
    assert [(c["offer_id"], c["existing_offer_id"]) for c in p["collisions"]] == [("o_url_only", "o_live_sg")]
    with pytest.raises(SystemExit, match="duplicate a live SG offer"):
        await tool.apply(database, p)
    assert await _markets(admin) == before
    # a suppressed SG offer is no shelf at all, and another channel is another shelf
    await admin.execute("UPDATE catalog_offers SET suppressed_at = NOW() WHERE offer_id = 'o_live_sg'")
    assert (await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))["collisions"] == []
    await admin.execute("UPDATE catalog_offers SET suppressed_at = NULL, channel = 'agent' WHERE offer_id = 'o_live_sg'")
    assert (await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))["collisions"] == []


async def test_drift_since_the_plan_writes_nothing(catalog):
    database, admin, tool, _ = catalog
    await _seed_rows(admin)
    p = await tool.plan(database, ["jsmbeauty.sg"], "US", "SG")
    await admin.execute("UPDATE catalog_offers SET currency = 'USD' WHERE offer_id = 'o_bare_domain'")
    before = await _markets(admin)
    with pytest.raises(RuntimeError, match="drift: 1 of 3"):
        await tool.apply(database, p)
    assert await _markets(admin) == before
    run_ids = {r["run_id"] for r in await admin.fetch("SELECT run_id FROM identity_resolution_events")}
    [run_id] = run_ids
    with pytest.raises(SystemExit, match="never committed"):  # the stored manifest alone is not a write
        await tool.revert(database, run_id)


async def test_a_market_with_no_pricing_currency_is_refused(catalog):
    database, admin, tool, _ = catalog
    with pytest.raises(ValueError):
        await tool.plan(database, ["jsmbeauty.sg"], "US", "ZZ")
    with pytest.raises(ValueError, match="both SG"):
        await tool.plan(database, ["jsmbeauty.sg"], "SG", "sg")

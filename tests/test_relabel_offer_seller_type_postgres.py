"""scripts/relabel_offer_seller_type.py on real PostgreSQL: plan, apply, drift, revert.

Rows are shaped like prod's (census 2026-10-06): public enrichment-lane listings whose brand-store offers were
written before the lane labelled seller type (offer_type NULL). Each test pins a behaviour a mutant of the tool
would change. Isolated schema built from the production model; never run against production.
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)

LANE = "catalog_enrichment_agent_v1"


@pytest.fixture
async def catalog(monkeypatch):
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.database as dbmod
    import db.catalog  # noqa: F401 -- registers the catalog tables on the shared metadata
    from scripts import relabel_offer_seller_type as tool
    from scripts import restamp_offer_market as restamp

    refreshed = []

    async def refresh_after(db, m, *, source):
        refreshed.append((source, sorted({o["content_key"] for o in m["offers"] if o.get("content_key")})))
        return {"refreshed": 0}
    monkeypatch.setattr(restamp, "refresh_after", refresh_after)

    url = os.environ["DATABASE_URL"]
    schema = "relabel_offer_seller_type_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    database = None
    try:
        for name in ("catalog_products", "catalog_offers"):
            await admin.execute(str(sa.schema.CreateTable(dbmod.metadata.tables[name]).compile(
                dialect=postgresql.dialect())))
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


async def _product(admin, key, *, brand, domain, url, ck, lane=LANE):
    await admin.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url,
                                         content_key, brand, source_domain, source_system)
           VALUES ($1, 'merch_obs_x', 'external_seed', $1, 'Item', $2, $3, $4, $5, $6)""",
        key, url, ck, brand, domain, lane)


async def _offer(admin, offer_id, key, *, merchant, domain, offer_type=None, first_party=False, lane=LANE):
    await admin.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, channel, market, currency,
                                       source_domain, catalog_track, source_system, offer_type, is_first_party)
           VALUES ($1, $2, $3, $4, 'default', 'US', 'USD', $5, 'external_referral', $6, $7, $8)""",
        offer_id, f"sku:{offer_id}", key, merchant, domain, lane, offer_type, first_party)


async def _seed_rows(admin):
    # The lane's rule: MAC Cosmetics owns maccosmetics.com (exact label) -> brand_direct.
    await _product(admin, "ext:mac::1", brand="MAC Cosmetics", domain="maccosmetics.com",
                   url="https://www.maccosmetics.com/product/1", ck="ck_mac")
    # The rule refuses to guess an affixed host: unknown, never labelled here.
    await _product(admin, "ext:tarte::2", brand="Tarte", domain="tartecosmetics.com",
                   url="https://tartecosmetics.com/p/2", ck="ck_tarte")
    # A known retailer host: the rule says retailer, never brand_direct.
    await _product(admin, "ext:bb::3", brand="MAC Cosmetics", domain="beautybay.com",
                   url="https://www.beautybay.com/p/3", ck="ck_bb")
    # Another lane's listing: out of scope entirely.
    await _product(admin, "ext:other::4", brand="MAC Cosmetics", domain="maccosmetics.com",
                   url="https://www.maccosmetics.com/product/4", ck="ck_other", lane="shopify_sync_v1")

    await _offer(admin, "o_mac", "ext:mac::1", merchant="agent_seed::mac-cosmetics", domain="maccosmetics.com")
    await _offer(admin, "o_mac_www", "ext:mac::1", merchant="agent_seed::mac-cosmetics", domain="www.maccosmetics.com")
    await _offer(admin, "o_mac_typed", "ext:mac::1", merchant="agent_seed::mac-cosmetics", domain="maccosmetics.com",
                 offer_type="retailer")
    await _offer(admin, "o_mac_retailer_role", "ext:mac::1", merchant="agent_seed::retailer::maccosmetics.com",
                 domain="maccosmetics.com")
    await _offer(admin, "o_mac_foreign_host", "ext:mac::1", merchant="agent_seed::mac-cosmetics", domain="sephora.com")
    await _offer(admin, "o_tarte", "ext:tarte::2", merchant="agent_seed::tarte", domain="tartecosmetics.com")
    await _offer(admin, "o_bb", "ext:bb::3", merchant="agent_seed::retailer::beautybay.com", domain="beautybay.com")
    await _offer(admin, "o_other_lane", "ext:other::4", merchant="agent_seed::mac-cosmetics",
                 domain="maccosmetics.com", lane="shopify_sync_v1")


PLANNED = ["o_mac", "o_mac_www"]


async def _labels(admin):
    return {r["offer_id"]: (r["offer_type"], r["is_first_party"]) for r in await admin.fetch(
        "SELECT offer_id, offer_type, is_first_party FROM catalog_offers")}


async def _updated(admin):
    return {r["offer_id"]: r["updated_at"] for r in await admin.fetch("SELECT offer_id, updated_at FROM catalog_offers")}


async def test_the_plan_labels_only_the_lanes_own_brand_direct_verdicts(catalog):
    database, admin, tool, _ = catalog
    await _seed_rows(admin)
    p = await tool.plan(database)
    assert sorted(o["offer_id"] for o in p["offers"]) == PLANNED
    s = tool.summary(p)
    assert s["skipped_retailer_role"] == 1
    assert [n["offer_id"] for n in p["near_misses"]] == ["o_mac_foreign_host"]
    assert s["listings"] == 1 and s["content_keys"] == 1
    # Already-labelled offers and other lanes are not even candidates.
    assert s["unlabelled_lane_offers"] == 6


async def test_apply_labels_the_plan_and_nothing_else(catalog):
    database, admin, tool, refreshed = catalog
    await _seed_rows(admin)
    before_labels, before_updated = await _labels(admin), await _updated(admin)
    out = await tool.apply(database, await tool.plan(database))
    after = await _labels(admin)
    assert out["labelled"] == 2
    for oid in PLANNED:
        assert after[oid] == ("brand_direct", True)
    for oid in set(after) - set(PLANNED):
        assert after[oid] == before_labels[oid]
    # A label is not a new observation: updated_at never moves.
    assert await _updated(admin) == before_updated
    assert refreshed == [(out["run_id"], ["ck_mac"])]
    actions = [r["action"] for r in await admin.fetch(
        "SELECT action FROM identity_resolution_events WHERE run_id = $1 ORDER BY id", out["run_id"])]
    assert actions == [tool.MANIFEST_ACTION, tool.APPLIED_ACTION]


async def test_drift_since_the_plan_aborts_the_whole_write(catalog):
    database, admin, tool, _ = catalog
    await _seed_rows(admin)
    p = await tool.plan(database)
    await admin.execute("UPDATE catalog_offers SET offer_type = 'retailer' WHERE offer_id = 'o_mac_www'")
    with pytest.raises(RuntimeError, match="drift"):
        await tool.apply(database, p)
    labels = await _labels(admin)
    assert labels["o_mac"] == (None, False)  # nothing written, not even the undrifted offer
    assert labels["o_mac_www"] == ("retailer", False)


async def test_revert_restores_exactly_and_only_once(catalog):
    database, admin, tool, refreshed = catalog
    await _seed_rows(admin)
    before = await _labels(admin)
    run_id = (await tool.apply(database, await tool.plan(database)))["run_id"]
    # Someone relabels one of them afterwards: revert leaves that one alone and says so.
    await admin.execute("UPDATE catalog_offers SET offer_type = 'retailer', is_first_party = FALSE "
                        "WHERE offer_id = 'o_mac_www'")
    out = await tool.revert(database, run_id)
    assert (out["unlabelled"], out["already"]) == (1, 1)
    after = await _labels(admin)
    assert after["o_mac"] == before["o_mac"] == (None, False)
    assert after["o_mac_www"] == ("retailer", False)
    assert refreshed[-1] == (f"{run_id}:revert", ["ck_mac"])
    with pytest.raises(SystemExit, match="already reverted"):
        await tool.revert(database, run_id)


async def test_revert_of_an_unknown_or_uncommitted_run_is_refused(catalog):
    database, admin, tool, _ = catalog
    with pytest.raises(SystemExit, match="no stored manifest"):
        await tool.revert(database, "relabel_000000000000")
    await admin.execute("INSERT INTO identity_resolution_events (action, run_id, detail) VALUES ($1, $2, $3)",
                        tool.MANIFEST_ACTION, "relabel_111111111111",
                        '{"run_id": "relabel_111111111111", "offer_type": "brand_direct", "offers": []}')
    with pytest.raises(SystemExit, match="never committed"):
        await tool.revert(database, "relabel_111111111111")


async def test_nothing_to_label_writes_nothing(catalog):
    database, admin, tool, _ = catalog
    with pytest.raises(SystemExit, match="nothing to label"):
        await tool.apply(database, await tool.plan(database))
    assert await admin.fetchval("SELECT count(*) FROM identity_resolution_events") == 0

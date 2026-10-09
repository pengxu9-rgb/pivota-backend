"""scripts/repair_brand_merchant_domain.py on real PostgreSQL: plan, apply, drift, revert.

Rows are shaped like prod's (census 2026-10-07): brand seller rows `agent_seed::<brand>` that one cocomo.sg run
(2026-09-06) overwrote with `source_ref = cocomo.sg`, beside the live offers of the brand's own store and that
run's suppressed cocomo offers. Each test pins a behaviour a mutant of the tool would change. Isolated schema
built from the production model; never run against production.
"""
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)

LANE = "catalog_enrichment_agent_v1"
POLLUTED = ["cocomo.sg"]


@pytest.fixture
async def catalog():
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.database as dbmod
    import db.catalog  # noqa: F401 -- registers the catalog tables on the shared metadata
    from scripts import repair_brand_merchant_domain as tool

    url = os.environ["DATABASE_URL"]
    schema = "repair_brand_merchant_domain_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    database = None
    try:
        for name in ("catalog_merchants", "catalog_products", "catalog_offers"):
            await admin.execute(str(sa.schema.CreateTable(dbmod.metadata.tables[name]).compile(
                dialect=postgresql.dialect())))
        await admin.execute("CREATE TABLE identity_resolution_events (id BIGSERIAL PRIMARY KEY, proposal_id TEXT, "
                            "action TEXT NOT NULL, run_id TEXT NOT NULL, detail JSONB, "
                            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        database = databases.Database(url, server_settings={"search_path": schema})
        await database.connect()
        yield database, admin, tool
    finally:
        if database is not None:
            await database.disconnect()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _merchant(admin, mid, ref, *, lane=LANE, extra=None, metadata=...):
    md = {"domain": ref, "agent_version": LANE, **(extra or {})} if metadata is ... else metadata
    await admin.execute(
        """INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status, source_system,
                                          source_ref, metadata_json, updated_at)
           VALUES ($1, $1, 'external_seed', 'active', $2, $3, CAST($4 AS jsonb), NOW() - INTERVAL '30 days')""",
        mid, lane, ref, None if md is None else json.dumps(md))


async def _listing(admin, key, domain, url):
    await admin.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url,
                                         content_key, brand, source_domain, source_system)
           VALUES ($1, 'merch_obs_x', 'external_seed', $1, 'Item', $2, $1, 'Brand', $3, $4)""",
        key, url, domain, LANE)


async def _offer(admin, oid, key, merchant, url, *, suppressed=False, lane=LANE, canonical=None, offer_type=None):
    # The lane writes the destination as source_ref and keeps the canonical URL in the payload.
    payload = {"destination_url": url, "canonical_url": canonical or url}
    await admin.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, channel, market, currency,
                                       source_domain, source_ref, catalog_track, source_system, suppressed_at,
                                       offer_payload, offer_type)
           VALUES ($1, $2, $3, $4, 'default', 'US', 'USD', NULL, $5, 'external_referral', $6,
                   CASE WHEN $7 THEN NOW() ELSE NULL END, CAST($8 AS jsonb), $9)""",
        oid, f"sku:{oid}", key, merchant, url, lane, suppressed, json.dumps(payload), offer_type)


async def _seed(admin):
    # Prod shape: the brand's own store sells live; the cocomo run's offer under the same seller is suppressed.
    await _merchant(admin, "agent_seed::axis-y", "cocomo.sg", extra={"brand_relationship": "kept"})
    await _listing(admin, "ext:axis-y::1", "axis-y.com", "https://axis-y.com/products/a")
    await _listing(admin, "ext:axis-y::2", "axis-y.com", "https://axis-y.com/products/b")
    await _listing(admin, "ext:retailer:c1", "cocomo.sg", "https://cocomo.sg/products/axis-y-a")
    await _offer(admin, "o_ax1", "ext:axis-y::1", "agent_seed::axis-y", "https://axis-y.com/products/a")
    await _offer(admin, "o_ax2", "ext:axis-y::2", "agent_seed::axis-y", "https://axis-y.com/products/b")
    await _offer(admin, "o_ax_coc", "ext:retailer:c1", "agent_seed::axis-y", "https://cocomo.sg/products/axis-y-a",
                 suppressed=True)
    # A store whose listing keeps www.: the lane writes the bare host.
    await _merchant(admin, "agent_seed::tower-28", "cocomo.sg")
    await _listing(admin, "ext:t28::1", "www.tower28beauty.com", "https://www.tower28beauty.com/products/x")
    await _offer(admin, "o_t28", "ext:t28::1", "agent_seed::tower-28", "https://www.tower28beauty.com/products/x")
    # Only the cocomo run ever sold under it: no evidence of the store (65 such rows in prod).
    await _merchant(admin, "agent_seed::aalok", "cocomo.sg")
    await _listing(admin, "ext:retailer:c2", "cocomo.sg", "https://cocomo.sg/products/aalok")
    await _offer(admin, "o_aalok", "ext:retailer:c2", "agent_seed::aalok", "https://cocomo.sg/products/aalok",
                 suppressed=True)
    # Two live hosts: ambiguous.
    await _merchant(admin, "agent_seed::medicube", "cocomo.sg")
    await _listing(admin, "ext:mc::1", "medicube.us", "https://medicube.us/p/1")
    await _listing(admin, "ext:mc::2", "themedicube.us.com", "https://themedicube.us.com/p/2")
    await _offer(admin, "o_mc1", "ext:mc::1", "agent_seed::medicube", "https://medicube.us/p/1")
    await _offer(admin, "o_mc2", "ext:mc::2", "agent_seed::medicube", "https://themedicube.us.com/p/2")
    # The retailer's own goods under a brand-named seller: it IS cocomo, nothing to restore.
    await _merchant(admin, "agent_seed::cocomo", "cocomo.sg")
    await _listing(admin, "ext:retailer:c3", "cocomo.sg", "https://cocomo.sg/products/gift-card")
    await _offer(admin, "o_gift", "ext:retailer:c3", "agent_seed::cocomo", "https://cocomo.sg/products/gift-card")
    # Offer and listing disagree on the host.
    await _merchant(admin, "agent_seed::mixed", "cocomo.sg")
    await _listing(admin, "ext:mixed::1", "cocomo.sg", "https://cocomo.sg/products/m")
    await _offer(admin, "o_mixed", "ext:mixed::1", "agent_seed::mixed", "https://mixed.com/products/m")
    # Out of scope: a retailer-role seller, another host's row, another lane's row.
    await _merchant(admin, "agent_seed::retailer::cocomo.sg", "cocomo.sg")
    await _merchant(admin, "agent_seed::round-lab", "sokoglam.com")
    await _listing(admin, "ext:rl::1", "roundlab.com", "https://roundlab.com/p/1")
    await _offer(admin, "o_rl", "ext:rl::1", "agent_seed::round-lab", "https://roundlab.com/p/1")
    await _merchant(admin, "agent_seed::other-lane", "cocomo.sg", lane="shopify_sync_v1")
    await _listing(admin, "ext:ol::1", "otherlane.com", "https://otherlane.com/p/1")
    await _offer(admin, "o_ol", "ext:ol::1", "agent_seed::other-lane", "https://otherlane.com/p/1")


async def _rows(admin):
    out = {}
    for r in await admin.fetch("SELECT merchant_id, source_ref, metadata_json, updated_at FROM catalog_merchants"):
        md = r["metadata_json"]
        out[r["merchant_id"]] = (r["source_ref"], json.loads(md) if isinstance(md, str) else md, r["updated_at"])
    return out


async def test_the_plan_restores_only_rows_whose_live_store_names_one_host(catalog):
    database, admin, tool = catalog
    await _seed(admin)
    p = await tool.plan(database, POLLUTED)
    assert {r["merchant_id"]: r["to_host"] for r in p["repairs"]} == {
        "agent_seed::axis-y": "axis-y.com", "agent_seed::tower-28": "tower28beauty.com"}
    assert p["skipped"] == {"no_live_offer": 1, "offers_from_several_hosts": 1, "only_the_polluted_host": 1,
                            "listing_host_disagrees": 1}
    # Suppressed offers are not evidence; the retailer-role row and other lanes are not candidates.
    assert p["rows_on_polluted_host"] == 6
    assert {r["merchant_id"]: r["live_offers"] for r in p["repairs"]}["agent_seed::axis-y"] == 2


async def test_apply_writes_the_plan_keeps_other_metadata_and_touches_nothing_else(catalog):
    database, admin, tool = catalog
    await _seed(admin)
    before = await _rows(admin)
    out = await tool.apply(database, await tool.plan(database, POLLUTED))
    after = await _rows(admin)
    assert out["repaired"] == 2
    assert after["agent_seed::axis-y"][0] == "axis-y.com"
    assert after["agent_seed::axis-y"][1] == {"domain": "axis-y.com", "agent_version": LANE,
                                              "brand_relationship": "kept"}
    assert after["agent_seed::tower-28"][:2] == ("tower28beauty.com", {"domain": "tower28beauty.com",
                                                                        "agent_version": LANE})
    for mid in set(after) - {"agent_seed::axis-y", "agent_seed::tower-28"}:
        assert after[mid] == before[mid]
    for mid in after:
        assert after[mid][2] == before[mid][2]  # updated_at untouched
    events = [r["action"] for r in await admin.fetch("SELECT action FROM identity_resolution_events ORDER BY id")]
    assert events == [tool.MANIFEST_ACTION, tool.APPLIED_ACTION]


async def test_a_row_changed_since_the_plan_aborts_every_write(catalog):
    database, admin, tool = catalog
    await _seed(admin)
    p = await tool.plan(database, POLLUTED)
    await admin.execute("UPDATE catalog_merchants SET source_ref = 'tower28beauty.com' "
                        "WHERE merchant_id = 'agent_seed::tower-28'")
    before = await _rows(admin)
    with pytest.raises(RuntimeError, match="drift"):
        await tool.apply(database, p)
    assert await _rows(admin) == before
    events = [r["action"] for r in await admin.fetch("SELECT action FROM identity_resolution_events")]
    assert events == [tool.MANIFEST_ACTION]  # the manifest stands; no applied record


async def test_revert_restores_exactly_once_and_leaves_a_row_changed_since(catalog):
    database, admin, tool = catalog
    await _seed(admin)
    before = await _rows(admin)
    run_id = (await tool.apply(database, await tool.plan(database, POLLUTED)))["run_id"]
    await admin.execute("UPDATE catalog_merchants SET source_ref = 'tower28.com', "
                        "metadata_json = metadata_json || '{\"domain\": \"tower28.com\"}' "
                        "WHERE merchant_id = 'agent_seed::tower-28'")
    out = await tool.revert(database, run_id)
    after = await _rows(admin)
    assert out["reverted"] == 1 and out["changed_since"] == ["agent_seed::tower-28"]
    assert after["agent_seed::axis-y"] == before["agent_seed::axis-y"]
    assert after["agent_seed::tower-28"][0] == "tower28.com"
    with pytest.raises(SystemExit, match="already reverted"):
        await tool.revert(database, run_id)


async def test_without_an_incident_host_nothing_is_planned(catalog):
    database, admin, tool = catalog
    await _seed(admin)
    with pytest.raises(SystemExit, match="polluted-host"):
        await tool.plan(database, [])
    p = await tool.plan(database, ["sokoglam.com"])
    # round-lab's row says sokoglam.com and its live store is roundlab.com: a different incident, named explicitly.
    assert [(r["merchant_id"], r["to_host"]) for r in p["repairs"]] == [("agent_seed::round-lab", "roundlab.com")]


async def _one_store(admin, mid, *, listing_domain="axis-y.com", url="https://axis-y.com/products/a", **offer):
    await _listing(admin, f"ext:{mid}::1", listing_domain, url)
    await _offer(admin, f"o_{mid}", f"ext:{mid}::1", mid, url, **offer)


async def test_rows_without_a_trustworthy_single_store_host_are_left(catalog):
    database, admin, tool = catalog
    # Medicube's prod path: the listing carries no domain.
    await _merchant(admin, "agent_seed::nohost", "cocomo.sg")
    await _one_store(admin, "agent_seed::nohost", listing_domain=None)
    # The lane keys the seller row on the CANONICAL url; a destination on another host is not the store.
    await _merchant(admin, "agent_seed::split", "cocomo.sg")
    await _one_store(admin, "agent_seed::split", listing_domain="shop.split.com",
                     url="https://shop.split.com/p", canonical="https://split.com/p")
    # A retailer is never a brand's own store: by the offer's label, or by the known-retailer list.
    await _merchant(admin, "agent_seed::labelled", "cocomo.sg")
    await _one_store(admin, "agent_seed::labelled", offer_type="retailer")
    await _merchant(admin, "agent_seed::listed", "cocomo.sg")
    await _one_store(admin, "agent_seed::listed", listing_domain="sephora.com", url="https://www.sephora.com/p/1")
    # Metadata that is the JSON value null is not an object to merge into.
    await _merchant(admin, "agent_seed::nullmeta", "cocomo.sg", metadata="null")
    await _one_store(admin, "agent_seed::nullmeta")
    # LIKE's underscore would have matched this id; the brand prefix is literal.
    await _merchant(admin, "agentXseed::lookalike", "cocomo.sg")
    await _one_store(admin, "agentXseed::lookalike")
    p = await tool.plan(database, POLLUTED)
    assert p["repairs"] == []
    assert p["skipped"] == {"host_missing": 1, "offers_from_several_hosts": 1, "target_is_a_retailer": 2,
                            "metadata_not_an_object": 1}
    assert p["rows_on_polluted_host"] == 5


async def test_the_dry_run_shows_whether_the_restored_row_can_serve(catalog):
    database, admin, tool = catalog
    await _merchant(admin, "agent_seed::axis-y", "cocomo.sg")
    await _one_store(admin, "agent_seed::axis-y")
    await admin.execute("UPDATE catalog_merchants SET indexable = FALSE")
    s = tool.summary(await tool.plan(database, POLLUTED))
    assert [list(r) for r in s["repairs"]] == [["agent_seed::axis-y", "cocomo.sg", "axis-y.com", 1, "active", False]]


@pytest.mark.parametrize("touch", [
    # A re-run of the brand's own store writes the SAME host and bumps updated_at.
    "UPDATE catalog_merchants SET updated_at = NOW() WHERE merchant_id = 'agent_seed::axis-y'",
    "UPDATE catalog_merchants SET source_ref = 'axis-y.co' WHERE merchant_id = 'agent_seed::axis-y'",
    "UPDATE catalog_merchants SET metadata_json = metadata_json || '{\"domain\": \"axis-y.co\"}' "
    "WHERE merchant_id = 'agent_seed::axis-y'",
])
async def test_revert_never_overwrites_a_row_any_writer_touched_since(catalog, touch):
    database, admin, tool = catalog
    await _merchant(admin, "agent_seed::axis-y", "cocomo.sg")
    await _one_store(admin, "agent_seed::axis-y")
    run_id = (await tool.apply(database, await tool.plan(database, POLLUTED)))["run_id"]
    await admin.execute(touch)
    touched = await _rows(admin)
    out = await tool.revert(database, run_id)
    assert out["reverted"] == 0 and out["changed_since"] == ["agent_seed::axis-y"]
    assert await _rows(admin) == touched


async def test_revert_restores_null_metadata_exactly(catalog):
    database, admin, tool = catalog
    await _merchant(admin, "agent_seed::bare", "cocomo.sg", metadata=None)
    await _one_store(admin, "agent_seed::bare")
    before = await _rows(admin)
    run_id = (await tool.apply(database, await tool.plan(database, POLLUTED)))["run_id"]
    assert (await _rows(admin))["agent_seed::bare"][:2] == ("axis-y.com", {"domain": "axis-y.com"})
    assert (await tool.revert(database, run_id))["reverted"] == 1
    assert await _rows(admin) == before

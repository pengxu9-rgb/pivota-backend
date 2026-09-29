"""scripts/restamp_offer_market.py on real PostgreSQL: plan, apply, drift, collisions, revert, rebuild.

The rows are shaped like prod's (census 2026-09-29): jsmbeauty.sg offers stamped market 'US' in SGD, some
reaching their host only through the product URL, one suppressed. Each test pins a behaviour a mutant of the
tool would change (review of #2450: the first version's drift guard survived `OR TRUE`).
Isolated schema built from the production model; never run against production.
"""
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)

SUPPRESSED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
async def catalog(monkeypatch):
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.database as dbmod
    import db.catalog  # noqa: F401 -- registers the catalog tables on the shared metadata
    from scripts import restamp_offer_market as tool

    real_refresh, refreshed = tool.refresh_after, []

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
        # Not on the shared metadata: migration 179's shape, and the two seed columns the plan reads.
        await admin.execute("CREATE TABLE identity_resolution_events (id BIGSERIAL PRIMARY KEY, proposal_id TEXT, "
                            "action TEXT NOT NULL, run_id TEXT NOT NULL, detail JSONB, "
                            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        await admin.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, tool TEXT)")
        database = databases.Database(url, server_settings={"search_path": schema})
        await database.connect()
        yield database, admin, tool, refreshed, real_refresh
    finally:
        if database is not None:
            await database.disconnect()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _product(admin, key, url, ck, source_ref=None):
    await admin.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url,
                                         content_key, source_ref)
           VALUES ($1, 'm_sg', 'external_seed', 'shopify:1', 'Cushion', $2, $3, $4)""", key, url, ck, source_ref)


async def _offer(admin, offer_id, key, *, market="US", currency="SGD", source_domain=None, suppressed=False,
                 sku=None, channel="default", track="internal_merchant_free"):
    await admin.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, channel, market, currency,
                                       source_domain, suppressed_at, catalog_track, source_system)
           VALUES ($1, $2, $3, 'm_sg', $4, $5, $6, $7, $8, $9, 'catalog_enrichment_agent_v1')""",
        offer_id, sku or f"sku:{offer_id}", key, channel, market, currency, source_domain,
        SUPPRESSED_AT if suppressed else None, track)


async def _seed_rows(admin):
    await _product(admin, "ext:jsm-cushion::1", "HTTPS://WWW.JSMBEAUTY.SG/products/cushion", "ck_cushion")
    await _product(admin, "ext:jsm-tint::2", "https://jsmbeauty.sg/products/tint", "ck_tint")
    await _product(admin, "ext:other::3", "https://cocomo.sg/products/x", "ck_other")
    await _offer(admin, "o_a_url_only", "ext:jsm-cushion::1")                              # host from the product URL
    await _offer(admin, "o_b_www_domain", "ext:jsm-tint::2", source_domain="www.jsmbeauty.sg")
    await _offer(admin, "o_c_padded", "ext:jsm-tint::2", market=" us")                    # another spelling of US
    await _offer(admin, "o_d_suppressed", "ext:jsm-tint::2", suppressed=True)
    await _offer(admin, "o_e_no_product", "ext:gone::9", source_domain="jsmbeauty.sg:443")  # no product row
    await _offer(admin, "o_f_usd_sibling", "ext:jsm-tint::2", currency="USD")             # Shopify-Markets sibling
    await _offer(admin, "o_g_other_host", "ext:other::3")                                  # not a named host
    await _offer(admin, "o_h_domain_wins", "ext:jsm-tint::2", source_domain="cocomo.sg")  # its own domain decides
    await _offer(admin, "o_i_subdomain", "ext:other::3", source_domain="shop.jsmbeauty.sg")


PLANNED = ["o_a_url_only", "o_b_www_domain", "o_c_padded", "o_d_suppressed", "o_e_no_product"]


async def _markets(admin):
    return {r["offer_id"]: r["market"] for r in await admin.fetch("SELECT offer_id, market FROM catalog_offers")}


async def _updated(admin):
    return {r["offer_id"]: r["updated_at"] for r in await admin.fetch("SELECT offer_id, updated_at FROM catalog_offers")}


# ------------------------------------------------------------------ plan

async def test_the_plan_takes_exactly_the_named_hosts_sgd_rows(catalog):
    database, admin, tool, _, _ = catalog
    await _seed_rows(admin)
    before = await _markets(admin)
    p = await tool.plan(database, ["https://www.JSMBEAUTY.sg/"], "us", "sg")
    assert [o["offer_id"] for o in p["offers"]] == PLANNED
    assert p["currency"] == "SGD" and p["collisions"] == [] and p["rewriting_lane_offers"] == []
    assert {(r["market"], r["currency"]): r["n"] for r in p["left_alone"]} == {("US", "USD"): 1}
    assert [r["offer_id"] for r in p["near_misses"]] == ["o_i_subdomain"]
    s = tool.summary(p)
    assert s["by_host"] == {"jsmbeauty.sg live": 4, "jsmbeauty.sg suppressed": 1}
    assert s["content_keys"] == 2 and s["offers_without_content_key"] == 1
    assert s["prior_market_spellings"] == {"US": 4, " us": 1}
    assert await _markets(admin) == before  # the plan writes nothing


NOT_JSM = ("shop.jsmbeauty.sg", "jsmbeauty.sg.evil.com", "", None, "mailto:x@jsmbeauty.sg", "ftp://jsmbeauty.sg/x",
           "https://evil.com\\@jsmbeauty.sg/x", "https://jsm beauty.sg")


@pytest.mark.parametrize("text", [
    "https://www.jsmbeauty.sg/products/x", "HTTPS://WWW.JSMBEAUTY.SG", "//jsmbeauty.sg/x", " jsmbeauty.sg ",
    "jsmbeauty.sg:443", "https://user@jsmbeauty.sg/x", "https://user:pw@jsmbeauty.sg:8443/x", "https://jsmbeauty.sg./x",
    "jsmbeauty.sg..", "www.www.jsmbeauty.sg", "jsmbeauty.sg?x=1", "jsmbeauty.sg#top", *NOT_JSM,
])
async def test_offer_host_and_host_sql_agree(catalog, text):
    """Only http(s) or scheme-less text names a web host; a browser reads `evil.com\\@jsmbeauty.sg` as evil.com,
    and mailto: is no host at all (review 2 of #2450)."""
    database, _, tool, _, _ = catalog
    got = await database.fetch_val(f"SELECT {tool._host_sql('CAST(:t AS text)')}", {"t": text})
    assert got == tool.offer_host(text)
    assert (got == "jsmbeauty.sg") is (text not in NOT_JSM)


async def test_a_blank_source_domain_falls_back_to_the_product_url(catalog):
    database, admin, tool, _, _ = catalog
    await _product(admin, "ext:jsm::1", "https://jsmbeauty.sg/products/a", "ck_a")
    await _offer(admin, "o_blank", "ext:jsm::1", source_domain="   ")
    assert [o["offer_id"] for o in (await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))["offers"]] == ["o_blank"]


@pytest.mark.parametrize("rewriter", ["catalog_sync_track", "external_brand_crawl_seed"])
async def test_a_lane_that_rewrites_market_is_refused(catalog, rewriter):
    """catalog_sync rewrites every column on re-sync; the crawl onboarder sets market from the seed: a restamp
    of their offers would be silently undone."""
    database, admin, tool, _, _ = catalog
    if rewriter == "catalog_sync_track":
        await _product(admin, "ext:jsm::1", "https://jsmbeauty.sg/products/a", "ck_a")
        await _offer(admin, "o_synced", "ext:jsm::1", track="internal_merchant")
    else:
        # the seed exactly as the producer writes it: id `external_brand_crawl::<epid>`, tool '*' (#2169)
        from scripts import onboard_external_brand_from_crawl as onboard
        assert tool.REWRITING_SEED_ID_PREFIXES == (f"{onboard.TOOL}::",)
        seed_id = onboard._seed_id("9000001")
        await admin.execute("INSERT INTO external_product_seeds (id, tool) VALUES ($1, $2)",
                            seed_id, onboard.SEED_TOOL_SCOPE)
        await _product(admin, "ext:jsm::1", "https://jsmbeauty.sg/products/a", "ck_a", source_ref=seed_id)
        await _offer(admin, "o_crawled", "ext:jsm::1")
    before = await _markets(admin)
    p = await tool.plan(database, ["jsmbeauty.sg"], "US", "SG")
    assert len(p["rewriting_lane_offers"]) == 1
    with pytest.raises(SystemExit, match="rewrites market on re-sync"):
        await tool.apply(database, p)
    assert await _markets(admin) == before


async def test_a_market_with_no_pricing_currency_is_refused(catalog):
    database, _, tool, _, _ = catalog
    with pytest.raises(ValueError):
        await tool.plan(database, ["jsmbeauty.sg"], "US", "ZZ")
    with pytest.raises(ValueError, match="both SG"):
        await tool.plan(database, ["jsmbeauty.sg"], "SG", "sg")


# ------------------------------------------------------------------ apply / revert

async def test_apply_restamps_records_and_revert_restores_each_spelling(catalog, capsys):
    database, admin, tool, refreshed, _ = catalog
    await _seed_rows(admin)
    before, stamps = await _markets(admin), await _updated(admin)
    out = await tool.apply(database, await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))
    assert out["moved"] == 5 and f"COMMITTED {out['run_id']}: 5 offer(s)" in capsys.readouterr().out
    after = await _markets(admin)
    assert {k: after[k] for k in PLANNED} == dict.fromkeys(PLANNED, "SG")
    assert {k: v for k, v in after.items() if k not in PLANNED} == {k: v for k, v in before.items() if k not in PLANNED}
    assert await _updated(admin) == stamps  # not a new observation: ORDER BY updated_at readers must not reorder
    actions = [r["action"] for r in await admin.fetch(
        "SELECT action FROM identity_resolution_events WHERE run_id = $1 ORDER BY id", out["run_id"])]
    assert actions == [tool.MANIFEST_ACTION, tool.APPLIED_ACTION]
    assert refreshed == [(out["run_id"], ["ck_cushion", "ck_tint"])]

    back = await tool.revert(database, out["run_id"])
    assert back["reverted"] == 5 and await _markets(admin) == before  # " us" comes back as " us"
    with pytest.raises(SystemExit, match="already reverted"):
        await tool.revert(database, out["run_id"])


async def test_a_market_drift_since_the_plan_writes_nothing(catalog):
    database, admin, tool, _, _ = catalog
    await _seed_rows(admin)
    p = await tool.plan(database, ["jsmbeauty.sg"], "US", "SG")
    await admin.execute("UPDATE catalog_offers SET market = 'CA' WHERE offer_id = 'o_e_no_product'")
    before = await _markets(admin)
    with pytest.raises(RuntimeError, match="drift: 1 of 4"):
        await tool.apply(database, p)
    assert await _markets(admin) == before  # the first spelling group, already moved, rolled back too
    [run_id] = {r["run_id"] for r in await admin.fetch("SELECT run_id FROM identity_resolution_events")}
    with pytest.raises(SystemExit, match="never committed"):  # the stored manifest alone is not a write
        await tool.revert(database, run_id)


async def test_a_currency_drift_since_the_plan_writes_nothing(catalog):
    database, admin, tool, _, _ = catalog
    await _seed_rows(admin)
    p = await tool.plan(database, ["jsmbeauty.sg"], "US", "SG")
    await admin.execute("UPDATE catalog_offers SET currency = 'USD' WHERE offer_id = 'o_c_padded'")
    before = await _markets(admin)
    with pytest.raises(RuntimeError, match="drift: 1 of 1"):
        await tool.apply(database, p)
    assert await _markets(admin) == before


async def test_a_drifted_revert_writes_nothing(catalog):
    database, admin, tool, _, _ = catalog
    await _seed_rows(admin)
    out = await tool.apply(database, await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))
    await admin.execute("UPDATE catalog_offers SET market = 'CA' WHERE offer_id = 'o_c_padded'")
    before = await _markets(admin)
    with pytest.raises(RuntimeError, match="drift"):
        await tool.revert(database, out["run_id"])
    assert await _markets(admin) == before


async def test_revert_of_an_older_run_is_refused_while_a_later_run_holds_its_offers(catalog):
    database, admin, tool, _, _ = catalog
    await _seed_rows(admin)
    original = await _markets(admin)
    a = await tool.apply(database, await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))
    await admin.execute("UPDATE catalog_offers SET market = 'US' WHERE offer_id = 'o_a_url_only'")
    b = await tool.apply(database, await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))
    assert b["moved"] == 1
    before = await _markets(admin)
    with pytest.raises(SystemExit, match=f"later run {b['run_id']}"):
        await tool.revert(database, a["run_id"])
    assert await _markets(admin) == before
    await tool.revert(database, b["run_id"])  # the later run reverts cleanly ...
    done = await tool.revert(database, a["run_id"])  # ... and then A: the offer B returned is already at US
    assert (done["reverted"], done["already"]) == (4, 1)
    assert await _markets(admin) == original  # every offer, " us" included, is back where it started


# ------------------------------------------------------------------ duplicate shelves

async def test_a_would_be_duplicate_shelf_refuses_the_whole_run(catalog):
    database, admin, tool, _, _ = catalog
    await _seed_rows(admin)
    await _offer(admin, "o_live_sg", "ext:jsm-cushion::1", market="sg ", currency="USD", sku="sku:o_a_url_only")
    # a suppressed planned offer still collides: its suppression can lift
    await _offer(admin, "o_live_sg2", "ext:jsm-tint::2", market="SG", currency="USD", sku="sku:o_d_suppressed")
    before = await _markets(admin)
    p = await tool.plan(database, ["jsmbeauty.sg"], "US", "SG")
    assert [(c["offer_id"], c["existing_offer_id"]) for c in p["collisions"]] == [
        ("o_a_url_only", "o_live_sg"), ("o_d_suppressed", "o_live_sg2")]
    with pytest.raises(SystemExit, match="duplicate a live SG shelf"):
        await tool.apply(database, p)
    assert await _markets(admin) == before
    assert await admin.fetchval("SELECT count(*) FROM identity_resolution_events") == 0  # refused before a manifest
    # a suppressed SG offer is no shelf at all, and another channel is another shelf
    await admin.execute("UPDATE catalog_offers SET suppressed_at = NOW() WHERE offer_id = 'o_live_sg'")
    await admin.execute("UPDATE catalog_offers SET channel = 'agent' WHERE offer_id = 'o_live_sg2'")
    assert (await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))["collisions"] == []


async def test_a_suppressed_and_a_live_planned_offer_on_one_shelf_do_not_collide(catalog):
    database, admin, tool, _, _ = catalog
    await _product(admin, "ext:jsm::1", "https://jsmbeauty.sg/products/a", "ck_a")
    await _offer(admin, "o_1", "ext:jsm::1", sku="sku:shared")
    await _offer(admin, "o_2", "ext:jsm::1", sku="sku:shared", suppressed=True)
    assert (await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))["collisions"] == []


async def test_two_planned_live_offers_on_one_shelf_collide(catalog):
    database, admin, tool, _, _ = catalog
    await _product(admin, "ext:jsm::1", "https://jsmbeauty.sg/products/a", "ck_a")
    await _offer(admin, "o_1", "ext:jsm::1", sku="sku:shared")
    await _offer(admin, "o_2", "ext:jsm::1", sku="sku:shared", market="us")
    p = await tool.plan(database, ["jsmbeauty.sg"], "US", "SG")
    assert sorted((c["offer_id"], c["existing_offer_id"]) for c in p["collisions"]) == [("o_1", "o_2"), ("o_2", "o_1")]


async def test_a_shelf_taken_after_the_plan_refuses_inside_the_write(catalog):
    database, admin, tool, _, _ = catalog
    await _seed_rows(admin)
    p = await tool.plan(database, ["jsmbeauty.sg"], "US", "SG")
    await _offer(admin, "o_new_sg", "ext:jsm-tint::2", market="SG", currency="USD", sku="sku:o_c_padded")
    before = await _markets(admin)
    with pytest.raises(SystemExit, match="duplicate a live SG shelf"):
        await tool.apply(database, p)
    assert await _markets(admin) == before


async def test_a_revert_onto_a_us_shelf_taken_since_is_refused(catalog):
    database, admin, tool, _, _ = catalog
    await _seed_rows(admin)
    out = await tool.apply(database, await tool.plan(database, ["jsmbeauty.sg"], "US", "SG"))
    await _offer(admin, "o_new_us", "ext:jsm-tint::2", market="US", currency="USD", sku="sku:o_c_padded")
    before = await _markets(admin)
    with pytest.raises(SystemExit, match="duplicate a live US shelf"):
        await tool.revert(database, out["run_id"])
    assert await _markets(admin) == before


# ------------------------------------------------------------------ the rebuild

async def test_the_rebuild_covers_every_touched_key_and_lists_a_failure(catalog, monkeypatch):
    database, _, _, _, real_refresh = catalog
    import services.agent_pdp_view_assembler as assembler
    import services.index_pipeline_state_service as ips
    views, recomputed = [], []

    async def refresh_view(ck, *, refresh_source, db):
        if ck == "ck_bad":
            raise RuntimeError("builder rejected")
        views.append((ck, refresh_source))
        return True

    async def recompute(ck, *, reason, db):
        recomputed.append((ck, reason))
    monkeypatch.setattr(assembler, "refresh_agent_pdp_view_for_content_key", refresh_view)
    monkeypatch.setattr(ips, "recompute_serving_eligibility", recompute)
    m = {"offers": [{"content_key": "ck_b"}, {"content_key": "ck_a"}, {"content_key": "ck_a"},
                    {"content_key": None}, {"content_key": "ck_bad"}]}
    out = await real_refresh(database, m, source="restamp_x")
    assert views == [("ck_a", "restamp_x"), ("ck_b", "restamp_x")] and recomputed == views
    assert (out["refreshed"], out["recomputed"], out["failed_keys_total"]) == (2, 2, 1)
    assert out["failed_keys"][0]["content_key"] == "ck_bad"


def test_a_failed_rebuild_exits_non_zero():
    """The nightly view reconciler keys on offer updated_at, which a restamp does not bump: only the exit code
    tells the operator to re-run `refresh --run-id`."""
    from scripts import restamp_offer_market as tool
    assert tool.exit_code({"refresh": {"failed_keys_total": 1}}) == 2
    assert tool.exit_code({"refresh": {"failed_keys_total": 0}}) == 0 and tool.exit_code({}) == 0

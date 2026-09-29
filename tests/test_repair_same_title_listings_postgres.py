"""scripts/repair_same_title_listings.py on real PostgreSQL: plan, apply, drift, shelf clash, revert, and agreement
with the ingest's keeper afterwards.

The rows are shaped like prod's (census 2026-09-29): a COCODOR "Black Cherry" row whose live offer is the refill
page while the row names the candle page and the other offers were suppressed `duplicate_offer`; a Merit row naming
its `-ukeu` copy while the US page sells; a row whose elected keeper has nothing to sell. Isolated schema built from
the production model; never run against production.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)

EARLIER = datetime(2026, 9, 1, tzinfo=timezone.utc)
SEED_TOUCHED = datetime(2026, 9, 28, 5, 15, 7, tzinfo=timezone.utc)
K_BC = "ext:cocodor-black-cherry::5f884f1a"
K_MIN = "ext:merit-the-minimalist::11111111"
K_SKIP = "ext:saie-glossybounce-duo::22222222"
K_ONE = "ext:merit-flush-balm::33333333"
K_DEAD = "ext:missha-m-perfect-cover-bb-cream::44444444"
K_GONE = "ext:banila-co-hydration-boost-kit::55555555"
K_NOIMG = "ext:veganifect-superfood-cleansing-oil::66666666"
K_ONE_HOST = "ext:foodology-acv-jelly::77777777"


def ck(key):
    return "ck_" + key.rsplit("::", 1)[-1]


def cocodor(handle):
    return f"https://cocodor.com/products/{handle}"


@pytest.fixture
async def catalog(monkeypatch):
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.database as dbmod
    import db.catalog  # noqa: F401 -- registers the catalog tables on the shared metadata
    from scripts import repair_same_title_listings as tool

    refreshed = []

    async def refresh_after(db, m, *, source):
        refreshed.append((source, sorted({g["content_key"] for g in m["groups"] if g.get("content_key")})))
        return {"refreshed": 0}
    monkeypatch.setattr(tool, "refresh_after", refresh_after)

    url = os.environ["DATABASE_URL"]
    schema = "repair_same_title_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    database = None
    try:
        for name in ("catalog_products", "catalog_offers", "catalog_skus"):
            await admin.execute(str(sa.schema.CreateTable(dbmod.metadata.tables[name]).compile(
                dialect=postgresql.dialect())))
        # Migration 246 (not on the model): when an offer's price was last read.
        await admin.execute("ALTER TABLE catalog_offers ADD COLUMN price_checked_at TIMESTAMPTZ NULL")
        # Not on the shared metadata: migration 179's shape, and the seed columns the tool reads and writes.
        await admin.execute("CREATE TABLE identity_resolution_events (id BIGSERIAL PRIMARY KEY, proposal_id TEXT, "
                            "action TEXT NOT NULL, run_id TEXT NOT NULL, detail JSONB, "
                            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        await admin.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, attached_product_key TEXT, "
                            "canonical_url TEXT, destination_url TEXT, image_url TEXT, status TEXT, "
                            "availability TEXT, destination_verdict TEXT, market TEXT, updated_at TIMESTAMPTZ)")
        await _rows(admin)
        database = databases.Database(url, server_settings={"search_path": schema})
        await database.connect()
        yield database, admin, tool, refreshed
    finally:
        if database is not None:
            await database.disconnect()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _product(admin, key, url, image, *, suppressed=False):
    await admin.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url,
                                         image_url, content_key, source_system, suppressed_at)
           VALUES ($1, 'm', 'external_seed', $1, 'T', $2, $3, $4, 'catalog_enrichment_agent_v1', $5)""",
        key, url, image, ck(key), EARLIER if suppressed else None)
    await admin.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, source_variant_id,
                                     title) VALUES ($1 || '::canonical', $1, 'm', 'external_seed', $1, $1, 'T')""", key)


async def _offer(admin, offer_id, key, url, *, reason=None, availability="in_stock", image=None,
                 updated=EARLIER.replace(tzinfo=None), metadata=None, market="US", price_checked=None):
    await admin.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, channel, market, availability,
                                       suppressed_at, suppression_reason, offer_payload, source_ref, updated_at,
                                       suppression_metadata, price_checked_at)
           VALUES ($1, $2 || '::canonical', $2, 'm', 'default', $10, $3, $4, $5, CAST($6 AS jsonb), $7, $8,
                   CAST($9 AS jsonb), $11)""",
        offer_id, key, availability, EARLIER if reason else None, reason,
        json.dumps({"destination_url": url, "canonical_url": url, "image_url": image or url + ".jpg"}), url, updated,
        None if metadata is None else json.dumps(metadata), market, price_checked)


async def _seed(admin, seed_id, key, url, *, status="active", availability="in_stock", verdict="live"):
    await admin.execute(
        """INSERT INTO external_product_seeds (id, attached_product_key, canonical_url, destination_url, image_url,
                                               status, availability, destination_verdict, market, updated_at)
           VALUES ($1, $2, $3, $3, $3 || '.jpg', $4, $5, $6, 'US', $7)""",
        seed_id, key, url, status, availability, verdict, SEED_TOUCHED)


async def _rows(admin):
    # COCODOR: the row names the candle; the refill offer is the live one (reconciler's pick).
    candle = cocodor("soy-candle-medium-black-cherry")
    await _product(admin, K_BC, candle, candle + ".jpg")
    await _offer(admin, "o_refill", K_BC, cocodor("diffuser-refill-6-7oz-black-cherry"),
                 metadata={"reconcile_batch_id": "rb_1"})  # a live offer the reconciler once kept
    # Two keeper offers on one shelf. The reconciler touched o_candle_stale last (updated_at), but o_candle's price
    # was read more recently: the price, not the touch, decides which comes back.
    await _offer(admin, "o_candle_stale", K_BC, cocodor("soy-candle-medium-black-cherry"), reason="duplicate_offer",
                 updated=datetime(2026, 9, 20))
    await _offer(admin, "o_candle", K_BC, cocodor("soy-candle-medium-black-cherry"), reason="duplicate_offer",
                 price_checked=datetime(2026, 9, 25, tzinfo=timezone.utc))
    # the refill page's SG offer: another market's election, never written by a US repair
    await _offer(admin, "o_refill_sg", K_BC, cocodor("diffuser-refill-6-7oz-black-cherry"), market="SG")
    # ...and the kept page's SG duplicate: an SG repair's to restore, not a US one's
    await _offer(admin, "o_candle_sg", K_BC, cocodor("soy-candle-medium-black-cherry"), market="SG",
                 reason="duplicate_offer")
    # another shelf, suppressed for a reason the repair does not own: never restored
    await _offer(admin, "o_candle_dead", K_BC, cocodor("soy-candle-medium-black-cherry"), market="SG",
                 reason="external_seed_destination_dead")
    await _offer(admin, "o_diffuser", K_BC, cocodor("signature-diffuser-6-7oz-black-cherry"), reason="duplicate_offer")
    for i, h in enumerate(("diffuser-refill-6-7oz-black-cherry", "soy-candle-medium-black-cherry",
                           "signature-diffuser-6-7oz-black-cherry")):
        await _seed(admin, f"s_bc{i}", K_BC, cocodor(h))
    # Merit: the row names its -ukeu copy (404 in the US); the US page sells.
    base = "https://meritbeauty.com/products/the-minimalist"
    copy = base + "-ukeu"
    await _product(admin, K_MIN, copy, copy + ".jpg")
    await _offer(admin, "o_min", K_MIN, base)
    await _offer(admin, "o_min_ukeu", K_MIN, copy, reason="duplicate_offer")
    await _seed(admin, "s_min", K_MIN, base)
    await _seed(admin, "s_min_ukeu", K_MIN, copy)
    # The elected keeper (the base listing) has only a seed: never take the row's last live offer away.
    await _product(admin, K_SKIP, "https://saiehello.com/products/glossybounce-duo-1", None)
    await _offer(admin, "o_skip", K_SKIP, "https://saiehello.com/products/glossybounce-duo-1")
    await _seed(admin, "s_skip", K_SKIP, "https://saiehello.com/products/glossybounce-duo")
    # misshaus.com: the base page is gone (seed dead_404) though its stale seed still says in stock; the -new
    # relist sells. The base ranks first by handle -- a dead page must lose to the live relist.
    missha = "https://misshaus.com/products/m-perfect-cover-bb-cream"
    await _product(admin, K_DEAD, missha, missha + ".jpg")
    await _offer(admin, "o_dead_base", K_DEAD, missha, reason="duplicate_offer")
    await _offer(admin, "o_dead_new", K_DEAD, missha + "-new")
    await _seed(admin, "s_dead_base", K_DEAD, missha, verdict="dead_404")
    await _seed(admin, "s_dead_new", K_DEAD, missha + "-new")
    # banilausa.com: both listings are gone (404); the row sells the -1 copy. Nothing to align it to.
    kit = "https://banilausa.com/products/hydration-boost-kit"
    await _product(admin, K_GONE, kit + "-1", kit + "-1.jpg")
    await _offer(admin, "o_gone_1", K_GONE, kit + "-1")
    await _offer(admin, "o_gone", K_GONE, kit, reason="duplicate_offer")
    await _seed(admin, "s_gone_1", K_GONE, kit + "-1", verdict="dead_404")
    await _seed(admin, "s_gone", K_GONE, kit, verdict="dead_404")
    # veganifectus.com: the keeper (base) is known only by a live offer without an image, and no seed.
    oil = "https://veganifectus.com/products/amazon-1-superfood-cleansing-oil"
    await _product(admin, K_NOIMG, oil + "-2", oil + "-2.jpg")
    await _offer(admin, "o_noimg_2", K_NOIMG, oil + "-2")
    await _offer(admin, "o_noimg", K_NOIMG, oil)
    await admin.execute("UPDATE catalog_offers SET offer_payload = offer_payload - 'image_url' "
                        "WHERE offer_id = 'o_noimg'")
    # Two listings, but already aligned: one live, the row names it, the other's seed already inactive.
    jelly = "https://foodology-global.com/products/amazon-acv-jelly-ver2"
    await _product(admin, K_ONE_HOST, jelly, jelly + ".jpg")
    await _offer(admin, "o_jelly", K_ONE_HOST, jelly)
    await _offer(admin, "o_jelly_1", K_ONE_HOST, jelly + "-kor", reason="duplicate_offer")
    await _seed(admin, "s_jelly_1", K_ONE_HOST, jelly + "-kor", status="inactive")
    # One listing per host (two hosts): nothing to repair.
    await _product(admin, K_ONE, "https://meritbeauty.com/products/flush-balm", None)
    await _offer(admin, "o_one", K_ONE, "https://meritbeauty.com/products/flush-balm")
    await _offer(admin, "o_one_ca", K_ONE, "https://merit.ca/products/flush-balm-ca")


async def _state(admin):
    offers = {r["offer_id"]: (r["suppressed_at"], r["suppression_reason"], json.loads(r["m"]) if r["m"] else None)
              for r in await admin.fetch("SELECT offer_id, suppressed_at, suppression_reason, "
                                         "suppression_metadata AS m FROM catalog_offers")}
    seeds = {r["id"]: (r["status"], r["updated_at"])
             for r in await admin.fetch("SELECT id, status, updated_at FROM external_product_seeds")}
    rows = {r["product_key"]: (r["canonical_url"], r["image_url"])
            for r in await admin.fetch("SELECT product_key, canonical_url, image_url FROM catalog_products")}
    return offers, seeds, rows


async def test_the_plan_elects_the_ingests_keeper_and_writes_nothing(catalog):
    db, admin, tool, _ = catalog
    before = await _state(admin)
    p = await tool.plan(db, hosts=[], keys=[])
    got = {g["product_key"]: g for g in p["groups"]}
    assert set(got) == {K_BC, K_MIN, K_SKIP, K_DEAD, K_GONE, K_NOIMG, K_ONE_HOST}
    assert got[K_GONE]["skipped"] == "no_available_listing"
    assert got[K_NOIMG]["skipped"] == "keeper_page_unknown"
    assert got[K_ONE_HOST]["skipped"] == "nothing_to_change"
    bc = got[K_BC]
    assert bc["keeper"] == "soy-candle-medium-black-cherry"  # the row's current listing wins the tie
    assert bc["suppress"] == ["o_refill"] and [r["offer_id"] for r in bc["restore"]] == ["o_candle"]
    assert sorted(bc["deactivate"]) == ["s_bc0", "s_bc2"] and bc["repoint"] is None
    dead = got[K_DEAD]
    assert dead["keeper"] == "m-perfect-cover-bb-cream-new" and dead["restore"] == []
    assert dead["repoint"]["canonical_url"] == "https://misshaus.com/products/m-perfect-cover-bb-cream-new"
    mn = got[K_MIN]
    assert mn["keeper"] == "the-minimalist"  # a base listing beats the -ukeu copy the row names
    assert mn["suppress"] == [] and mn["restore"] == [] and mn["deactivate"] == ["s_min_ukeu"]
    assert mn["repoint"]["canonical_url"] == "https://meritbeauty.com/products/the-minimalist"
    assert got[K_SKIP]["skipped"] == "keeper_has_no_sellable_offer"
    s = tool.summary(p)
    assert (s["offers_to_suppress"], s["offers_to_restore"], s["seeds_to_deactivate"], s["rows_to_repoint"]) == \
        (1, 1, 4, 2)
    assert bc["other_market_left"] == 3 and s["restored_price_checked"] == {"2026-09-25": 1}
    assert await _state(admin) == before


async def test_apply_writes_every_change_after_storing_its_manifest(catalog):
    db, admin, tool, refreshed = catalog
    out = await tool.apply(db, await tool.plan(db, hosts=[], keys=[]))
    offers, seeds, rows = await _state(admin)
    seeds = {k: v[0] for k, v in seeds.items()}
    assert offers["o_refill"][0] is not None and offers["o_refill"][1] == "same_key_other_listing"
    assert offers["o_refill"][2] == {"reconcile_batch_id": "rb_1", "same_listing_run": out["run_id"],
                                     "same_listing_keeper": "soy-candle-medium-black-cherry"}
    assert offers["o_candle"][:2] == (None, None)
    assert offers["o_candle_stale"][1] == "duplicate_offer" and offers["o_refill_sg"][0] is None
    assert offers["o_candle_sg"][1] == "duplicate_offer"
    assert offers["o_candle_dead"][1] == "external_seed_destination_dead"
    assert offers["o_diffuser"][1] == "duplicate_offer"  # left out, already suppressed: untouched
    assert offers["o_skip"][0] is None and seeds["s_skip"] == "active"  # skipped whole
    assert (seeds["s_bc0"], seeds["s_bc1"], seeds["s_bc2"], seeds["s_min_ukeu"]) == \
        ("inactive", "active", "inactive", "inactive")
    assert rows[K_MIN] == ("https://meritbeauty.com/products/the-minimalist",
                           "https://meritbeauty.com/products/the-minimalist.jpg")
    events = [r["action"] for r in await admin.fetch("SELECT action FROM identity_resolution_events ORDER BY id")]
    assert events == [tool.MANIFEST_ACTION, tool.APPLIED_ACTION]
    assert refreshed == [(out["run_id"], sorted([ck(K_BC), ck(K_MIN), ck(K_DEAD)]))]
    # every live offer of a repaired row is now its keeper's
    live = {r["product_key"]: r["n"] for r in await admin.fetch(
        "SELECT product_key, count(*) n FROM catalog_offers WHERE suppressed_at IS NULL AND market = 'US' GROUP BY 1")}
    assert live[K_BC] == 1 and live[K_MIN] == 1


async def test_drift_since_the_plan_aborts_the_whole_write(catalog):
    db, admin, tool, _ = catalog
    p = await tool.plan(db, hosts=[], keys=[])
    before = await _state(admin)
    await admin.execute("UPDATE external_product_seeds SET status = 'paused' WHERE id = 's_min_ukeu'")
    with pytest.raises(RuntimeError, match="drift"):
        await tool.apply(db, p)
    offers, seeds, rows = await _state(admin)
    assert offers == before[0] and rows == before[2]
    assert seeds == {**before[1], "s_min_ukeu": ("paused", SEED_TOUCHED)}
    events = [r["action"] for r in await admin.fetch("SELECT action FROM identity_resolution_events")]
    assert events == [tool.MANIFEST_ACTION]  # stored, never applied


async def test_an_offer_suppressed_since_the_plan_is_drift(catalog):
    db, admin, tool, _ = catalog
    p = await tool.plan(db, hosts=[], keys=[])
    await admin.execute("UPDATE catalog_offers SET suppressed_at = NOW(), suppression_reason = 'other' "
                        "WHERE offer_id = 'o_refill'")
    before = await _state(admin)
    with pytest.raises(RuntimeError, match="drift"):
        await tool.apply(db, p)
    assert await _state(admin) == before


async def test_a_restore_that_would_duplicate_a_live_shelf_is_refused(catalog):
    db, admin, tool, _ = catalog
    p = await tool.plan(db, hosts=[], keys=[])
    before = await _state(admin)
    await _offer(admin, "o_new", K_BC, cocodor("soy-candle-medium-black-cherry"))  # written since the plan
    with pytest.raises(SystemExit, match="duplicate live offer o_new"):
        await tool.apply(db, p)
    offers, seeds, rows = await _state(admin)
    assert {k: v for k, v in offers.items() if k != "o_new"} == before[0] and seeds == before[1]


async def test_revert_puts_back_every_prior_value_once(catalog):
    db, admin, tool, _ = catalog
    before = await _state(admin)
    out = await tool.apply(db, await tool.plan(db, hosts=[], keys=[]))
    await tool.revert(db, out["run_id"])
    offers, seeds, rows = await _state(admin)
    assert seeds == before[1] and rows == before[2]
    assert {k: v[:2] for k, v in offers.items()} == {k: v[:2] for k, v in before[0].items()}
    assert offers == before[0]  # metadata too: {"reconcile_batch_id": "rb_1"} on o_refill, NULL elsewhere
    with pytest.raises(SystemExit, match="already reverted"):
        await tool.revert(db, out["run_id"])


async def test_revert_refuses_drift_and_a_later_run(catalog):
    db, admin, tool, _ = catalog
    out = await tool.apply(db, await tool.plan(db, hosts=[], keys=[]))
    await admin.execute("UPDATE catalog_products SET image_url = 'x' WHERE product_key = $1", K_MIN)
    with pytest.raises(RuntimeError, match="drift"):
        await tool.revert(db, out["run_id"])
    await admin.execute("UPDATE catalog_products SET image_url = $2 WHERE product_key = $1", K_MIN,
                        "https://meritbeauty.com/products/the-minimalist.jpg")
    # a later run on the same row (the refill listing comes back live, and a new repair suppresses it again)
    await admin.execute("UPDATE catalog_offers SET suppressed_at = NULL, suppression_reason = NULL "
                        "WHERE offer_id = 'o_refill'")
    later = await tool.apply(db, await tool.plan(db, hosts=[], keys=[K_BC]))
    with pytest.raises(SystemExit, match=f"later run {later['run_id']}"):
        await tool.revert(db, out["run_id"])


async def test_after_the_repair_the_ingest_keeps_the_same_listing(catalog):
    """The ingest reads the row's listing (apply.current_listings) and elects with the same function: a re-crawl of
    all three pages must write the page the repair kept, not bring a second one back."""
    from services import curated_brand_feed as feed
    from services.catalog_enrichment_agent.apply import current_listings
    from services.catalog_enrichment_agent.ingestion import derive_product_key, ingest_validated_jsonl

    db, admin, tool, _ = catalog
    await tool.apply(db, await tool.plan(db, hosts=[], keys=[]))

    def record(handle, ptype, price, vid):
        return feed.shopify_product_to_record(
            {"id": 8_000_000_000 + vid % 1000, "vendor": "COCODOR", "title": "Black Cherry", "handle": handle,
             "product_type": ptype, "body_html": "<p>x</p>", "images": [{"src": f"https://cdn.example/{handle}.jpg"}],
             "variants": [{"id": vid, "price": price, "available": True, "sku": handle}]},
            domain="cocodor.com", category_path="beauty", brand_override="COCODOR", currency="USD",
            source_role="brand_official", emit_native_variants=True)
    records = [record("diffuser-refill-6-7oz-black-cherry", "refill", "6.99", 49_000_000_000_001),
               record("signature-diffuser-6-7oz-black-cherry", "diffuser", "11.19", 49_000_000_000_002),
               record("soy-candle-medium-black-cherry", "candle", "9.99", 49_000_000_000_003)]
    key = derive_product_key("COCODOR", "Black Cherry")
    await admin.execute("UPDATE catalog_products SET product_key = $1 WHERE product_key = $2", key, K_BC)
    plan = ingest_validated_jsonl(records, current_listings=await current_listings([key], db=db))
    kept = {c["kept"]["handle"] for c in plan["listing_collisions"]}
    assert kept == {"soy-candle-medium-black-cherry"}


async def test_revert_refuses_a_restored_offer_suppressed_since(catalog):
    db, admin, tool, _ = catalog
    out = await tool.apply(db, await tool.plan(db, hosts=[], keys=[]))
    await admin.execute("UPDATE catalog_offers SET suppressed_at = NOW(), suppression_reason = 'duplicate_offer' "
                        "WHERE offer_id = 'o_candle'")  # the reconciler ran again
    with pytest.raises(RuntimeError, match="drift"):
        await tool.revert(db, out["run_id"])


async def test_a_host_scoped_plan_touches_only_that_host(catalog):
    db, _, tool, _ = catalog
    p = await tool.plan(db, hosts=["www.meritbeauty.com"], keys=[])
    assert [g["product_key"] for g in p["groups"]] == [K_MIN]


async def test_a_keeper_offer_relabelled_since_the_plan_is_not_restored(catalog):
    db, admin, tool, _ = catalog
    p = await tool.plan(db, hosts=[], keys=[])
    await admin.execute("UPDATE catalog_offers SET suppression_reason = 'product_suppressed' "
                        "WHERE offer_id = 'o_candle'")
    before = await _state(admin)
    with pytest.raises(RuntimeError, match="drift"):
        await tool.apply(db, p)
    assert await _state(admin) == before


async def test_after_the_repair_an_out_of_stock_or_missing_page_never_moves_the_row(catalog):
    """Review of #2463 at 533184dcd (P1), on the repaired catalog: one crawl with the kept candle page sold out
    keeps it; a crawl without it holds the row (listing_moves) instead of moving it."""
    from services import curated_brand_feed as feed
    from services.catalog_enrichment_agent.apply import plan_with_current_listings
    from services.catalog_enrichment_agent.ingestion import derive_product_key

    db, admin, tool, _ = catalog
    await tool.apply(db, await tool.plan(db, hosts=[], keys=[]))
    key = derive_product_key("COCODOR", "Black Cherry")
    await admin.execute("UPDATE catalog_products SET product_key = $1 WHERE product_key = $2", key, K_BC)

    def record(handle, ptype, price, vid, available=True):
        return feed.shopify_product_to_record(
            {"id": 8_000_000_000 + vid % 1000, "vendor": "COCODOR", "title": "Black Cherry", "handle": handle,
             "product_type": ptype, "body_html": "<p>x</p>", "images": [{"src": f"https://cdn.example/{handle}.jpg"}],
             "variants": [{"id": vid, "price": price, "available": available, "sku": handle}]},
            domain="cocodor.com", category_path="beauty", brand_override="COCODOR", currency="USD",
            source_role="brand_official", emit_native_variants=True)
    refill = record("diffuser-refill-6-7oz-black-cherry", "refill", "6.99", 49_000_000_000_001)
    candle_oos = record("soy-candle-medium-black-cherry", "candle", "9.99", 49_000_000_000_003, available=False)
    plan = await plan_with_current_listings([refill, candle_oos], db=db)
    assert [c["kept"]["handle"] for c in plan["listing_collisions"]] == ["soy-candle-medium-black-cherry"]
    held = await plan_with_current_listings([refill], db=db)
    assert held["pdps"] == [] and [m["current"] for m in held["listing_moves"]] == ["soy-candle-medium-black-cherry"]

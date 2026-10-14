"""scripts/backfill_seed_variant_id_provenance.py on real PostgreSQL: plan, apply, drift, revert.

THE ROWS COME FROM THE REAL PRODUCER. Each listing is a drain record (`curated_brand_feed.shopify_product_to_record`)
run through the lane's real ingest (`ingestion.ingest_validated_jsonl`); its seed and variant SKU rows are inserted
as written. A pre-2026-09-08 seed is that same seed with the `variant_id_provenance` key removed from its variants
(what the writer emitted before #2123), so the backfill's target is exactly what the writer stamps today:
`backfill(strip(writer)) == writer`. Isolated schema; never run against production.
"""
import json
import os
import uuid
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)

V50, V15 = "37169662034071", "41430446375063"  # real-shaped Shopify variant ids
GTIN = "0812345678905"  # what the refresh's JSON-LD extractor reads as a variant's id (sku/gtin13)


def _ingest(title: str, variants: List[Dict[str, Any]], *, handle: str,
            source_handles: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    from services import curated_brand_feed as feed
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    record = feed.shopify_product_to_record(
        {"id": 6644068500001, "vendor": "Example", "title": title, "handle": handle, "product_type": "Fragrance",
         "body_html": "<p>A fragrance.</p>", "images": [{"src": "https://cdn.brand.example/a.jpg"}],
         "options": [{"name": "Size"}], "variants": variants},
        domain="brand.example", category_path="beauty", brand_override="Example", currency="USD",
        source_role="brand_official", retailer_name=None, emit_native_variants=True)
    for v in record["pdp"]["variants"]:  # a fold record names each shade's own listing handle
        if source_handles and v["variant_id"] in source_handles:
            v["source_handle"] = source_handles[v["variant_id"]]
    out = ingest_validated_jsonl([record])
    (seed,) = out["seeds"]
    return {"seed": seed, "skus": [k for k in out["skus"] if "::v:" in k["sku_key"]]}


def _two_sizes(title: str = "Example Eau De Toilette", **kw) -> Dict[str, Any]:
    return _ingest(title, [
        {"id": int(V50), "price": "80.00", "available": True, "sku": "EX50", "option1": "1.7 fl. oz. / 50 mL"},
        {"id": int(V15), "price": "32.00", "available": True, "sku": "EX15", "option1": "0.5 fl. oz. / 15 mL"},
    ], handle="example-eau-de-toilette", **kw)


def _strip(variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{k: val for k, val in v.items() if k != "variant_id_provenance"} for v in variants]


@pytest.fixture
async def db():
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.database as dbmod
    import db.catalog  # noqa: F401 -- registers the catalog tables on the shared metadata
    from scripts import backfill_seed_variant_id_provenance as tool

    url = os.environ["DATABASE_URL"]
    schema = "seed_variant_provenance_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    database = None
    try:
        await admin.execute(str(sa.schema.CreateTable(dbmod.metadata.tables["catalog_skus"]).compile(
            dialect=postgresql.dialect())))
        # The columns this tool reads, typed as routes/employee_products._ensure_external_seeds_table makes them.
        await admin.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, external_product_id TEXT, "
                            "tool TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', attached_product_key TEXT, "
                            "seed_data JSONB NOT NULL DEFAULT '{}'::jsonb, "
                            "updated_at TIMESTAMPTZ NOT NULL DEFAULT '2026-09-06T00:00:00Z')")
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


async def _store(admin, listing: Dict[str, Any], *, variants: Optional[List[Dict[str, Any]]] = None,
                 seed_id: Optional[str] = None, tool: Optional[str] = None, status: str = "active") -> str:
    seed = listing["seed"]
    seed_data = json.loads(seed["seed_data"])
    if variants is not None:
        seed_data["variants"] = variants
    sid = seed_id or seed["id"]
    await admin.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, tool, status, attached_product_key, seed_data) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
        sid, seed["external_product_id"], tool or seed["tool"], status, seed["attached_product_key"],
        json.dumps(seed_data))
    for k in listing["skus"]:
        await admin.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, "
            "source_variant_id, title, sku_payload, readiness_tier) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9) "
            "ON CONFLICT (sku_key) DO NOTHING",
            k["sku_key"], k["product_key"], k["merchant_id"], k["platform"], k["source_product_id"],
            k["source_variant_id"], k["title"], k["sku_payload"], k["readiness_tier"])
    return sid


async def _variants(admin, sid: str) -> List[Dict[str, Any]]:
    return json.loads(await admin.fetchval("SELECT seed_data->'variants' FROM external_product_seeds WHERE id=$1", sid))


async def _seed_data(admin, sid: str) -> Dict[str, Any]:
    return json.loads(await admin.fetchval("SELECT seed_data FROM external_product_seeds WHERE id=$1", sid))


async def _events(admin) -> List[str]:
    return [r["action"] for r in await admin.fetch("SELECT action FROM identity_resolution_events ORDER BY id")]


# ── the premise ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_writer_stamps_the_seed_and_records_its_inputs_on_the_sku_rows():
    listing = _two_sizes()
    variants = json.loads(listing["seed"]["seed_data"])["variants"]
    assert [(v["variant_id"], v["variant_id_provenance"]) for v in variants] == [
        (V50, "merchant_issued"), (V15, "merchant_issued")]
    payloads = [json.loads(k["sku_payload"]) for k in listing["skus"]]
    assert sorted(p["variant_id"] for p in payloads) == sorted([V50, V15])
    assert all("source_handle" in p and p["agent_version"] == "catalog_enrichment_agent_v1" for p in payloads)


# ── the round trip: backfill(strip(writer)) == writer ───────────────────────────────────────────────────────

async def test_apply_restores_exactly_what_the_writer_stamps(db):
    database, admin, tool = db
    listing = _two_sizes()
    written = json.loads(listing["seed"]["seed_data"])
    sid = await _store(admin, listing, variants=_strip(written["variants"]))

    p = await tool.dry_run(database)
    s = tool.summary(p)
    assert s["seeds_to_stamp"] == 1 and s["variants_to_stamp_by_verdict"] == {"merchant_issued": 2}
    assert await _events(admin) == []  # the dry run wrote nothing

    out = await tool.apply(database, p)
    assert out["variants"] == 2
    assert await _seed_data(admin, sid) == written
    assert await _events(admin) == [tool.MANIFEST_ACTION, tool.APPLIED_ACTION]
    updated = await admin.fetchval("SELECT updated_at FROM external_product_seeds WHERE id=$1", sid)
    assert updated.isoformat().startswith("2026-09-06"), "a stamp is not a new observation: updated_at stays"

    assert tool.summary(await tool.dry_run(database))["candidate_seeds"] == 0  # idempotent


async def test_the_writers_handle_is_an_input_and_is_read_from_its_sku_row(db):
    # A fold record names each shade's listing handle. An id that restates its handle is product_derived to the
    # writer; without the handle the same digits read merchant_issued. The backfill must answer as the writer did.
    database, admin, tool = db
    listing = _two_sizes(source_handles={V15: V15[:-3]})
    written = json.loads(listing["seed"]["seed_data"])
    assert [v["variant_id_provenance"] for v in written["variants"]] == ["merchant_issued", "product_derived"]
    sid = await _store(admin, listing, variants=_strip(written["variants"]))

    await tool.apply(database, await tool.plan(database))
    assert await _seed_data(admin, sid) == written


async def test_the_dry_run_cannot_write(db, monkeypatch):
    import asyncpg

    database, admin, tool = db

    async def writing_plan(db_, **_):
        await db_.execute("INSERT INTO identity_resolution_events (action, run_id) VALUES ('x', 'x')")
    monkeypatch.setattr(tool, "plan", writing_plan)
    with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
        await tool.dry_run(database)
    assert await _events(admin) == []


# ── what it refuses ────────────────────────────────────────────────────────────────────────────────────────

async def test_a_page_reads_digits_are_never_stamped(db):
    # The refresh rewrote this seed from the page's JSON-LD: the id is a GTIN, not one the writer classified.
    database, admin, tool = db
    listing = _two_sizes()
    read = [{"variant_id": GTIN, "title": "Example Eau De Toilette", "price_amount": 32.0, "price_currency": "USD"}]
    sid = await _store(admin, listing, variants=read)

    p = await tool.plan(database)
    s = tool.summary(p)
    assert s["variants_to_stamp"] == 0 and s["variants_refused"] == {"unattested": 1}
    assert s["variants_refused_numeric_8plus"] == {"unattested": 1}
    with pytest.raises(SystemExit):
        await tool.apply(database, p)
    assert await _variants(admin, sid) == read


async def test_an_unrecorded_handle_is_refused(db):
    database, admin, tool = db
    listing = _two_sizes()
    for k in listing["skus"]:
        payload = json.loads(k["sku_payload"])
        payload.pop("source_handle")
        k["sku_payload"] = json.dumps(payload)
    await _store(admin, listing, variants=_strip(json.loads(listing["seed"]["seed_data"])["variants"]))
    assert tool.summary(await tool.plan(database))["variants_refused"] == {"handle_unrecorded": 2}


async def test_a_disagreeing_sku_stamp_is_refused(db):
    database, admin, tool = db
    listing = _two_sizes()
    k = next(k for k in listing["skus"] if json.loads(k["sku_payload"])["variant_id"] == V15)
    k["sku_payload"] = json.dumps({**json.loads(k["sku_payload"]), "variant_id_provenance": "unverifiable"})
    await _store(admin, listing, variants=_strip(json.loads(listing["seed"]["seed_data"])["variants"]))
    s = tool.summary(await tool.plan(database))
    assert s["variants_to_stamp_by_verdict"] == {"merchant_issued": 1}
    assert s["variants_refused"] == {"sku_stamp_disagrees": 1}


async def test_another_products_sku_row_attests_nothing(db):
    database, admin, tool = db
    listing = _two_sizes()
    other = _two_sizes("Example Eau De Parfum")
    stripped = _strip(json.loads(listing["seed"]["seed_data"])["variants"])
    await _store(admin, {"seed": listing["seed"], "skus": []}, variants=stripped)
    await _store(admin, {"seed": other["seed"], "skus": other["skus"]}, variants=[])
    s = tool.summary(await tool.plan(database))
    assert s["variants_refused"] == {"unattested": 2}


async def test_an_existing_stamp_is_never_touched(db):
    database, admin, tool = db
    listing = _two_sizes()
    written = json.loads(listing["seed"]["seed_data"])["variants"]
    kept = {**written[0], "variant_id_provenance": "unverifiable"}  # disagrees with today's call; still not ours
    sid = await _store(admin, listing, variants=[kept, _strip(written)[1]])
    p = await tool.plan(database)
    assert tool.summary(p)["variants_to_stamp"] == 1
    await tool.apply(database, p)
    assert await _variants(admin, sid) == [kept, written[1]]


async def test_only_active_lane_seeds_are_scanned(db):
    database, admin, tool = db
    listing = _two_sizes()
    stripped = _strip(json.loads(listing["seed"]["seed_data"])["variants"])
    await _store(admin, listing, variants=stripped, seed_id="seed:inactive", status="inactive")
    await _store(admin, listing, variants=stripped, seed_id="seed:other-tool", tool="employee_seed")
    await _store(admin, listing, seed_id="seed:synthetic-only",
                 variants=[{"variant_id": f"{listing['seed']['external_product_id']}::canonical", "title": "x"}])
    assert tool.summary(await tool.plan(database))["candidate_seeds"] == 0


async def test_paging_sees_every_seed(db):
    database, admin, tool = db
    ids = []
    for n in range(3):
        listing = _two_sizes()
        ids.append(await _store(admin, listing, seed_id=f"seed:{n}",
                                variants=_strip(json.loads(listing["seed"]["seed_data"])["variants"])))
    p = await tool.plan(database, page=1)
    assert [s["seed_id"] for s in p["seeds"]] == ids


def test_two_sellers_whose_handles_disagree_are_ambiguous():
    from scripts.backfill_seed_variant_id_provenance import verdict_for

    rows = [{"handle_recorded": True, "source_handle": None, "sku_stamp": None},
            {"handle_recorded": True, "source_handle": V15[:-3], "sku_stamp": None}]
    v = verdict_for(V15, brand="Example", product_name="Example", product_key="ext:k", writer_rows=rows)
    assert v["outcome"] == "ambiguous"


# ── drift and revert ───────────────────────────────────────────────────────────────────────────────────────

async def test_a_seed_changed_since_the_plan_aborts_the_whole_write(db):
    database, admin, tool = db
    a, b = _two_sizes(), _two_sizes("Example Eau De Parfum")
    sa_ = await _store(admin, a, variants=_strip(json.loads(a["seed"]["seed_data"])["variants"]))
    sb = await _store(admin, b, variants=_strip(json.loads(b["seed"]["seed_data"])["variants"]))
    p = await tool.plan(database)
    # The nightly refresh lands between the plan and the write.
    await admin.execute("UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{variants,0,price_amount}',"
                        " '79.0') WHERE id = $1", sb)
    before = {sa_: await _variants(admin, sa_), sb: await _variants(admin, sb)}
    with pytest.raises(RuntimeError, match="drift"):
        await tool.apply(database, p)
    assert {sa_: await _variants(admin, sa_), sb: await _variants(admin, sb)} == before
    assert await _events(admin) == [tool.MANIFEST_ACTION]  # stored first; never marked applied


async def test_revert_removes_only_this_runs_stamps_from_seeds_unchanged_since(db):
    database, admin, tool = db
    a, b = _two_sizes(), _two_sizes("Example Eau De Parfum")
    stripped_a = _strip(json.loads(a["seed"]["seed_data"])["variants"])
    sa_ = await _store(admin, a, variants=stripped_a)
    sb = await _store(admin, b, variants=_strip(json.loads(b["seed"]["seed_data"])["variants"]))
    run_id = (await tool.apply(database, await tool.plan(database)))["run_id"]
    # A re-ingest rewrites b after the apply: its stamps are now that writer's.
    rewritten = json.loads(b["seed"]["seed_data"])["variants"]
    rewritten[0]["price_amount"] = 81.0
    await admin.execute("UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{variants}', $2::jsonb) "
                        "WHERE id = $1", sb, json.dumps(rewritten))

    out = await tool.revert(database, run_id)
    assert (out["reverted"], out["changed_since_apply"]) == (1, 1)
    assert await _variants(admin, sa_) == stripped_a
    assert await _variants(admin, sb) == rewritten
    with pytest.raises(SystemExit, match="already reverted"):
        await tool.revert(database, run_id)


async def test_revert_is_refused_while_a_later_run_holds_its_seeds(db):
    database, admin, tool = db
    listing = _two_sizes()
    written = json.loads(listing["seed"]["seed_data"])["variants"]
    sid = await _store(admin, listing, variants=_strip(written))
    # Two runs on one seed: the first stamps both; strip one again and a second run re-stamps it.
    first = (await tool.apply(database, await tool.plan(database)))["run_id"]
    await admin.execute("UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{variants,1}', "
                        "(seed_data->'variants'->1) - 'variant_id_provenance') WHERE id = $1", sid)
    second = (await tool.apply(database, await tool.plan(database)))["run_id"]
    with pytest.raises(SystemExit, match="later run"):
        await tool.revert(database, first)
    assert (await tool.revert(database, second))["reverted"] == 1
    assert await _variants(admin, sid) == [written[0], _strip(written)[1]]


async def test_revert_of_a_run_that_never_committed_is_refused(db):
    database, admin, tool = db
    listing = _two_sizes()
    await _store(admin, listing, variants=_strip(json.loads(listing["seed"]["seed_data"])["variants"]))
    p = await tool.plan(database)
    await admin.execute("UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{variants,0,title}', "
                        "'\"changed\"')")
    with pytest.raises(RuntimeError):
        await tool.apply(database, p)
    run_id = (await admin.fetchrow("SELECT run_id FROM identity_resolution_events"))["run_id"]
    with pytest.raises(SystemExit, match="never committed"):
        await tool.revert(database, run_id)

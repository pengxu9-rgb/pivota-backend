"""services/brand_relabel.py's SQL, executed on Postgres against a scratch schema.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs `tests/test_*_postgres.py`).

What only Postgres proves: the family match `lower(regexp_replace(brand, '[^[:alnum:]]', '', 'g'))`, the
drift-guarded UPDATE ... RETURNING through `databases`, the membership insert's ON CONFLICT, and the jsonb_set
seed rewrite -- forward, then reversed from the manifest back to the exact starting state.

Minimal stub tables in a PRIVATE schema (dropped after), never under the shared gate schema: the gate files
share one database and a stub under a real table's name there breaks every sibling
(tests/test_canonical_feed_tombstoned_flag_postgres.py).
"""
import json
import os
from urllib.parse import urlsplit

import pytest

from services import brand_relabel as rl
from services.catalog_identity import make_content_key
from services.product_group_autogrouper import derive_product_group_id

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="needs a Postgres DATABASE_URL")
_SCHEMA = f"brand_relabel_{os.getpid()}"


@pytest.fixture
async def db():
    import asyncpg
    import databases

    dbname = urlsplit(URL).path.rsplit("/", 1)[-1]
    if "test" not in dbname and dbname != "pivota_dialect_check":
        pytest.skip("scratch-schema tests require a test database")
    conn = await asyncpg.connect(URL)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE; CREATE SCHEMA {_SCHEMA};")
        await conn.execute(f"""
            CREATE TABLE {_SCHEMA}.catalog_products (
                product_key TEXT PRIMARY KEY, brand TEXT, title TEXT, gtin TEXT, content_key TEXT,
                merchant_id TEXT, platform TEXT, source_product_id TEXT, source_domain TEXT,
                suppression_reason TEXT, updated_at TIMESTAMPTZ);
            CREATE TABLE {_SCHEMA}.product_group_members (
                product_group_id TEXT NOT NULL, merchant_id TEXT NOT NULL, platform TEXT NOT NULL,
                platform_product_id TEXT NOT NULL, is_primary BOOLEAN, updated_at TIMESTAMPTZ,
                UNIQUE (merchant_id, platform, platform_product_id));
            CREATE TABLE {_SCHEMA}.external_product_seeds (
                -- TEXT, as in prod (mig 044: "seed:catalog_enrichment_agent_v1:<hex>"); an integer stub hid a cast
                id TEXT PRIMARY KEY DEFAULT ('seed:catalog_enrichment_agent_v1:' || md5(random()::text)),
                attached_product_key TEXT, seed_data JSONB, updated_at TIMESTAMPTZ);
            CREATE TABLE {_SCHEMA}.identity_resolution_events (
                id BIGSERIAL PRIMARY KEY, proposal_id TEXT NULL, action TEXT NOT NULL, run_id TEXT NOT NULL,
                detail JSONB NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        """)
    finally:
        await conn.close()
    database = databases.Database(URL, server_settings={"search_path": _SCHEMA})
    await database.connect()
    try:
        yield database
    finally:
        await database.disconnect()
        conn = await asyncpg.connect(URL)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()


async def _product(db, pk, brand, title, host, *, group=True, suppressed=None, seed=True):
    ck = make_content_key(brand, title)
    await db.execute("""INSERT INTO catalog_products VALUES (:pk, :b, :t, NULL, :ck, :m, 'external_seed', :spid,
                        :host, :sup, NOW())""",
                     {"pk": pk, "b": brand, "t": title, "ck": ck, "m": f"m_{host}", "spid": f"retailer:{pk}",
                      "host": host, "sup": suppressed})
    if group:
        await db.execute("INSERT INTO product_group_members VALUES (:pg, :m, 'external_seed', :spid, TRUE, NOW())",
                         {"pg": derive_product_group_id(ck), "m": f"m_{host}", "spid": f"retailer:{pk}"})
    if seed:
        await db.execute("INSERT INTO external_product_seeds (attached_product_key, seed_data) VALUES (:pk, "
                         "CAST(:d AS jsonb))", {"pk": pk, "d": json.dumps({"brand": brand, "title": title})})
    return ck


async def _state(db):
    rows = await db.fetch_all("SELECT product_key, brand, content_key FROM catalog_products ORDER BY 1")
    groups = await db.fetch_all("SELECT platform_product_id, product_group_id FROM product_group_members ORDER BY 1")
    seeds = await db.fetch_all("SELECT attached_product_key, seed_data ->> 'brand' AS b FROM external_product_seeds "
                               "ORDER BY 1")
    vals = lambda recs: [tuple(dict(r._mapping).values()) for r in recs]  # noqa: E731
    return (vals(rows), vals(groups), vals(seeds))


async def test_relabel_joins_the_canonical_product_and_reverts_exactly(db):
    canon_ck = await _product(db, "ext:retailer:c1", "ETUDE", "Fixing Tint", "dodoskin.com")
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    await _product(db, "ext:retailer:b", "Etude House", "Fixing Tint", "holiholic.com", group=False)
    await _product(db, "ext:retailer:e", "Etude", "Lash Perm", "theglowbeautyshop.com")
    await _product(db, "ext:retailer:gone", "ETUDE HOUSE", "Old", "luxiface.com", suppressed="x")
    await _product(db, "ext:retailer:other", "Innisfree", "Fixing Tint", "luxiface.com")
    # a second seed on row a, already canonical before the run: never touched, forward or back
    await db.execute("INSERT INTO external_product_seeds (attached_product_key, seed_data) VALUES "
                     "('ext:retailer:a', CAST(:d AS jsonb))", {"d": json.dumps({"brand": "ETUDE"})})
    before = await _state(db)

    rows, neighbours, seeds = await rl.load(db, ["etudehouse", "etude"], "ETUDE")
    assert sorted(r["product_key"] for r in rows) == ["ext:retailer:a", "ext:retailer:b", "ext:retailer:e"]
    plan = rl.plan_relabel(rows, neighbours, "ETUDE", seeds)
    assert plan["counts"] == {"joined_by_title": 2, "case_only": 1}
    manifest = rl.manifest_for(plan)
    counts = await rl.write_moves(db, manifest["moves"])
    assert counts == {"products": 3, "groups": 2, "seeds": 3}

    rows_after, groups_after, seeds_after = await _state(db)
    live = {pk: (b, ck) for pk, b, ck in rows_after}
    assert live["ext:retailer:a"] == ("ETUDE", canon_ck) and live["ext:retailer:b"] == ("ETUDE", canon_ck)
    assert live["ext:retailer:e"][0] == "ETUDE"
    assert live["ext:retailer:gone"][0] == "ETUDE HOUSE" and live["ext:retailer:other"][0] == "Innisfree"
    grp = dict(groups_after)
    assert grp["retailer:ext:retailer:a"] == grp["retailer:ext:retailer:b"] == derive_product_group_id(canon_ck)
    assert sorted(b for pk, b in seeds_after if pk == "ext:retailer:a") == ["ETUDE", "ETUDE"]
    assert dict(seeds_after)["ext:retailer:gone"] == "ETUDE HOUSE"

    await rl.write_moves(db, manifest["moves"], reverse=True)
    assert await _state(db) == before


async def test_a_drifted_row_rolls_the_whole_write_back(db):
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    await _product(db, "ext:retailer:b", "ETUDE HOUSE", "Lash Perm", "luxiface.com")
    rows, neighbours, seeds = await rl.load(db, ["etudehouse", "etude"], "ETUDE")
    manifest = rl.manifest_for(rl.plan_relabel(rows, neighbours, "ETUDE", seeds))
    before = await _state(db)
    await db.execute("UPDATE catalog_products SET brand = 'Someone Else' WHERE product_key = 'ext:retailer:b'")
    drifted = await _state(db)
    with pytest.raises(RuntimeError, match="drift"):
        await rl.write_moves(db, manifest["moves"])
    assert await _state(db) == drifted != before  # row a's move rolled back with b's refusal



async def _plan(db):
    rows, neighbours, seeds = await rl.load(db, ["etudehouse", "etude"], "ETUDE")
    return rl.manifest_for(rl.plan_relabel(rows, neighbours, "ETUDE", seeds))


@pytest.mark.parametrize("drift", ["group", "seed", "suppressed"])
async def test_each_real_sql_guard_refuses_a_row_that_moved_since_the_plan(db, drift):
    """The unit fake re-implements the guards; these run the SQL's own WHERE clauses (review of #2434)."""
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    manifest = await _plan(db)
    if drift == "group":
        await db.execute("UPDATE product_group_members SET product_group_id = 'pg_moved' "
                         "WHERE platform_product_id = 'retailer:ext:retailer:a'")
    elif drift == "seed":
        await db.execute("UPDATE external_product_seeds SET seed_data = CAST(:d AS jsonb) "
                         "WHERE attached_product_key = 'ext:retailer:a'", {"d": json.dumps({"brand": "Re-crawled"})})
    else:
        await db.execute("UPDATE catalog_products SET suppression_reason = 'x' WHERE product_key = 'ext:retailer:a'")
    drifted = await _state(db)
    with pytest.raises(RuntimeError, match="drift"):
        await rl.write_moves(db, manifest["moves"])
    assert await _state(db) == drifted


async def test_a_suppressed_row_is_neither_a_candidate_nor_a_neighbour(db):
    await _product(db, "ext:retailer:gone", "ETUDE", "Fixing Tint", "dodoskin.com", suppressed="x")
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    rows, neighbours, _ = await rl.load(db, ["etudehouse", "etude"], "ETUDE")
    assert [r["product_key"] for r in rows] == ["ext:retailer:a"]
    assert "ext:retailer:gone" not in {n["product_key"] for n in neighbours}
    [m] = rl.plan_relabel(rows, neighbours, "ETUDE")["moves"]
    assert m["reason"] == "minted"  # the tombstoned ETUDE row is nobody's product to join


async def test_a_revert_is_all_or_nothing(db):
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    await _product(db, "ext:retailer:b", "ETUDE HOUSE", "Lash Perm", "luxiface.com")
    manifest = await _plan(db)
    await rl.write_moves(db, manifest["moves"])
    await db.execute("UPDATE catalog_products SET title = 'retitled', content_key = 'ck_other' "
                     "WHERE product_key = 'ext:retailer:b'")
    moved = await _state(db)
    with pytest.raises(RuntimeError, match="drift"):
        await rl.write_moves(db, manifest["moves"], reverse=True)
    assert await _state(db) == moved  # row a stayed relabelled with b



async def test_a_manifest_larger_than_a_log_line_round_trips_through_the_database(db):
    """Cloud Logging cut the first ETUDE apply's 303-move manifest at 100 KB (2026-09-28): it is stored in
    identity_resolution_events instead, and read back whole."""
    moves = [{"product_key": f"ext:retailer:{i:040x}", "from_brand": "ETUDE HOUSE", "to_brand": "ETUDE",
              "from_ck": f"ck_{i:032x}", "to_ck": f"ck_{i + 1:032x}", "from_pg": f"pg_{i:032x}",
              "to_pg": f"pg_{i + 1:032x}", "merchant_id": "merch_obs_luxiface", "platform": "external_seed",
              "spid": f"retailer:{i:040x}", "seed_ids": [f"seed:catalog_enrichment_agent_v1:{i:016x}"]}
             for i in range(400)]
    manifest = {"run_id": "relabel_big", "canonical": "ETUDE", "at": "t", "moves": moves}
    assert len(json.dumps(manifest)) > 102400
    await rl.store_manifest(db, manifest)
    assert await rl.load_manifest(db, "relabel_big") == manifest
    with pytest.raises(SystemExit):
        await rl.load_manifest(db, "relabel_missing")



async def test_only_a_run_whose_write_committed_can_be_reverted_and_only_once(db):
    """Review of #2436: run A stored its manifest and failed; retry B (same moves) applied. Reverting A must not
    undo B's rows under A's id."""
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    a = await _plan(db)
    await rl.store_manifest(db, a)
    await db.execute("UPDATE catalog_products SET brand = 'drifted' WHERE product_key = 'ext:retailer:a'")
    with pytest.raises(RuntimeError, match="drift"):
        await rl.write_moves(db, a["moves"], run_id=a["run_id"])
    await db.execute("UPDATE catalog_products SET brand = 'ETUDE HOUSE' WHERE product_key = 'ext:retailer:a'")
    b = await _plan(db)
    await rl.store_manifest(db, b)
    await rl.write_moves(db, b["moves"], run_id=b["run_id"])
    applied = await _state(db)
    with pytest.raises(RuntimeError, match="never applied"):
        await rl.write_moves(db, (await rl.load_manifest(db, a["run_id"]))["moves"], reverse=True, run_id=a["run_id"])
    assert await _state(db) == applied
    await rl.write_moves(db, b["moves"], reverse=True, run_id=b["run_id"])
    with pytest.raises(RuntimeError, match="already reverted"):
        await rl.write_moves(db, b["moves"], reverse=True, run_id=b["run_id"])
    events = await db.fetch_all("SELECT action, run_id FROM identity_resolution_events ORDER BY id")
    assert [(e["action"], e["run_id"]) for e in events] == [
        ("brand_relabel_manifest", a["run_id"]), ("brand_relabel_manifest", b["run_id"]),
        ("brand_relabel_applied", b["run_id"]), ("brand_relabel_reverted", b["run_id"])]


async def test_a_failed_write_records_no_applied_event(db):
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    a = await _plan(db)
    await db.execute("UPDATE product_group_members SET product_group_id = 'pg_moved'")
    with pytest.raises(RuntimeError):
        await rl.write_moves(db, a["moves"], run_id=a["run_id"])
    assert await db.fetch_all("SELECT 1 FROM identity_resolution_events WHERE action = 'brand_relabel_applied'") == []


async def test_the_applied_event_commits_with_the_write_or_neither_does(db):
    """If recording "applied" fails, the relabel must roll back with it -- a committed write with no applied
    event could never be reverted by run id."""
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    a = await _plan(db)
    before = await _state(db)
    await db.execute("ALTER TABLE identity_resolution_events ADD CONSTRAINT no_applied "
                     "CHECK (action <> 'brand_relabel_applied')")
    with pytest.raises(Exception):
        await rl.write_moves(db, a["moves"], run_id=a["run_id"])
    assert await _state(db) == before

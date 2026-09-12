"""The exact-ID repair against Postgres, including atomic stale-plan rejection."""

import os
import uuid

import pytest

from scripts import audit_crawl_seed_recall_scope as repair

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")


@pytest.fixture
async def seed_db():
    import asyncpg
    from databases import Database

    if "test" not in URL.rsplit("/", 1)[-1]:
        pytest.skip("repair fixture requires a test database")
    schema = "test_seed_scope_" + uuid.uuid4().hex
    conn = await asyncpg.connect(URL)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}"')
    await conn.execute("""
        CREATE TABLE external_product_seeds (
            id text PRIMARY KEY, market text NOT NULL, tool text NOT NULL,
            external_product_id text NULL, status text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT NOW());
        CREATE UNIQUE INDEX seed_active_scope ON external_product_seeds
            (market, tool, external_product_id) WHERE status='active' AND external_product_id IS NOT NULL;
    """)
    db = Database(URL, server_settings={"search_path": schema})
    await db.connect()
    try:
        yield db
    finally:
        await db.disconnect()
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


async def insert(db, name, *, tool=repair.OLD_TOOL, epid=None):
    seed_id = repair.PREFIX + name
    await db.execute(
        "INSERT INTO external_product_seeds(id,market,tool,external_product_id,status) "
        "VALUES(:id,'US',:tool,:epid,'active')",
        {"id": seed_id, "tool": tool, "epid": epid or name})
    return seed_id


async def test_dry_run_is_read_only_and_apply_changes_only_reviewed_ids(seed_db):
    chosen = await insert(seed_db, "chosen")
    untouched = await insert(seed_db, "untouched")
    before = await seed_db.fetch_one(repair.ROW_SQL, {"id": chosen})
    plan = await repair.audit(seed_db, [chosen])
    assert len(plan["changes"]) == 1 and not plan["blocked"]
    assert repair.snapshot(await seed_db.fetch_one(repair.ROW_SQL, {"id": chosen})) == repair.snapshot(before)
    result = await repair.apply_plan(seed_db, plan)
    assert result["applied_ids"] == [chosen]
    after = await seed_db.fetch_one(repair.ROW_SQL, {"id": chosen})
    assert after["tool"] == "*"
    assert all(after[key] == before[key] for key in ("id", "market", "external_product_id", "status"))
    assert (await seed_db.fetch_one(repair.ROW_SQL, {"id": untouched}))["tool"] == repair.OLD_TOOL
    assert (await repair.audit(seed_db, [chosen]))["already_visible"] == [chosen]


async def test_existing_collision_is_blocked_without_mutation(seed_db):
    seed_id = await insert(seed_db, "blocked", epid="same")
    collision = await insert(seed_db, "other", tool="*", epid="same")
    plan = await repair.audit(seed_db, [seed_id])
    assert plan["changes"] == []
    assert plan["blocked"][0]["collision_ids"] == [collision]
    with pytest.raises(ValueError, match="blocked"):
        await repair.apply_plan(seed_db, plan)
    assert (await seed_db.fetch_one(repair.ROW_SQL, {"id": seed_id}))["tool"] == repair.OLD_TOOL


async def test_stale_last_row_rolls_back_first_row(seed_db):
    first = await insert(seed_db, "a")
    last = await insert(seed_db, "z")
    plan = await repair.audit(seed_db, [first, last])
    await seed_db.execute("UPDATE external_product_seeds SET updated_at=NOW()+interval '1 second' WHERE id=:id", {"id": last})
    with pytest.raises(ValueError, match="changed since audit"):
        await repair.apply_plan(seed_db, plan)
    rows = await seed_db.fetch_all("SELECT tool FROM external_product_seeds")
    assert all(row["tool"] == repair.OLD_TOOL for row in rows)


async def test_collision_created_after_audit_aborts_apply(seed_db):
    seed_id = await insert(seed_db, "target", epid="same")
    plan = await repair.audit(seed_db, [seed_id])
    await insert(seed_db, "new-competitor", tool="*", epid="same")
    with pytest.raises(ValueError, match="collision appeared"):
        await repair.apply_plan(seed_db, plan)
    assert (await seed_db.fetch_one(repair.ROW_SQL, {"id": seed_id}))["tool"] == repair.OLD_TOOL


async def test_edited_plan_cannot_change_identity_or_target_scope(seed_db):
    seed_id = await insert(seed_db, "target")
    plan = await repair.audit(seed_db, [seed_id])
    plan["changes"][0]["after_tool"] = "creator_agents"
    with pytest.raises(ValueError, match="invalid"):
        await repair.apply_plan(seed_db, plan)
    assert (await seed_db.fetch_one(repair.ROW_SQL, {"id": seed_id}))["tool"] == repair.OLD_TOOL


async def test_null_identity_outside_partial_unique_index_is_not_repairable(seed_db):
    seed_id = await insert(seed_db, "missing-identity")
    await seed_db.execute("UPDATE external_product_seeds SET external_product_id=NULL WHERE id=:id", {"id": seed_id})
    plan = await repair.audit(seed_db, [seed_id])
    assert not plan["changes"] and plan["blocked"][0]["reason"] == "missing_scope_identity"
    with pytest.raises(ValueError, match="blocked"):
        await repair.apply_plan(seed_db, plan)


async def test_concurrent_insert_after_collision_check_rolls_back_entire_repair(seed_db, monkeypatch):
    import asyncpg

    first = await insert(seed_db, "a")
    last = await insert(seed_db, "z")
    plan = await repair.audit(seed_db, [first, last])
    check = repair.collisions

    async def race(db, row):
        result = await check(db, row)
        if row["id"] == last:
            # An independent connection, not a child task inheriting databases'
            # transaction ContextVar. Commit a real target-scope phantom.
            connection = await asyncpg.connect(URL, **seed_db.options)
            try:
                await connection.execute(
                    "INSERT INTO external_product_seeds(id,market,tool,external_product_id,status) "
                    "VALUES($1,'US','*','z','active')", repair.PREFIX + "concurrent")
            finally:
                await connection.close()
        return result

    monkeypatch.setattr(repair, "collisions", race)
    with pytest.raises(asyncpg.UniqueViolationError):
        await repair.apply_plan(seed_db, plan)
    assert (await seed_db.fetch_one(repair.ROW_SQL, {"id": first}))["tool"] == repair.OLD_TOOL
    assert (await seed_db.fetch_one(repair.ROW_SQL, {"id": last}))["tool"] == repair.OLD_TOOL
    assert (await seed_db.fetch_one(repair.ROW_SQL, {"id": repair.PREFIX + "concurrent"}))["tool"] == "*"

"""scripts/ops/repair_seed_main_lists.py on real PostgreSQL: dry run, digest-bound apply, CAS skip, audit, revert.

The stored lists are what main's real extractor + seed writer store (tests/test_repair_seed_main_lists.py builds
them). Isolated schema; never run against production.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)

from tests.test_repair_seed_main_lists import (  # noqa: E402
    SKU50, TITLE, V50, _gtin_pair, _jurlique, _murad, _snap,
)

GTIN50 = "8809640733451"


@pytest.fixture
async def db():
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.catalog  # noqa: F401 -- registers writer_audit_log on the shared metadata
    import db.database as dbmod
    from scripts.ops import repair_seed_main_lists as tool

    url = os.environ["DATABASE_URL"]
    schema = "seed_main_list_repair_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    database = None
    try:
        await admin.execute(str(sa.schema.CreateTable(dbmod.metadata.tables["writer_audit_log"]).compile(
            dialect=postgresql.dialect())))
        # The columns this tool reads, typed as routes/employee_products._ensure_external_seeds_table makes them.
        await admin.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, market TEXT NOT NULL, "
                            "tool TEXT NOT NULL DEFAULT '*', status TEXT NOT NULL DEFAULT 'active', "
                            "destination_url TEXT NOT NULL, canonical_url TEXT, domain TEXT, title TEXT, "
                            "price_amount DOUBLE PRECISION NULL, attached_product_key TEXT NULL, "
                            "seed_data JSONB NOT NULL DEFAULT '{}'::jsonb, "
                            "updated_at TIMESTAMPTZ NOT NULL DEFAULT '2026-10-01T00:00:00Z')")
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


def _seed_data(variants: Any, *, snapshot_variants: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {"title": TITLE, "extracted_at": "2026-10-15T05:20:00+00:00",
                                "variant_refresh": {"status": "not_all_re_read"}}
    if snapshot_variants is not None:
        snapshot["variants"] = snapshot_variants
    return {"title": TITLE, "variants": variants, "snapshot": snapshot, "pdp_description_raw": "Copy."}


async def _store(admin, seed_id: str, seed_data: Any, *, status: str = "active", market: str = "US",
                 raw: Optional[str] = None) -> str:
    await admin.execute(
        "INSERT INTO external_product_seeds (id, market, status, destination_url, canonical_url, domain, title, "
        "seed_data) VALUES ($1, $2, $3, $4, $4, 'eyurs.com', $5, $6::jsonb)",
        seed_id, market, status, f"https://eyurs.com/products/{seed_id}", TITLE, raw or json.dumps(seed_data))
    return seed_id


async def _row(admin, seed_id: str) -> Dict[str, Any]:
    r = await admin.fetchrow("SELECT seed_data::text AS sd, seed_data->'variants' AS v, updated_at, status, title "
                             "FROM external_product_seeds WHERE id = $1", seed_id)
    return {"seed_data": json.loads(r["sd"]), "variants_text": r["v"], "updated_at": r["updated_at"],
            "status": r["status"], "title": r["title"]}


async def _actions(admin) -> List[str]:
    return [r["action"] for r in await admin.fetch("SELECT action FROM identity_resolution_events ORDER BY id")]


async def _three(admin) -> Dict[str, str]:
    return {
        "murad": await _store(admin, "eps_murad", _seed_data(_murad())),
        "jurlique": await _store(admin, "eps_jurlique", _seed_data(_jurlique(), snapshot_variants=[_snap(V50, sku=SKU50)])),
        "gtin": await _store(admin, "eps_gtin", _seed_data(_gtin_pair(), snapshot_variants=[_snap(V50, barcode=GTIN50)])),
        "held": await _store(admin, "eps_held", _seed_data(_jurlique())),
    }


async def _digest(tool, database, **caps) -> str:
    return tool.plan_digest((await tool.dry_run(database, **caps))["seeds"])


async def test_apply_writes_only_the_main_list_and_records_its_revert(db):
    database, admin, tool = db
    ids = await _three(admin)
    before = {k: await _row(admin, v) for k, v in ids.items()}

    p = await tool.dry_run(database)
    assert p["scanned"] == 4 and await _actions(admin) == []  # the dry run wrote nothing
    out = await tool.apply(database, expect_digest=tool.plan_digest(p["seeds"]), max_seeds=10, sleep_s=0, batch_size=2)
    assert (out["applied"], out["cas_skipped"]) == (3, 0)

    after = {k: await _row(admin, v) for k, v in ids.items()}
    assert after["murad"]["seed_data"]["variants"] == [{**_murad()[0], "title": TITLE}]
    assert after["jurlique"]["seed_data"]["variants"] == [{**_jurlique()[0], "sku": SKU50}]
    assert after["gtin"]["seed_data"]["variants"] == [{**_gtin_pair()[0], "barcode": GTIN50}]
    assert after["held"] == before["held"]
    for k in ids:
        # Nothing but seed_data.variants moves: not the snapshot (its variants, extracted_at), not updated_at.
        b, a = before[k]["seed_data"], after[k]["seed_data"]
        assert {x: y for x, y in a.items() if x != "variants"} == {x: y for x, y in b.items() if x != "variants"}
        assert (after[k]["updated_at"], after[k]["status"], after[k]["title"]) == (
            before[k]["updated_at"], before[k]["status"], before[k]["title"])

    assert await _actions(admin) == [tool.RUN_ACTION] + [tool.SEED_ACTION] * 3 + [tool.APPLIED_ACTION]
    audit = await admin.fetch("SELECT writer_name, batch_id, dry_run_report_hash, applied_rows, skipped_rows "
                              "FROM writer_audit_log ORDER BY id")
    assert [(r["writer_name"], r["dry_run_report_hash"], r["applied_rows"]) for r in audit] == [
        (tool.WRITER, out["digest"], 2), (tool.WRITER, out["digest"], 1)]

    again = await tool.dry_run(database)
    assert [s["seed_id"] for s in again["seeds"] if s["action"] == "write"] == []  # idempotent


async def test_numbers_are_never_round_tripped_through_python(db):
    database, admin, tool = db
    variants = _murad()
    raw = json.dumps(_seed_data(variants)).replace('"price_amount": 48.0', '"price_amount": 48.00')
    await _store(admin, "eps_murad", None, raw=raw)
    assert '"price_amount": 48.00' in (await _row(admin, "eps_murad"))["variants_text"]
    await tool.apply(database, expect_digest=await _digest(tool, database), max_seeds=1, sleep_s=0)
    assert '"price_amount": 48.00' in (await _row(admin, "eps_murad"))["variants_text"]


async def test_the_dry_run_cannot_write(db, monkeypatch):
    import asyncpg

    database, admin, tool = db

    async def writing_plan(db_, **_caps):
        await db_.execute("INSERT INTO identity_resolution_events (action, run_id) VALUES ('x', 'x')")
    monkeypatch.setattr(tool, "plan", writing_plan)
    with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
        await tool.dry_run(database)
    assert await _actions(admin) == []


async def test_apply_refuses_any_digest_but_the_fresh_plans(db):
    database, admin, tool = db
    ids = await _three(admin)
    reviewed = await _digest(tool, database)
    for wrong in ("", "0" * 64, await _digest(tool, database, max_seeds=1)):
        with pytest.raises(SystemExit, match="refused"):
            await tool.apply(database, expect_digest=wrong, max_seeds=10, sleep_s=0)
    # A cap that binds changes the plan, so the uncapped review no longer matches.
    with pytest.raises(SystemExit, match="refused"):
        await tool.apply(database, expect_digest=reviewed, max_seeds=1, sleep_s=0)
    # A planned seed changed between the review and the apply moves the digest too.
    await admin.execute("UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{variants,0,price_amount}',"
                        " '47.0') WHERE id = $1", ids["murad"])
    with pytest.raises(SystemExit, match="refused"):
        await tool.apply(database, expect_digest=reviewed, max_seeds=10, sleep_s=0)
    assert await _actions(admin) == [] and await admin.fetchval("SELECT count(*) FROM writer_audit_log") == 0


async def test_a_seed_changed_after_the_plan_is_skipped_and_counted(db, monkeypatch):
    database, admin, tool = db
    ids = await _three(admin)
    real = tool.dry_run

    async def refresh_lands_mid_apply(db_, **caps):
        p = await real(db_, **caps)
        # The nightly refresh rewrites one seed between the apply's own plan and its write.
        await admin.execute("UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, "
                            "'{snapshot,variants}', '[]'::jsonb) WHERE id = $1", ids["jurlique"])
        # ...and an operator deactivates another (its bytes unchanged).
        await admin.execute("UPDATE external_product_seeds SET status = 'inactive' WHERE id = $1", ids["gtin"])
        return p
    reviewed = await _digest(tool, database)
    monkeypatch.setattr(tool, "dry_run", refresh_lands_mid_apply)
    out = await tool.apply(database, expect_digest=reviewed, max_seeds=10, sleep_s=0)
    assert (out["applied"], out["cas_skipped"], sorted(out["cas_skipped_ids"])) == (
        1, 2, sorted([ids["gtin"], ids["jurlique"]]))
    assert (await _row(admin, ids["jurlique"]))["seed_data"]["variants"] == _jurlique()
    assert (await _row(admin, ids["gtin"]))["seed_data"]["variants"] == _gtin_pair()
    reasons = [json.loads(r["reasons"]) for r in await admin.fetch("SELECT reasons::text AS reasons FROM writer_audit_log")]
    assert sum(len(r["cas_skipped"]) for r in reasons) == 2
    assert await admin.fetchval("SELECT count(*) FROM identity_resolution_events WHERE action = $1",
                                tool.SEED_ACTION) == 1  # no revert record for a seed not written


async def test_inactive_seeds_and_single_entry_lists_are_not_candidates(db):
    database, admin, tool = db
    await _store(admin, "eps_inactive", _seed_data(_murad()), status="inactive")
    await _store(admin, "eps_one", _seed_data(_murad()[:1]))
    await _store(admin, "eps_snapshot_only", {"title": TITLE, "snapshot": {"variants": _murad()}})
    p = await tool.dry_run(database)
    assert p["scanned"] == 0


async def test_paging_sees_every_seed(db):
    database, admin, tool = db
    for n in range(3):
        await _store(admin, f"eps_{n}", _seed_data(_murad()))
    p = await tool.plan(database, page=1)
    assert [s["seed_id"] for s in p["seeds"]] == ["eps_0", "eps_1", "eps_2"]


async def test_the_summary_gap_is_counted(db):
    database, admin, tool = db
    gap = {"title": TITLE, "snapshot": {"pdp_field_quality_summary": {"ok": 1}}}
    await _store(admin, "eps_gap", gap)
    await _store(admin, "eps_gap_inactive", gap, status="inactive")
    await _store(admin, "eps_both", {**gap, "pdp_field_quality_summary": {"ok": 1}})
    p = await tool.dry_run(database)
    assert p["summary_gap"] == {"active": 1}


async def test_revert_restores_the_exact_before_bytes_of_unchanged_seeds(db):
    database, admin, tool = db
    ids = await _three(admin)
    before = {k: await _row(admin, v) for k, v in ids.items()}
    out = await tool.apply(database, expect_digest=await _digest(tool, database), max_seeds=10, sleep_s=0)
    # The refresh rewrites the gtin seed's list after the apply: its new list is the refresh's.
    await admin.execute("UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{variants,0,price_amount}',"
                        " '30.0') WHERE id = $1", ids["gtin"])
    refreshed = await _row(admin, ids["gtin"])

    done = await tool.revert(database, out["run_id"], sleep_s=0)
    assert (done["reverted"], done["changed_since_apply"], done["changed_ids"]) == (2, 1, [ids["gtin"]])
    for k in ("murad", "jurlique"):
        assert (await _row(admin, ids[k]))["variants_text"] == before[k]["variants_text"]
        assert (await _row(admin, ids[k]))["seed_data"] == before[k]["seed_data"]
    assert await _row(admin, ids["gtin"]) == refreshed
    with pytest.raises(SystemExit, match="already reverted"):
        await tool.revert(database, out["run_id"], sleep_s=0)
    assert await admin.fetchval("SELECT count(*) FROM writer_audit_log WHERE batch_id LIKE $1",
                                out["run_id"] + ":revert:%") == 1


async def test_revert_is_refused_while_a_later_run_rewrote_its_seeds(db):
    database, admin, tool = db
    sid = await _store(admin, "eps_murad", _seed_data(_murad()))
    first = await tool.apply(database, expect_digest=await _digest(tool, database), max_seeds=1, sleep_s=0)
    # Something puts a plan entry back, and a second run repairs it again.
    await admin.execute("UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{variants}', "
                        "(seed_data->'variants') || $2::jsonb) WHERE id = $1", sid, json.dumps([{**_murad()[1], "title": "Subscribe & Save"}]))
    second = await tool.apply(database, expect_digest=await _digest(tool, database), max_seeds=1, sleep_s=0)
    with pytest.raises(SystemExit, match="later run"):
        await tool.revert(database, first["run_id"], sleep_s=0)
    assert (await tool.revert(database, second["run_id"], sleep_s=0))["reverted"] == 1
    # The second revert restores what the second run read -- not the first run's after -- so the first run's
    # revert finds its seed changed since it wrote it, and leaves it.
    out = await tool.revert(database, first["run_id"], sleep_s=0)
    assert (out["reverted"], out["changed_since_apply"]) == (0, 1)
    assert [v["title"] for v in (await _row(admin, sid))["seed_data"]["variants"]] == [TITLE, "Subscribe & Save"]


async def test_revert_of_an_unknown_run_is_refused(db):
    database, admin, tool = db
    with pytest.raises(SystemExit, match="nothing to revert"):
        await tool.revert(database, "mainlist_nope", sleep_s=0)


async def test_apply_fails_on_a_held_row_lock_instead_of_waiting(db, monkeypatch):
    import asyncio
    import asyncpg

    database, admin, tool = db
    monkeypatch.setattr(tool, "LOCK_TIMEOUT", "200ms")
    sid = await _store(admin, "eps_murad", _seed_data(_murad()))
    reviewed = await _digest(tool, database)
    held = admin.transaction()
    await held.start()
    try:
        await admin.execute("SELECT 1 FROM external_product_seeds WHERE id = $1 FOR UPDATE", sid)  # a refresh
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await asyncio.wait_for(tool.apply(database, expect_digest=reviewed, max_seeds=1, sleep_s=0), timeout=10)
    finally:
        await held.rollback()
    assert (await _row(admin, sid))["seed_data"]["variants"] == _murad()
    assert await _actions(admin) == [tool.RUN_ACTION]  # manifest only: no seed written, no revert record


async def test_the_dry_run_report_reads_the_market_and_host(db):
    database, admin, tool = db
    await _three(admin)
    await _store(admin, "eps_sg", _seed_data(_murad()), market="SG")
    lines = tool.report_lines(await tool.dry_run(database))
    by = {x["line"]: x for x in lines}
    assert by["counts"]["kinds"] == {"ii": 1, "iii": 2, "iv": 1, "ii+iv": 0}
    assert {r["key"] for r in by["by_market"]["top"]} == {"US", "SG"}
    assert by["by_host"]["top"][0]["key"] == "eyurs.com"


# ── review of #2638 (controller, 2026-10-16) ─────────────────────────────────────────────────────────────────

async def test_l6_the_updates_own_fingerprint_recheck_skips_a_seed_changed_under_its_lock(db):
    """`cur` reads the statement's snapshot; only the UPDATE's own WHERE is re-evaluated on the row a concurrent
    writer commits while the apply waits for its lock (READ COMMITTED re-check). This pins THAT check alone: the
    refresh holds the row with an uncommitted change through the plan and into the write, then commits."""
    import asyncio

    database, admin, tool = db
    sid = await _store(admin, "eps_murad", _seed_data(_murad()))
    reviewed = await _digest(tool, database)
    held = admin.transaction()
    await held.start()
    committed = False
    try:
        await admin.execute("UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{variants,0,title}', "
                            "'\"Refreshed\"') WHERE id = $1", sid)
        task = asyncio.ensure_future(tool.apply(database, expect_digest=reviewed, max_seeds=1, sleep_s=0))
        await asyncio.sleep(1.0)  # the apply has planned (old bytes) and now waits on the row lock
        assert not task.done()
        await held.commit()
        committed = True
        out = await asyncio.wait_for(task, timeout=10)
    finally:
        if not committed:
            await held.rollback()
    assert (out["applied"], out["cas_skipped"]) == (0, 1)
    after = (await _row(admin, sid))["seed_data"]["variants"]
    assert [v["title"] for v in after] == ["Refreshed", "1 month", "2 months", "3 months"]
    assert await admin.fetchval("SELECT count(*) FROM identity_resolution_events WHERE action = $1",
                                tool.SEED_ACTION) == 0


@pytest.mark.parametrize("freshen", [
    "UPDATE external_product_seeds SET updated_at = updated_at + interval '1 minute' WHERE id = $1",
    "UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{snapshot,extracted_at}', "
    "'\"2026-10-17T05:20:00+00:00\"') WHERE id = $1",
], ids=["updated_at", "extracted_at"])
async def test_l1_revert_leaves_a_seed_a_refresh_re_stamped(db, freshen):
    database, admin, tool = db
    sid = await _store(admin, "eps_murad", _seed_data(_murad()))
    out = await tool.apply(database, expect_digest=await _digest(tool, database), max_seeds=1, sleep_s=0)
    repaired = await _row(admin, sid)
    await admin.execute(freshen, sid)  # the main list's bytes are unchanged; its freshness is not
    done = await tool.revert(database, out["run_id"], sleep_s=0)
    assert (done["reverted"], done["changed_since_apply"]) == (0, 1)
    assert (await _row(admin, sid))["seed_data"]["variants"] == repaired["seed_data"]["variants"]


async def test_l5_an_interrupted_revert_rerun_reports_already_reverted_seeds(db):
    database, admin, tool = db
    ids = await _three(admin)
    out = await tool.apply(database, expect_digest=await _digest(tool, database), max_seeds=10, sleep_s=0)
    # A first revert restored one seed, then died before recording the run as reverted.
    first = await admin.fetchval("SELECT id FROM identity_resolution_events WHERE action = $1 ORDER BY id LIMIT 1",
                                 tool.SEED_ACTION)
    assert await database.fetch_one(tool.REVERT_SQL, {"event_id": first, "action": tool.SEED_ACTION})
    done = await tool.revert(database, out["run_id"], sleep_s=0)
    assert (done["reverted"], done["already_reverted"], done["changed_since_apply"]) == (2, 1, 0)
    assert (await _row(admin, ids["jurlique"]))["seed_data"]["variants"] == _jurlique()


async def test_a_sole_pair_is_written_only_with_its_lane_selected(db):
    database, admin, tool = db
    sid = await _store(admin, "eps_sole", _seed_data(_jurlique(), snapshot_variants=[_snap(V50)]))
    p = await tool.dry_run(database)
    assert [(s["action"], s["reason"]) for s in p["seeds"]] == [("held", "sole_pair_not_selected")]
    with pytest.raises(SystemExit, match="nothing to write"):
        await tool.apply(database, expect_digest=tool.plan_digest(p["seeds"]), max_seeds=1, sleep_s=0)
    reviewed = await _digest(tool, database, include_sole_pair=True)
    await tool.apply(database, expect_digest=reviewed, max_seeds=1, include_sole_pair=True, sleep_s=0)
    assert (await _row(admin, sid))["seed_data"]["variants"] == [_jurlique()[0]]  # dropped, no sku written


async def test_a_non_object_seed_data_is_skipped_not_fatal(db):
    database, admin, tool = db
    await _store(admin, "eps_scalar", None, raw='"not an object"')
    await _store(admin, "eps_murad", _seed_data(_murad()))
    p = await tool.dry_run(database)
    assert [s["seed_id"] for s in p["seeds"]] == ["eps_murad"]

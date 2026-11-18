"""The AgenPas consent record's SQL on Postgres (db/agent_purchase_consents.py, migration 289).

What only Postgres can show: the module and the migration build the same table; the insert-or-nothing then
re-select gives one row and says which call wrote it; the page read binds its ids as one jsonb array; the
agent-scoped rail reads scope by the agent; the rails' PII scrubs and expiry sweeps leave the consents.

The gate and the routes are tested on SQLite over the real app (tests/test_agenpas_consent_gate.py). This
file calls the db layer and the ledgers directly.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_agenpas_consent_gate_postgres.py

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process).
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL; the consent record's dialect gate")

assert "main" not in sys.modules or not _IS_PG, (
    "main is imported: it registers every model on the shared metadata at collection time "
    "and poisons later create_all calls in the Postgres gate"
)

AGENT = "agent_consent_pg"
OTHER_AGENT = "agent_consent_pg_other"
OWNER = "c" * 64
COMMON = dict(terms_version="terms-2026-11", privacy_version="privacy-2026-11",
              terms_url="https://legal.example/terms", privacy_url="https://legal.example/privacy")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to touch agent_purchase_consents in database {dbname!r}: throwaway only")


async def _cleanup(database) -> None:
    for statement in (
        "DELETE FROM agent_purchase_consents WHERE agent_id IN (:a, :b)",
        "DELETE FROM a2a_rail_purchases WHERE agent_id IN (:a, :b)",
        "DELETE FROM reap_agentic_purchases WHERE agent_id IN (:a, :b)",
    ):
        try:
            await database.execute(statement, {"a": AGENT, "b": OTHER_AGENT})
        except Exception:  # noqa: BLE001 - a table this run never built is not a leak
            pass


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_required_schema_light()
    await _cleanup(database)
    try:
        yield database
    finally:
        await _cleanup(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _record(rail="reap", rail_purchase_id="rp_" + "a" * 24, step="approval", epoch=1, agent_id=AGENT, **extra):
    import db.agent_purchase_consents as consents_db

    return await consents_db.record(rail=rail, rail_purchase_id=rail_purchase_id, agent_id=agent_id, step=step,
                                    approval_epoch=epoch, offer_issued_at=extra.pop("issued", datetime.now(timezone.utc)),
                                    **COMMON, **extra)


async def _columns(database, table: str):
    rows = await database.fetch_all(
        "SELECT column_name, data_type, is_nullable, column_default, character_maximum_length "
        "FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = :t "
        "ORDER BY ordinal_position", {"t": table})
    return [tuple(r) for r in rows]


async def _checks(database, table: str):
    rows = await database.fetch_all(
        "SELECT replace(c.conname, :t, 'T') AS name, pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
        "JOIN pg_class r ON r.oid = c.conrelid WHERE r.relname = :t AND r.relnamespace = current_schema()::regnamespace "
        "ORDER BY 1", {"t": table})
    return [tuple(r) for r in rows]


async def test_the_migration_and_the_module_build_the_same_table(_db):
    database = _db
    twin = "agent_purchase_consents_mig289"
    sql = (_REPO / "db/migrations/289_agent_purchase_consents.sql").read_text("utf-8")
    sql = re.sub(r"--[^\n]*", "", sql).replace("agent_purchase_consents", twin)
    await database.execute(f"DROP TABLE IF EXISTS {twin}")
    try:
        for statement in [s.strip() for s in sql.split(";") if s.strip()]:
            await database.execute(statement)
        assert await _columns(database, twin) == await _columns(database, "agent_purchase_consents")
        assert await _checks(database, twin) == await _checks(database, "agent_purchase_consents")
        index = await database.fetch_one(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :i",
            {"i": "uq_agent_purchase_consents_step"})
        assert index is not None and "UNIQUE" in index["indexdef"]
        assert "(rail_purchase_id, rail, step, approval_epoch)" in index["indexdef"]
    finally:
        await database.execute(f"DROP TABLE IF EXISTS {twin}")


async def test_the_heal_is_idempotent(_db):
    import db.agent_purchase_consents as consents_db

    await consents_db.ensure_agent_purchase_consents_schema()
    await consents_db.ensure_agent_purchase_consents_schema()


async def test_a_double_write_is_one_row_and_the_reselect_names_the_winner(_db):
    issued = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    first, created = await _record(issued=issued)
    second, created_again = await _record(issued=issued + timedelta(seconds=30))
    assert created is True and created_again is False
    assert second["id"] == first["id"] and second["offer_issued_at"] == issued
    assert first["accepted_at"].tzinfo is not None
    assert first["evidence"] == "link_holder_on_pivota_page" and first["purchase_id"] is None
    count = await _db.fetch_one("SELECT COUNT(*) AS n FROM agent_purchase_consents WHERE agent_id = :a", {"a": AGENT})
    assert count["n"] == 1
    # Another step, another epoch: their own rows.
    assert (await _record(step="card_binding", epoch=0))[1] is True
    assert (await _record(epoch=2))[1] is True


async def test_the_check_constraints_refuse_what_the_module_would(_db):
    with pytest.raises(Exception):
        await _db.execute(
            "INSERT INTO agent_purchase_consents (id, rail, rail_purchase_id, agent_id, step, terms_version, "
            "privacy_version, terms_url, privacy_url, evidence, offer_issued_at) VALUES ('pcn_bad', 'reap', 'rp_x', "
            ":a, 'pay', 't', 'p', 'https://x.example', 'https://x.example', 'link_holder_on_pivota_page', now())",
            {"a": AGENT})


async def test_the_page_read_binds_its_ids_as_one_array_and_scopes_by_agent(_db):
    import db.agent_purchase_consents as consents_db

    await _record()
    await _record(rail="a2a", rail_purchase_id="ap_" + "a" * 24, step="card_binding", epoch=0)
    await _record(rail_purchase_id="rp_" + "b" * 24, agent_id=OTHER_AGENT)
    got = await consents_db.consents_for(AGENT, ["rp_" + "a" * 24, "ap_" + "a" * 24, "rp_" + "b" * 24, "rp_" + "c" * 24])
    assert got == {("reap", "rp_" + "a" * 24, "approval", 1), ("a2a", "ap_" + "a" * 24, "card_binding", 0)}
    assert await consents_db.consents_for(AGENT, []) == frozenset()


async def _a2a(agent_id=AGENT, state="awaiting_authorization"):
    import db.a2a_rail_ledger as a2a_ledger

    row = await a2a_ledger.create_purchase(
        agent_id=agent_id, agent_user_ref_hash=OWNER, merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/40000000001:1", currency="USD", our_price_minor=4650,
        buyer_email="buyer@example.test", shipping_address={"addressLine1": "1 Placeholder Way"})
    await _db_execute("UPDATE a2a_rail_purchases SET state = :s WHERE id = :i", {"s": state, "i": row["id"]})
    return row["id"]


async def _reap(agent_id=AGENT, state="needs_enrollment"):
    import db.reap_agentic_ledger as reap_ledger

    row = await reap_ledger.create_purchase(
        buyer_ref="bref_consent_pg", agent_id=agent_id, agent_user_ref_hash=OWNER, merchant_domain="merchant.example",
        product_key="pk_1", product_name="Placeholder Serum", quantity=1, currency="USD", our_price_minor=4250,
        buyer_email="buyer@example.test", shipping_address={"addressLine1": "1 Placeholder Way", "city": "X"})
    await _db_execute("UPDATE reap_agentic_purchases SET state = :s, enrollment_id = 're_consent_pg_1' WHERE id = :i",
                      {"s": state, "i": row["id"]})
    return row["id"]


async def _db_execute(sql, values):
    from db.database import database

    await database.execute(sql, values)


async def test_the_rail_reads_are_agent_scoped_and_carry_no_pii(_db):
    import db.agent_purchase_consents as consents_db

    reap_id, a2a_id = await _reap(), await _a2a()
    reap_row = await consents_db.rail_purchase_for_agent("reap", reap_id, AGENT)
    assert reap_row == {"id": reap_id, "state": "needs_enrollment", "rail_buyer_ref": "re_consent_pg_1",
                        "contact_purged_at": None, "last_error_code": None}
    await _db.execute("UPDATE reap_agentic_purchases SET contact_purged_at = now(), "
                      "last_error_code = 'contact_retention_elapsed' WHERE id = :i", {"i": reap_id})
    purged = await consents_db.rail_purchase_for_agent("reap", reap_id, AGENT)
    assert purged["contact_purged_at"] is not None and purged["last_error_code"] == "contact_retention_elapsed"
    a2a_row = await consents_db.rail_purchase_for_agent("a2a", a2a_id, AGENT)
    assert a2a_row["state"] == "awaiting_authorization" and a2a_row["rail_buyer_ref"].startswith("pvm_")
    assert await consents_db.rail_purchase_for_agent("reap", reap_id, OTHER_AGENT) is None
    assert await consents_db.rail_purchase_for_agent("a2a", reap_id, AGENT) is None
    assert await consents_db.rail_purchase_for_agent("other", reap_id, AGENT) is None


async def test_scrubs_and_sweeps_leave_the_consents(_db):
    import db.a2a_rail_ledger as a2a_ledger
    import db.reap_agentic_ledger as reap_ledger

    reap_id, a2a_id = await _reap(state="awaiting_approval"), await _a2a()
    await _record(rail_purchase_id=reap_id, step="card_binding", epoch=0)
    await _record(rail="a2a", rail_purchase_id=a2a_id)
    before = [dict(r) for r in await _db.fetch_all(
        "SELECT * FROM agent_purchase_consents WHERE agent_id = :a ORDER BY id", {"a": AGENT})]
    for table in ("reap_agentic_purchases", "a2a_rail_purchases"):
        await _db.execute(f"UPDATE {table} SET created_at = now() - interval '30 days', "
                          f"state_entered_at = now() - interval '30 days', updated_at = now() - interval '30 days' "
                          f"WHERE agent_id = :a", {"a": AGENT})
    moved = await reap_ledger.expire_overdue_purchases(max_age_seconds=60, enrollment_grace_seconds=0)
    await reap_ledger.scrub_reconciling_purchase_pii(max_age_seconds=60)
    purged = await a2a_ledger.purge_stale_contact(max_age_seconds=3600)
    await a2a_ledger.expire_overdue_purchases(max_age_seconds=60, max_lifetime_seconds=60)
    assert reap_id in moved and purged >= 1
    states = {r["id"]: r["state"] for r in await _db.fetch_all(
        "SELECT id, state FROM reap_agentic_purchases WHERE id = :r UNION ALL "
        "SELECT id, state FROM a2a_rail_purchases WHERE id = :a", {"r": reap_id, "a": a2a_id})}
    assert states == {reap_id: "expired", a2a_id: "expired"}
    after = [dict(r) for r in await _db.fetch_all(
        "SELECT * FROM agent_purchase_consents WHERE agent_id = :a ORDER BY id", {"a": AGENT})]
    assert after == before and len(after) == 2


def test_this_module_does_not_import_main():
    assert "main" not in sys.modules

"""The rail-neutral purchase ledger (migration 264): one `agent_purchases` row per purchase on ANY
payment rail, pointing at that rail's own purchase row.

Payment orchestration P0. Two rails, both executor `rail_managed` (the rail places the order):
Reap (`reap_agentic_purchases`) and the A2A token rail (`a2a_rail_purchases`, migration 268).

── THE PARENT HAS NO STATE, ON PURPOSE ──────────────────────────────────────────────────────

The rail's own row is the state machine, and on Reap it is moved by a dozen writers: the
fenced transition, the claim/release, three sweeps, the operator resolutions. A `state` column
here would be a second copy of that answer, and this driver cannot hold a transaction across the
two writes (db/reap_agentic_ledger.py, driver properties 1-6), so the copy WOULD drift. So the
parent holds only what never changes after the purchase opens -- which rail, which executor,
whose purchase, how the rail was chosen -- and every status read goes to the child.
`services/payment_orchestration/rails.py` maps the child's state onto the shared vocabulary.

── WRITTEN AFTER THE RAIL'S OWN COMMIT, AND HEALED ──────────────────────────────────────────

`ensure_reap_parent` copies identity FROM the committed child row in one INSERT ... SELECT, so
the parent cannot disagree with the child about who owns it or when it was opened. It is called
by the Reap create route after that route's own transaction commits, best-effort and behind
`AGENT_PURCHASE_LEDGER_ENABLED`; it is never inside the rail's transaction, so a fault here can
never fail or roll back a Reap purchase. A parent that was never written is not an error: the
unified read heals it on first touch (`heal_reap_parents_for_owner`) and
`backfill_reap_parents` heals the rest. Both are idempotent on the (rail, rail_purchase_id)
unique index, so any number of concurrent healers leave exactly one row.

This module opens no database transactions (same reason as the Reap ledger:
databases==0.7.0 scopes connections by ContextVar, so a held transaction swallows concurrent
tasks' writes). Every statement is a module-level string literal passed by name, with dialect splits as
`if`/`else` statements, so the PREPARE gate (tests/test_repo_sql_prepare_postgres.py) plans each one; a
test in tests/test_agent_purchase_ledger_postgres.py holds that.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional

from db.database import IS_POSTGRES, database

# Generic row decoders, shared with the Reap ledger rather than copied: jsonb arrives as text on
# raw SQL and SQLite timestamps arrive as text; both come out of here as Python values.
from db.reap_agentic_ledger import _decode_dt, _decode_json

AGENT_PURCHASE_LEDGER_ENABLED_ENV = "AGENT_PURCHASE_LEDGER_ENABLED"
_TRUTHY = frozenset({"1", "true", "on", "yes"})

#: The vocabularies. The CHECKs in `_CREATE_TABLE_PG` hold the same strings; a test parses them
#: back out so the two cannot disagree. The CHECKs are NAMED so the next rail's migration can
#: `DROP CONSTRAINT ck_agent_purchases_rail` and re-add it (CREATE TABLE IF NOT EXISTS never alters).
RAILS = ("reap", "a2a")
EXECUTORS = ("rail_managed",)


def is_enabled() -> bool:
    """Default OFF. An allowlist of truthy spellings, like every dial on the payment rails."""
    return (os.getenv(AGENT_PURCHASE_LEDGER_ENABLED_ENV) or "").strip().lower() in _TRUTHY


# ── schema ───────────────────────────────────────────────────────────────────────────────────
#
# db/migrations/264_agent_purchases.sql is the same DDL. Which of the two builds the table in a given
# environment depends on its startup mode (a fast-mode boot skips db/migrations/; the boot runner
# applies unledgered files otherwise), so BOTH must be safe and identical:
# db/schema_guard.ensure_required_schema_light calls `ensure_agent_purchase_schema` on both dialects,
# and tests/test_agent_purchase_ledger_postgres.py proves the two build the same columns, indexes and
# CHECKs. The DDL is a new, empty table and two indexes on it: it locks no existing table, so it is
# safe under the boot runner's single transaction without a lock_timeout.

_CREATE_TABLE_PG = """
CREATE TABLE IF NOT EXISTS agent_purchases (
    id VARCHAR(64) PRIMARY KEY,
    rail VARCHAR(16) NOT NULL CONSTRAINT ck_agent_purchases_rail CHECK (rail IN ('reap', 'a2a')),
    executor VARCHAR(24) NOT NULL
        CONSTRAINT ck_agent_purchases_executor CHECK (executor IN ('rail_managed')),
    rail_purchase_id VARCHAR(64) NOT NULL,
    agent_id VARCHAR(128),
    agent_user_ref_hash VARCHAR(64),
    routing_plan JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_CREATE_TABLE_SQLITE = (
    _CREATE_TABLE_PG.replace("TIMESTAMPTZ", "TIMESTAMP")
    .replace("JSONB", "TEXT")
    .replace("now()", "CURRENT_TIMESTAMP")
)

#: One parent per rail purchase. This is the index every healer's ON CONFLICT names.
_CREATE_RAIL_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_purchases_rail_purchase
    ON agent_purchases (rail, rail_purchase_id)
"""

#: The owner's history, newest first.
_CREATE_OWNER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_agent_purchases_owner
    ON agent_purchases (agent_id, agent_user_ref_hash, created_at)
"""


#: Migration 268 widened the rail CHECK. A table built before it keeps the narrow one (CREATE TABLE IF
#: NOT EXISTS never alters), so the heal re-adds it. On Postgres, on a LIVE table, in two guarded steps
#: (schema_guard.guarded_ddl: each runs only while a lock-free catalog read says it is still needed,
#: under the heal lock_timeout, and a timeout defers it to the next boot):
#:   1. DROP + ADD ... NOT VALID, in one transaction: ACCESS EXCLUSIVE only for a catalog change, no
#:      scan, and the wide CHECK binds every new row from that moment;
#:   2. VALIDATE CONSTRAINT: the scan of existing rows, under SHARE UPDATE EXCLUSIVE, which blocks
#:      neither reads nor writes.
#: Once the constraint is wide and validated, neither step's guard is true and neither ALTER runs.
#: (Step 1's guard calls pg_get_constraintdef, which takes ACCESS SHARE on the table: it waits only
#: behind an ACCESS EXCLUSIVE holder, and guarded_ddl sets its lock_timeout inside the IF, after the
#: guard, so that wait is bounded by the startup DDL deadline rather than by the heal's 500 ms.)
#: On SQLite, which cannot alter a CHECK, by rebuilding the table (local and test databases only).
_RAIL_CHECK_PG_DEF = (
    "CHECK (((rail)::text = ANY ((ARRAY['reap'::character varying, 'a2a'::character varying])::text[])))"
)
_WIDEN_RAIL_CHECK_PG = """
    ALTER TABLE agent_purchases DROP CONSTRAINT IF EXISTS ck_agent_purchases_rail;
    ALTER TABLE agent_purchases ADD CONSTRAINT ck_agent_purchases_rail CHECK (rail IN ('reap', 'a2a')) NOT VALID;
"""
_VALIDATE_RAIL_CHECK_PG = """
    ALTER TABLE agent_purchases VALIDATE CONSTRAINT ck_agent_purchases_rail;
"""
#: Step 1 is needed while no constraint of that name has the wide list, validated or not.
_RAIL_CHECK_WIDE_ABSENT = (
    "NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('agent_purchases') "
    "AND conname = 'ck_agent_purchases_rail' "
    "AND replace(pg_get_constraintdef(oid), ' NOT VALID', '') = $def$" + _RAIL_CHECK_PG_DEF + "$def$)"
)
#: Step 2 is needed while that constraint exists and is not yet validated.
_RAIL_CHECK_UNVALIDATED = (
    "EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('agent_purchases') "
    "AND conname = 'ck_agent_purchases_rail' AND NOT convalidated)"
)
_SQLITE_TABLES_SQL = (
    "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
    "AND name IN ('agent_purchases', 'agent_purchases_pre268')"
)
_SQLITE_RENAME_OLD = "ALTER TABLE agent_purchases RENAME TO agent_purchases_pre268"
_SQLITE_COPY_OLD = """
    INSERT OR IGNORE INTO agent_purchases (id, rail, executor, rail_purchase_id, agent_id, agent_user_ref_hash,
                                 routing_plan, created_at)
    SELECT id, rail, executor, rail_purchase_id, agent_id, agent_user_ref_hash, routing_plan, created_at
      FROM agent_purchases_pre268
"""
_SQLITE_DROP_OLD = "DROP TABLE agent_purchases_pre268"


async def _sqlite_tables() -> Dict[str, str]:
    return {str(r["name"]): str(r["sql"]) for r in await database.fetch_all(_SQLITE_TABLES_SQL)}


async def _sqlite_widen_rail_check() -> None:
    """Rebuild a narrow-CHECK table, resumably. The four steps are separate statements, so a crash
    can stop between any two; every boot first finishes whatever an earlier one left:
      only `_pre268`           -> crashed after the rename: create, copy, drop;
      both                     -> crashed after the create or the copy: copy (INSERT OR IGNORE), drop;
      only a narrow table      -> start: rename, create, copy, drop.
    The old table is dropped BEFORE the indexes are created: it owns their names until then."""
    # SQLite only. The early return is also what tells the PREPARE gate the statements below are
    # SQLite's (INSERT OR IGNORE is not Postgres).
    if IS_POSTGRES:
        return
    tables = await _sqlite_tables()
    if "agent_purchases" in tables and "'a2a'" not in tables["agent_purchases"] and "agent_purchases_pre268" not in tables:
        await database.execute(_SQLITE_RENAME_OLD)
        tables = await _sqlite_tables()
    await database.execute(_CREATE_TABLE_SQLITE)
    if "agent_purchases_pre268" in tables:
        await database.execute(_SQLITE_COPY_OLD)
        await database.execute(_SQLITE_DROP_OLD)


async def ensure_agent_purchase_schema() -> None:
    """Self-heal parity with migrations 264 and 268. Idempotent; called at every startup."""
    # `if`/`else` statements, not a conditional expression: the PREPARE gate resolves only a literal or a
    # module-level name as the first argument, and an IfExp hides BOTH statements from it.
    if IS_POSTGRES:
        await database.execute(_CREATE_TABLE_PG)
        from db.schema_guard import _heal_guarded

        await _heal_guarded("agent_purchases", _RAIL_CHECK_WIDE_ABSENT, _WIDEN_RAIL_CHECK_PG)
        await _heal_guarded("agent_purchases", _RAIL_CHECK_UNVALIDATED, _VALIDATE_RAIL_CHECK_PG)
        # A bare CREATE INDEX IF NOT EXISTS takes the table's SHARE lock BEFORE it looks for the name,
        # so on this live table every boot would queue behind any open writer, and every later
        # writer behind the boot. schema_guard._ensure_index builds only while the index is missing
        # (a lock-free pg_class read) and defers on its lock_timeout.
        from db.schema_guard import _ensure_index

        await _ensure_index(_CREATE_RAIL_INDEX)
        await _ensure_index(_CREATE_OWNER_INDEX)
    else:
        await _sqlite_widen_rail_check()
        await database.execute(_CREATE_RAIL_INDEX)
        await database.execute(_CREATE_OWNER_INDEX)


# ── writes ───────────────────────────────────────────────────────────────────────────────────


def new_purchase_id() -> str:
    return f"pp_{uuid.uuid4().hex[:24]}"


def _routing_plan(basis: str, rail: str = "reap") -> str:
    """How this purchase came to be on its rail. With one rail there is no choice to record, so
    this says who decided: the agent called the Reap route itself, or the row predates the
    ledger and was backfilled. The router writes ranked candidates here once a second rail
    exists."""
    return json.dumps(
        {"rail": rail, "executor": "rail_managed", "basis": basis},
        separators=(",", ":"),
        sort_keys=True,
    )


#: Identity is COPIED from the committed child, never passed in: the parent cannot disagree with
#: the child about who owns the purchase or when it opened. No child row -> nothing inserted.
_INSERT_REAP_PARENT_SQL = """
    INSERT INTO agent_purchases (
        id, rail, executor, rail_purchase_id, agent_id, agent_user_ref_hash, routing_plan,
        created_at
    )
    SELECT :id, 'reap', 'rail_managed', r.id, r.agent_id, r.agent_user_ref_hash,
           CAST(:routing_plan AS JSONB), r.created_at
      FROM reap_agentic_purchases r
     WHERE r.id = :rail_purchase_id
    ON CONFLICT (rail, rail_purchase_id) DO NOTHING
"""
_INSERT_REAP_PARENT_SQL_SQLITE = _INSERT_REAP_PARENT_SQL.replace(
    "CAST(:routing_plan AS JSONB)", ":routing_plan"
)

_SELECT_PARENT_ID_SQL = """
    SELECT id FROM agent_purchases WHERE rail = :rail AND rail_purchase_id = :rail_purchase_id
"""


async def ensure_reap_parent(rail_purchase_id: str, *, basis: str = "agent_selected_rail") -> Optional[str]:
    """The parent id for a Reap purchase, writing it if absent. None when no such Reap row exists.

    Idempotent and race-safe: the INSERT is ON CONFLICT DO NOTHING on (rail, rail_purchase_id),
    and the id is read back afterwards, so a concurrent writer's row is the answer either way.
    """
    rail_purchase_id = str(rail_purchase_id or "").strip()
    if not rail_purchase_id:
        return None
    values = {
        "id": new_purchase_id(),
        "routing_plan": _routing_plan(basis),
        "rail_purchase_id": rail_purchase_id,
    }
    # Statement form, not an IfExp, so the PREPARE gate plans the Postgres INSERT (see the schema note).
    if IS_POSTGRES:
        await database.execute(_INSERT_REAP_PARENT_SQL, values)
    else:
        await database.execute(_INSERT_REAP_PARENT_SQL_SQLITE, values)
    row = await database.fetch_one(
        _SELECT_PARENT_ID_SQL, {"rail": "reap", "rail_purchase_id": rail_purchase_id}
    )
    return str(row["id"]) if row else None


_INSERT_A2A_PARENT_SQL = """
    INSERT INTO agent_purchases (
        id, rail, executor, rail_purchase_id, agent_id, agent_user_ref_hash, routing_plan,
        created_at
    )
    SELECT :id, 'a2a', 'rail_managed', r.id, r.agent_id, r.agent_user_ref_hash,
           CAST(:routing_plan AS JSONB), r.created_at
      FROM a2a_rail_purchases r
     WHERE r.id = :rail_purchase_id
    ON CONFLICT (rail, rail_purchase_id) DO NOTHING
"""
_INSERT_A2A_PARENT_SQL_SQLITE = _INSERT_A2A_PARENT_SQL.replace(
    "CAST(:routing_plan AS JSONB)", ":routing_plan"
)


async def ensure_a2a_parent(rail_purchase_id: str, *, basis: str = "orchestrator_selected_rail") -> Optional[str]:
    """The parent id for an A2A-rail purchase, writing it if absent; None when no such child row.
    Same contract as `ensure_reap_parent`: identity copied from the committed child, idempotent."""
    rail_purchase_id = str(rail_purchase_id or "").strip()
    if not rail_purchase_id:
        return None
    values = {
        "id": new_purchase_id(),
        "routing_plan": _routing_plan(basis, "a2a"),
        "rail_purchase_id": rail_purchase_id,
    }
    if IS_POSTGRES:
        await database.execute(_INSERT_A2A_PARENT_SQL, values)
    else:
        await database.execute(_INSERT_A2A_PARENT_SQL_SQLITE, values)
    row = await database.fetch_one(
        _SELECT_PARENT_ID_SQL, {"rail": "a2a", "rail_purchase_id": rail_purchase_id}
    )
    return str(row["id"]) if row else None


_SELECT_REAP_MISSING_PARENTS_SQL = """
    SELECT r.id
      FROM reap_agentic_purchases r
     WHERE NOT EXISTS (
            SELECT 1 FROM agent_purchases p
             WHERE p.rail = 'reap' AND p.rail_purchase_id = r.id
           )
     ORDER BY r.created_at, r.id
     LIMIT :limit
"""

_COUNT_REAP_MISSING_PARENTS_SQL = """
    SELECT COUNT(*) AS missing
      FROM reap_agentic_purchases r
     WHERE NOT EXISTS (
            SELECT 1 FROM agent_purchases p
             WHERE p.rail = 'reap' AND p.rail_purchase_id = r.id
           )
"""


async def count_missing_reap_parents() -> int:
    row = await database.fetch_one(_COUNT_REAP_MISSING_PARENTS_SQL)
    return int(row["missing"]) if row else 0


_SELECT_REAP_MISSING_PARENTS_FOR_OWNER_SQL = """
    SELECT r.id
      FROM reap_agentic_purchases r
     WHERE r.agent_id = :agent_id
       AND r.agent_user_ref_hash = :agent_user_ref_hash
       AND NOT EXISTS (
            SELECT 1 FROM agent_purchases p
             WHERE p.rail = 'reap' AND p.rail_purchase_id = r.id
           )
     ORDER BY r.created_at DESC, r.id DESC
     LIMIT :limit
"""


def _clamp(limit: Any, *, maximum: int) -> int:
    return max(1, min(maximum, int(limit)))


async def backfill_reap_parents(*, limit: int = 500) -> int:
    """Give up to `limit` parentless Reap purchases a parent, oldest first. Returns how many it
    examined; each of them has a parent afterwards (this call's, or a concurrent healer's).
    Run until it returns 0."""
    rows = await database.fetch_all(
        _SELECT_REAP_MISSING_PARENTS_SQL, {"limit": _clamp(limit, maximum=5000)}
    )
    for row in rows:
        await ensure_reap_parent(row["id"], basis="backfill")
    return len(rows)


_SELECT_A2A_MISSING_PARENTS_FOR_OWNER_SQL = """
    SELECT r.id
      FROM a2a_rail_purchases r
     WHERE r.agent_id = :agent_id
       AND r.agent_user_ref_hash = :agent_user_ref_hash
       AND NOT EXISTS (
            SELECT 1 FROM agent_purchases p
             WHERE p.rail = 'a2a' AND p.rail_purchase_id = r.id
           )
     ORDER BY r.created_at DESC, r.id DESC
     LIMIT :limit
"""


async def heal_a2a_parents_for_owner(
    agent_id: str, agent_user_ref_hash: str, *, limit: int = 100
) -> int:
    """The owner-scoped heal for the A2A token rail, newest first (see `heal_reap_parents_for_owner`)."""
    rows = await database.fetch_all(
        _SELECT_A2A_MISSING_PARENTS_FOR_OWNER_SQL,
        {
            "agent_id": agent_id,
            "agent_user_ref_hash": agent_user_ref_hash,
            "limit": _clamp(limit, maximum=500),
        },
    )
    for row in rows:
        await ensure_a2a_parent(row["id"], basis="backfill")
    return len(rows)


async def heal_reap_parents_for_owner(
    agent_id: str, agent_user_ref_hash: str, *, limit: int = 100
) -> int:
    """The same backfill, scoped to one owner and NEWEST first (the order the list pages in), so the
    first page is complete the first time it is read rather than after the next backfill run."""
    rows = await database.fetch_all(
        _SELECT_REAP_MISSING_PARENTS_FOR_OWNER_SQL,
        {
            "agent_id": agent_id,
            "agent_user_ref_hash": agent_user_ref_hash,
            "limit": _clamp(limit, maximum=500),
        },
    )
    for row in rows:
        await ensure_reap_parent(row["id"], basis="backfill")
    return len(rows)


# ── owner-scoped reads ───────────────────────────────────────────────────────────────────────
#
# BOTH OWNER CONJUNCTS IN THE SQL, as on the Reap ledger: "not yours" and "does not exist" are
# the same None from the same statement, so these reads cannot be used to probe for ids.

# Plain literals, not f-strings: the PREPARE gate (tests/test_repo_sql_prepare_postgres.py) only
# resolves a statement bound to a string LITERAL, so a composed one would ship unplanned.
_SELECT_FOR_OWNER_SQL = """
    SELECT id, rail, executor, rail_purchase_id, routing_plan, created_at
      FROM agent_purchases
     WHERE id = :id
       AND agent_id = :agent_id
       AND agent_user_ref_hash = :agent_user_ref_hash
"""

_SELECT_BY_RAIL_ID_FOR_OWNER_SQL = """
    SELECT id, rail, executor, rail_purchase_id, routing_plan, created_at
      FROM agent_purchases
     WHERE rail = :rail
       AND rail_purchase_id = :rail_purchase_id
       AND agent_id = :agent_id
       AND agent_user_ref_hash = :agent_user_ref_hash
"""

_LIST_FOR_OWNER_SQL = """
    SELECT id, rail, executor, rail_purchase_id, routing_plan, created_at
      FROM agent_purchases
     WHERE agent_id = :agent_id
       AND agent_user_ref_hash = :agent_user_ref_hash
     ORDER BY created_at DESC, id DESC
     LIMIT :limit OFFSET :offset
"""


def _parent(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    out["routing_plan"] = _decode_json(out.get("routing_plan"))
    out["created_at"] = _decode_dt(out.get("created_at"))
    return out


async def get_for_owner(
    purchase_id: str, agent_id: str, agent_user_ref_hash: str
) -> Optional[Dict[str, Any]]:
    return _parent(
        await database.fetch_one(
            _SELECT_FOR_OWNER_SQL,
            {"id": purchase_id, "agent_id": agent_id, "agent_user_ref_hash": agent_user_ref_hash},
        )
    )


async def get_by_rail_id_for_owner(
    rail: str, rail_purchase_id: str, agent_id: str, agent_user_ref_hash: str
) -> Optional[Dict[str, Any]]:
    return _parent(
        await database.fetch_one(
            _SELECT_BY_RAIL_ID_FOR_OWNER_SQL,
            {
                "rail": rail,
                "rail_purchase_id": rail_purchase_id,
                "agent_id": agent_id,
                "agent_user_ref_hash": agent_user_ref_hash,
            },
        )
    )


async def list_for_owner(
    agent_id: str, agent_user_ref_hash: str, *, limit: int = 20, offset: int = 0
) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        _LIST_FOR_OWNER_SQL,
        {
            "agent_id": agent_id,
            "agent_user_ref_hash": agent_user_ref_hash,
            "limit": _clamp(limit, maximum=500),
            "offset": max(0, int(offset)),
        },
    )
    return [p for p in (_parent(r) for r in rows) if p is not None]

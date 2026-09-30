"""The cart-proof refresh cursors (migration 250) and the mirror lane's stalest-first read, on the
PRODUCTION dialect.

    DATABASE_URL=postgresql://<user>@localhost:5432/pivota_<name>_dialect_check \\
        .venv/bin/python -m pytest tests/test_reap_cart_proof_refresh_cursors_postgres.py

WHAT ONLY POSTGRES CAN SHOW:
  * CATALOG PARITY: production deploys skip db/migrations/, so `ensure_table()` IS the production
    schema. The two builds are compared through the catalog.
  * `SELECT_OLDEST_VALID_MIRROR_PROOF_SQL`'s jsonb accessors and its text cutoff, on real rows.
  * The real `run_lane` (mirror, apply) writing its cursors through the real database, and the next
    run resuming from them.

Every dialect-agnostic case of tests/test_reap_cart_proof_refresh_cursors.py is imported below, so the
dialect gate (which collects only `*_postgres.py`) runs them on Postgres too. This module drops and
recreates ONLY its own table; `external_product_seeds` is extended column by column (the gate shares
one database across files) and cleaned by a file-unique id prefix.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
_THROWAWAY = any(marker in _DBNAME for marker in ("dialect_check", "_test", "test_"))

pytestmark = pytest.mark.skipif(
    not (_IS_PG and _THROWAWAY),
    reason=(
        "needs a Postgres DATABASE_URL naming a THROWAWAY database (…dialect_check…, …_test, "
        "test_…) — this is the production-dialect gate; see the module docstring"
    ),
)

if _IS_PG and _THROWAWAY:
    from tests.test_reap_cart_proof_refresh_cursors import *  # noqa: F401,F403,E402
    from tests.test_reap_cart_proof_refresh_cursors import cursor_db  # noqa: F401,E402

_MIGRATION = Path(__file__).resolve().parent.parent / "db" / "migrations" / "250_reap_cart_proof_refresh_cursors.sql"
_TABLE = "reap_cart_proof_refresh_cursors"
_PREFIX = "epsv_rcpr_"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def pg():
    from db.database import database

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    await database.execute("CREATE TABLE IF NOT EXISTS external_product_seeds (id TEXT)")
    for name, column_type in (("domain", "TEXT"), ("seed_data", "JSONB DEFAULT '{}'::jsonb"),
                              ("status", "TEXT DEFAULT 'active'"), ("canonical_url", "TEXT"),
                              ("destination_url", "TEXT"), ("updated_at", "TIMESTAMPTZ DEFAULT NOW()")):
        await database.execute(f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {name} {column_type}")
    await database.execute("DELETE FROM external_product_seeds WHERE id LIKE :p", {"p": _PREFIX + "%"})
    yield database
    await database.execute("DELETE FROM external_product_seeds WHERE id LIKE :p", {"p": _PREFIX + "%"})
    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _fingerprint(db):
    columns = await db.fetch_all(
        """
        SELECT column_name, data_type, is_nullable, column_default, ordinal_position
          FROM information_schema.columns
         WHERE table_schema = current_schema() AND table_name = :t
         ORDER BY ordinal_position
        """, {"t": _TABLE})
    constraints = await db.fetch_all(
        """
        SELECT CAST(c.contype AS text) AS kind, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = current_schema() AND t.relname = :t
        """, {"t": _TABLE})
    return ([tuple(dict(r).values()) for r in columns], sorted((r["kind"], r["def"]) for r in constraints))


async def test_the_self_heal_builds_what_migration_250_builds(pg):
    import db.reap_cart_proof_refresh_cursors as cursors
    from db.sql_migrations import split_statements

    for statement in split_statements(_MIGRATION.read_text(encoding="utf-8")):
        await pg.execute(statement)
    from_migration = await _fingerprint(pg)
    await pg.execute(f"DROP TABLE {_TABLE}")
    assert await cursors.ensure_table(pg)
    assert await _fingerprint(pg) == from_migration
    columns, constraints = from_migration
    assert [c[0] for c in columns] == ["lane", "domain", "next_cursor", "last_status", "last_completed_at",
                                       "blocked_until", "crash_count", "updated_at"]
    assert ("p", "PRIMARY KEY (lane, domain)") in constraints


async def _seed(db, suffix, domain, proof, status="active"):
    seed_data = {"snapshot": {"variants": [{"sku": "s"}]}}
    if proof is not ...:
        seed_data["snapshot"]["shopify_cart_proof"] = proof
    await db.execute(
        "INSERT INTO external_product_seeds (id, domain, destination_url, seed_data, status) "
        "VALUES (:id, :domain, :url, CAST(:sd AS jsonb), :status)",
        {"id": _PREFIX + suffix, "domain": domain, "url": f"https://{domain}/products/h{suffix}",
         "sd": json.dumps(seed_data), "status": status})


async def test_the_stalest_first_read_counts_only_valid_active_object_proofs(pg):
    import jobs.reap_cart_proof_refresh as refresh

    def proof(age):
        return {"source": "products_js_v1", "checked_at": (NOW - age).isoformat()}

    await _seed(pg, "1", "rcpr-a.com", proof(timedelta(days=2)))
    await _seed(pg, "2", "www.rcpr-a.com", proof(timedelta(days=5)))
    await _seed(pg, "2b", "RCPR-A.com", proof(timedelta(days=6)))       # the backfill never selects it
    await _seed(pg, "3", "rcpr-b.com", proof(timedelta(days=9)))          # lapsed: ignored
    await _seed(pg, "4", "rcpr-b.com", None)                              # revoked (JSON null)
    await _seed(pg, "5", "rcpr-c.com", ...)                               # never proven
    await _seed(pg, "6", "rcpr-c.com", proof(timedelta(days=1)), status="inactive")
    await _seed(pg, "7", "rcpr-d.com", "not an object")
    got = await refresh.select_oldest_valid_mirror_proofs(pg, now=NOW, max_age=timedelta(days=7))
    ours = {d: t for d, t in got.items() if "rcpr-" in d}
    assert ours == {"rcpr-a.com": NOW - timedelta(days=2), "www.rcpr-a.com": NOW - timedelta(days=5)}
    order = refresh.order_mirror_domains(["rcpr-a.com", "rcpr-b.com"], ours, {})
    assert order == ["rcpr-b.com", "rcpr-a.com"], "no proof at all sorts before an aging one"
    # The backfill's own selection agrees about which seeds belong to rcpr-a.com.
    from scripts.backfill_shopify_variant_ids import select_candidates

    picked = {r["domain"] for r in await select_candidates(limit=50, domain="rcpr-a.com")}
    assert picked == {"rcpr-a.com", "www.rcpr-a.com"}, "RCPR-A.com is neither walked nor counted"


async def test_the_real_mirror_lane_checkpoints_and_the_next_run_resumes(pg):
    import jobs.reap_cart_proof_refresh as refresh

    class Writer:
        GLOBAL_MIN_INTERVAL_S = 0.0
        PER_DOMAIN_MIN_GAP_S = 0.0
        CONSECUTIVE_BLOCK_ABORT = 8

        def __init__(self, pages):
            self.pages = pages
            self.calls = []

        async def run(self, **kwargs):
            self.calls.append((kwargs["domain"], kwargs["after"]))
            return self.pages.pop(0)

    full = {"candidates": refresh.MIRROR_PAGE_SIZE, "aborted_on_block": False}
    first = Writer([{**full, "next_cursor": "epsv_010"}, {**full, "next_cursor": "epsv_020"},
                    {**full, "aborted_on_block": True, "next_cursor": None}])
    plan = refresh.LanePlan(lane="mirror", domains=["rcpr-a.com"], writer=first, gap_s=0.0,
                            proof_max_age=timedelta(days=7))
    state = refresh.RunState()
    # run_lane connects and disconnects the database it is given; keep the fixture's connection.
    await refresh.run_lane(plan, apply=True, budget_s=60, emit=lambda line: None, state=state, db=_KeepOpen(pg),
                           now=lambda: NOW)
    assert state.results["rcpr-a.com"].status == refresh.ABORTED
    import db.reap_cart_proof_refresh_cursors as cursors

    row = (await cursors.load(pg, "mirror", table_must_exist=True))["rcpr-a.com"]
    assert row.next_cursor == "epsv_020" and row.last_status == refresh.ABORTED and row.last_completed_at is None
    assert row.blocked_until == NOW + refresh.BLOCK_BACKOFF, "the store that blocked us is backed off"

    # Inside the back-off the store is not walked at all.
    idle = Writer([])
    plan.writer = idle
    skipped = refresh.RunState()
    await refresh.run_lane(plan, apply=True, budget_s=60, emit=lambda line: None, state=skipped,
                           db=_KeepOpen(pg), now=lambda: NOW + timedelta(days=1))
    assert idle.calls == [] and skipped.results["rcpr-a.com"].status == refresh.BACKED_OFF

    second = Writer([{"candidates": 3, "aborted_on_block": False, "next_cursor": "epsv_023"}])
    plan.writer = second
    later = NOW + refresh.BLOCK_BACKOFF + timedelta(hours=1)
    await refresh.run_lane(plan, apply=True, budget_s=60, emit=lambda line: None, state=refresh.RunState(),
                           db=_KeepOpen(pg), now=lambda: later)
    assert second.calls == [("rcpr-a.com", "epsv_020")], "after the back-off it resumes from its cursor"
    row = (await cursors.load(pg, "mirror", table_must_exist=True))["rcpr-a.com"]
    assert row.next_cursor is None and row.last_status == refresh.DONE and row.blocked_until is None
    assert row.last_completed_at == later


class _KeepOpen:
    """The fixture's database, with connect/disconnect made no-ops."""

    def __init__(self, db):
        self._db = db

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    def __getattr__(self, name):
        return getattr(self._db, name)

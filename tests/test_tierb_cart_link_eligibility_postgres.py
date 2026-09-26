"""Tier B cart-link eligibility against REAL Postgres — the production dialect gate.

Picked up by .github/workflows/postgres-dialect-gate.yml via the `tests/test_*_postgres.py`
glob. Three things here, the first two of which SQLite cannot show:

  1. THE SELF-HEAL BUILDS THE SAME SCHEMA AS MIGRATION 228, compared through the CATALOG
     (columns, `pg_indexes.indexdef`, `pg_get_constraintdef`) rather than the source text:
     production never runs db/migrations, so the self-heal IS the production schema.
  2. PREPARE: every Postgres SQL constant in db/tierb_cart_link_eligibility.py is planned.
  3. EVERY DATABASE CASE of the two dialect-agnostic suites, re-run here (imported below), so
     the gate — which runs only `*_postgres.py` — exercises the storage rules and the mocked
     end-to-end job on Postgres, not just on SQLite.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_tierb_elig_test \\
        .venv/bin/python -m pytest tests/test_tierb_cart_link_eligibility_postgres.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
# This module DROPS its table, so it must be incapable of running anywhere but a throwaway
# database — decided before any fixture (the imported autouse ones included) can run.
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
_THROWAWAY = any(m in _DBNAME for m in ("dialect_check", "_test", "test_"))

pytestmark = pytest.mark.skipif(
    not (_IS_PG and _THROWAWAY),
    reason=(
        "needs a Postgres DATABASE_URL naming a THROWAWAY database (…_test, test_…, "
        "…dialect_check…) — this is the production-dialect gate; see the docstring"
    ),
)

# The database cases of both dialect-agnostic suites. Their fixtures come with them: the storage
# suite's autouse fixture rebuilds the table for every test in THIS module too.
from tests.test_tierb_cart_link_eligibility import *  # noqa: E402,F401,F403
from tests.test_tierb_cart_link_eligibility import eligibility_db  # noqa: E402,F401
from tests.test_tierb_cart_link_eligibility_job import (  # noqa: E402,F401
    _no_real_network,
    db,
    test_a_dry_run_writes_nothing,
    test_end_to_end_records_definite_verdicts_and_keeps_a_prior_one_on_a_transport_error,
)

_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/228_tierb_cart_link_eligibility.sql"
_DOWN = Path(__file__).resolve().parent.parent / "db/migrations/down/228_tierb_cart_link_eligibility_down.sql"
#: 232 WIDENS 228's verdict vocabulary (NO_CARD_PAYMENT, PRICE_DRIFT). It belongs in the parity
#: build for exactly the reason the reap gate's list gives for 227: the parity test compares what
#: the MIGRATIONS built against what the SELF-HEAL built, and the self-heal carries 232 — both the
#: widened CREATE TABLE in db/tierb_cart_link_eligibility_schema.py and the in-place widening in
#: db/schema_guard.py. Without it here, parity fails for a change that is correct.
#:
#: THE RULE THIS FILE LEARNED: when a later migration ALTERS an earlier table, the earlier table's
#: own parity suite is what catches it — not the CI-order subset, which does not run this file.
_MIGRATION_232 = Path(__file__).resolve().parent.parent / "db/migrations/232_tierb_verdict_vocabulary.sql"
_MIGRATIONS = (_MIGRATION, _MIGRATION_232)
_TABLE = "tierb_cart_link_eligibility"

#: The two verdicts 232 adds. Named here so the control below can fail for a REASON.
_WIDENED_VERDICTS = ("NO_CARD_PAYMENT", "PRICE_DRIFT")


async def _apply(path: Path) -> None:
    from db.database import database
    from db.sql_migrations import split_statements

    for statement in split_statements(path.read_text(encoding="utf-8")):
        await database.execute(statement)


async def _schema_fingerprint():
    from db.database import database

    columns = await database.fetch_all(
        """
        SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = :t
         ORDER BY column_name
        """,
        {"t": _TABLE},
    )
    indexes = await database.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t",
        {"t": _TABLE},
    )
    constraints = await database.fetch_all(
        """
        SELECT c.conname AS name, CAST(c.contype AS text) AS kind, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'public' AND t.relname = :t
        """,
        {"t": _TABLE},
    )

    def norm(text: str) -> str:
        return " ".join((text or "").split())

    # CHECK constraint NAMES differ between builds only when unnamed (Postgres numbers them), so
    # unnamed checks are compared by definition; named ones by name AND definition.
    return (
        [tuple(dict(r).values()) for r in columns],
        {r["indexname"]: norm(r["indexdef"]) for r in indexes},
        sorted((r["kind"], norm(r["def"])) for r in constraints),
        sorted(r["name"] for r in constraints if r["name"].startswith(("ck_", "uq_"))),
    )


async def test_the_self_heal_builds_the_same_schema_as_migration_228():
    from db.database import database
    from db.tierb_cart_link_eligibility_schema import ensure_schema

    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    for migration in _MIGRATIONS:
        await _apply(migration)
    from_migration = await _schema_fingerprint()
    columns, indexes, constraints, names = from_migration
    assert len(columns) == 18, columns
    assert any("UNIQUE" in d.upper() and "(shop_domain, market)" in d for d in indexes.values()), indexes
    assert sum(1 for kind, _ in constraints if kind == "c") == 6, constraints
    assert "checkout_country" in {c[0] for c in columns}
    assert all("CHECKOUT_MARKET_MISMATCH" in d for k, d in constraints if k == "c" and "ELIGIBLE" in d)
    # 232's widening, on the MIGRATION side. Both verdict CHECKs must carry both new members:
    # if this ever fails, the migration list above has drifted from what the self-heal builds.
    verdict_checks = [d for k, d in constraints if k == "c" and "ELIGIBLE" in d]
    assert len(verdict_checks) == 2, verdict_checks
    for definition in verdict_checks:
        for verdict in _WIDENED_VERDICTS:
            assert verdict in definition, f"migration 232 did not widen: {definition}"
    assert names == ["ck_tierb_cart_link_eligibility_verdict_has_clock",
                     "uq_tierb_cart_link_eligibility_shop_market"]

    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    await ensure_schema()
    from_self_heal = await _schema_fingerprint()

    assert from_self_heal[0] == from_migration[0], "columns differ between self-heal and migration"
    assert from_self_heal[1] == from_migration[1], "indexes differ between self-heal and migration"
    assert from_self_heal[2] == from_migration[2], "constraints differ between self-heal and migration"
    assert from_self_heal[3] == from_migration[3], "constraint names differ"


async def test_a_228_only_build_FAILS_this_parity_and_names_the_check():
    """THE CONTROL. Without it the parity test above could pass vacuously — it passed for months
    while 228 was the only migration, and it went on passing the moment 232 shipped only because
    the assertion was comparing two builds that happened to agree.

    A migration side built from 228 ALONE must differ from the self-heal, and the difference must
    be THE VERDICT CHECK, named: the widened members are present on one side and absent on the
    other. A control that merely asserted "they differ" would also pass if the two builds
    disagreed about something else entirely.
    """
    from db.database import database
    from db.tierb_cart_link_eligibility_schema import ensure_schema

    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    await _apply(_MIGRATION)  # 228 ONLY — deliberately not _MIGRATIONS
    narrow = await _schema_fingerprint()

    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    await ensure_schema()
    wide = await _schema_fingerprint()

    assert wide[2] != narrow[2], (
        "a 228-only migration build is indistinguishable from the self-heal — either 232 stopped "
        "widening anything, or the fingerprint stopped looking at CHECK constraints, and in "
        "both cases the parity test above proves nothing"
    )

    def _verdict_checks(fingerprint):
        return [d for kind, d in fingerprint[2] if kind == "c" and "ELIGIBLE" in d]

    narrow_checks, wide_checks = _verdict_checks(narrow), _verdict_checks(wide)
    assert len(narrow_checks) == len(wide_checks) == 2
    for verdict in _WIDENED_VERDICTS:
        assert all(verdict not in d for d in narrow_checks), (
            f"228 alone already admits {verdict} — 232 is not what widened it"
        )
        assert all(verdict in d for d in wide_checks), (
            f"the self-heal does not admit {verdict}"
        )

    # And everything OTHER than the verdict vocabulary must already agree, so the control is
    # pointing at the CHECK and not at some unrelated drift.
    assert wide[0] == narrow[0], "columns should not differ between 228 and the self-heal"
    assert wide[1] == narrow[1], "indexes should not differ between 228 and the self-heal"
    assert wide[3] == narrow[3], "constraint names should not differ"


async def test_the_schema_guard_heals_a_228_shaped_table_to_the_widened_vocabulary():
    """The OTHER arrival path, and the one production takes: a table that already exists in
    228's shape. `CREATE TABLE IF NOT EXISTS` does nothing there, so only the in-place widening
    in db/schema_guard.py can fix it — measured, because the first cut of 232 had no self-heal
    and left production refusing every NO_CARD_PAYMENT write."""
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    await _apply(_MIGRATION)  # 228 only: production's shape before this PR
    before = [d for k, d in (await _schema_fingerprint())[2] if k == "c" and "ELIGIBLE" in d]
    assert all(v not in d for d in before for v in _WIDENED_VERDICTS), "precondition: narrow"

    await ensure_required_schema_light()

    after = [d for k, d in (await _schema_fingerprint())[2] if k == "c" and "ELIGIBLE" in d]
    for verdict in _WIDENED_VERDICTS:
        assert all(verdict in d for d in after), f"the heal did not widen for {verdict}"
    # Behaviourally, not just in the catalog.
    await database.execute(
        f"INSERT INTO {_TABLE} (shop_domain, market, verdict, checked_at) "
        "VALUES ('judydoll.com', 'US', 'NO_CARD_PAYMENT', now())"
    )


async def test_the_schema_guard_builds_it_too_and_the_unique_key_holds():
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    await ensure_required_schema_light()
    await database.execute(f"INSERT INTO {_TABLE} (shop_domain, market) VALUES ('judydoll.com', 'US')")
    with pytest.raises(Exception, match="(?i)unique|duplicate"):
        await database.execute(f"INSERT INTO {_TABLE} (shop_domain, market) VALUES ('judydoll.com', 'US')")


async def test_the_down_migration_removes_the_table_and_the_up_migration_rebuilds_it():
    from db.database import database

    await _apply(_DOWN)
    row = await database.fetch_one("SELECT to_regclass(:t) AS r", {"t": f"public.{_TABLE}"})
    assert row["r"] is None
    await _apply(_MIGRATION)
    row = await database.fetch_one("SELECT to_regclass(:t) AS r", {"t": f"public.{_TABLE}"})
    assert row["r"] is not None


def _module_sql_constants():
    import db.tierb_cart_link_eligibility as elig

    return {n: v for n, v in vars(elig).items() if n.endswith("_SQL") and isinstance(v, str)}


_BIND = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


async def test_every_postgres_sql_constant_prepares():
    import asyncpg

    constants = _module_sql_constants()
    assert set(constants) == {
        "_UPSERT_DEFINITE_SQL", "_UPSERT_INDEFINITE_SQL", "_SELECT_ROW_SQL", "_SELECT_ELIGIBLE_SQL",
    }, sorted(constants)
    conn = await asyncpg.connect(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    failures = []
    try:
        for name, sql in sorted(constants.items()):
            order: list = []

            def positional(match):
                if match.group(1) not in order:
                    order.append(match.group(1))
                return f"${order.index(match.group(1)) + 1}"

            try:
                await conn.prepare(_BIND.sub(positional, sql))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
    finally:
        await conn.close()
    assert not failures, "\n".join(failures)

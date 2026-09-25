"""ensure_ugc_tables_exist() must build the UGC tables it finds missing, on the production dialect.

    TZ=UTC DATABASE_URL=postgresql://postgres@127.0.0.1:5432/pivota_ugccreate_test \\
        .venv/bin/python -m pytest tests/test_ugc_tables_create_postgres.py

WHAT WAS WRONG. Each "table missing" branch (buyer_review_user_subject, ugc_questions,
ugc_question_replies) sent its CREATE TABLE and every CREATE INDEX as ONE string to
`database.execute()`. `databases` 0.7.0 runs execute() through asyncpg's prepared-statement path,
and Postgres refuses it: "cannot insert multiple commands into a prepared statement". The branch
had no try, so on a database without the tables every UGC call raised, and the tables were never
built. SQLite would not show it (its test databases get the tables from create_all), so this
runs on Postgres only.

WHAT THIS PINS. From a schema holding only product_reviews (the one table the branches reference
without creating), one call builds all three tables, their foreign keys and exactly the indexes
the branches declare, each index with its definition (unique, partial, DESC) intact. A second
call, now on the "tables present" path, changes nothing and raises nothing.

ISOLATION. Everything runs in a per-test scratch schema, through a `Database` whose connections
set search_path to that schema ALONE, so nothing resolves to a table a sibling gate file built.
The module's `database` is swapped for that one for the duration of the test.

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process).
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Dict, List, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — only Postgres refuses a multi-command prepared statement",
)

assert "main" not in sys.modules or not _IS_PG, (
    "main is imported: it registers every model on the shared metadata at collection time "
    "and poisons later create_all calls in the Postgres gate"
)

# Same convention as the sibling gates: this creates and drops schemas, so it must be
# incapable of running anywhere but a throwaway database (CI's is pivota_dialect_check).
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

_UGC_TABLES = ("buyer_review_user_subject", "ugc_questions", "ugc_question_replies")

#: Every index the three CREATE branches declare, as pg_indexes renders it ({s} = the schema).
#: Primary keys included: they come with the CREATE TABLE and must be there too.
_EXPECTED_INDEXES = {
    "buyer_review_user_subject_pkey":
        "CREATE UNIQUE INDEX buyer_review_user_subject_pkey ON {s}.buyer_review_user_subject "
        "USING btree (id)",
    "idx_buyer_review_user_subject_user":
        "CREATE INDEX idx_buyer_review_user_subject_user ON {s}.buyer_review_user_subject "
        "USING btree (user_id)",
    "idx_buyer_review_user_subject_subject":
        "CREATE INDEX idx_buyer_review_user_subject_subject ON {s}.buyer_review_user_subject "
        "USING btree (subject_type, subject_id)",
    "idx_buyer_review_user_subject_order_id":
        "CREATE INDEX idx_buyer_review_user_subject_order_id ON {s}.buyer_review_user_subject "
        "USING btree (order_id)",
    "ux_buyer_review_user_subject_order":
        "CREATE UNIQUE INDEX ux_buyer_review_user_subject_order ON {s}.buyer_review_user_subject "
        "USING btree (user_id, subject_type, subject_id, order_id)",
    "ux_buyer_review_user_subject_legacy_null_order":
        "CREATE UNIQUE INDEX ux_buyer_review_user_subject_legacy_null_order "
        "ON {s}.buyer_review_user_subject USING btree (user_id, subject_type, subject_id) "
        "WHERE (order_id IS NULL)",
    "ugc_questions_pkey":
        "CREATE UNIQUE INDEX ugc_questions_pkey ON {s}.ugc_questions USING btree (id)",
    "idx_ugc_questions_user_created":
        "CREATE INDEX idx_ugc_questions_user_created ON {s}.ugc_questions "
        "USING btree (user_id, created_at DESC)",
    "idx_ugc_questions_subject_created":
        "CREATE INDEX idx_ugc_questions_subject_created ON {s}.ugc_questions "
        "USING btree (subject_type, subject_id, created_at DESC)",
    "ugc_question_replies_pkey":
        "CREATE UNIQUE INDEX ugc_question_replies_pkey ON {s}.ugc_question_replies USING btree (id)",
    "idx_ugc_question_replies_question_created":
        "CREATE INDEX idx_ugc_question_replies_question_created ON {s}.ugc_question_replies "
        "USING btree (question_id, created_at DESC)",
    "idx_ugc_question_replies_user_created":
        "CREATE INDEX idx_ugc_question_replies_user_created ON {s}.ugc_question_replies "
        "USING btree (user_id, created_at DESC)",
}

#: (table, referenced table, ON DELETE action) for each foreign key the branches declare.
_EXPECTED_FOREIGN_KEYS = [
    ("buyer_review_user_subject", "product_reviews", "c"),
    ("ugc_question_replies", "ugc_questions", "c"),
]


@pytest.fixture
async def _scratch(monkeypatch):
    """(Database on a fresh scratch schema holding only product_reviews, its schema name)."""
    from databases import Database

    import services.ugc_capabilities_service as ugc

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in {dbname!r} — throwaway only")

    schema = f"ugc_create_{uuid.uuid4().hex[:10]}"
    db = Database(
        DATABASE_URL, min_size=1, max_size=2, server_settings={"search_path": f'"{schema}"'}
    )
    await db.connect()
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        assert await db.fetch_val("SELECT current_schema()") == schema, "search_path did not take"
        await db.execute("CREATE TABLE product_reviews (id BIGSERIAL PRIMARY KEY)")
        monkeypatch.setattr(ugc, "database", db)
        yield db, schema
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.disconnect()


async def _tables(db, schema: str) -> List[str]:
    rows = await db.fetch_all(
        "SELECT tablename FROM pg_tables WHERE schemaname = :s ORDER BY tablename", {"s": schema}
    )
    return [r["tablename"] for r in rows]


async def _indexes(db, schema: str) -> Dict[str, str]:
    rows = await db.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s AND tablename <> 'product_reviews'",
        {"s": schema},
    )
    return {r["indexname"]: r["indexdef"] for r in rows}


async def _foreign_keys(db, schema: str) -> List[Tuple[str, str, str]]:
    rows = await db.fetch_all(
        """
        SELECT c.conrelid::regclass::text AS t, c.confrelid::regclass::text AS ref,
               c.confdeltype::text AS on_delete
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE n.nspname = :s AND c.contype = 'f'
        ORDER BY 1, 2
        """,
        {"s": schema},
    )
    return [(r["t"], r["ref"], r["on_delete"]) for r in rows]


async def test_ensure_ugc_tables_exist_builds_all_three_tables_and_their_indexes(_scratch):
    from services.ugc_capabilities_service import ensure_ugc_tables_exist

    db, schema = _scratch
    assert await _tables(db, schema) == ["product_reviews"], "precondition: only product_reviews"

    await ensure_ugc_tables_exist()

    assert await _tables(db, schema) == sorted(("product_reviews",) + _UGC_TABLES)
    assert await _indexes(db, schema) == {
        name: sql.format(s=schema) for name, sql in _EXPECTED_INDEXES.items()
    }
    assert await _foreign_keys(db, schema) == _EXPECTED_FOREIGN_KEYS


async def test_a_second_call_on_the_built_tables_changes_nothing(_scratch):
    from services.ugc_capabilities_service import ensure_ugc_tables_exist

    db, schema = _scratch
    await ensure_ugc_tables_exist()
    built = (await _tables(db, schema), await _indexes(db, schema), await _foreign_keys(db, schema))

    await ensure_ugc_tables_exist()

    assert (
        await _tables(db, schema), await _indexes(db, schema), await _foreign_keys(db, schema)
    ) == built

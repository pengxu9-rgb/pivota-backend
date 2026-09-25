"""Every identity-conflict review task must actually land in `pdp_review_tasks`.

THE DEFECT THIS GATE EXISTS FOR. `enqueue_audit_identity_review` built its row with
`pdp_review_tasks.insert().values(...)` and did not name `qa_sample`. The Table declares
`qa_sample` as `nullable=False, default=False` — a PYTHON-SIDE default. On
databases==0.7.0 the statement is compiled and bound by `databases`, not executed through a
SQLAlchemy connection, so the Python-side default is never evaluated: the compiler still
lists the column (a prefetch default needs a bind), and `construct_params()` binds it as
None. Postgres receives an EXPLICIT NULL — which no column DEFAULT replaces, and prod's
table has none anyway (created by `create_all`, so migration 179's `DEFAULT FALSE` never
applied; information_schema, 2026-09-22) — and rejects it:

    null value in column "qa_sample" of relation "pdp_review_tasks" violates not-null constraint

The helper is best-effort (logs at WARNING, returns None), so intake proceeded and the review
task was silently lost — every FLAG from intake_identity._flag_review and every SKIP from
apply_intake_brand_fragmentation_guard, from every intake door. Prod evidence: curated apply
job oneoff-29431-10355 (2026-09-18, Haruharu Wonder at ohlolly.com), 13 of 13 enqueues failed;
prod has never held a single `audit_intake` review task (40 FLAG/SKIP events, 0 tasks).

No faked-DB test can see this: a double accepts whatever it is handed. The existing unit tests
monkeypatch the helper away entirely.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_audit_identity_review_enqueue_postgres.py

🚨 THESE GATE FILES SHARE ONE DATABASE. Additive, order-proof DDL only (#1651); never DROP.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN = uuid.uuid4().hex[:8]
PREFIX = f"qasample_{RUN}"


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.sql_migrations import split_statements

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Built from migration 179 — the migration that owns pdp_review_tasks — rather than
    # restated here. It carries a DEFAULT FALSE that prod lacks; the explicit NULL fails
    # either way, so the gate is no weaker for it.
    # Only the pdp_review_tasks statements: the rest of 179 touches tables this gate does
    # not need, and its BEGIN/COMMIT must not run on the process-shared connection. Match
    # on the DDL, not the bare name: the file's header comment names the table too, and
    # that chunk ends in BEGIN.
    migration = (REPO_ROOT / "db" / "migrations" / "179_identity_resolution_d2.sql").read_text(
        encoding="utf-8"
    )
    for statement in split_statements(migration):
        if re.search(r"(TABLE IF NOT EXISTS|\bON) pdp_review_tasks\b", statement):
            await database.execute(statement)
    try:
        yield database
    finally:
        await database.execute(
            "DELETE FROM pdp_review_tasks WHERE pdp_id LIKE :p", {"p": f"{PREFIX}%"}
        )
        # `databases` shares one connection across the process; leave it as found.
        if not was_connected:
            await database.disconnect()


async def _row(task_id: str):
    from db.database import database

    return await database.fetch_one(
        "SELECT * FROM pdp_review_tasks WHERE id = :id", {"id": task_id}
    )


async def test_enqueue_audit_identity_review_lands_a_row():
    """The real helper, the real table: the returned id must name a stored row."""
    from services.audit_index_intake import enqueue_audit_identity_review

    task_id = await enqueue_audit_identity_review(
        {"product_key": f"{PREFIX}_audit", "content_key": "ck_audit"},
        {
            "product_key": f"{PREFIX}_candidate",
            "matcher": "gtin_match_brand_title_drift",
            "confidence": None,
            "evidence": {"door": "curated_apply"},
        },
    )

    # None is the swallowed-exception answer; that is the prod failure, not a pass.
    assert task_id is not None, "enqueue swallowed an insert failure (see WARNING log)"
    row = await _row(task_id)
    assert row is not None
    assert row["module_key"] == "identity"
    assert row["status"] == "needs_review"
    assert row["priority"] == "normal"
    assert row["qa_sample"] is False


async def test_flag_review_from_intake_identity_lands_a_row():
    """The FLAG caller (gtin_match_brand_title_drift / brand_title_collision) end to end."""
    from db.database import database
    from services.intake_identity import _flag_review

    pdp_id = f"{PREFIX}_flag"
    await _flag_review(
        "curated_apply",
        {"product_key": pdp_id},
        "ck_flag",
        "brand_title_collision",
        {"conflict_product_key": f"{PREFIX}_other"},
    )

    row = await database.fetch_one(
        "SELECT qa_sample, checklist->>'source' AS source, checklist->>'matcher' AS matcher "
        "FROM pdp_review_tasks WHERE pdp_id = :p",
        {"p": pdp_id},
    )
    assert row is not None, "FLAG review task was lost"
    assert row["qa_sample"] is False
    assert row["source"] == "audit_intake"
    assert row["matcher"] == "brand_title_collision"


async def test_sweep_review_task_sql_lands_a_row():
    """The other pdp_review_tasks writer outside the governance service. It is raw asyncpg
    SQL that names qa_sample explicitly, so it never had this defect — pinned so it keeps
    naming every NOT NULL column it relies on."""
    import asyncpg

    from services.identity_reconcile_sweep import ENQUEUE_REVIEW_TASK_SQL

    task_id = f"pdptask_ir_{PREFIX}"
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        returned = await conn.fetchval(
            ENQUEUE_REVIEW_TASK_SQL, task_id, f"{PREFIX}_sweep", "{}", "[]"
        )
    finally:
        await conn.close()
    assert returned == task_id
    row = await _row(task_id)
    assert row is not None and row["qa_sample"] is False

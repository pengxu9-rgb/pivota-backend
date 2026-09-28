"""The dedup sweep never auto-suppresses the served row: the hold SQL, executed on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_identity_served_row_hold_postgres.py

#2423 held a same_url_dup / junk_url proposal at APPROVE time when a row it would suppress is the
content_key's served row (the catalog_products row carrying agent_pdp_view's signature). Its tests
asserted on the SQL text only. `apply_approved` then applied every 'approved' proposal with no such
check, so an approval that predates the guard, or one whose served row moved onto a loser after a
drift skip left it 'approved', would still suppress the listing being served.

This file EXECUTES APPROVE_ALLOWLIST_SQL, HELD_AUTO_APPROVE_SQL, REVIEW_HELD_SQL and the apply guard
(SERVED_LOSER_SQL + HOLD_FOR_REVIEW_SQL) through the real sweep and engine functions: the correlated
`= ANY(p.subject_product_keys)` over TEXT[], the scalar `(... LIMIT 1)` subquery, and the JSONB merge.

ISOLATION as in test_external_seed_freshness_census_postgres.py: a per-process scratch schema with the
REAL migrations, reached through connections whose search_path is that schema ALONE, dropped at
teardown. The dialect gate's database is `pivota_dialect_check`; a skip fails the gate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "db/migrations"
_TABLE_MIGRATIONS = (
    "058_catalog_core.sql",                          # catalog_products
    "071_pivota_canonical_pdp.sql",                  # + pivota_signature_id (unique)
    "083_catalog_products_content_key.sql",          # + content_key
    "135_catalog_product_sku_stale_suppression.sql",  # + suppression_reason / _at / _metadata
    "085_agent_pdp_view.sql",                        # the served signature per content_key
    "044_external_product_seeds.sql",                # apply's seed deactivation
    "179_identity_resolution_d2.sql",                # proposals, events, pdp_review_tasks
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"identity_served_hold_test_{os.getpid()}"
_MERCHANT = "m_hold"
_AUTO = ["same_url_dup", "junk_url"]


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url.replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture()
async def conn():
    import asyncpg

    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    admin = await asyncpg.connect(_url())
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = await asyncpg.connect(_url(), server_settings={"search_path": _SCHEMA})
    try:
        for name in _TABLE_MIGRATIONS:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                # 179's header comment and its BEGIN split into ONE chunk: judge the code, not the comment.
                code = "\n".join(l for l in statement.splitlines() if not l.strip().startswith("--"))
                if code.strip().rstrip(";").upper() in {"", "BEGIN", "COMMIT"}:
                    continue
                await scoped.execute(statement)
        yield scoped
    finally:
        await scoped.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.close()


async def _product(conn, key: str, content_key: str, *, merchant: str = _MERCHANT) -> None:
    """An external_seed row backed by its own active seed (apply's keeper-orphan post-check reads it)."""
    await conn.execute(
        "INSERT INTO external_product_seeds (id, market, destination_url, attached_product_key)"
        " VALUES ($1, 'US', $2, $3)", f"eps_{key}", f"https://shop.example/products/{key}", key)
    await conn.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title,"
        " content_key, pivota_signature_id, source_ref)"
        " VALUES ($1, $2, 'external_seed', $1, 'Serum', $3, $4, $5)",
        key, merchant, content_key, f"sig_{key}", f"eps_{key}",
    )


async def _serve(conn, content_key: str, product_key: str) -> None:
    """agent_pdp_view serves `product_key`'s signature for the content_key (upsert: a re-pick)."""
    await conn.execute(
        "INSERT INTO agent_pdp_view (content_key, title, pivota_signature_id) VALUES ($1, 'Serum', $2)"
        " ON CONFLICT (content_key) DO UPDATE SET pivota_signature_id = EXCLUDED.pivota_signature_id",
        content_key, f"sig_{product_key}",
    )


async def _group(conn, content_key: str, keys, keeper: str, *, strategy: str = "same_url_dup",
                 served: str, status: str = "proposed", decided_by=None):
    """A live same-merchant group, its serving pick, and the engine's own proposal for it."""
    from services.identity_resolution import new_proposal, upsert_proposals

    for k in keys:
        await _product(conn, k, content_key)
    await _serve(conn, content_key, served)
    p = new_proposal(kind="suppress_dup", strategy=strategy, subject_product_keys=keys,
                     keeper_product_key=keeper, merchant_id=_MERCHANT, content_key=content_key,
                     confidence=0.99)
    await upsert_proposals(conn, [p])
    if status != "proposed":
        await conn.execute(
            "UPDATE identity_resolution_proposals SET status = $2, decided_by = $3, decided_at = NOW()"
            " WHERE proposal_id = $1", p["proposal_id"], status, decided_by)
    return p["proposal_id"]


async def _status(conn, pid: str) -> str:
    return await conn.fetchval("SELECT status FROM identity_resolution_proposals WHERE proposal_id = $1", pid)


async def _suppressed(conn, keys) -> list:
    rows = await conn.fetch(
        "SELECT product_key FROM catalog_products WHERE product_key = ANY($1::text[])"
        " AND suppression_reason IS NOT NULL ORDER BY product_key", list(keys))
    return [r["product_key"] for r in rows]


async def _task(conn, pid: str):
    row = await conn.fetchrow("SELECT * FROM pdp_review_tasks WHERE id = $1", f"pdptask_ir_{pid}")
    if row is None:
        return None
    checklist = row["checklist"]
    return {**dict(row), "checklist": json.loads(checklist) if isinstance(checklist, str) else checklist}


@pytest.mark.asyncio
async def test_approve_holds_only_a_proposal_that_would_suppress_the_served_row(conn):
    from services.identity_reconcile_sweep import (
        APPROVE_ALLOWLIST_SQL, HELD_AUTO_APPROVE_SQL, REVIEW_HELD_SQL, _enqueue_review_tasks)

    loser_served = await _group(conn, "ck_loser", ["a1", "a2"], "a1", served="a2")
    keeper_served = await _group(conn, "ck_keeper", ["b1", "b2"], "b1", served="b1")
    # The content_key serves ANOTHER merchant's row, outside the proposal: that holds nothing.
    other_served = await _group(conn, "ck_other", ["c1", "c2"], "c1", served="c1", strategy="junk_url")
    await _product(conn, "c_elsewhere", "ck_other", merchant="m_other")
    await _serve(conn, "ck_other", "c_elsewhere")
    # Not a mechanical strategy: the allowlist never reaches it, served or not.
    judged = await _group(conn, "ck_judge", ["d1", "d2"], "d1", served="d2", strategy="tier3_judge")

    approved = {r["proposal_id"] for r in await conn.fetch(APPROVE_ALLOWLIST_SQL, _AUTO)}
    assert approved == {keeper_served, other_served}
    assert await _status(conn, loser_served) == "proposed"
    assert await _status(conn, judged) == "proposed"
    assert (await conn.fetchrow(HELD_AUTO_APPROVE_SQL, _AUTO))["n"] == 1

    [held] = await conn.fetch(REVIEW_HELD_SQL, _AUTO)
    assert (held["proposal_id"], held["hold_reason"], held["served_product_key"]) == (
        loser_served, "a_loser_is_served", "a2")

    enqueued = await _enqueue_review_tasks(conn)
    assert f"pdptask_ir_{loser_served}" in enqueued
    task = await _task(conn, loser_served)
    assert task["module_key"] == "identity" and task["status"] == "needs_review"
    assert task["checklist"]["hold_reason"] == "a_loser_is_served"
    assert task["checklist"]["served_product_key"] == "a2"
    assert await _task(conn, keeper_served) is None


@pytest.mark.asyncio
async def test_apply_holds_an_approved_proposal_whose_loser_is_now_served(conn):
    """The gap after #2423: approved before the guard (or before the serving pick moved), then applied."""
    from services.identity_reconcile_sweep import REVIEW_HELD_SQL, _enqueue_review_tasks
    from services.identity_resolution import apply_approved

    stale = await _group(conn, "ck_stale", ["s1", "s2", "s3"], "s1", served="s3",
                         status="approved", decided_by="sweep_auto_allowlist")
    fine = await _group(conn, "ck_fine", ["f1", "f2"], "f1", served="f1",
                        status="approved", decided_by="sweep_auto_allowlist")

    result = await apply_approved(conn, run_id="RUN_HOLD", strategies=_AUTO)

    assert result["applied"] == [fine]
    assert result["skipped"] == [(stale, "a_loser_is_served")]
    assert await _suppressed(conn, ["s1", "s2", "s3"]) == []  # the served row, and its siblings, stay live
    assert await _suppressed(conn, ["f1", "f2"]) == ["f2"]
    assert await _status(conn, fine) == "applied"
    seeds = {r["id"]: r["status"] for r in await conn.fetch(
        "SELECT id, status FROM external_product_seeds WHERE id LIKE 'eps_s%' OR id LIKE 'eps_f%'")}
    assert seeds == {"eps_s1": "active", "eps_s2": "active", "eps_s3": "active",
                     "eps_f1": "active", "eps_f2": "inactive"}

    row = await conn.fetchrow(
        "SELECT status, decided_by, decided_at, evidence FROM identity_resolution_proposals"
        " WHERE proposal_id = $1", stale)
    assert (row["status"], row["decided_by"], row["decided_at"]) == ("proposed", None, None)
    held = json.loads(row["evidence"])["apply_held"]
    assert (held["reason"], held["served_product_key"], held["run_id"]) == ("a_loser_is_served", "s3", "RUN_HOLD")
    assert held["was_decided_by"] == "sweep_auto_allowlist" and held["was_decided_at"]
    events = await conn.fetch(
        "SELECT action, detail FROM identity_resolution_events WHERE proposal_id = $1", stale)
    assert [e["action"] for e in events] == ["held"]
    assert json.loads(events[0]["detail"])["served_product_key"] == "s3"

    # Back on the review rail #2423 built, with the reason on the task.
    assert [r["proposal_id"] for r in await conn.fetch(REVIEW_HELD_SQL, _AUTO)] == [stale]
    await _enqueue_review_tasks(conn)
    task = await _task(conn, stale)
    assert task["checklist"]["hold_reason"] == "a_loser_is_served"
    assert task["checklist"]["served_product_key"] == "s3"
    assert task["checklist"]["evidence"]["apply_held"]["run_id"] == "RUN_HOLD"

    # A second apply finds nothing to retry: the held proposal is no longer 'approved'.
    again = await apply_approved(conn, run_id="RUN_AGAIN", strategies=_AUTO)
    assert again["applied"] == [] and again["skipped"] == []


@pytest.mark.asyncio
async def test_apply_still_suppresses_when_the_served_row_is_the_keeper_or_outside_the_group(conn):
    from services.identity_resolution import apply_approved

    keeper = await _group(conn, "ck_k", ["k1", "k2"], "k1", served="k1",
                          status="approved", decided_by="sweep_auto_allowlist")
    outside = await _group(conn, "ck_o", ["o1", "o2"], "o1", served="o1", strategy="junk_url",
                           status="approved", decided_by="sweep_auto_allowlist")
    await _product(conn, "o_elsewhere", "ck_o", merchant="m_other")
    await _serve(conn, "ck_o", "o_elsewhere")
    unserved = await _group(conn, "ck_u", ["u1", "u2"], "u1", served="u1",
                            status="approved", decided_by="sweep_auto_allowlist")
    await conn.execute("DELETE FROM agent_pdp_view WHERE content_key = 'ck_u'")

    result = await apply_approved(conn, run_id="RUN_OK", strategies=_AUTO)
    assert sorted(result["applied"]) == sorted([keeper, outside, unserved])
    assert result["skipped"] == []
    assert await _suppressed(conn, ["k1", "k2", "o1", "o2", "u1", "u2"]) == ["k2", "o2", "u2"]

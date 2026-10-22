"""Production-dialect gate for mvp_idempotency_keys expiry (mvp/idempotency.py).

PostgresIdempotencyStore creates its own table (BIGSERIAL, JSONB) and its put()
is an upsert whose DO UPDATE only fires for an expired row, plus a bounded
DELETE ... WHERE id IN (SELECT ... LIMIT n). SQLite cannot run that DDL, so this
module EXECUTES the store against Postgres.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        pytest tests/test_mvp_idempotency_store_postgres.py

Every row this file writes uses a `pgtest_idem_<uuid>` scope, and put()'s cleanup
is scoped, so it never touches another file's rows in the shared gate database.

Never point this at prod.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

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

_SCOPE_PREFIX = "pgtest_idem_"


@pytest.fixture(autouse=True)
async def _db(monkeypatch: pytest.MonkeyPatch):
    from db.database import database
    from mvp.idempotency import PostgresIdempotencyStore

    monkeypatch.delenv("MVP_IDEMPOTENCY_TTL_DAYS", raising=False)
    # Per-test connect: each test runs on a fresh event loop.
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Force the store's own DDL to run against this database.
    PostgresIdempotencyStore._table_ensured = False
    PostgresIdempotencyStore._ensure_lock = None
    await PostgresIdempotencyStore().get(scope=f"{_SCOPE_PREFIX}warmup", key="none")
    yield database
    await database.execute(
        "DELETE FROM mvp_idempotency_keys WHERE scope LIKE :p",
        {"p": f"{_SCOPE_PREFIX}%"},
    )
    PostgresIdempotencyStore._table_ensured = False
    PostgresIdempotencyStore._ensure_lock = None
    if not was_connected and database.is_connected:
        await database.disconnect()


def _scope() -> str:
    return f"{_SCOPE_PREFIX}{uuid.uuid4().hex[:12]}"


async def _age(db, scope: str, key: str, *, days: float) -> None:
    await db.execute(
        "UPDATE mvp_idempotency_keys SET created_at = :ts WHERE scope = :scope AND idem_key = :key",
        {"ts": datetime.now(timezone.utc) - timedelta(days=days), "scope": scope, "key": key},
    )


async def _seed(db, scope: str, key: str, *, days_old: float, value: str = "{}") -> None:
    await db.execute(
        """
        INSERT INTO mvp_idempotency_keys (scope, idem_key, value_json, created_at)
        VALUES (:scope, :key, CAST(:v AS jsonb), :ts)
        """,
        {
            "scope": scope,
            "key": key,
            "v": value,
            "ts": datetime.now(timezone.utc) - timedelta(days=days_old),
        },
    )


async def _keys(db, scope: str) -> set[str]:
    rows = await db.fetch_all(
        "SELECT idem_key FROM mvp_idempotency_keys WHERE scope = :scope", {"scope": scope}
    )
    return {r["idem_key"] for r in rows}


async def test_fresh_duplicate_key_returns_the_original_record(_db) -> None:
    from mvp.idempotency import PostgresIdempotencyStore

    store = PostgresIdempotencyStore()
    scope = _scope()

    first = await store.put(scope=scope, key="k", value={"order_id": "ord_1"})
    second = await store.put(scope=scope, key="k", value={"order_id": "ord_2"})
    got = await store.get(scope=scope, key="k")

    assert first.value == {"order_id": "ord_1"}
    assert second.value == {"order_id": "ord_1"}
    assert got is not None and got.value == {"order_id": "ord_1"}
    assert second.created_at == first.created_at


async def test_expired_key_is_missing_and_a_new_put_stores_the_new_value(_db) -> None:
    from mvp.idempotency import PostgresIdempotencyStore

    store = PostgresIdempotencyStore()
    scope = _scope()
    await store.put(scope=scope, key="k", value={"order_id": "ord_old"})
    await _age(_db, scope, "k", days=8)

    assert await store.get(scope=scope, key="k") is None

    rec = await store.put(scope=scope, key="k", value={"order_id": "ord_new"})
    assert rec.value == {"order_id": "ord_new"}
    assert rec.created_at > datetime.now(timezone.utc) - timedelta(minutes=5)
    got = await store.get(scope=scope, key="k")
    assert got is not None and got.value == {"order_id": "ord_new"}
    row = await _db.fetch_one(
        "SELECT COUNT(*) AS n FROM mvp_idempotency_keys WHERE scope = :scope", {"scope": scope}
    )
    assert row["n"] == 1


async def test_ttl_env_moves_the_expiry_boundary(_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from mvp.idempotency import PostgresIdempotencyStore

    store = PostgresIdempotencyStore()
    scope = _scope()
    await store.put(scope=scope, key="k", value={"v": 1})
    await _age(_db, scope, "k", days=8)

    monkeypatch.setenv("MVP_IDEMPOTENCY_TTL_DAYS", "30")
    assert (await store.get(scope=scope, key="k")) is not None
    monkeypatch.setenv("MVP_IDEMPOTENCY_TTL_DAYS", "999")  # clamps to 30
    assert (await store.get(scope=scope, key="k")) is not None
    monkeypatch.setenv("MVP_IDEMPOTENCY_TTL_DAYS", "garbage")  # falls back to 7
    assert (await store.get(scope=scope, key="k")) is None

    await _age(_db, scope, "k", days=2)
    monkeypatch.setenv("MVP_IDEMPOTENCY_TTL_DAYS", "0")  # clamps to 1
    assert (await store.get(scope=scope, key="k")) is None
    monkeypatch.delenv("MVP_IDEMPOTENCY_TTL_DAYS")
    assert (await store.get(scope=scope, key="k")) is not None


async def test_cleanup_removes_only_expired_rows_of_the_put_scope(_db) -> None:
    from mvp.idempotency import PostgresIdempotencyStore

    scope_a, scope_b = _scope(), _scope()
    for key in ("old1", "old2", "old3"):
        await _seed(_db, scope_a, key, days_old=9)
    for key in ("fresh1", "fresh2"):
        await _seed(_db, scope_a, key, days_old=6.9)
    for key in ("other_old1", "other_old2"):
        await _seed(_db, scope_b, key, days_old=40)

    await PostgresIdempotencyStore().put(scope=scope_a, key="new", value={"v": 1})

    assert await _keys(_db, scope_a) == {"fresh1", "fresh2", "new"}
    assert await _keys(_db, scope_b) == {"other_old1", "other_old2"}


async def test_cleanup_is_bounded_per_put(_db) -> None:
    from mvp.idempotency import EXPIRED_CLEANUP_BATCH, PostgresIdempotencyStore

    scope = _scope()
    total = EXPIRED_CLEANUP_BATCH + 5
    await _db.execute(
        """
        INSERT INTO mvp_idempotency_keys (scope, idem_key, value_json, created_at)
        SELECT :scope, 'old_' || g::text, '{}'::jsonb, :ts
        FROM generate_series(1, :n) AS g
        """,
        {"scope": scope, "ts": datetime.now(timezone.utc) - timedelta(days=10), "n": total},
    )

    store = PostgresIdempotencyStore()
    await store.put(scope=scope, key="new1", value={})
    keys = await _keys(_db, scope)
    assert len([k for k in keys if k.startswith("old_")]) == 5
    assert "new1" in keys

    await store.put(scope=scope, key="new2", value={})
    assert await _keys(_db, scope) == {"new1", "new2"}

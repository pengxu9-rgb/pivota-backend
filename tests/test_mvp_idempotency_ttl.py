"""TTL wiring for PostgresIdempotencyStore (no database needed).

The store's DDL is Postgres-only (BIGSERIAL, JSONB, CAST AS jsonb), so the
behaviour itself (fresh replay, expired-as-missing, bounded cleanup) is
exercised against a real Postgres in tests/test_mvp_idempotency_store_postgres.py.
This file pins the env-var contract and that every statement carries the cutoff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mvp import idempotency as idem_mod
from mvp.idempotency import PostgresIdempotencyStore, idempotency_cutoff, idempotency_ttl_days


@pytest.fixture(autouse=True)
def _reset_store_and_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MVP_IDEMPOTENCY_TTL_DAYS", raising=False)
    PostgresIdempotencyStore._table_ensured = False
    PostgresIdempotencyStore._ensure_lock = None
    yield
    PostgresIdempotencyStore._table_ensured = False
    PostgresIdempotencyStore._ensure_lock = None


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 7),
        ("", 7),
        ("   ", 7),
        ("abc", 7),
        ("7.5", 7),
        ("3", 3),
        (" 14 ", 14),
        ("1", 1),
        ("30", 30),
        ("0", 1),
        ("-5", 1),
        ("31", 30),
        ("365", 30),
    ],
)
def test_ttl_env_is_clamped_and_falls_back(monkeypatch: pytest.MonkeyPatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv("MVP_IDEMPOTENCY_TTL_DAYS", raising=False)
    else:
        monkeypatch.setenv("MVP_IDEMPOTENCY_TTL_DAYS", raw)
    assert idempotency_ttl_days() == expected


def test_ttl_env_is_read_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    assert idempotency_cutoff(now) == now - timedelta(days=7)
    monkeypatch.setenv("MVP_IDEMPOTENCY_TTL_DAYS", "2")
    assert idempotency_cutoff(now) == now - timedelta(days=2)


class _RecordingDb:
    def __init__(self, *, fail_cleanup: bool = False) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_cleanup = fail_cleanup

    async def execute(self, query: str, values=None):  # noqa: ANN001
        self.calls.append(("execute", str(query), dict(values or {})))
        if self.fail_cleanup and "DELETE FROM mvp_idempotency_keys" in str(query):
            raise RuntimeError("cleanup boom")
        return None

    async def fetch_one(self, query: str, values=None):  # noqa: ANN001
        self.calls.append(("fetch_one", str(query), dict(values or {})))
        return None

    def dml(self) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if "CREATE " not in c[1]]


async def test_get_filters_on_the_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _RecordingDb()
    monkeypatch.setattr(PostgresIdempotencyStore, "_try_get_db", lambda self: db)
    monkeypatch.setenv("MVP_IDEMPOTENCY_TTL_DAYS", "3")
    before = datetime.now(timezone.utc)

    assert await PostgresIdempotencyStore().get(scope="refund", key="k1") is None

    [(kind, sql, values)] = db.dml()
    assert kind == "fetch_one"
    assert "created_at >= :cutoff" in sql
    cutoff = values["cutoff"]
    assert before - timedelta(days=3, seconds=5) <= cutoff <= datetime.now(timezone.utc) - timedelta(days=3)


async def test_put_overwrites_only_expired_and_runs_bounded_scoped_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RecordingDb()
    monkeypatch.setattr(PostgresIdempotencyStore, "_try_get_db", lambda self: db)

    rec = await PostgresIdempotencyStore().put(scope="order_create", key="k1", value={"a": 1})
    assert rec.value == {"a": 1}

    insert, cleanup, reread = db.dml()
    assert insert[1].lstrip().startswith("INSERT INTO mvp_idempotency_keys")
    assert "ON CONFLICT (scope, idem_key) DO UPDATE" in insert[1]
    assert "WHERE mvp_idempotency_keys.created_at < :cutoff" in insert[1]
    assert "DO NOTHING" not in insert[1]

    assert "DELETE FROM mvp_idempotency_keys" in cleanup[1]
    assert "scope = :scope AND created_at < :cutoff" in cleanup[1]
    assert "LIMIT :batch" in cleanup[1]
    assert cleanup[2]["scope"] == "order_create"
    assert cleanup[2]["batch"] == idem_mod.EXPIRED_CLEANUP_BATCH == 100
    # One cutoff for the whole put: the overwrite and the cleanup agree on "expired".
    assert cleanup[2]["cutoff"] == insert[2]["cutoff"]

    assert reread[0] == "fetch_one"


async def test_cleanup_failure_never_fails_the_put(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _RecordingDb(fail_cleanup=True)
    monkeypatch.setattr(PostgresIdempotencyStore, "_try_get_db", lambda self: db)

    rec = await PostgresIdempotencyStore().put(scope="refund", key="k2", value={"ok": True})

    assert rec.value == {"ok": True}
    assert any("DELETE FROM mvp_idempotency_keys" in c[1] for c in db.calls)

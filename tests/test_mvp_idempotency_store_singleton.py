from __future__ import annotations

import pytest

from mvp.idempotency import PostgresIdempotencyStore


class _FakeIdempotencyDb:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.fetches = 0

    async def execute(self, query: str, values=None):  # noqa: ANN001
        self.executed.append(str(query))
        return None

    async def fetch_one(self, query: str, values=None):  # noqa: ANN001
        self.fetches += 1
        return None

    @property
    def table_ddl_count(self) -> int:
        return sum("CREATE TABLE IF NOT EXISTS mvp_idempotency_keys" in query for query in self.executed)

    @property
    def index_ddl_count(self) -> int:
        return sum("CREATE INDEX IF NOT EXISTS idx_mvp_idem_scope_time" in query for query in self.executed)


@pytest.fixture(autouse=True)
def _reset_postgres_idempotency_store() -> None:
    PostgresIdempotencyStore._table_ensured = False
    PostgresIdempotencyStore._ensure_lock = None
    yield
    PostgresIdempotencyStore._table_ensured = False
    PostgresIdempotencyStore._ensure_lock = None


@pytest.mark.asyncio
async def test_postgres_idempotency_store_ensures_table_once_same_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeIdempotencyDb()
    store = PostgresIdempotencyStore()
    monkeypatch.setattr(PostgresIdempotencyStore, "_try_get_db", lambda self: db)

    for index in range(5):
        assert await store.get(scope="order_create", key=f"k_{index}") is None

    assert db.table_ddl_count == 1
    assert db.index_ddl_count == 1
    assert db.fetches == 5


@pytest.mark.asyncio
async def test_postgres_idempotency_store_ensures_table_once_across_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeIdempotencyDb()
    monkeypatch.setattr(PostgresIdempotencyStore, "_try_get_db", lambda self: db)

    for index in range(5):
        assert await PostgresIdempotencyStore().get(scope="order_create", key=f"k_{index}") is None

    assert db.table_ddl_count == 1
    assert db.index_ddl_count == 1
    assert db.fetches == 5

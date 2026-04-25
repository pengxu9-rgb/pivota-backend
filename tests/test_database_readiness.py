from __future__ import annotations

import pytest

from utils import database_readiness as readiness


class FakeDatabase:
    def __init__(
        self,
        *,
        connected: bool,
        execute_results=None,
        connect_raises: Exception | None = None,
        disconnect_raises: Exception | None = None,
    ):
        self.is_connected = connected
        self.execute_results = list(execute_results or [])
        self.connect_raises = connect_raises
        self.disconnect_raises = disconnect_raises
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.execute_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_raises is not None:
            raise self.connect_raises
        self.is_connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_raises is not None:
            raise self.disconnect_raises
        self.is_connected = False

    async def execute(self, _query):
        self.execute_calls += 1
        if self.execute_results:
            result = self.execute_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return 1


@pytest.mark.asyncio
async def test_ensure_database_ready_connects_when_startup_left_db_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = FakeDatabase(connected=False, execute_results=[1])
    monkeypatch.setattr(readiness, "database", fake_db)

    await readiness.ensure_database_ready()

    assert fake_db.connect_calls == 1
    assert fake_db.execute_calls == 1
    assert fake_db.is_connected is True


@pytest.mark.asyncio
async def test_ensure_database_ready_reconnects_after_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = FakeDatabase(connected=True, execute_results=[TimeoutError("stale"), 1])
    monkeypatch.setattr(readiness, "database", fake_db)

    await readiness.ensure_database_ready()

    assert fake_db.disconnect_calls == 1
    assert fake_db.connect_calls == 1
    assert fake_db.execute_calls == 2
    assert fake_db.is_connected is True


@pytest.mark.asyncio
async def test_ensure_database_ready_forces_reconnect_when_disconnect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = FakeDatabase(
        connected=True,
        execute_results=[RuntimeError("pool is closed"), 1],
        disconnect_raises=RuntimeError("pool is closed"),
    )
    monkeypatch.setattr(readiness, "database", fake_db)

    await readiness.ensure_database_ready()

    assert fake_db.disconnect_calls == 1
    assert fake_db.connect_calls == 1
    assert fake_db.execute_calls == 2
    assert fake_db.is_connected is True


@pytest.mark.asyncio
async def test_ensure_database_ready_raises_when_connect_cannot_be_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = FakeDatabase(connected=False, connect_raises=TimeoutError("db down"))
    monkeypatch.setattr(readiness, "database", fake_db)

    with pytest.raises(readiness.DatabaseUnavailableError) as exc_info:
        await readiness.ensure_database_ready()

    assert exc_info.value.phase == "connect"
    assert exc_info.value.error_type == "TimeoutError"

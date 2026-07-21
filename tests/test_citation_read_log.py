"""Unit tests for the B④-P1 external citation-read telemetry accessor
(db.citation_read_log) — best-effort writes, disconnect skip, fetch."""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

import db.citation_read_log as crl


class FakeDB:
    def __init__(self, *, connected: bool = True, rows: List[Any] | None = None) -> None:
        self.is_connected = connected
        self.executed: List[Any] = []
        self._rows = rows or []

    async def execute(self, query: Any, params: Any = None) -> None:
        self.executed.append(query)

    async def fetch_all(self, query: Any) -> List[Any]:
        return self._rows


@pytest.fixture(autouse=True)
def _skip_ddl(monkeypatch: pytest.MonkeyPatch):
    # Treat the inline DDL backstop as already applied so unit tests exercise
    # only the insert/select, not the create-table dance.
    monkeypatch.setattr(crl, "_DDL_READY", True)


async def test_log_skips_when_disconnected(monkeypatch: pytest.MonkeyPatch):
    fake = FakeDB(connected=False)
    monkeypatch.setattr(crl, "database", fake)
    rid = await crl.log_citation_read(
        endpoint="item", status="hit", content_key="ck_x"
    )
    assert rid is None
    assert fake.executed == []  # never touched the pool


async def test_log_writes_when_connected(monkeypatch: pytest.MonkeyPatch):
    fake = FakeDB(connected=True)
    monkeypatch.setattr(crl, "database", fake)
    rid = await crl.log_citation_read(
        endpoint="search", status=crl.STATUS_HIT, query="hair", result_count=3,
        agent="openai-chatgpt/1.0",
    )
    assert rid is not None
    assert len(fake.executed) == 1  # one insert (DDL skipped)


async def test_log_never_raises_on_db_error(monkeypatch: pytest.MonkeyPatch):
    class Boom(FakeDB):
        async def execute(self, query: Any, params: Any = None) -> None:
            raise RuntimeError("db down")

    monkeypatch.setattr(crl, "database", Boom(connected=True))
    rid = await crl.log_citation_read(endpoint="item", status="miss")
    assert rid is None  # swallowed, not raised


async def test_log_caps_oversized_free_text(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    class Capture(FakeDB):
        async def execute(self, query: Any, params: Any = None) -> None:
            # databases passes a compiled query; pull the bound params.
            captured.update(dict(query.compile().params))

    monkeypatch.setattr(crl, "database", Capture(connected=True))
    await crl.log_citation_read(
        endpoint="search", status="hit", query="x" * 5000, agent="y" * 5000,
    )
    assert len(captured["query"]) <= 512
    assert len(captured["agent"]) <= 256


async def test_fetch_returns_rows_and_filters(monkeypatch: pytest.MonkeyPatch):
    fake = FakeDB(connected=True, rows=[{"read_id": "r1", "agent": "openai/1.0"}])
    monkeypatch.setattr(crl, "database", fake)
    rows = await crl.fetch_citation_reads(agent="openai/1.0", content_key="ck_z")
    assert rows and rows[0]["read_id"] == "r1"


async def test_fetch_best_effort_returns_empty_on_error(monkeypatch: pytest.MonkeyPatch):
    class Boom(FakeDB):
        async def fetch_all(self, query: Any) -> List[Any]:
            raise RuntimeError("db down")

    monkeypatch.setattr(crl, "database", Boom(connected=True))
    assert await crl.fetch_citation_reads() == []

"""record_funnel_link's column guards and the FK-miss fallback in _write_funnel_link.

The real foreign keys are exercised in tests/test_decision_funnel_link_postgres.py; these pin the
logic with a fake database.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import pytest
from asyncpg.exceptions import ForeignKeyViolationError, UniqueViolationError

from services import agent_decision_event_store as store

CK = "ck_" + "0123456789abcdef" * 2


def _fk_miss(column: Optional[str] = None, *, constraint: Optional[str] = None) -> ForeignKeyViolationError:
    exc = ForeignKeyViolationError("insert or update violates foreign key constraint")
    exc.detail = f"Key ({column})=(some value) is not present in table \"t\"." if column else None
    exc.constraint_name = constraint
    return exc


class _FakeDb:
    """execute() raises an FK miss while a row still carries a column listed in `missing`."""

    def __init__(self, missing: Dict[str, Any], *, other: Optional[Exception] = None) -> None:
        self.missing = missing
        self.other = other
        self.rows: List[Dict[str, Any]] = []

    async def execute(self, sql: str, row: Dict[str, Any]) -> None:
        self.rows.append(dict(row))
        if self.other is not None:
            raise self.other
        for column, value in self.missing.items():
            if row.get(column) == value:
                raise _fk_miss(column)


async def _enqueued_link(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Dict[str, Any]:
    enqueued: list = []

    async def fake_enqueue(op: str, payload: Dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)
    await store.record_funnel_link(funnel_event_id="event-1", **kwargs)
    assert [op for op, _ in enqueued] == ["funnel_link"]
    return enqueued[0][1]


def _link(**over: Any) -> Dict[str, Any]:
    row = {col: None for col in store._SPEC["funnel_link"][1]}
    row.update(link_id="link-1", funnel_event_id="event-1", decision_id="dec", protocol="pdp_direct",
               attribution_model="last_agent_decision_v1", attribution_window_seconds=2592000)
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_the_catalog_offer_prefix_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = await _enqueued_link(monkeypatch, catalog_offer_id="of:catalog_offer:offer_9", content_key=CK)
    assert (payload["catalog_offer_id"], payload["content_key"]) == ("offer_9", CK)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["product_key_not_a_content_key", "ck_short", "x" * 64, "", None])
async def test_a_content_key_that_is_not_a_ck_key_is_nulled(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    payload = await _enqueued_link(monkeypatch, content_key=value, catalog_offer_id="offer_9")
    assert payload["content_key"] is None
    assert payload["catalog_offer_id"] == "offer_9"


@pytest.mark.asyncio
async def test_an_over_long_offer_id_is_nulled_and_a_synthetic_one_is_left_to_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (await _enqueued_link(monkeypatch, catalog_offer_id="o" * 256))["catalog_offer_id"] is None
    synthetic = "of:internal_checkout:merch_1:sku_9"
    assert (await _enqueued_link(monkeypatch, catalog_offer_id=synthetic))["catalog_offer_id"] == synthetic


@pytest.mark.asyncio
async def test_an_fk_miss_nulls_only_that_column_and_the_link_is_written(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    db = _FakeDb({"catalog_offer_id": "of:internal_checkout:x", "content_key": CK})
    monkeypatch.setattr(store, "database", db)
    row = _link(catalog_offer_id="of:internal_checkout:x", content_key=CK, commerce_attribution_edge_id="edge_1")
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        await store._write_funnel_link(row)
    written = db.rows[-1]
    assert len(db.rows) == 3
    assert (written["catalog_offer_id"], written["content_key"]) == (None, None)
    assert (written["decision_id"], written["commerce_attribution_edge_id"]) == ("dec", "edge_1")
    assert row["catalog_offer_id"] == "of:internal_checkout:x", "the queued payload is not mutated"
    [line] = [json.loads(r.getMessage()) for r in caplog.records]
    assert line["event"] == "agent_decision_event_store.funnel_link_refs_nulled"
    assert line["columns"] == ["catalog_offer_id", "content_key"]
    assert "of:internal_checkout:x" not in caplog.text, "values are never logged"


@pytest.mark.asyncio
async def test_a_clean_link_is_one_insert_and_no_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    db = _FakeDb({})
    monkeypatch.setattr(store, "database", db)
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        await store._write_funnel_link(_link(catalog_offer_id="offer_9", content_key=CK))
    assert len(db.rows) == 1
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["decision_id", "funnel_event_id", "merchant_id"])
async def test_an_fk_miss_on_a_required_or_unknown_column_still_fails(
    monkeypatch: pytest.MonkeyPatch, column: str,
) -> None:
    db = _FakeDb({column: _link()[column]})
    monkeypatch.setattr(store, "database", db)
    with pytest.raises(ForeignKeyViolationError):
        await store._write_funnel_link(_link())
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_an_fk_miss_that_names_no_column_fails_instead_of_looping(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Db(_FakeDb):
        async def execute(self, sql: str, row: Dict[str, Any]) -> None:
            self.rows.append(dict(row))
            raise _fk_miss(None)

    db = _Db({})
    monkeypatch.setattr(store, "database", db)
    with pytest.raises(ForeignKeyViolationError):
        await store._write_funnel_link(_link(catalog_offer_id="offer_9"))
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_a_column_that_misses_again_once_null_fails_instead_of_looping(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Db(_FakeDb):
        async def execute(self, sql: str, row: Dict[str, Any]) -> None:
            self.rows.append(dict(row))
            raise _fk_miss("catalog_offer_id")

    db = _Db({})
    monkeypatch.setattr(store, "database", db)
    with pytest.raises(ForeignKeyViolationError):
        await store._write_funnel_link(_link(catalog_offer_id="offer_9"))
    assert len(db.rows) == 2


@pytest.mark.asyncio
async def test_a_non_fk_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb({}, other=UniqueViolationError("duplicate key"))
    monkeypatch.setattr(store, "database", db)
    with pytest.raises(UniqueViolationError):
        await store._write_funnel_link(_link(catalog_offer_id="offer_9"))
    assert len(db.rows) == 1


def test_the_column_comes_from_the_detail_or_the_default_constraint_name() -> None:
    assert store._fk_column(_fk_miss("content_key")) == "content_key"
    assert store._fk_column(_fk_miss(constraint="agent_decision_funnel_links_catalog_offer_id_fkey")) == "catalog_offer_id"
    assert store._fk_column(_fk_miss(constraint="some_other_fkey")) is None
    assert store._fk_column(ValueError("no detail")) is None


def test_the_optional_refs_are_link_columns_and_exclude_the_decision() -> None:
    columns = store._SPEC["funnel_link"][1]
    assert set(store._LINK_OPTIONAL_REFS) <= set(columns)
    assert "decision_id" not in store._LINK_OPTIONAL_REFS
    assert "funnel_event_id" not in store._LINK_OPTIONAL_REFS


@pytest.mark.asyncio
async def test_the_flush_loop_writes_funnel_links_through_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb({"catalog_offer_id": "gone"})
    monkeypatch.setattr(store, "database", db)
    reports: list = []
    monkeypatch.setattr(store, "_report", lambda exc, **kw: reports.append(exc))
    queue: asyncio.Queue = asyncio.Queue()
    worker = asyncio.ensure_future(store._flush_loop(queue))
    try:
        queue.put_nowait(("funnel_link", _link(catalog_offer_id="gone")))
        await asyncio.wait_for(queue.join(), timeout=5)
    finally:
        worker.cancel()
    assert reports == []
    assert db.rows[-1]["catalog_offer_id"] is None

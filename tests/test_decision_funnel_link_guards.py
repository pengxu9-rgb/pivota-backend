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


def _fk_miss(
    column: Optional[str] = None, *, table: str = "agent_decision_funnel_links", detail: bool = False,
) -> ForeignKeyViolationError:
    """Shaped like the real server's error (see the Postgres file): the structured fields name the
    table and the default '<table>_<col>_fkey' constraint; the human-readable detail is optional."""
    exc = ForeignKeyViolationError("insert or update violates foreign key constraint")
    exc.table_name = table
    exc.constraint_name = f"{table}_{column}_fkey" if column and not detail else None
    exc.detail = f"Key ({column})=(some value) is not present in table \"t\"." if column and detail else None
    return exc


class _FakeDb:
    """execute()/fetch_val() raise an FK miss while a row still carries a column listed in `missing`;
    fetch_val returns the link_id, or None when `conflict` (an ON CONFLICT DO NOTHING no-op)."""

    def __init__(self, missing: Dict[str, Any], *, other: Optional[Exception] = None, conflict: bool = False) -> None:
        self.missing = missing
        self.other = other
        self.conflict = conflict
        self.rows: List[Dict[str, Any]] = []
        self.sql: List[str] = []

    async def execute(self, sql: str, row: Dict[str, Any]) -> None:
        self.sql.append(sql)
        self.rows.append(dict(row))
        if self.other is not None:
            raise self.other
        for column, value in self.missing.items():
            if row.get(column) == value:
                raise _fk_miss(column)

    async def fetch_val(self, sql: str, row: Dict[str, Any]) -> Optional[str]:
        await self.execute(sql, row)
        return None if self.conflict else row["link_id"]


def _nulled_lines(caplog: pytest.LogCaptureFixture) -> List[Dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.levelno == logging.WARNING and "funnel_link_refs_nulled" in r.getMessage()]


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
@pytest.mark.parametrize("value", ["product_key_not_a_content_key", "ck_short", "x" * 64])
async def test_an_enqueue_null_is_a_countable_warning_without_the_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str,
) -> None:
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        await _enqueued_link(monkeypatch, content_key=value, decision_id="dec")
    [line] = _nulled_lines(caplog)
    assert (line["stage"], line["column"], line["length"], line["prefix"]) == ("enqueue", "content_key", len(value), value[:3])
    assert (line["funnel_event_id"], line["decision_id"]) == ("event-1", "dec")
    assert value not in caplog.text, "values are never logged"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", CK])
async def test_an_absent_or_valid_content_key_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: Any,
) -> None:
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        await _enqueued_link(monkeypatch, content_key=value, catalog_offer_id="offer_9")
    assert _nulled_lines(caplog) == []


@pytest.mark.asyncio
async def test_an_over_long_offer_id_is_nulled_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        assert (await _enqueued_link(monkeypatch, catalog_offer_id="o" * 256))["catalog_offer_id"] is None
    [line] = _nulled_lines(caplog)
    assert (line["stage"], line["column"], line["length"]) == ("enqueue", "catalog_offer_id", 256)
    assert "o" * 256 not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("synthetic", ["of:internal_checkout:merch_1:sku_9", "of:something_else:1"])
async def test_a_synthetic_offer_id_is_nulled_before_the_insert_and_is_not_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, synthetic: str,
) -> None:
    # catalog_offers never holds an of: id, so the insert would always miss: nulled up front.
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        payload = await _enqueued_link(monkeypatch, catalog_offer_id=synthetic, content_key=CK)
    assert (payload["catalog_offer_id"], payload["content_key"]) == (None, CK)
    assert caplog.records == []


@pytest.mark.asyncio
async def test_an_fk_miss_nulls_only_that_column_and_the_link_is_written(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    db = _FakeDb({"catalog_offer_id": "offer_deleted_since", "content_key": CK})
    monkeypatch.setattr(store, "database", db)
    row = _link(catalog_offer_id="offer_deleted_since", content_key=CK, commerce_attribution_edge_id="edge_1",
                checkout_decision_id="cd_1")
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        await store._write_funnel_link(row)
    written = db.rows[-1]
    assert len(db.rows) == 3
    assert (written["catalog_offer_id"], written["content_key"]) == (None, None)
    assert (written["decision_id"], written["commerce_attribution_edge_id"]) == ("dec", "edge_1")
    assert written["checkout_decision_id"] == "cd_1"
    assert row["catalog_offer_id"] == "offer_deleted_since", "the queued payload is not mutated"
    [line] = [json.loads(r.getMessage()) for r in caplog.records]
    assert line["event"] == "agent_decision_event_store.funnel_link_refs_nulled"
    assert (line["stage"], line["link_id"]) == ("write", "link-1")
    assert line["columns"] == ["catalog_offer_id", "content_key"]
    assert "offer_deleted_since" not in caplog.text, "values are never logged"
    assert "RETURNING" not in db.sql[0] and all("RETURNING link_id" in q for q in db.sql[1:])


@pytest.mark.asyncio
async def test_a_retry_that_hits_a_conflict_is_not_counted_as_nulled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # another writer linked this funnel event between the miss and the retry: nothing landed nulled
    db = _FakeDb({"catalog_offer_id": "offer_deleted_since"}, conflict=True)
    monkeypatch.setattr(store, "database", db)
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        await store._write_funnel_link(_link(catalog_offer_id="offer_deleted_since"))
    assert len(db.rows) == 2
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["checkout_decision_id", "exposure_id"])
async def test_a_miss_on_a_queued_parent_drops_the_link_so_it_stays_repairable(
    monkeypatch: pytest.MonkeyPatch, column: str,
) -> None:
    # the parent row goes through this same async queue and may simply not be written yet: nulling
    # would be permanent, so the link fails (the flush loop reports it) and a re-send lands it whole
    db = _FakeDb({column: "not_written_yet"})
    monkeypatch.setattr(store, "database", db)
    with pytest.raises(ForeignKeyViolationError):
        await store._write_funnel_link(_link(**{column: "not_written_yet"}, catalog_offer_id="offer_9"))
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_a_link_naming_no_decision_and_no_checkout_decision_is_not_written(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    db = _FakeDb({})
    monkeypatch.setattr(store, "database", db)
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        await store._write_funnel_link(_link(decision_id=None, catalog_offer_id="offer_9", content_key=CK))
    assert db.rows == []
    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_link_naming_only_a_checkout_decision_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb({})
    monkeypatch.setattr(store, "database", db)
    await store._write_funnel_link(_link(decision_id=None, checkout_decision_id="cd_1"))
    assert [r["checkout_decision_id"] for r in db.rows] == ["cd_1"]


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
@pytest.mark.parametrize("column", ["decision_id", "funnel_event_id", "merchant_id", "checkout_decision_id"])
async def test_an_fk_miss_on_a_required_or_unknown_column_still_fails(
    monkeypatch: pytest.MonkeyPatch, column: str,
) -> None:
    row = _link(checkout_decision_id="cd_1")
    db = _FakeDb({column: row[column]})
    monkeypatch.setattr(store, "database", db)
    with pytest.raises(ForeignKeyViolationError):
        await store._write_funnel_link(row)
    assert len(db.rows) == 1


def _fk_miss_on_another_table() -> ForeignKeyViolationError:
    # same constraint name, raised for another table (a trigger's insert): not a funnel-link column
    exc = _fk_miss("catalog_offer_id")
    exc.table_name = "some_other_table"
    return exc


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    _fk_miss_on_another_table(),
    _fk_miss("catalog_offer_id", detail=True),
], ids=["fk_on_another_table", "message_text_only"])
async def test_only_the_structured_fields_of_this_table_null_a_column(
    monkeypatch: pytest.MonkeyPatch, exc: ForeignKeyViolationError,
) -> None:
    class _Db(_FakeDb):
        async def execute(self, sql: str, row: Dict[str, Any]) -> None:
            self.rows.append(dict(row))
            raise exc

    db = _Db({})
    monkeypatch.setattr(store, "database", db)
    with pytest.raises(ForeignKeyViolationError):
        await store._write_funnel_link(_link(catalog_offer_id="offer_9"))
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


def test_the_column_comes_from_the_constraint_name_on_this_table_only() -> None:
    assert store._fk_column(_fk_miss("content_key")) == "content_key"
    assert store._fk_column(_fk_miss("commerce_attribution_edge_id")) == "commerce_attribution_edge_id"
    assert store._fk_column(_fk_miss("content_key", table="agent_decision_candidates")) is None
    assert store._fk_column(_fk_miss_on_another_table()) is None
    assert store._fk_column(_fk_miss("content_key", detail=True)) is None, "the message text is not parsed"
    other = _fk_miss("content_key")
    other.constraint_name = "some_other_fkey"
    assert store._fk_column(other) is None
    assert store._fk_column(ValueError("no detail")) is None


def test_the_optional_refs_are_link_columns_and_exclude_what_the_link_is_about() -> None:
    columns = store._SPEC["funnel_link"][1]
    assert set(store._LINK_OPTIONAL_REFS) <= set(columns)
    for column in ("decision_id", "funnel_event_id", "checkout_decision_id", "exposure_id"):
        assert column not in store._LINK_OPTIONAL_REFS


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

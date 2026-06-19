"""Lazy backlog dedup (page-usability Step 1): collapse duplicate PENDING tasks
that share a canonical identity down to the newest, idempotently. The persistent
action-plan scope surfaced an accumulated pile of identical pending tasks the old
latest_completed scope was masking; this self-heals it on read.
"""

import os

import pytest

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)

from db import merchant_tasks as mt


def _row(task_id, *, title, lever="indexing_acceleration", created_at,
         target_host="", product_key="", status="pending"):
    return {
        "task_id": task_id,
        "merchant_id": "m1",
        "title": title,
        "lever": lever,
        "status": status,
        "created_at": created_at,
        "evidence": {"target_host": target_host, "product_key": product_key},
    }


@pytest.mark.asyncio
async def test_collapses_identical_pending_keeps_newest(monkeypatch):
    rows = [
        _row("idx-old", title="Index your canonical PDPs", created_at="2026-05-01T00:00:00+00:00"),
        _row("idx-mid", title="Index your canonical PDPs", created_at="2026-05-10T00:00:00+00:00"),
        _row("idx-new", title="Index your canonical PDPs", created_at="2026-06-01T00:00:00+00:00"),
        _row("other", title="Convert category mentions", lever="general_recommendation",
             created_at="2026-06-01T00:00:00+00:00"),
    ]
    superseded = []

    async def _noop_ensure():
        return None

    class FakeDB:
        async def fetch_all(self, q):
            return rows

    async def _fake_mark(*, task_id, superseded_by_task_id=None):
        superseded.append((task_id, superseded_by_task_id))
        return True

    monkeypatch.setattr(mt, "ensure_merchant_tasks_table", _noop_ensure)
    monkeypatch.setattr(mt, "database", FakeDB())
    monkeypatch.setattr(mt, "_row_to_dict", lambda r: r)
    monkeypatch.setattr(mt, "mark_task_superseded", _fake_mark)

    n = await mt.dedupe_pending_tasks(merchant_id="m1")

    # the two older "Index" copies collapse onto the newest; the unique task is untouched
    assert n == 2
    assert ("idx-old", "idx-new") in superseded
    assert ("idx-mid", "idx-new") in superseded
    assert all(t != "other" for t, _ in superseded)


@pytest.mark.asyncio
async def test_distinct_product_keys_are_not_collapsed(monkeypatch):
    """Same title but different product — these are genuinely different tasks."""
    rows = [
        _row("a", title="Fill the gaps", lever="content_revision",
             created_at="2026-06-01T00:00:00+00:00", product_key="PK-A"),
        _row("b", title="Fill the gaps", lever="content_revision",
             created_at="2026-06-01T00:00:00+00:00", product_key="PK-B"),
    ]
    superseded = []

    async def _noop_ensure():
        return None

    class FakeDB:
        async def fetch_all(self, q):
            return rows

    async def _fake_mark(*, task_id, superseded_by_task_id=None):
        superseded.append(task_id)
        return True

    monkeypatch.setattr(mt, "ensure_merchant_tasks_table", _noop_ensure)
    monkeypatch.setattr(mt, "database", FakeDB())
    monkeypatch.setattr(mt, "_row_to_dict", lambda r: r)
    monkeypatch.setattr(mt, "mark_task_superseded", _fake_mark)

    n = await mt.dedupe_pending_tasks(merchant_id="m1")
    assert n == 0
    assert superseded == []


@pytest.mark.asyncio
async def test_idempotent_no_duplicates_is_noop(monkeypatch):
    rows = [_row("solo", title="Index your canonical PDPs", created_at="2026-06-01T00:00:00+00:00")]

    async def _noop_ensure():
        return None

    class FakeDB:
        async def fetch_all(self, q):
            return rows

    called = []

    async def _fake_mark(*, task_id, superseded_by_task_id=None):
        called.append(task_id)
        return True

    monkeypatch.setattr(mt, "ensure_merchant_tasks_table", _noop_ensure)
    monkeypatch.setattr(mt, "database", FakeDB())
    monkeypatch.setattr(mt, "_row_to_dict", lambda r: r)
    monkeypatch.setattr(mt, "mark_task_superseded", _fake_mark)

    n = await mt.dedupe_pending_tasks(merchant_id="m1")
    assert n == 0
    assert called == []

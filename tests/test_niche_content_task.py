"""Phase 3: graduate a winnable-niche action into a tracked distribution task."""

from __future__ import annotations

import pytest

from routes.merchant_audit_routes import _NicheContentBody, create_niche_content_task


@pytest.mark.asyncio
async def test_creates_tracked_niche_task(monkeypatch):
    captured = {}

    async def fake_find(*, merchant_id, lever, title):
        return []

    async def fake_record(**kw):
        captured.update(kw)
        return "task-1"

    monkeypatch.setattr("db.merchant_tasks.find_pending_supersede_candidates", fake_find)
    monkeypatch.setattr("db.merchant_tasks.record_task_created", fake_record)

    out = await create_niche_content_task(
        _NicheContentBody(query="vegan collagen for sleep", sku_name="Aruen",
                          why_you_fit="vegan, collagen", sku_key="s1"),
        merchant_id="m1",
    )
    assert out["status"] == "success"
    assert out["task_id"] == "task-1"
    assert captured["lever"] == "niche_content"
    assert "vegan collagen for sleep" in captured["title"]
    assert captured["evidence"]["query"] == "vegan collagen for sleep"
    assert captured["evidence"]["kind"] == "niche_content"
    assert "Aruen" in captured["body"]


@pytest.mark.asyncio
async def test_idempotent_returns_existing(monkeypatch):
    async def fake_find(*, merchant_id, lever, title):
        return [{"task_id": "existing-1"}]

    async def fake_record(**kw):
        raise AssertionError("should not create a new task when one exists")

    monkeypatch.setattr("db.merchant_tasks.find_pending_supersede_candidates", fake_find)
    monkeypatch.setattr("db.merchant_tasks.record_task_created", fake_record)

    out = await create_niche_content_task(_NicheContentBody(query="q"), merchant_id="m1")
    assert out["status"] == "exists"
    assert out["task_id"] == "existing-1"


@pytest.mark.asyncio
async def test_blank_query_rejected():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await create_niche_content_task(_NicheContentBody(query="   "), merchant_id="m1")
    assert exc.value.status_code == 422

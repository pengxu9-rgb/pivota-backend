from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services import external_seed_servability as svc


class _FakeDB:
    def __init__(self, content_key="ck1"):
        self.executes = []
        self.content_key = content_key

    async def execute(self, query, params):
        self.executes.append((query, params))

    async def fetch_one(self, query, params):
        return {"content_key": self.content_key}


def test_backlink_seed_to_product_targets_seed_row():
    db = _FakeDB()
    asyncio.run(svc.backlink_seed_to_product("seed1", "pk1", db=db))
    query, params = db.executes[0]
    assert "external_product_seeds" in query
    assert "attached_product_key" in query
    assert params == {"product_key": "pk1", "seed_id": "seed1"}


def test_make_external_seed_servable_runs_all_steps(monkeypatch):
    calls = {}

    async def fake_quality(**kw):
        calls["quality"] = kw

    async def fake_apv(**kw):
        calls["apv"] = kw

    async def fake_recompute(content_key, reason):
        calls["recompute"] = (content_key, reason)
        return True

    monkeypatch.setattr(svc, "full_quality_eval", fake_quality)
    monkeypatch.setattr(svc, "refresh_agent_pdp_view_for_seed", fake_apv)
    monkeypatch.setattr(svc, "recompute_serving_eligibility", fake_recompute)

    db = _FakeDB("ckX")
    summary = asyncio.run(svc.make_external_seed_servable(
        product_key="pk1", seed_id="seed1", source_product_id="sp1",
        quality_payload={"title": "t"}, reason="r", db=db))

    assert summary == {"backlink": True, "quality": True, "apv": True, "serving_eligible": True}
    assert calls["quality"]["platform_product_id"] == "sp1"
    assert calls["quality"]["merchant_id"] == "external_seed"
    assert calls["apv"]["seed_id"] == "seed1"
    assert calls["recompute"] == ("ckX", "r")
    assert any("attached_product_key" in q for q, _ in db.executes)


def test_make_external_seed_servable_is_best_effort(monkeypatch):
    async def boom_quality(**kw):
        raise RuntimeError("quality scorer unavailable")

    async def fake_apv(**kw):
        pass

    async def fake_recompute(content_key, reason):
        return False

    monkeypatch.setattr(svc, "full_quality_eval", boom_quality)
    monkeypatch.setattr(svc, "refresh_agent_pdp_view_for_seed", fake_apv)
    monkeypatch.setattr(svc, "recompute_serving_eligibility", fake_recompute)

    db = _FakeDB()
    # A failing step must NOT raise and must not block the others.
    summary = asyncio.run(svc.make_external_seed_servable(
        product_key="pk1", seed_id="s1", source_product_id="sp", quality_payload={}, db=db))
    assert summary["quality"] is False
    assert summary["backlink"] is True
    assert summary["apv"] is True
    assert summary["serving_eligible"] is False

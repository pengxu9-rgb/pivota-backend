"""Orchestration tests for the programmatic Path-C runner (no Gemini, no DB)."""

import asyncio

import services.catalog_enrichment_agent.runner as runner


def _patch(monkeypatch, *, applied_counts=None, calls=None):
    async def fake_validate(cand, **kw):
        return {"pdp": dict(cand), "offers": [{"canonical_url": "https://x/p", "confidence": 0.9}]}

    def fake_plan(validated):
        pdps = [v for v in validated if v.get("offers")]
        return {"pdps": pdps, "offers": [{}] * len(pdps), "skipped": 0}

    async def fake_apply(plan, *, batch_label, db=None):
        if calls is not None:
            calls.append(batch_label)
        return applied_counts or {"pdps": len(plan.get("pdps") or [])}

    monkeypatch.setattr(runner, "validate_candidate", fake_validate)
    monkeypatch.setattr(runner, "ingest_validated_jsonl", fake_plan)
    monkeypatch.setattr(runner, "apply_ingest_plan", fake_apply)


def test_validate_only_does_not_apply(monkeypatch):
    calls = []
    _patch(monkeypatch, calls=calls)
    cands = [{"brand": "A", "product_name": "A1"}, {"brand": "B", "product_name": "B1"}]
    out = asyncio.run(runner.run_candidates(cands, batch_label="t", apply=False))
    assert out["candidates"] == 2
    assert out["validated_with_offers"] == 2
    assert out["plan_pdps"] == 2
    assert out["applied"] is None
    assert calls == []  # apply never called when apply=False


def test_apply_invokes_executor(monkeypatch):
    calls = []
    _patch(monkeypatch, applied_counts={"pdps": 1, "offers": 1}, calls=calls)
    out = asyncio.run(
        runner.run_candidates([{"brand": "A", "product_name": "A1"}], batch_label="lbl", apply=True)
    )
    assert out["applied"] == {"pdps": 1, "offers": 1}
    assert calls == ["lbl"]  # executor called once with the batch label


def test_empty_is_noop(monkeypatch):
    calls = []
    _patch(monkeypatch, calls=calls)
    out = asyncio.run(runner.run_candidates([], batch_label="t", apply=True))
    assert out["candidates"] == 0 and out["applied"] is None
    assert calls == []

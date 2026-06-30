"""Catalog-onboard queue worker tests (mocked queue + feeds; no DB/network)."""

import asyncio

import services.catalog_onboard_worker as w


def test_enqueue_curated_dedup_key_and_priority(monkeypatch):
    calls = []

    async def fake_enqueue(*, kind, dedup_key, payload, priority, source, db=None):
        calls.append((kind, dedup_key, priority))
        return "id"

    monkeypatch.setattr(w.q, "enqueue", fake_enqueue)
    brands = [{"domain": "cosrx.com", "brand": "COSRX"}, {"domain": "", "brand": "skip"}]
    n = asyncio.run(w.enqueue_curated_brands(brands, priority_rank={"cosrx": 7}))
    assert n == 1  # blank domain skipped
    assert calls == [("curated_brand", "cosrx.com", 7)]


def test_enqueue_audit_candidates_normalizes_key(monkeypatch):
    calls = []

    async def fake_enqueue(*, kind, dedup_key, payload, priority, source, db=None):
        calls.append((kind, dedup_key, priority))
        return "id"

    monkeypatch.setattr(w.q, "enqueue", fake_enqueue)
    cands = [{"product_name": "OLLY Collagen"}, {"product_name": ""}]
    n = asyncio.run(w.enqueue_audit_candidates(cands, priority_rank={"olly collagen": 5}))
    assert n == 1
    assert calls == [("audit_candidate", "olly collagen", 5)]


def test_enqueue_defaults_to_recurrence_rank(monkeypatch):
    # No priority_rank supplied → falls back to live cross-audit demand.
    calls = []

    async def fake_enqueue(*, kind, dedup_key, payload, priority, source, db=None):
        calls.append((dedup_key, priority))
        return "id"

    async def fake_rank(*, db=None):
        return {"cosrx": 9}

    monkeypatch.setattr(w.q, "enqueue", fake_enqueue)
    monkeypatch.setattr(w, "recurrence_rank", fake_rank)
    n = asyncio.run(w.enqueue_curated_brands([{"domain": "cosrx.com", "brand": "COSRX"}]))
    assert n == 1
    assert calls == [("cosrx.com", 9)]  # priority pulled from the recurrence default


def _patch_drain(monkeypatch, items, *, raise_on=None, done=None, failed=None):
    async def claim_batch(limit, db=None):
        return items

    async def mark_done(item_id, *, result=None, db=None):
        done.append(item_id)

    async def mark_failed(item_id, *, error, attempts, max_attempts, db=None):
        failed.append((item_id, attempts >= max_attempts))

    async def fake_curated(payload, *, apply, db):
        if raise_on == "curated_brand":
            raise RuntimeError("boom")
        return {"records": 1, "applied": {"pdps": 1} if apply else None}

    async def fake_audit(payload, *, apply, db):
        return {"plan_pdps": 1, "applied": {"pdps": 1} if apply else None}

    monkeypatch.setattr(w.q, "claim_batch", claim_batch)
    monkeypatch.setattr(w.q, "mark_done", mark_done)
    monkeypatch.setattr(w.q, "mark_failed", mark_failed)
    monkeypatch.setattr(w, "_process_curated_brand", fake_curated)
    monkeypatch.setattr(w, "_process_audit_candidate", fake_audit)


def test_process_queue_dispatch_and_done(monkeypatch):
    done, failed = [], []
    items = [
        {"id": "a", "kind": "curated_brand", "payload": {"domain": "x.com"}, "attempts": 1, "max_attempts": 3},
        {"id": "b", "kind": "audit_candidate", "payload": {"product_name": "Y"}, "attempts": 1, "max_attempts": 3},
    ]
    _patch_drain(monkeypatch, items, done=done, failed=failed)
    summary = asyncio.run(w.process_queue(limit=10, apply=True))
    assert summary == {"claimed": 2, "done": 2, "failed": 0, "skipped": 0}
    assert set(done) == {"a", "b"} and failed == []


def test_process_queue_failure_marks_failed(monkeypatch):
    done, failed = [], []
    items = [{"id": "a", "kind": "curated_brand", "payload": {}, "attempts": 3, "max_attempts": 3}]
    _patch_drain(monkeypatch, items, raise_on="curated_brand", done=done, failed=failed)
    summary = asyncio.run(w.process_queue(limit=10, apply=True))
    assert summary["failed"] == 1 and summary["done"] == 0
    assert failed == [("a", True)]  # attempts==max → terminal failure


def test_process_queue_unknown_kind_skipped(monkeypatch):
    done, failed = [], []
    items = [{"id": "z", "kind": "weird", "payload": {}, "attempts": 1, "max_attempts": 3}]
    skipped = []

    async def mark_skipped(item_id, *, reason="", db=None):
        skipped.append(item_id)

    _patch_drain(monkeypatch, items, done=done, failed=failed)
    monkeypatch.setattr(w.q, "mark_skipped", mark_skipped)
    summary = asyncio.run(w.process_queue(limit=10, apply=True))
    assert summary["skipped"] == 1 and skipped == ["z"]

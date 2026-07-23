"""The rescore backfill must complete the TWO-STEP flip: rescore -> serving_eligible,
then upsert_catalog_row_trust -> serving_decision='public'. Only rows that actually
became serving_eligible should be handed to the trust upsert.
"""

import asyncio
from typing import Any, Dict, List

import pytest

import scripts.backfill_external_seed_quality_rescore as bf


class _FakeDB:
    """Minimal async stand-in: fetch_all returns the seeded rows, then [] for the
    _rescored_ids query."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self._fetch_calls = 0

    async def connect(self):  # noqa: D401
        return None

    async def disconnect(self):
        return None

    async def fetch_all(self, query, values=None):
        self._fetch_calls += 1
        # First fetch_all in run() is the FETCH (rows); _rescored_ids() is second.
        if self._fetch_calls == 1:
            return self._rows
        return []


def _row(pk: str, epid: str) -> Dict[str, Any]:
    return {
        "product_key": pk, "source_product_id": epid, "title": "t", "description": "d",
        "brand": "b", "product_type": None, "category_kind": "skincare", "image_url": "i",
        "seed_id": f"seed_{epid}", "price_amount": 10, "raw_inci": None,
        "pdp_details_sections": [{"title": "x", "body": "y"}],
    }


def _install(monkeypatch, rows, *, eligible_keys):
    monkeypatch.setattr(bf, "database", _FakeDB(rows))

    async def fake_servable(*, product_key, seed_id, source_product_id, quality_payload, reason):
        return {"serving_eligible": product_key in eligible_keys}

    monkeypatch.setattr(bf, "make_external_seed_servable", fake_servable)

    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        keys = list(product_keys)
        calls.append(keys)
        return len(keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    return calls


def test_only_eligible_rows_are_promoted_to_trust(monkeypatch):
    rows = [_row("pk_a", "e_a"), _row("pk_b", "e_b"), _row("pk_c", "e_c")]
    calls = _install(monkeypatch, rows, eligible_keys={"pk_a", "pk_c"})  # pk_b stays blocked

    asyncio.run(bf.run(apply=True, limit=None, force=True))

    flushed = [k for chunk in calls for k in chunk]
    assert flushed == ["pk_a", "pk_c"], "only serving_eligible rows get the trust flip"


def test_trust_flush_chunks_and_final_flush(monkeypatch):
    rows = [_row(f"pk_{i}", f"e_{i}") for i in range(5)]
    calls = _install(monkeypatch, rows, eligible_keys={r["product_key"] for r in rows})

    asyncio.run(bf.run(apply=True, limit=None, force=True, trust_flush_every=2))

    # 5 eligible with flush-every=2 -> chunks [2, 2] mid-loop + [1] final flush.
    assert [len(c) for c in calls] == [2, 2, 1]
    assert sum(len(c) for c in calls) == 5


def test_abort_still_flushes_pre_abort_promotions(monkeypatch):
    # 1 eligible row, then 5 consecutive failures trip the circuit breaker. The
    # pre-abort promotion MUST still be published by the post-loop final flush —
    # the docstring's core guarantee.
    rows = [_row("pk_ok", "e_ok")] + [_row(f"pk_fail_{i}", f"e_fail_{i}") for i in range(6)]
    monkeypatch.setattr(bf, "database", _FakeDB(rows))

    async def flaky_servable(*, product_key, **_):
        if product_key == "pk_ok":
            return {"serving_eligible": True}
        raise RuntimeError("dead socket")

    monkeypatch.setattr(bf, "make_external_seed_servable", flaky_servable)
    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        calls.append(list(product_keys))
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)

    asyncio.run(bf.run(apply=True, limit=None, force=True))
    flushed = [k for chunk in calls for k in chunk]
    assert flushed == ["pk_ok"], "pre-abort promotion must survive the circuit-breaker break"


def test_none_serving_eligible_is_not_promoted(monkeypatch):
    # make_external_seed_servable returns serving_eligible=None when content_key is
    # missing / recompute was skipped. `is True` must exclude it (locks the gate
    # against a loosened truthy check).
    rows = [_row("pk_none", "e_none"), _row("pk_true", "e_true")]
    monkeypatch.setattr(bf, "database", _FakeDB(rows))

    async def servable(*, product_key, **_):
        return {"serving_eligible": None if product_key == "pk_none" else True}

    monkeypatch.setattr(bf, "make_external_seed_servable", servable)
    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        calls.append(list(product_keys))
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    asyncio.run(bf.run(apply=True, limit=None, force=True))
    flushed = [k for chunk in calls for k in chunk]
    assert flushed == ["pk_true"], "serving_eligible=None must not be promoted"


def test_skip_trust_does_no_trust_calls(monkeypatch):
    rows = [_row("pk_a", "e_a")]
    calls = _install(monkeypatch, rows, eligible_keys={"pk_a"})

    asyncio.run(bf.run(apply=True, limit=None, force=True, skip_trust=True))

    assert calls == [], "--skip-trust must never call the trust upsert"


def test_dry_run_does_no_writes(monkeypatch):
    rows = [_row("pk_a", "e_a")]
    calls = _install(monkeypatch, rows, eligible_keys={"pk_a"})

    async def fail_servable(**_):
        raise AssertionError("dry-run must not call make_external_seed_servable")

    monkeypatch.setattr(bf, "make_external_seed_servable", fail_servable)
    asyncio.run(bf.run(apply=False, limit=None, force=True))
    assert calls == [], "dry-run must not touch trust"

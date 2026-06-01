"""Regression test: per-SKU probe fan-out must survive transient chunk timeouts.

Background
----------
The third live Ownist run produced citation evidence for only 1 of 4 SKUs. Root
cause was NOT a coverage/short-circuit bug — all 4 SKUs were probed, but the
Gemini calls hit `ReadTimeout` and the fan-out loop did `break` on the FIRST
chunk failure, so a single transient timeout zeroed the rest of a SKU's chunks
(p2/p3/p4 → 0 runs; p1 → only its first chunk's 8 runs).

Fix:
- chunk smaller (4, not 8) so each grounded call is well under the probe timeout;
- on a chunk failure, CONTINUE to later chunks instead of `break`, but bail the
  (sku, provider) after `_PER_SKU_AUDIT_MAX_CONSECUTIVE_CHUNK_FAILURES`
  consecutive failures so a genuinely-down provider still fails fast.

This test drives the real `run_per_sku_audit_probe_fanout` loop with a fake
`llm_client.probe` that times out on chosen chunks. DB-dependent helpers
(`_sku_keys_for_per_sku_mode`, `load_sku_context`) are stubbed — they are not
the code under test.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import services.agent_center_bd_report_service as bd

MERCHANT = "merch_test_fanout_001"
SKU_KEY = "p1::v::var1"
PROMPTS = 16  # with chunk size 4 -> 4 chunks


def _sku_ctx() -> Dict[str, Any]:
    return {
        "product": {"title": "Triple Shine Grape", "brand": "Ownist",
                    "product_type": "supplement",
                    "canonical_url": "https://ownist.com/products/triple-shine-grape"},
        "sku": {"title": "14 Servings, 2-Week Routine", "sku": "var1"},
        "sku_key": SKU_KEY, "product_key": "p1",
    }


def _expected_chunks() -> List[List[Any]]:
    specs = bd._build_per_sku_audit_query_specs(_sku_ctx(), PROMPTS)
    return bd._chunk_query_specs(specs)


def _install(monkeypatch, *, fail_on):
    """Stub the DB helpers; fake probe fails on the given 1-based chunk indices."""
    calls: List[Dict[str, Any]] = []

    async def _fake_sku_keys(products, merchant_id):
        return [SKU_KEY]

    async def _fake_load_ctx(sku_key, merchant_id):
        return _sku_ctx()

    async def _fake_probe(*, scan_mode, scan_target_id, merchant_id, store_id,
                          context, provider, max_runs, model=None,
                          model_is_override=False):
        chunk_idx = int(scan_target_id.rsplit(":per_sku:", 1)[1])
        calls.append({"chunk_idx": chunk_idx, "max_runs": max_runs,
                      "queries": list(context.get("queries") or [])})
        if chunk_idx in fail_on:
            raise bd.llm_client.AgentCenterLlmClientError(
                "llm probe transport failed after retry (ReadTimeout): ReadTimeout('')"
            )
        return {"provider": provider,
                "raw_runs": [{"query": q} for q in context.get("queries") or []]}

    monkeypatch.setattr(bd, "_sku_keys_for_per_sku_mode", _fake_sku_keys)
    monkeypatch.setattr(bd, "load_sku_context", _fake_load_ctx)
    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)
    return calls


async def _run():
    return await bd.run_per_sku_audit_probe_fanout(
        merchant_id=MERCHANT,
        audit_run_id="run_fanout_test",
        products=[{"product_key": "p1"}],
        coverage_profile="pilot_gemini",
        prompts_per_sku=PROMPTS,
    )


async def test_single_transient_timeout_does_not_zero_the_sku(monkeypatch) -> None:
    """A failure on chunk 2 must NOT stop chunks 3+; the SKU keeps real runs."""
    chunks = _expected_chunks()
    n = len(chunks)
    assert n >= 3, "need >=3 chunks to test continue-past-failure"

    calls = _install(monkeypatch, fail_on={2})
    out = await _run()

    # All chunks attempted (the single failure at chunk 2 did not break the loop).
    assert len(calls) == n, f"expected all {n} chunks attempted, got {len(calls)}"
    # Chunk size really is small now (<= 4), so each call is light.
    assert all(c["max_runs"] <= 4 for c in calls)
    assert bd._PER_SKU_AUDIT_UPSTREAM_CHUNK_SIZE <= 4

    entries = out[SKU_KEY]
    runs = bd._flatten_probe_runs(entries)
    # Successful chunks (all but chunk 2) still produced evidence — SKU not zeroed.
    expected_runs = sum(len(c) for i, c in enumerate(chunks, start=1) if i != 2)
    assert len(runs) == expected_runs > 0


async def test_consecutive_failures_bail_after_cap(monkeypatch) -> None:
    """If every chunk times out, bail after the consecutive-failure cap — don't
    grind through every remaining chunk at the full per-call timeout."""
    cap = bd._PER_SKU_AUDIT_MAX_CONSECUTIVE_CHUNK_FAILURES
    calls = _install(monkeypatch, fail_on=set(range(1, 99)))  # fail all
    out = await _run()

    assert len(calls) == cap, f"expected bail after {cap} consecutive failures, got {len(calls)}"
    assert bd._flatten_probe_runs(out[SKU_KEY]) == []

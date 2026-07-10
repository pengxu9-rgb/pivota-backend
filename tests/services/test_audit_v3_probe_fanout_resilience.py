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
    """The chunks the fanout will attempt, in order. Queries route by intent to
    one of two scan modes (branded findability vs organic category), and EACH
    mode group is chunked independently — so this mirrors that partition rather
    than chunking the flat spec list."""
    recs = bd._build_per_sku_audit_query_records(_sku_ctx(), PROMPTS)
    specs = [
        (str(r.get("query") or ""), str(r.get("axis") or "intent")) for r in recs
    ]
    chunks: List[List[Any]] = []
    for _mode, mode_specs in bd._partition_query_specs_by_scan_mode(specs):
        chunks.extend(bd._chunk_query_specs(mode_specs))
    return chunks


def _install(monkeypatch, *, fail_on):
    """Stub the DB helpers; fake probe fails on the given 1-based ORDINALS (the
    Nth chunk attempted, across both scan-mode groups for the provider)."""
    calls: List[Dict[str, Any]] = []

    async def _fake_sku_keys(products, merchant_id):
        return [SKU_KEY]

    async def _fake_load_ctx(sku_key, merchant_id):
        return _sku_ctx()

    async def _fake_probe(*, scan_mode, scan_target_id, merchant_id, store_id,
                          context, provider, max_runs, model=None,
                          model_is_override=False):
        # probe_run_id now carries a mode tag ("...:per_sku:branded:1"), so the
        # chunk index is no longer a bare int. Track attempt ORDER instead.
        ordinal = len(calls) + 1
        calls.append({"chunk_idx": ordinal, "scan_mode": scan_mode,
                      "max_runs": max_runs,
                      "queries": list(context.get("queries") or [])})
        if ordinal in fail_on:
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


async def test_refresh_prompt_basis_threads_to_resolver(monkeypatch) -> None:
    """The `refresh_prompt_basis` fan-out arg must reach resolve_prompt_basis's
    `refresh=` — that's the whole point of the flag: a re-audit that regenerates
    the basis (reflecting newly-grounded attributes) instead of pinning a prior
    run's frozen query set. Default must stay False so every existing audit pins
    exactly as before."""
    _install(monkeypatch, fail_on=set())
    captured: List[bool] = []

    async def _fake_resolve(*, merchant_id, sku_key, generate_winnable,
                            generate_scenario, refresh=False):
        captured.append(refresh)
        return {"winnable": [], "scenario": [], "selected_specs": [], "meta": {}}

    # resolve_prompt_basis is imported inside the fan-out at call time.
    monkeypatch.setattr("services.prompt_basis.resolve_prompt_basis", _fake_resolve)

    async def _run_with(refresh_flag: bool):
        captured.clear()
        await bd.run_per_sku_audit_probe_fanout(
            merchant_id=MERCHANT, audit_run_id="r",
            products=[{"product_key": "p1"}], coverage_profile="pilot_gemini",
            prompts_per_sku=PROMPTS, winnable_prompts=True,
            refresh_prompt_basis=refresh_flag,
        )
        return captured[:]

    assert await _run_with(False) == [False], "default must pin (refresh=False)"
    assert await _run_with(True) == [True], "refresh must thread through to the resolver"


async def test_fanout_matches_shared_probe_per_sku_ctx(monkeypatch) -> None:
    """Refactor regression: DB fan-out is just load_sku_context + shared loop."""
    calls: List[Dict[str, Any]] = []
    ctx = _sku_ctx()

    async def _fake_sku_keys(products, merchant_id):
        return [SKU_KEY]

    async def _fake_load_ctx(sku_key, merchant_id):
        assert sku_key == SKU_KEY
        assert merchant_id == MERCHANT
        return ctx

    async def _fake_probe(*, scan_mode, scan_target_id, merchant_id, store_id,
                          context, provider, max_runs, model=None,
                          model_is_override=False):
        calls.append({
            "scan_target_id": scan_target_id,
            "provider": provider,
            "queries": list(context.get("queries") or []),
        })
        return {
            "scan_mode": scan_mode,
            "provider": provider,
            "model": model,
            "model_is_override": model_is_override,
            "raw_runs": [
                {
                    "query": q,
                    "parsed": {"product_visible": True, "correct_sku": True},
                    "grounding_sources": [
                        {"uri": ctx["product"]["canonical_url"], "title": "Ownist"}
                    ],
                }
                for q in context.get("queries") or []
            ],
        }

    monkeypatch.setattr(bd, "_sku_keys_for_per_sku_mode", _fake_sku_keys)
    monkeypatch.setattr(bd, "load_sku_context", _fake_load_ctx)
    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)

    fanout = await bd.run_per_sku_audit_probe_fanout(
        merchant_id=MERCHANT,
        audit_run_id="run_fanout_test",
        products=[{"product_key": "p1"}],
        coverage_profile="pilot_gemini",
        prompts_per_sku=4,
    )
    coverage = bd.resolve_coverage_profile(coverage_profile="pilot_gemini")
    provider_models = bd.resolve_provider_models(coverage["providers"])
    direct = await bd._probe_per_sku_ctx(
        sku_ctx=ctx,
        merchant_id=MERCHANT,
        coverage=coverage,
        provider_model_metadata=provider_models,
        prompts_per_sku=4,
        audit_run_id="run_fanout_test",
    )

    assert fanout == {SKU_KEY: direct}
    assert fanout[SKU_KEY][0]["scan_mode"] == "per_sku_audit"
    assert fanout[SKU_KEY][0]["provider"] == "gemini"
    assert fanout[SKU_KEY][0]["runs_count"] == 4
    assert len(calls) == 2


async def test_scan_mode_routes_by_query_intent(monkeypatch) -> None:
    """The core honesty fix: a BRANDED/navigational query (names the product)
    is probed for FINDABILITY (open_product_visibility_test); a DISCOVERY
    category/problem query (does NOT name the brand) is probed for ORGANIC
    appearance (category_visibility_test). A single product-centric mode over-
    reports discovery — "best X" just retrieves the named product's page."""
    calls = _install(monkeypatch, fail_on=set())
    await _run()

    # Expected query -> scan_mode from the SAME partition the fanout uses (the
    # routing key is the spec's axis, which is lost once only the query string
    # crosses the upstream boundary — so reconstruct it from the records here).
    recs = bd._build_per_sku_audit_query_records(_sku_ctx(), PROMPTS)
    specs = [
        (str(r.get("query") or ""), str(r.get("axis") or "intent")) for r in recs
    ]
    expected_mode: Dict[str, str] = {}
    for mode, mode_specs in bd._partition_query_specs_by_scan_mode(specs):
        for q, _ax in mode_specs:
            expected_mode[q] = mode

    # The 16-prompt set yields BOTH branded and discovery queries, so the split
    # is genuinely exercised (otherwise this test would be vacuous).
    assert set(expected_mode.values()) == {
        bd._PER_SKU_BRANDED_SCAN_MODE,
        bd._PER_SKU_DISCOVERY_SCAN_MODE,
    }

    # Every probed query ran under its intended mode, and no upstream call mixes
    # modes (each call is one mode group's chunk).
    for c in calls:
        for q in c["queries"]:
            assert expected_mode.get(q) == c["scan_mode"], (
                f"{q!r} probed under {c['scan_mode']}, "
                f"expected {expected_mode.get(q)}"
            )


async def test_custom_prompts_are_probed_not_billed_and_dropped(monkeypatch) -> None:
    """Regression: merchant-input custom_prompts (billed as prompt credits) must
    actually be PROBED. They were billed-but-never-probed — the worker never read
    them. They run once (first SKU) to avoid an N-SKU × providers multiplier."""
    calls = _install(monkeypatch, fail_on=set())
    custom = [
        "best Korean collagen for glowing skin",
        "collagen before bed for skin repair",
    ]
    out = await bd.run_per_sku_audit_probe_fanout(
        merchant_id=MERCHANT,
        audit_run_id="run_custom_test",
        products=[{"product_key": "p1"}],
        coverage_profile="pilot_gemini",
        prompts_per_sku=PROMPTS,
        custom_prompts=custom,
    )

    probed: set = set()
    for c in calls:
        probed.update(c["queries"])
    for cp in custom:
        assert cp in probed, f"custom prompt was NOT probed: {cp!r}"

    # And they land in the persisted probe runs the report reads.
    probed_runs = {r.get("query") for r in bd._flatten_probe_runs(out[SKU_KEY])}
    assert all(cp in probed_runs for cp in custom)


async def test_custom_prompts_run_once_on_first_sku_only(monkeypatch) -> None:
    """Multi-SKU audit: brand-level custom_prompts run ONCE (first SKU), not per
    SKU — otherwise N SKUs × M prompts × providers multiplies LLM calls."""
    sku_a, sku_b = "p1::v::a", "p1::v::b"

    async def _fake_sku_keys(products, merchant_id):
        return [sku_a, sku_b]

    async def _fake_load_ctx(sku_key, merchant_id):
        c = _sku_ctx()
        c["sku_key"] = sku_key
        return c

    seen: dict = {}

    async def _fake_probe(*, scan_mode, scan_target_id, merchant_id, store_id,
                          context, provider, max_runs, model=None,
                          model_is_override=False):
        # probe_run_id = "{run}:{sku_key}:{provider}:per_sku:{chunk}" and sku_key
        # itself contains ':' — strip the known prefix/suffix to recover it.
        body = scan_target_id[len("run_multi:"):].rsplit(":per_sku:", 1)[0]
        sk = body.rsplit(":", 1)[0]  # drop trailing ":{provider}"
        seen.setdefault(sk, []).extend(context.get("queries") or [])
        return {"provider": provider,
                "raw_runs": [{"query": q} for q in context.get("queries") or []]}

    monkeypatch.setattr(bd, "_sku_keys_for_per_sku_mode", _fake_sku_keys)
    monkeypatch.setattr(bd, "load_sku_context", _fake_load_ctx)
    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)

    await bd.run_per_sku_audit_probe_fanout(
        merchant_id=MERCHANT,
        audit_run_id="run_multi",
        products=[{"product_key": "p1"}],
        coverage_profile="pilot_gemini",
        prompts_per_sku=PROMPTS,
        custom_prompts=["my niche lane prompt"],
    )

    assert "my niche lane prompt" in seen.get(sku_a, [])
    assert "my niche lane prompt" not in seen.get(sku_b, [])

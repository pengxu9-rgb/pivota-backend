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


def _install(monkeypatch, *, fail_on=frozenset(), fail_first_attempt=frozenset()):
    """Stub the DB helpers; fake probe fails per DISTINCT CHUNK (keyed by the
    stable scan_target_id, so a chunk's retry shares its ordinal).

    - `fail_on`:            chunk ordinals that fail on EVERY attempt (a real,
                            un-retryable outage).
    - `fail_first_attempt`: chunk ordinals that fail their FIRST attempt only,
                            then succeed — the transient-blip case the one-retry
                            is meant to recover.
    Chunk ordinals are 1-based in first-seen order across both scan-mode groups.
    """
    # Zero the retry backoff so retry-exercising tests don't actually sleep.
    monkeypatch.setattr(bd, "_PER_SKU_AUDIT_CHUNK_RETRY_BACKOFF_S", 0.0)
    calls: List[Dict[str, Any]] = []
    ordinal_by_run: Dict[str, int] = {}
    attempts_by_run: Dict[str, int] = {}

    async def _fake_sku_keys(products, merchant_id):
        return [SKU_KEY]

    async def _fake_load_ctx(sku_key, merchant_id):
        return _sku_ctx()

    async def _fake_probe(*, scan_mode, scan_target_id, merchant_id, store_id,
                          context, provider, max_runs, model=None,
                          model_is_override=False):
        # One distinct chunk == one scan_target_id (probe_run_id); a retry of the
        # same chunk reuses it, so track attempts per-id and assign a stable
        # first-seen ordinal.
        if scan_target_id not in ordinal_by_run:
            ordinal_by_run[scan_target_id] = len(ordinal_by_run) + 1
        ordinal = ordinal_by_run[scan_target_id]
        attempts_by_run[scan_target_id] = attempts_by_run.get(scan_target_id, 0) + 1
        attempt = attempts_by_run[scan_target_id]
        calls.append({"chunk_idx": ordinal, "attempt": attempt,
                      "scan_mode": scan_mode, "max_runs": max_runs,
                      "queries": list(context.get("queries") or [])})
        if ordinal in fail_on or (ordinal in fail_first_attempt and attempt == 1):
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


async def test_single_transient_timeout_recovers_via_retry(monkeypatch) -> None:
    """A transient failure on a chunk's FIRST attempt is retried once and
    recovers — the full prompt budget still lands, nothing silently dropped."""
    chunks = _expected_chunks()
    n = len(chunks)
    assert n >= 3, "need >=3 chunks to exercise continue + retry"

    calls = _install(monkeypatch, fail_first_attempt={2})
    out = await _run()

    # Chunk 2 was attempted twice (fail -> retry); every other chunk once.
    assert len([c for c in calls if c["chunk_idx"] == 2]) == 2
    assert len(calls) == n + 1  # exactly one extra call (the single retry)
    assert all(c["max_runs"] <= 4 for c in calls)
    assert bd._PER_SKU_AUDIT_UPSTREAM_CHUNK_SIZE <= 4

    runs = bd._flatten_probe_runs(out[SKU_KEY])
    # Retry recovered chunk 2 -> the FULL budget is measured, not n-1 chunks.
    assert len(runs) == sum(len(c) for c in chunks) > 0


async def test_chunk_failing_both_attempts_continues_but_is_zeroed(monkeypatch) -> None:
    """A chunk that fails BOTH attempts (a real outage, not a blip) still doesn't
    break the loop — later chunks run — but that one chunk contributes no runs."""
    chunks = _expected_chunks()
    n = len(chunks)
    assert n >= 3

    calls = _install(monkeypatch, fail_on={2})
    out = await _run()

    # Chunk 2 attempted (retries + 1) times, then given up on.
    assert len([c for c in calls if c["chunk_idx"] == 2]) == (
        bd._PER_SKU_AUDIT_CHUNK_RETRIES + 1
    )
    # All chunks were still reached (the isolated failure did not break the loop).
    assert {c["chunk_idx"] for c in calls} == set(range(1, n + 1))

    runs = bd._flatten_probe_runs(out[SKU_KEY])
    # SKU not zeroed: every chunk EXCEPT 2 produced runs.
    expected_runs = sum(len(c) for i, c in enumerate(chunks, start=1) if i != 2)
    assert len(runs) == expected_runs > 0


async def test_consecutive_failures_bail_after_cap(monkeypatch) -> None:
    """If every chunk times out (even after its retry), bail after the
    consecutive-failure cap — don't grind through every remaining chunk. Each
    failed chunk now costs (retries + 1) calls before it's counted failed."""
    cap = bd._PER_SKU_AUDIT_MAX_CONSECUTIVE_CHUNK_FAILURES
    per_chunk = bd._PER_SKU_AUDIT_CHUNK_RETRIES + 1
    calls = _install(monkeypatch, fail_on=set(range(1, 99)))  # fail all, always
    out = await _run()

    distinct_chunks = {c["chunk_idx"] for c in calls}
    assert len(distinct_chunks) == cap, (
        f"expected bail after {cap} failed chunks, saw {len(distinct_chunks)}"
    )
    assert len(calls) == cap * per_chunk
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
    # #1521: the 4-prompt budget now rebalances to 2 branded + 2 unbranded (was
    # 4 branded), which the scan-mode partition routes into two probe groups
    # (findability vs organic). The whole 4-prompt budget is still probed — assert
    # on the preserved TOTAL rather than a single group's run count.
    assert sum(r["runs_count"] for r in fanout[SKU_KEY]) == 4
    # Two scan-mode partitions × two paths (fanout + direct) = 4 upstream calls
    # (was 2 when the thin SKU produced a single all-branded partition).
    assert len(calls) == 4


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


async def test_per_sku_custom_prompts_probe_on_their_own_sku(monkeypatch) -> None:
    """custom_prompts_by_sku attaches merchant prompts to a SPECIFIC SKU: the
    prompt probes inside THAT SKU's context (joining its per-prompt results),
    not on the first SKU like the brand-level slots."""
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
        body = scan_target_id[len("run_by_sku:"):].rsplit(":per_sku:", 1)[0]
        sk = body.rsplit(":", 1)[0]
        seen.setdefault(sk, []).extend(context.get("queries") or [])
        return {"provider": provider,
                "raw_runs": [{"query": q} for q in context.get("queries") or []]}

    monkeypatch.setattr(bd, "_sku_keys_for_per_sku_mode", _fake_sku_keys)
    monkeypatch.setattr(bd, "load_sku_context", _fake_load_ctx)
    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)

    out = await bd.run_per_sku_audit_probe_fanout(
        merchant_id=MERCHANT,
        audit_run_id="run_by_sku",
        products=[{"product_key": "p1"}],
        coverage_profile="pilot_gemini",
        prompts_per_sku=PROMPTS,
        custom_prompts_by_sku={sku_b: ["collagen jelly for red-eye flights"]},
    )

    assert "collagen jelly for red-eye flights" not in seen.get(sku_a, [])
    assert "collagen jelly for red-eye flights" in seen.get(sku_b, [])

    # The probed row carries the merchant stamp downstream consumers key on:
    # axis="custom" + axis_metadata.prompt_source="merchant_custom" (evidence
    # selector exemption + UI badge).
    rows = [
        r for r in bd._flatten_probe_runs(out[sku_b])
        if r.get("query") == "collagen jelly for red-eye flights"
    ]
    assert rows, "per-SKU custom prompt missing from persisted probe runs"
    meta = rows[0].get("axis_metadata") or {}
    assert meta.get("axis") == "custom"
    assert meta.get("prompt_source") == "merchant_custom"
    # Scope keeps per-SKU rows out of the brand-level "Your prompts" panel.
    assert meta.get("custom_scope") == "sku"
    # Review round: a merchant prompt has NO generator weight — stamping a
    # synthetic 0.0 sidewalk_intent_weight zeroed its opportunity score (the
    # metadata passthrough beats the heuristic intent classifier downstream).
    assert "sidewalk_intent_weight" not in meta
    # And the brand-level panel grouping excludes per-SKU rows entirely.
    panel_prompts = set(bd._custom_prompt_runs_by_prompt(out))
    assert "collagen jelly for red-eye flights" not in panel_prompts


async def test_brand_custom_prompts_stay_in_brand_panel(monkeypatch) -> None:
    """Brand-level slots keep flowing to the 'Your prompts' panel (scope
    'brand'), with no synthetic intent weight stamped on them either."""
    _install(monkeypatch, fail_on=set())
    out = await bd.run_per_sku_audit_probe_fanout(
        merchant_id=MERCHANT,
        audit_run_id="run_scope_test",
        products=[{"product_key": "p1"}],
        coverage_profile="pilot_gemini",
        prompts_per_sku=PROMPTS,
        custom_prompts=["my brand slot prompt"],
    )
    rows = [
        r for r in bd._flatten_probe_runs(out[SKU_KEY])
        if r.get("query") == "my brand slot prompt"
    ]
    assert rows, "brand custom prompt missing from persisted probe runs"
    meta = rows[0].get("axis_metadata") or {}
    assert meta.get("custom_scope") == "brand"
    assert "sidewalk_intent_weight" not in meta
    assert "my brand slot prompt" in set(bd._custom_prompt_runs_by_prompt(out))


async def test_per_sku_customs_pinned_into_basis_brand_customs_not(monkeypatch) -> None:
    """Pinning contract: this SKU's own merchant prompts join _selected_specs_out
    (the set persisted as the next run's pinned basis -> week-over-week
    comparability); the brand-level one-shot slots do NOT."""
    ctxs: list = []

    async def _fake_sku_keys(products, merchant_id):
        return [SKU_KEY]

    async def _fake_load_ctx(sku_key, merchant_id):
        c = _sku_ctx()
        ctxs.append(c)
        return c

    async def _fake_probe(*, scan_mode, scan_target_id, merchant_id, store_id,
                          context, provider, max_runs, model=None,
                          model_is_override=False):
        return {"provider": provider,
                "raw_runs": [{"query": q} for q in context.get("queries") or []]}

    monkeypatch.setattr(bd, "_sku_keys_for_per_sku_mode", _fake_sku_keys)
    monkeypatch.setattr(bd, "load_sku_context", _fake_load_ctx)
    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)

    await bd.run_per_sku_audit_probe_fanout(
        merchant_id=MERCHANT,
        audit_run_id="run_pin_test",
        products=[{"product_key": "p1"}],
        coverage_profile="pilot_gemini",
        prompts_per_sku=PROMPTS,
        custom_prompts=["brand level one-shot prompt"],
        custom_prompts_by_sku={SKU_KEY: ["my pinned niche prompt"]},
    )

    assert ctxs, "fanout never loaded the sku ctx"
    selected = ctxs[0].get("_selected_specs_out") or []
    selected_queries = {str(r.get("query")) for r in selected}
    assert "my pinned niche prompt" in selected_queries
    assert "brand level one-shot prompt" not in selected_queries
    # And the pinned record keeps its stamps, so a pinned re-run re-probes it
    # with the same axis + prompt_source (basis identity, badge included).
    pinned = next(
        r for r in selected if r.get("query") == "my pinned niche prompt"
    )
    assert pinned.get("axis") == "custom"
    assert pinned.get("source") == "merchant_custom"
    assert pinned.get("custom_scope") == "sku"
    # clean_selected_specs (durable storage) must preserve the scope, or the
    # pinned re-probe would resurface in the brand-level panel next run.
    from services.prompt_basis import clean_selected_specs

    stored = clean_selected_specs(selected)
    stored_custom = next(
        r for r in stored if r.get("query") == "my pinned niche prompt"
    )
    assert stored_custom.get("custom_scope") == "sku"
    assert stored_custom.get("source") == "merchant_custom"

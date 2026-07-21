"""#1502: failing_prompts dedupes at the SOURCE (one entry per unique query).

Probes run per provider × per repeat; pre-fix the report listed one entry per
failing RUN, so merchant reports showed the same losing query 2-3× per SKU,
scaling with provider count (worst measured: 19 duplicate rows in one 3-SKU
us_shopper run; present in every vertical). The earlier fix lived only in the
win-plan consumer's merge — these tests pin the source-level dedup plus the
win plan's tolerance of both entry shapes.
"""
from services.agent_center_bd_report_service import _failing_prompts


def _fail(query, provider, competitors=None, sources=None):
    run = {"query": query, "_provider": provider, "parsed": {}}
    if competitors:
        run["competitors_listed"] = competitors
    if sources:
        run["grounding_sources"] = sources
    return run


def test_duplicate_failing_runs_merge_to_one_entry():
    out = _failing_prompts([
        _fail("best camera drone", "gemini", ["DJI"], [{"uri": "a"}]),
        _fail("Best  Camera  Drone", "chatgpt", ["DJI", "Autel"], [{"uri": "b"}]),
        _fail("best camera drone", "chatgpt"),          # repeat run, same engine
        _fail("drone for travel", "gemini"),
    ])
    assert [e["query"] for e in out] == ["best camera drone", "drone for travel"]
    merged = out[0]
    # Legacy singular stays (first engine); union carries every failing engine.
    assert merged["provider"] == "gemini"
    assert merged["providers"] == ["gemini", "chatgpt"]
    # Evidence merges: competitors order-preserving union, sources concatenated.
    assert merged["competitors_named"] == ["DJI", "Autel"]
    assert merged["grounding_sources"] == [{"uri": "a"}, {"uri": "b"}]


def test_passing_runs_never_emit():
    out = _failing_prompts([
        {"query": "q1", "_provider": "gemini", "parsed": {"sku_mentioned": True}},
        {"query": "q2", "_provider": "gemini", "parsed": {}, "product_visible": True},
    ])
    assert out == []


def test_cap_counts_unique_queries_and_post_cap_duplicates_still_merge():
    runs = [_fail(f"query {i}", "gemini") for i in range(25)]
    # A duplicate of query 0 arriving AFTER the cap is hit must still merge.
    runs.append(_fail("query 0", "chatgpt"))
    out = _failing_prompts(runs, cap=20)
    assert len(out) == 20
    assert out[0]["providers"] == ["gemini", "chatgpt"]


def test_win_plan_merge_reads_both_entry_shapes():
    from services.win_plan_builder import build_win_plan

    def plan_providers(fp_row):
        report = {"failing_prompts": [fp_row], "sku_key": "sku-1", "sku_title": "T"}
        plan = build_win_plan(
            per_sku_reports=[report], authority_map=None,
            merchant_name="M", merchant_category=None,
        )
        rows = (plan or {}).get("sku_plans") or []
        queries = (rows[0].get("losing_queries") or []) if rows else []
        return (queries[0].get("providers") if queries else None)

    new_shape = {
        "query": "best camera drone", "axis": "category",
        "provider": "gemini", "providers": ["gemini", "chatgpt"],
        "grounding_sources": [], "competitors_named": [],
    }
    old_shape = {
        "query": "best camera drone", "axis": "category",
        "provider": "chatgpt",
        "grounding_sources": [], "competitors_named": [],
    }
    assert plan_providers(new_shape) == ["chatgpt", "gemini"]  # sorted label
    assert plan_providers(old_shape) == ["chatgpt"]


def test_errored_runs_are_not_failing_evidence():
    """An upstream-errored run (`__error__:` raw / `error` key) never answered,
    so it is not evidence the engine failed to cite — pre-fix a wholesale-
    errored provider lane listed every one of its prompts as failing and
    stamped itself into `providers` (2026-07-21 scale smoke, run 509cf81c)."""
    out = _failing_prompts([
        _fail("best camera drone", "gemini"),
        {"query": "best camera drone", "_provider": "chatgpt",
         "raw": "__error__:429 You exceeded your current quota", "parsed": None},
        {"query": "drone for travel", "_provider": "chatgpt",
         "raw": "__error__:429 You exceeded your current quota"},
        {"query": "drone under 500", "_provider": "gemini", "error": "quota"},
    ])
    # Only the genuinely-answered failing run survives; the errored chatgpt
    # run neither adds queries nor joins the failing-engine union.
    assert [e["query"] for e in out] == ["best camera drone"]
    assert out[0]["providers"] == ["gemini"]

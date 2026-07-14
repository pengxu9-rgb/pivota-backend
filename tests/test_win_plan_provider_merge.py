"""Win-plan per-provider merge: one labeled row per losing query.

failing_prompts emits one entry per provider run, and the win plan used to map
them 1:1 into losing-query rows — the same query rendered as unlabeled
duplicates with contradictory limits ("no publisher to pitch here" beside
"get cited in techradar" for the same query — live Mojawa run 7420c2b5).

Now: rows merge by query. Grounding sources union (a publisher target found by
ANY engine wins the row), competitor benchmark unions (deduped by the #1324
normalizer), and a `providers` label lists the engines that failed it.
"""

from services.win_plan_builder import build_win_plan


def _plan(failing_prompts, authority_hosts=None):
    per_sku_reports = [
        {
            "sku_key": "sku-x",
            "sku_title": "X",
            "failing_prompts": failing_prompts,
        }
    ]
    authority_map = {
        "skus": [{"sku_key": "sku-x", "authority_hosts": authority_hosts or []}]
    }
    return build_win_plan(
        per_sku_reports=per_sku_reports,
        authority_map=authority_map,
        merchant_name="Mojawa",
        merchant_category="Headphones",
    )


def _fp(query, provider, sources=None, competitors=None):
    return {
        "query": query,
        "axis": "category",
        "provider": provider,
        "grounding_sources": sources or [],
        "competitors_named": competitors or [],
    }


def test_duplicate_provider_rows_merge_into_one_labeled_row():
    authority_hosts = [
        {
            "host": "techradar.com",
            "citation_role": "editorial_review",
            "evidence_urls": ["vx://techradar-1"],
        }
    ]
    plan = _plan(
        [
            # gemini failed with no resolvable sources...
            _fp("what headphones should I buy", "gemini", competitors=["Sony", "SHOKZ"]),
            # ...chatgpt failed but grounded techradar.
            _fp(
                "what headphones should I buy",
                "chatgpt",
                sources=[{"uri": "vx://techradar-1"}],
                competitors=["Shokz", "Bose"],
            ),
        ],
        authority_hosts=authority_hosts,
    )
    rows = plan["sku_plans"][0]["losing_queries"]
    assert len(rows) == 1, "per-provider duplicates must merge into one row"
    row = rows[0]
    assert row["providers"] == ["chatgpt", "gemini"]
    # the union grounds the row: chatgpt's techradar target wins -> publisher
    assert row["win_path"] == "publisher"
    assert "techradar.com" in (row["win_condition"] or "")
    assert row["limit"] is None
    # competitor benchmark unions and dedupes (SHOKZ+Shokz -> Shokz via #1324)
    assert row["competitor_benchmark"] == ["Sony", "Shokz", "Bose"]


def test_coverage_counts_unique_queries():
    plan = _plan(
        [
            _fp("best headphones", "gemini"),
            _fp("best headphones", "chatgpt"),
            _fp("bone conduction headphones for swimming", "gemini"),
        ]
    )
    coverage = plan["sku_plans"][0]["coverage"]
    assert coverage["losing_category_queries"] == 2


def test_single_provider_row_still_labeled():
    # A specific (non-head) query: gets the own-content path AND the label.
    # ("best headphones" is now gated to win_path=None — a bare head term
    # can't be won with own content either; see test_report_copy_cleanup.)
    plan = _plan([_fp("bone conduction headphones for swimming", "gemini")])
    row = plan["sku_plans"][0]["losing_queries"][0]
    assert row["providers"] == ["gemini"]
    assert row["win_path"] == "own_content"


def test_missing_provider_degrades_to_empty_label():
    # Old stored failing_prompts rows (pre-deploy) carry no provider field —
    # the merge must not fabricate one.
    plan = _plan(
        [
            {
                "query": "best headphones",
                "axis": "category",
                "grounding_sources": [],
                "competitors_named": [],
            }
        ]
    )
    row = plan["sku_plans"][0]["losing_queries"][0]
    assert row["providers"] == []


def test_failing_prompts_stamp_provider():
    from services.agent_center_bd_report_service import _failing_prompts

    probe_runs = [
        {
            "runs_count": 1,
            "raw_runs": [
                {
                    "query": "best headphones",
                    "_provider": "gemini",
                    "parsed": {"correct_sku": False, "product_visible": False},
                    "url_match": {"in_grounding": False, "llm_self_report": {}},
                    "axis_metadata": {"axis": "category"},
                    "grounding_sources": [],
                }
            ],
        }
    ]
    out = _failing_prompts(probe_runs)
    assert out and out[0]["provider"] == "gemini"


def test_failing_prompts_strips_merchant_own_brand_from_competitors():
    """#1382 follow-up (nit #3, sibling path): _failing_prompts fans a run's raw
    competitors_listed into `competitors_named`, which feeds the win-plan
    competitor benchmark (win_plan_builder._competitor_benchmark). An engine that
    echoes the merchant back inside competitors_listed (common on branded /
    where-to-buy queries) must not have the merchant's OWN brand surface as a
    "competitor to beat" for its own failing prompt. Only the genuine rival
    survives; the authority-map path already applies the same own-brand skip."""
    from services.agent_center_bd_report_service import _failing_prompts
    from services.brand_alias import derive_brand_aliases

    probe_runs = [{
        "raw_runs": [{
            "query": "where to buy BB Lab collagen",
            "_provider": "gemini",
            "parsed": {
                "correct_sku": False,
                "product_visible": False,
                # LLM listed the merchant itself (exact + spaced + aliased) next
                # to one real rival.
                "competitors_listed": ["BB Lab Global", "BB Lab", "bblab", "GlowCo"],
            },
            "url_match": {"in_grounding": False, "llm_self_report": {}},
            "axis_metadata": {"axis": "category"},
            "grounding_sources": [],
        }],
    }]
    # No brand context → legacy behaviour (nothing stripped), proving the filter
    # is what removes the own brand, not some other path.
    unfiltered = _failing_prompts(probe_runs)[0]["competitors_named"]
    assert "BB Lab" in unfiltered

    named = _failing_prompts(
        probe_runs,
        brand_lower="bb lab global",
        brand_aliases=derive_brand_aliases("BB Lab Global", "bblab.com"),
    )[0]["competitors_named"]
    assert named == ["GlowCo"], named

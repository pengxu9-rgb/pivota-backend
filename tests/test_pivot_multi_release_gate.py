from __future__ import annotations

import scripts.pivot_multi_release_gate as module


def test_release_gate_summaries_are_source_and_semantic_class_aware() -> None:
    cases = [
        {
            "source": "shopping_agent",
            "query_semantic_class": "default",
            "pivot_rollout_mode": "shadow",
            "pass": True,
        },
        {
            "source": "shopping_agent",
            "query_semantic_class": "fragrance",
            "pivot_rollout_mode": "shadow",
            "pass": True,
        },
        {
            "source": "shopping-agent-ui",
            "query_semantic_class": "default",
            "pivot_rollout_mode": "legacy",
            "pass": False,
        },
    ]

    source_summary = module._summarize_by_source(cases)
    semantic_summary = module._summarize_by_semantic_class(cases)

    assert source_summary["shopping_agent"]["source_stage"] == "stage_1"
    assert source_summary["shopping_agent"]["ready_for_canary"] is True
    assert source_summary["shopping-agent-ui"]["failed_cases"] == 1
    assert semantic_summary["default"]["sample_count"] == 2
    assert semantic_summary["default"]["sources"]["shopping_agent"] == 1
    assert semantic_summary["default"]["sources"]["shopping-agent-ui"] == 1


def test_release_gate_can_filter_corpus_by_source() -> None:
    corpus = [
        {"case_id": "a", "source": "shopping_agent", "query": "x"},
        {"case_id": "b", "source": "shopping-agent-ui", "query": "y"},
    ]

    filtered = module._filter_corpus_by_source(corpus, ["shopping-agent-ui"])

    assert [case["case_id"] for case in filtered] == ["b"]

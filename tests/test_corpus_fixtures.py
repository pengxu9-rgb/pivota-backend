from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "scripts" / "fixtures"


def _load_fixture(name: str):
    payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return payload


def test_serve_canary_corpus_covers_all_sources_and_categories() -> None:
    payload = _load_fixture("serve_canary_corpus.json")
    required_keys = {"case_id", "category", "query", "source", "page", "limit", "semantic_class"}
    by_source = defaultdict(set)
    for case in payload:
        assert required_keys.issubset(case)
        by_source[case["source"]].add(case["category"])
    assert set(by_source) == {"shopping_agent", "shopping-agent-ui", "shopping-agent-web"}
    assert by_source["shopping_agent"] == {"merchant_exact", "product_exact", "blended_incentive", "vertical_style_query"}
    assert by_source["shopping-agent-ui"] == {"merchant_exact", "product_exact", "blended_incentive", "vertical_style_query"}
    assert by_source["shopping-agent-web"] == {"merchant_exact", "product_exact", "blended_incentive", "vertical_style_query"}


def test_beauty_ranking_golden_corpus_v2_has_expected_size_and_mix() -> None:
    payload = _load_fixture("beauty_ranking_golden_corpus_v2.json")
    required_keys = {"case_id", "query", "source", "page", "limit", "semantic_class", "origin"}
    assert len(payload) == 30
    origin_counts = Counter()
    for case in payload:
        assert required_keys.issubset(case)
        assert case["semantic_class"] == "beauty"
        origin_counts[case["origin"]] += 1
    assert origin_counts["production_log"] == 18
    assert origin_counts["curated_regression"] == 12


def test_generic_commerce_shadow_corpus_is_limited_to_default_and_fragrance() -> None:
    payload = _load_fixture("generic_commerce_shadow_corpus.json")
    required_keys = {"case_id", "query", "source", "page", "limit", "semantic_class"}
    semantic_classes = Counter()
    sources = Counter()
    for case in payload:
        assert required_keys.issubset(case)
        semantic_classes[case["semantic_class"]] += 1
        sources[case["source"]] += 1
    assert set(semantic_classes) == {"default", "fragrance"}
    assert set(sources) == {"shopping_agent", "shopping-agent-ui", "shopping-agent-web"}

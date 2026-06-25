"""extract_winnable_prompts — LLM value-prop extraction into NON-branded,
specific, winnable discovery prompts (vs unwinnable generic category heads)."""
from __future__ import annotations

import pytest

from services import agent_center_bd_report_service as svc


def test_parse_winnable_prompts_handles_array_and_code_fence():
    assert svc._parse_winnable_prompts('["a", "b"]') == ["a", "b"]
    # tolerant of prose / code fences around the JSON array
    fenced = 'Here you go:\n```json\n["best shea butter hair oil for damaged hair"]\n```'
    assert svc._parse_winnable_prompts(fenced) == [
        "best shea butter hair oil for damaged hair"
    ]
    assert svc._parse_winnable_prompts("not json") == []
    assert svc._parse_winnable_prompts("") == []


@pytest.mark.asyncio
async def test_extract_filters_branded_short_and_dupes(monkeypatch):
    import json as _json
    from config.settings import settings as app_settings
    import services.llm_synthesis as llm

    monkeypatch.setattr(app_settings, "strategic_brief_provider", "deepseek", raising=False)

    async def fake_synthesize(**kwargs):
        return {
            "text": _json.dumps([
                "best shea butter hair oil for damaged hair",   # keep
                "anuko hair oil for frizz",                     # drop: brand leaked
                "hair oil",                                     # drop: < 3 words
                "best green tea hair treatment for dry scalp",  # keep
                "BEST shea butter hair oil for damaged hair",   # drop: dup (norm)
            ])
        }

    # extract_winnable_prompts imports these from services.llm_synthesis at call
    # time, so patch them there.
    monkeypatch.setattr(llm, "synthesize", fake_synthesize, raising=False)
    monkeypatch.setattr(llm, "configured_key_for_provider", lambda p: "sk-test", raising=False)
    monkeypatch.setattr(llm, "default_model_for_provider", lambda p: "deepseek-chat", raising=False)

    sku_ctx = {"product": {"title": "Anuko Bond & Repair Hair Oil", "vendor": "Anuko"}}
    out = await svc.extract_winnable_prompts(sku_ctx)

    assert "best shea butter hair oil for damaged hair" in out
    assert "best green tea hair treatment for dry scalp" in out
    assert not any("anuko" in p for p in out)          # brand filtered
    assert all(len(p.split()) >= 3 for p in out)        # short filtered
    assert len(out) == len(set(out))                    # deduped


def test_spec_builder_appends_winnable_prompts_as_discovery():
    sku_ctx = {
        "product": {"title": "Anuko Bond & Repair Hair Oil", "vendor": "Anuko"},
        "_winnable_prompts": [
            "best hair oil for chemically treated damaged hair",
            "bond repair hair oil for breakage",
        ],
    }
    specs, _title, _ptype = svc._build_per_sku_base_query_specs(sku_ctx)
    queries = {q for q, _axis in specs}
    assert "best hair oil for chemically treated damaged hair" in queries
    assert "bond repair hair oil for breakage" in queries
    # they ride the discovery ("category") axis
    assert ("bond repair hair oil for breakage", "category") in specs

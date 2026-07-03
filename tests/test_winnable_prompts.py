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


@pytest.mark.asyncio
async def test_prompt_gen_provider_overrides_brief_provider(monkeypatch):
    """PROMPT_GEN_PROVIDER/MODEL select the stage-1 generation model
    independently of the strategic brief, so generators can be A/B compared in
    prod. Fallback chain: prompt_gen_* -> strategic_brief_* -> provider default."""
    import json as _json
    from config.settings import settings as app_settings
    import services.llm_synthesis as llm

    monkeypatch.setattr(app_settings, "strategic_brief_provider", "deepseek", raising=False)
    monkeypatch.setattr(app_settings, "strategic_brief_model", "deepseek-chat", raising=False)
    monkeypatch.setattr(app_settings, "prompt_gen_provider", "openai", raising=False)
    monkeypatch.setattr(app_settings, "prompt_gen_model", "gpt-4o-mini", raising=False)

    captured = {}

    async def fake_synthesize(**kwargs):
        captured.update(kwargs)
        return {"text": _json.dumps(["best bond repair oil for damaged hair"])}

    monkeypatch.setattr(llm, "synthesize", fake_synthesize, raising=False)
    monkeypatch.setattr(llm, "configured_key_for_provider", lambda p: "sk-test", raising=False)
    monkeypatch.setattr(llm, "default_model_for_provider", lambda p: "default-model", raising=False)

    out = await svc.extract_winnable_prompts(
        {"product": {"title": "Anuko Bond & Repair Hair Oil", "vendor": "Anuko"}}
    )
    assert out == ["best bond repair oil for damaged hair"]
    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-4o-mini"


def test_prompt_gen_enabled_setting_defaults_on():
    """The stage-1 LLM prompt-gen gate exists and defaults ON (env kill switch
    AUDIT_LLM_PROMPT_GEN_ENABLED). The worker reads this as the default when a
    launch option doesn't specify winnable_prompts."""
    from config.settings import settings as app_settings

    assert getattr(app_settings, "prompt_gen_enabled") is True


def test_query_records_stamp_llm_winnable_source():
    """LLM-generated discovery prompts are stamped source='llm_winnable' in the
    query records so per-model prompt quality is comparable across runs."""
    sku_ctx = {
        "sku_key": "s1",
        "merchant_id": "m1",
        "product": {
            "title": "Anuko Bond & Repair Hair Oil",
            "vendor": "Anuko",
            "product_type": "hair oil",
        },
        "_winnable_prompts": ["bond repair hair oil for chemically damaged hair"],
    }
    records = svc._build_per_sku_audit_query_records(sku_ctx, 14)
    by_query = {str(r.get("query")).lower(): r for r in records}
    marked = by_query.get("bond repair hair oil for chemically damaged hair")
    assert marked is not None, "winnable prompt must survive budgeting at target=14"
    assert marked.get("source") == "llm_winnable"
    assert marked.get("axis") == "category"
    # deterministic records are untouched
    assert all(
        r.get("source") != "llm_winnable"
        for q, r in by_query.items()
        if q != "bond repair hair oil for chemically damaged hair"
    )

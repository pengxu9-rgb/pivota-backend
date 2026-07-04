"""W3 — the single LLM JSON parser. One implementation of the bare→fence→
substring recovery that ~14 modules each carried a copy of, plus a CI guard
that new LLM call sites route through it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.llm_io import (
    parse_llm_json,
    parse_llm_object,
    parse_llm_str_array,
    parse_stats,
)


# ---- object parsing ----------------------------------------------------------

def test_bare_object():
    assert parse_llm_object('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_fenced_object():
    assert parse_llm_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_llm_object('```\n{"a": 1}\n```') == {"a": 1}


def test_object_embedded_in_prose():
    text = 'Sure! Here is the result:\n```json\n{"verdict": "pass"}\n```\nHope that helps.'
    assert parse_llm_object(text) == {"verdict": "pass"}


def test_object_substring_no_fence():
    assert parse_llm_object('noise {"a": 1} trailing') == {"a": 1}


def test_unterminated_fence_recovers():
    # The Rahua-class leak: a ```json prefix with no closing fence.
    assert parse_llm_object('```json {"a": 1, "b": 2}') == {"a": 1, "b": 2}


def test_object_rejects_array_and_scalar():
    assert parse_llm_object("[1, 2, 3]") is None
    assert parse_llm_object("42") is None
    assert parse_llm_object('"hello"') is None


def test_object_on_garbage_is_none():
    assert parse_llm_object("") is None
    assert parse_llm_object(None) is None
    assert parse_llm_object("not json at all") is None
    assert parse_llm_object("{ broken: ") is None


# ---- array parsing -----------------------------------------------------------

def test_str_array_bare_and_fenced():
    assert parse_llm_str_array('["a", "b"]') == ["a", "b"]
    assert parse_llm_str_array('```json\n["a", "b"]\n```') == ["a", "b"]


def test_str_array_filters_non_strings():
    assert parse_llm_str_array('["a", 2, null, "b"]') == ["a", "b"]


def test_str_array_embedded():
    assert parse_llm_str_array('Here you go: ["x", "y"] done') == ["x", "y"]


def test_str_array_on_object_or_garbage_is_empty():
    assert parse_llm_str_array('{"a": 1}') == []
    assert parse_llm_str_array("nope") == []
    assert parse_llm_str_array(None) == []


# ---- expect="any" ------------------------------------------------------------

def test_any_returns_object_or_array():
    assert parse_llm_json('{"a": 1}', expect="any") == {"a": 1}
    assert parse_llm_json("[1, 2]", expect="any") == [1, 2]


def test_any_prefers_first_opening_delimiter():
    # object opens first
    assert parse_llm_json('{"a": [1]} then [2]', expect="any") == {"a": [1]}


# ---- telemetry ---------------------------------------------------------------

def test_outcome_counter_tracks_ok_recovered_failed():
    before = parse_stats()
    parse_llm_object('{"a": 1}', label="t_ok")
    parse_llm_object('```json\n{"a": 1}\n```', label="t_rec")
    parse_llm_object("garbage", label="t_fail")
    after = parse_stats()
    assert after.get("t_ok:ok", 0) == before.get("t_ok:ok", 0) + 1
    assert after.get("t_rec:recovered", 0) == before.get("t_rec:recovered", 0) + 1
    assert after.get("t_fail:failed", 0) == before.get("t_fail:failed", 0) + 1
    # global counters advance too
    assert after.get("ok", 0) >= before.get("ok", 0) + 1


# ---- equivalence with the parsers this replaces ------------------------------

def test_matches_legacy_deepseek_shapes():
    # bare, fenced, embedded — the three _parse_deepseek_response handled
    for text, want in [
        ('{"product_visible": true}', {"product_visible": True}),
        ('```json\n{"product_visible": false}\n```', {"product_visible": False}),
        ('answer: {"product_visible": true} end', {"product_visible": True}),
    ]:
        assert parse_llm_object(text) == want


# ---- CI guard: no NEW raw json.loads on model output outside llm_io -----------

def test_no_new_raw_llm_json_parsers():
    """Every module that previously hand-rolled a bare→fence→substring JSON
    parser now routes through services/llm_io. This scans for the tell-tale
    fence regex (```json … ```) paired with a json.loads in the same file —
    the signature of a local re-implementation — and holds the known remaining
    set flat so a NEW one is a red build, not a silent 15th copy.

    Allowlist entries are parsers not yet migrated (payload-envelope extractors
    with provider-specific shapes) or the shared module itself. Shrinking this
    list is the W3 follow-up; it must never grow.
    """
    services = Path("services")
    fence = re.compile(r"```\(\?:json\)\?|```json|```\(\?:json\)")
    # Files still carrying a local fence-strip + json.loads. Provider-envelope
    # extractors (Gemini/OpenAI payload → text → json) whose migration is W3b,
    # plus llm_io itself. NEVER add to this without migrating first.
    allow = {
        "services/llm_io.py",
        # agent_center: its remaining fence handling is _salvage_competitor_prose
        # (specialized TRUNCATED-envelope recovery — its well-formed parse now
        # delegates to llm_io); its json.loads are JSONB DB decodes, not LLM
        # output. A 17k-line heterogeneous module the file-level heuristic can't
        # cleanly judge.
        "services/agent_center_bd_report_service.py",
        # Provider-envelope extractors (payload → text → json) whose migration
        # is W3b. NEVER add without migrating first; shrinking this is the goal.
        "services/catalog_enrichment_agent/gemini_url_validator.py",
        "services/bd_brand_signals.py",
        "services/bd_brand_category_inferrer.py",
        "services/executor_agents/content_brief.py",
        "services/pdp_matcher/llm_match.py",
        "services/pdp_label_agent.py",
    }
    offenders = []
    for path in services.rglob("*.py"):
        rel = str(path)
        if rel in allow:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # A ```json fence literal AND a json.loads in the same module = the
        # local-parser signature the shared module replaces.
        if ("```json" in text or "```(?:json)?" in text) and "json.loads" in text:
            offenders.append(rel)
    assert not offenders, (
        "New local LLM-JSON parser(s) — route through services/llm_io "
        f"(parse_llm_json/parse_llm_object/parse_llm_str_array): {offenders}"
    )


# ---- W3b: generate_structured (schema enforcement + one targeted repair) ------

import services.llm_io as llm_io


class _FakeSynth:
    """Records synthesize() calls and replays scripted responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __call__(self, *, system, user, provider, model, max_tokens,
                       response_schema=None):
        self.calls.append({"user": user, "response_schema": response_schema})
        if isinstance(self._responses[0], Exception):
            raise self._responses.pop(0)
        return {"text": self._responses.pop(0)}


def _patch_synth(monkeypatch, responses):
    import services.llm_synthesis as synth_mod
    fake = _FakeSynth(responses)
    monkeypatch.setattr(synth_mod, "synthesize", fake)
    return fake


@pytest.mark.asyncio
async def test_structured_ok_first_try(monkeypatch):
    fake = _patch_synth(monkeypatch, ['{"a": 1}'])
    res = await llm_io.generate_structured(
        system="s", user="u", provider="gemini",
        schema={"type": "object"}, expect="object",
    )
    assert res.outcome == "ok" and res.ok
    assert res.value == {"a": 1}
    assert res.violations == []
    # the schema reached the provider
    assert fake.calls[0]["response_schema"] == {"type": "object"}
    assert len(fake.calls) == 1  # no repair needed


@pytest.mark.asyncio
async def test_structured_repairs_on_validation_failure(monkeypatch):
    fake = _patch_synth(monkeypatch, ['{"a": 1}', '{"a": 1, "b": 2}'])

    def needs_b(value):
        return [] if "b" in value else ["missing required key 'b'"]

    res = await llm_io.generate_structured(
        system="s", user="draft the thing", provider="openai",
        validate=needs_b, expect="object",
    )
    assert res.outcome == "repaired" and res.ok
    assert res.value == {"a": 1, "b": 2}
    assert len(fake.calls) == 2
    # the repair retry fed the SPECIFIC violation back
    assert "missing required key 'b'" in fake.calls[1]["user"]
    assert "draft the thing" in fake.calls[1]["user"]  # original preserved


@pytest.mark.asyncio
async def test_structured_fails_honestly_after_repair(monkeypatch):
    # both attempts violate -> failed (not a silent fallback)
    fake = _patch_synth(monkeypatch, ['{"a": 1}', '{"a": 1}'])
    res = await llm_io.generate_structured(
        system="s", user="u", provider="deepseek",
        validate=lambda v: ["still wrong"], expect="object",
    )
    assert res.outcome == "failed" and not res.ok
    assert res.violations == ["still wrong"]
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_structured_unparseable_triggers_repair(monkeypatch):
    fake = _patch_synth(monkeypatch, ["not json", '{"a": 1}'])
    res = await llm_io.generate_structured(
        system="s", user="u", provider="gemini", expect="object",
    )
    assert res.outcome == "repaired"
    assert res.value == {"a": 1}
    assert "not valid JSON" in fake.calls[1]["user"]


@pytest.mark.asyncio
async def test_structured_no_repair_flag(monkeypatch):
    fake = _patch_synth(monkeypatch, ["garbage"])
    res = await llm_io.generate_structured(
        system="s", user="u", provider="gemini", expect="object", repair=False,
    )
    assert res.outcome == "failed"
    assert len(fake.calls) == 1  # no retry when repair disabled


@pytest.mark.asyncio
async def test_structured_provider_error_never_raises(monkeypatch):
    from services.llm_synthesis import LLMSynthesisError
    _patch_synth(monkeypatch, [LLMSynthesisError("boom", provider="gemini")])
    res = await llm_io.generate_structured(
        system="s", user="u", provider="gemini", expect="object",
    )
    assert res.outcome == "error" and not res.ok
    assert res.value is None
    assert "boom" in (res.error or "")


@pytest.mark.asyncio
async def test_structured_bad_validator_does_not_crash(monkeypatch):
    _patch_synth(monkeypatch, ['{"a": 1}'])

    def exploding(value):
        raise RuntimeError("validator bug")

    res = await llm_io.generate_structured(
        system="s", user="u", provider="gemini", validate=exploding,
        expect="object",
    )
    # a broken validator is treated as no-violation, not a crash
    assert res.outcome == "ok"
    assert res.value == {"a": 1}

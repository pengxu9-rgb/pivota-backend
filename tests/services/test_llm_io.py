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

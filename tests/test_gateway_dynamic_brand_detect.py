"""GATEWAY_DYNAMIC_BRAND_DETECT — recognize real catalog brands so branded
queries stop falling to ingredient/external-seed junk.
See docs/gateway-brand-detection-recall-fix.md."""
from __future__ import annotations

import importlib

agent_api = importlib.import_module("routes.agent_api")


def _set_cache(brands):
    agent_api._DYNAMIC_BRAND_SET = frozenset(brands)


def test_off_does_not_use_catalog_brands(monkeypatch):
    monkeypatch.delenv("GATEWAY_DYNAMIC_BRAND_DETECT", raising=False)
    _set_cache({"the ordinary", "skin1004", "anuko"})
    # Flag off → the catalog set is ignored; "the ordinary niacinamide" is NOT
    # detected as a brand (static list doesn't include it).
    r = agent_api._detect_brand_query("the ordinary niacinamide 10% + zinc 1%")
    assert r["brand_like"] is False


def test_on_detects_catalog_brand(monkeypatch):
    monkeypatch.setenv("GATEWAY_DYNAMIC_BRAND_DETECT", "1")
    _set_cache({"the ordinary", "skin1004", "anuko", "beauty of joseon"})
    r = agent_api._detect_brand_query("the ordinary niacinamide 10% + zinc 1%")
    assert r["brand_like"] is True
    assert r["mode"] == "catalog"
    assert "the ordinary" in r["brand_terms"]
    # multi-token catalog brand also matches as a contiguous span
    r2 = agent_api._detect_brand_query("beauty of joseon glow deep serum")
    assert r2["brand_like"] is True and r2["mode"] == "catalog"


def test_on_does_not_over_detect_generic_query(monkeypatch):
    monkeypatch.setenv("GATEWAY_DYNAMIC_BRAND_DETECT", "1")
    # Even if a stopword-like single word were in the set, generic category
    # queries must NOT become brand queries.
    _set_cache({"the ordinary", "serum", "cleanser"})
    for q in ("acne cleanser", "salicylic acid serum for acne", "vanilla perfume"):
        assert agent_api._detect_brand_query(q)["brand_like"] is False, q


def test_static_brands_still_win_when_on(monkeypatch):
    monkeypatch.setenv("GATEWAY_DYNAMIC_BRAND_DETECT", "1")
    _set_cache(set())  # no dynamic entries
    r = agent_api._detect_brand_query("fenty beauty gloss bomb")
    assert r["brand_like"] is True
    assert r["mode"] == "static"


def test_short_brand_below_min_len_ignored(monkeypatch):
    monkeypatch.setenv("GATEWAY_DYNAMIC_BRAND_DETECT", "1")
    _set_cache({"abc"})  # 3 chars < _MIN_DYNAMIC_BRAND_LEN
    assert agent_api._detect_brand_query("abc serum")["brand_like"] is False


def test_brand_dictionary_aliases_indexes_leading_segment():
    # "Biodance | Better Formula for Better Glow" must yield a matchable
    # "biodance" alias (the incident: a bare brand query missed the full span).
    aliases = agent_api._brand_dictionary_aliases("biodance | better formula for better glow")
    assert "biodance" in aliases
    assert "biodance better formula for better glow" in aliases
    # the tagline segment is NOT indexed on its own (no false brand hits)
    assert "better formula for better glow" not in aliases
    # a clean single-segment brand yields exactly one alias (no dup)
    assert agent_api._brand_dictionary_aliases("acropass") == ["acropass"]
    # newline separator handled too
    assert "rovectin" in agent_api._brand_dictionary_aliases("rovectin\nskin essentials")


def test_piped_brand_detected_by_leading_token(monkeypatch):
    monkeypatch.setenv("GATEWAY_DYNAMIC_BRAND_DETECT", "1")
    # Mirror what _ensure_brand_dictionary_loaded builds from the raw brand row.
    _set_cache(set(agent_api._brand_dictionary_aliases("biodance | better formula for better glow")))
    r = agent_api._detect_brand_query("biodance collagen mask")
    assert r["brand_like"] is True
    assert r["mode"] == "catalog"
    assert "biodance" in r["brand_terms"]

"""Tests for services.category_classifier_llm + the async wrapper in
services.pdp_category_classifier.fold_category_with_llm_fallback.

LLM HTTP calls are mocked via monkeypatch on _call_deepseek_classify
so tests stay deterministic and free.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import category_classifier_llm as cc  # noqa: E402
from services.category_classifier_llm import (  # noqa: E402
    CATEGORY_SOURCE_LLM,
    classify_via_llm,
    _invalidate_cache_for_tests,
)
from services.pdp_category_classifier import (  # noqa: E402
    fold_category_from_variants,
    fold_category_with_llm_fallback,
    CATEGORY_SOURCE_MERCHANT,
    CATEGORY_SOURCE_VARIANT,
)


def _assert_regex_misses(**fields) -> None:
    """Assert the PREMISE of a regex-miss test, not just its conclusion.

    These tests are only meaningful while CATEGORY_PATTERNS does NOT match
    the fixture. When the electronics patterns landed they turned the old
    "Sony WH-1000XM5" / "Wireless Headphones" fixture into a regex HIT, and
    both tests then failed on a downstream assertion — `source` came back
    'merchant_payload' — which reads as a bug in the LLM wrapper rather than
    as a stale fixture. Asserting the premise up front makes the next pattern
    that swallows this fixture say so directly.
    """
    hit = fold_category_from_variants(variants=None, **fields)
    assert hit is None, (
        f"fixture {fields!r} is no longer a regex miss (matched {hit!r}); "
        "pick a title/product_type CATEGORY_PATTERNS does not classify"
    )


def _enable_flag(monkeypatch):
    monkeypatch.setenv("LLM_CATEGORY_CLASSIFIER_ENABLED", "true")


def _install_llm(monkeypatch, response: Optional[Dict[str, Any]]):
    async def _fake(*, user_message: str, timeout_s: float = 15.0):
        return response
    monkeypatch.setattr(cc, "_call_deepseek_classify", _fake)


@pytest.fixture(autouse=True)
def _reset_cache():
    _invalidate_cache_for_tests()
    yield
    _invalidate_cache_for_tests()


# ============================================================
# classify_via_llm — gate, parse, validate, cache
# ============================================================

@pytest.mark.asyncio
async def test_flag_off_returns_none(monkeypatch):
    monkeypatch.delenv("LLM_CATEGORY_CLASSIFIER_ENABLED", raising=False)
    _install_llm(monkeypatch, {"label": "X", "path": "fashion/x/y", "confidence": 0.9})
    assert await classify_via_llm(title="Mystery Item") is None


@pytest.mark.asyncio
async def test_no_signal_returns_none_without_llm_call(monkeypatch):
    _enable_flag(monkeypatch)
    called = {"n": 0}
    async def _fake(*, user_message, timeout_s=15.0):
        called["n"] += 1
        return {"label": "x", "path": "fashion/x/y", "confidence": 0.9}
    monkeypatch.setattr(cc, "_call_deepseek_classify", _fake)
    assert await classify_via_llm() is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_clean_classification_passes_through(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm(monkeypatch, {"label": "Yoga Mat", "path": "sports/yoga/mat", "confidence": 0.85})
    result = await classify_via_llm(title="Eco-Friendly Yoga Mat")
    assert result == ("Yoga Mat", "sports/yoga/mat", 0.85)


@pytest.mark.asyncio
async def test_invalid_path_returns_none(monkeypatch):
    _enable_flag(monkeypatch)
    # 1-segment path doesn't match the 2-5 segment shape
    _install_llm(monkeypatch, {"label": "X", "path": "fashion", "confidence": 0.9})
    assert await classify_via_llm(title="Item") is None


@pytest.mark.asyncio
async def test_path_with_uppercase_normalized_lower(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm(monkeypatch, {"label": "Tee", "path": "Fashion/Apparel/Tops/Tshirt", "confidence": 0.9})
    result = await classify_via_llm(title="Cotton Tee")
    assert result == ("Tee", "fashion/apparel/tops/tshirt", 0.9)


@pytest.mark.asyncio
async def test_unknown_root_coerced_to_other_with_low_confidence(monkeypatch):
    _enable_flag(monkeypatch)
    # 'gadgets' isn't in _ALLOWED_ROOTS
    _install_llm(monkeypatch, {"label": "Widget", "path": "gadgets/smart/widget", "confidence": 0.9})
    result = await classify_via_llm(title="Smart Widget")
    label, path, conf = result
    assert label == "Widget"
    assert path == "other/gadgets/smart/widget"
    assert conf == 0.4


@pytest.mark.asyncio
async def test_missing_label_falls_back_to_path_leaf(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm(monkeypatch, {"path": "home/kitchen/cookware/pan", "confidence": 0.8})
    result = await classify_via_llm(title="Cast Iron Pan")
    assert result is not None
    label, path, _ = result
    assert label == "Pan"
    assert path == "home/kitchen/cookware/pan"


@pytest.mark.asyncio
async def test_self_report_clamped_to_half_when_invalid(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm(monkeypatch, {"label": "X", "path": "fashion/x/y", "confidence": 1.7})
    _, _, conf = await classify_via_llm(title="Item")
    assert conf == 0.5


@pytest.mark.asyncio
async def test_llm_transport_failure_returns_none(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm(monkeypatch, None)
    assert await classify_via_llm(title="Item") is None


@pytest.mark.asyncio
async def test_cache_hits_by_product_type(monkeypatch):
    _enable_flag(monkeypatch)
    calls = {"n": 0}
    async def _fake(*, user_message, timeout_s=15.0):
        calls["n"] += 1
        return {"label": "Harness", "path": "pet/accessory/harness", "confidence": 0.9}
    monkeypatch.setattr(cc, "_call_deepseek_classify", _fake)

    r1 = await classify_via_llm(merchant_id="m1", product_type="Dog Harness", title="Reflective Dog Harness")
    r2 = await classify_via_llm(merchant_id="m1", product_type="Dog Harness", title="Escape-Proof Dog Harness")
    assert r1 == r2
    assert calls["n"] == 1  # second call hit the cache


@pytest.mark.asyncio
async def test_cache_falls_back_to_title_when_no_product_type(monkeypatch):
    _enable_flag(monkeypatch)
    calls = {"n": 0}
    async def _fake(*, user_message, timeout_s=15.0):
        calls["n"] += 1
        return {"label": "X", "path": "other/x/y", "confidence": 0.5}
    monkeypatch.setattr(cc, "_call_deepseek_classify", _fake)
    await classify_via_llm(merchant_id="m1", title="Aaa Bbb")
    await classify_via_llm(merchant_id="m1", title="Aaa Bbb")  # same title → cached
    await classify_via_llm(merchant_id="m1", title="Ccc Ddd")  # different title → new call
    assert calls["n"] == 2


# ============================================================
# fold_category_with_llm_fallback — the async wrapper
# ============================================================

@pytest.mark.asyncio
async def test_regex_hit_does_not_call_llm(monkeypatch):
    _enable_flag(monkeypatch)
    called = {"n": 0}
    async def _fake(*, user_message, timeout_s=15.0):
        called["n"] += 1
        return {"label": "X", "path": "fashion/x/y", "confidence": 0.9}
    monkeypatch.setattr(cc, "_call_deepseek_classify", _fake)
    # Beauty-pattern hit
    result = await fold_category_with_llm_fallback(title="MAC Ruby Woo Matte Lipstick")
    assert result is not None
    (label, path), source, _ = result
    assert label == "Lipstick"
    assert source == CATEGORY_SOURCE_MERCHANT
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_regex_miss_falls_back_to_llm(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm(monkeypatch, {"label": "Thermostat", "path": "electronics/home/thermostat", "confidence": 0.92})
    fields = dict(title="Ecobee Smart Thermostat Premium", product_type="Smart Thermostat")
    _assert_regex_misses(category=None, **fields)
    result = await fold_category_with_llm_fallback(**fields)
    assert result is not None
    (label, path), source, confidence = result
    assert label == "Thermostat"
    assert path == "electronics/home/thermostat"
    assert source == CATEGORY_SOURCE_LLM
    assert confidence == 0.92


@pytest.mark.asyncio
async def test_both_regex_and_llm_miss_returns_none(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm(monkeypatch, {"value": None, "reason": "insufficient_signal"})  # no path
    result = await fold_category_with_llm_fallback(title="Mystery Object")
    assert result is None


@pytest.mark.asyncio
async def test_llm_not_invoked_when_flag_off_even_on_regex_miss(monkeypatch):
    monkeypatch.delenv("LLM_CATEGORY_CLASSIFIER_ENABLED", raising=False)
    called = {"n": 0}
    async def _fake(*, user_message, timeout_s=15.0):
        called["n"] += 1
        return None
    monkeypatch.setattr(cc, "_call_deepseek_classify", _fake)
    _assert_regex_misses(category=None, product_type=None, title="Ecobee Smart Thermostat Premium")
    result = await fold_category_with_llm_fallback(title="Ecobee Smart Thermostat Premium")
    assert result is None
    assert called["n"] == 0

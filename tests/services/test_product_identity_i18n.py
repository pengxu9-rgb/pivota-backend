"""Tests for the English product-identity resolver (services.product_identity_i18n).

Covers the flag gate, the language gate (no LLM for English titles), grounding +
claim-safety, brand preservation, low-confidence fail-safe, and the in-place
synthetic-item substitution used by the URL-wedge worker.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

import services.product_identity_i18n as m

ANUKO_KO = "아누코 루트 액티베이팅 탈모 볼륨 샴푸"


def _ko_product(title: str = ANUKO_KO) -> Dict[str, Any]:
    return {
        "title": title,
        "raw_title": title,
        "vendor": "ANUKO",
        "product_type": "샴푸",
        "attributes_raw": {"description": "나이아신아마이드 두피 샴푸. 비건, 무향."},
    }


def _stub_llm(monkeypatch, payload: Optional[Dict[str, Any]]):
    async def _fake(*, user_message: str, timeout_s: float = 15.0):
        _fake.calls += 1
        _fake.last_message = user_message
        return payload
    _fake.calls = 0
    _fake.last_message = ""
    monkeypatch.setattr(m, "_call_deepseek_resolve", _fake)
    return _fake


def test_looks_non_english():
    assert m.looks_non_english(ANUKO_KO)
    assert m.looks_non_english("ANUKO 샴푸 shampoo")  # mixed still flags
    assert not m.looks_non_english("ANUKO Root Activating Volume Shampoo")
    assert not m.looks_non_english("")


async def test_flag_off_is_noop(monkeypatch):
    monkeypatch.delenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", raising=False)
    llm = _stub_llm(monkeypatch, {"english_name": "x", "confidence": 0.9})
    assert await m.resolve_english_identity(_ko_product()) is None
    assert llm.calls == 0  # never even reaches the model


async def test_english_title_short_circuits_without_llm(monkeypatch):
    monkeypatch.setenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "true")
    llm = _stub_llm(monkeypatch, {"english_name": "x", "confidence": 0.9})
    product = _ko_product("ANUKO Root Activating Volume Shampoo")
    assert await m.resolve_english_identity(product) is None
    assert llm.calls == 0  # language gate saves the call


async def test_resolves_korean_to_english(monkeypatch):
    monkeypatch.setenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "on")
    _stub_llm(monkeypatch, {
        "english_name": "ANUKO Root Activating Volumizing Shampoo",
        "confidence": 0.88,
    })
    res = await m.resolve_english_identity(_ko_product())
    assert res is not None
    assert res["english_name"] == "ANUKO Root Activating Volumizing Shampoo"
    assert res["method"] == "deepseek_v1"
    assert res["confidence"] == 0.88
    assert res["raw_title"] == ANUKO_KO
    assert res["english_name"].isascii()


async def test_medical_claim_is_rejected(monkeypatch):
    """탈모 must not be laundered into a treatment claim; fail SAFE to raw."""
    monkeypatch.setenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "1")
    _stub_llm(monkeypatch, {
        "english_name": "ANUKO Anti-Hair-Loss Volume Shampoo",
        "confidence": 0.95,
    })
    assert await m.resolve_english_identity(_ko_product()) is None


async def test_model_echoing_korean_is_rejected(monkeypatch):
    monkeypatch.setenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "1")
    _stub_llm(monkeypatch, {"english_name": "아누코 샴푸", "confidence": 0.9})
    assert await m.resolve_english_identity(_ko_product()) is None


async def test_dropped_brand_is_restored(monkeypatch):
    monkeypatch.setenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "1")
    _stub_llm(monkeypatch, {
        "english_name": "Root Activating Volumizing Shampoo",
        "confidence": 0.9,
    })
    res = await m.resolve_english_identity(_ko_product())
    assert res is not None
    assert res["english_name"].lower().startswith("anuko")


async def test_low_confidence_fails_safe(monkeypatch):
    monkeypatch.setenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "1")
    _stub_llm(monkeypatch, {
        "english_name": "ANUKO Volumizing Shampoo",
        "confidence": 0.2,
    })
    assert await m.resolve_english_identity(_ko_product()) is None


async def test_synthetic_items_inplace_substitution(monkeypatch):
    monkeypatch.setenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "1")
    _stub_llm(monkeypatch, {
        "english_name": "ANUKO Root Activating Volumizing Shampoo",
        "confidence": 0.9,
    })
    items = [
        _ko_product(),  # Korean -> resolved
        {"title": "Some English Serum", "vendor": "X",
         "attributes_raw": {"description": "vitamin c serum"}},  # untouched
    ]
    n = await m.resolve_synthetic_items_inplace(items, "merch_test")
    assert n == 1
    assert items[0]["title"] == "ANUKO Root Activating Volumizing Shampoo"
    assert items[0]["raw_title"] == ANUKO_KO
    assert items[0]["title_i18n"]["original"] == ANUKO_KO
    assert items[0]["title_i18n"]["confidence"] == 0.9
    # English item is left exactly as-is (no title_i18n stamped).
    assert items[1]["title"] == "Some English Serum"
    assert "title_i18n" not in items[1]


async def test_synthetic_items_inplace_flag_off(monkeypatch):
    monkeypatch.delenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", raising=False)
    items = [_ko_product()]
    assert await m.resolve_synthetic_items_inplace(items, "m") == 0
    assert items[0]["title"] == ANUKO_KO  # unchanged


async def test_resolution_survives_llm_failure(monkeypatch):
    monkeypatch.setenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "1")
    _stub_llm(monkeypatch, None)  # transport returned nothing
    items = [_ko_product()]
    assert await m.resolve_synthetic_items_inplace(items, "m") == 0
    assert items[0]["title"] == ANUKO_KO

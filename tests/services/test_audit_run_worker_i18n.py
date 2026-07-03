"""Integration: the URL-wedge discovery stage substitutes the English identity
before registering synthetic contexts, so both the products list (attribute
graph) and the registered context (probe identity) carry the English title."""
from __future__ import annotations

from typing import Any, Dict, Optional

import services.product_identity_i18n as i18n
from services import audit_run_worker
from services.agent_center_bd_report_service import _SYNTHETIC_SKU_CONTEXTS

ANUKO_KO = "아누코 루트 액티베이팅 탈모 볼륨 샴푸"
EN = "ANUKO Root Activating Volumizing Shampoo"


def _stub_llm(monkeypatch, payload: Optional[Dict[str, Any]]):
    async def _fake(*, user_message: str, timeout_s: float = 15.0):
        return payload
    monkeypatch.setattr(i18n, "_call_deepseek_resolve", _fake)


def _launch_options() -> Dict[str, Any]:
    return {
        "synthetic_products": [
            {
                "sku_key": "urlwedge:deadbeefdeadbeef",
                "product_key": "urlwedge:deadbeefdeadbeef",
                "title": ANUKO_KO,
                "raw_title": ANUKO_KO,
                "vendor": "ANUKO",
                "product_type": "샴푸",
                "pdp_url": "https://anukoofficial.com/product/x",
                "attributes_raw": {"description": "나이아신아마이드 두피 샴푸. 비건, 무향."},
            }
        ]
    }


async def test_wedge_discovery_substitutes_english_title(monkeypatch):
    monkeypatch.setenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "1")
    _stub_llm(monkeypatch, {"english_name": EN, "confidence": 0.9})
    merchant_id = "merch_i18n_test"

    (_name, _domain, products, _url, _integ) = await audit_run_worker._resolve_synthetic_url_products(
        launch_options=_launch_options(), merchant_id=merchant_id,
    )

    # products (feeds build_sku_attribute_graph) carries the English title.
    assert products and products[0]["title"] == EN
    # the registered synthetic context (feeds probe identity) does too.
    ctx = _SYNTHETIC_SKU_CONTEXTS.get(("urlwedge:deadbeefdeadbeef", merchant_id))
    assert ctx is not None
    assert EN in str(ctx.get("product", {}).get("title") or ctx.get("sku_title") or "")


async def test_wedge_discovery_flag_off_keeps_korean(monkeypatch):
    monkeypatch.delenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", raising=False)
    _stub_llm(monkeypatch, {"english_name": EN, "confidence": 0.9})
    (_n, _d, products, _u, _i) = await audit_run_worker._resolve_synthetic_url_products(
        launch_options=_launch_options(), merchant_id="merch_off",
    )
    assert products and products[0]["title"] == ANUKO_KO

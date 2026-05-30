from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict

import pytest


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return self._payload


def _deepseek_payload(content: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


@pytest.mark.asyncio
async def test_generate_copy_review_rubric_includes_source_text(monkeypatch):
    from config.settings import settings
    from services import pdp_copy_review as review

    captured: Dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json=None, headers=None):
            captured["request"] = json
            return _FakeResponse(
                _deepseek_payload({
                    "decision": "pass",
                    "checks": {
                        "copy_is_topical_to_product": True,
                        "no_cross_seller_or_checkout_mention": True,
                        "internally_consistent_variants_and_market": True,
                        "no_medical_regulated_promo_or_fake_review_claim": True,
                        "machine_publish_allowed_module": True,
                    },
                    "confidence": 0.93,
                    "evidence_refs": ["marine collagen"],
                    "reviewed_in": "codex_external_window",
                })
            )

    async def fake_cost_today_for_merchant(*, merchant_id):
        return Decimal("0")

    async def fake_record_probe_run(**kwargs):
        captured["record"] = kwargs
        return "probe-1"

    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_api_base_url", "https://deepseek.test")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-chat")
    monkeypatch.setattr(review.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(review, "cost_today_for_merchant", fake_cost_today_for_merchant)
    monkeypatch.setattr(review, "record_probe_run", fake_record_probe_run)

    rubric = await review.generate_copy_review_rubric(
        merchant_id="m-1",
        payload={"pdp_description_raw": "Ownist marine collagen jelly."},
        source_url="https://ownist.test/products/triple-shine",
        source_text="Ownist Triple Shine contains marine collagen and grape flavor.",
        catalog_brand="Ownist",
        catalog_title="Triple Shine Grape",
    )

    user_message = captured["request"]["messages"][1]["content"]
    assert "SOURCE TEXT (verbatim from the source URL, truncated):" in user_message
    assert "Ownist Triple Shine contains marine collagen" in user_message
    assert "- brand: Ownist" in user_message
    assert rubric is not None
    assert captured["record"]["request_payload_jsonb"]["messages"][1]["content"] == user_message


@pytest.mark.asyncio
async def test_generate_copy_review_rubric_source_prompt_defaults_false_without_text(monkeypatch):
    from config.settings import settings
    from services import pdp_copy_review as review

    captured: Dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json=None, headers=None):
            captured["request"] = json
            return _FakeResponse(
                _deepseek_payload({
                    "decision": "needs_human_review",
                    "checks": {
                        "copy_is_topical_to_product": False,
                        "no_cross_seller_or_checkout_mention": True,
                        "internally_consistent_variants_and_market": True,
                        "no_medical_regulated_promo_or_fake_review_claim": True,
                        "machine_publish_allowed_module": False,
                    },
                    "confidence": 0.55,
                    "evidence_refs": [],
                    "reviewed_in": "codex_external_window",
                })
            )

    async def fake_cost_today_for_merchant(*, merchant_id):
        return Decimal("0")

    async def fake_record_probe_run(**kwargs):
        return "probe-2"

    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_api_base_url", "https://deepseek.test")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-chat")
    monkeypatch.setattr(review.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(review, "cost_today_for_merchant", fake_cost_today_for_merchant)
    monkeypatch.setattr(review, "record_probe_run", fake_record_probe_run)

    rubric = await review.generate_copy_review_rubric(
        merchant_id="m-1",
        payload={"pdp_description_raw": "Ownist marine collagen jelly."},
        source_url="https://ownist.test/products/triple-shine",
        source_text=None,
        catalog_brand="Ownist",
        catalog_title="Triple Shine Grape",
    )

    system_prompt = captured["request"]["messages"][0]["content"]
    assert "When SOURCE TEXT is not provided, set copy_is_topical_to_product=false" in system_prompt
    assert rubric is not None
    assert rubric["checks"]["copy_is_topical_to_product"] is False

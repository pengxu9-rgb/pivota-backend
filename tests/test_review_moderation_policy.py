from __future__ import annotations

import json
from typing import Any

import pytest

from services.review_moderation_policy import assess_review_text_risk, assess_review_text_risk_with_deepseek


def test_assess_review_text_risk_marks_normal_text_as_low() -> None:
    result = assess_review_text_risk(title="Great quality", body="Loved the texture and fit.")
    assert result["policy"] == "text_rules_v1"
    assert result["risk_level"] == "low"
    assert result["moderation_state"] == "active"
    assert result["reason_codes"] == []


def test_assess_review_text_risk_flags_sexual_content() -> None:
    result = assess_review_text_risk(
        title="xxx",
        body="This is porn content and explicit sex material.",
    )
    assert result["risk_level"] == "high"
    assert result["moderation_state"] == "under_review"
    assert "sexual_content" in result["reason_codes"]


def test_assess_review_text_risk_flags_hate_or_spam_content() -> None:
    result = assess_review_text_risk(
        title="Contact me on telegram",
        body="Kill all people in this group. https://a.test https://b.test https://c.test",
    )
    assert result["risk_level"] == "high"
    assert result["moderation_state"] == "under_review"
    assert "spam_or_irrelevant" in result["reason_codes"]
    assert "hate_content" in result["reason_codes"]


@pytest.mark.asyncio
async def test_deepseek_moderation_without_key_routes_to_employee_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_REVIEW_MODERATION_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = await assess_review_text_risk_with_deepseek(
        title="Great quality",
        body="Loved the texture and fit.",
    )

    assert result["policy"] == "deepseek_review_moderation_v1"
    assert result["decision"] == "needs_human_review"
    assert result["moderation_state"] == "under_review"
    assert result["employee_review_queue"] is True
    assert result["fallback_reason"] == "deepseek_api_key_missing"


@pytest.mark.asyncio
async def test_deepseek_moderation_approves_high_confidence_clean_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "approve",
                                    "risk_level": "low",
                                    "reason_codes": [],
                                    "confidence": 0.98,
                                    "review_notes": "Clean product review.",
                                }
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setenv("DEEPSEEK_REVIEW_MODERATION_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_REVIEW_MODERATION_MODEL", "deepseek-v4-flash")
    monkeypatch.setattr("services.review_moderation_policy.httpx.AsyncClient", FakeClient)

    result = await assess_review_text_risk_with_deepseek(
        title="Great",
        body="This moisturizer felt light and worked well under makeup.",
    )

    assert result["moderation_state"] == "active"
    assert result["decision"] == "approve"
    assert result["confidence"] == 0.98
    assert result["model"] == "deepseek-v4-flash"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["kwargs"]["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_deepseek_moderation_uncertain_review_stays_in_employee_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "needs_human_review",
                                    "risk_level": "medium",
                                    "reason_codes": ["irrelevant_to_product"],
                                    "confidence": 0.71,
                                    "review_notes": "May not be about the product.",
                                }
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setenv("DEEPSEEK_REVIEW_MODERATION_API_KEY", "test-key")
    monkeypatch.setattr("services.review_moderation_policy.httpx.AsyncClient", FakeClient)

    result = await assess_review_text_risk_with_deepseek(
        title="Shipping",
        body="This is mostly about an unrelated marketplace dispute.",
    )

    assert result["moderation_state"] == "under_review"
    assert result["employee_review_queue"] is True
    assert result["reason_codes"] == ["irrelevant_to_product"]

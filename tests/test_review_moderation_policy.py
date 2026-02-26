from __future__ import annotations

from services.review_moderation_policy import assess_review_text_risk


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

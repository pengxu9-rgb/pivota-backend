from __future__ import annotations

from mvp.governance import DefaultPolicyEvaluator, GovernanceService, PolicyInput


def test_policy_allows_by_default():
    svc = GovernanceService(evaluator=DefaultPolicyEvaluator(enforce=False))
    d = svc.evaluate(
        PolicyInput(
            merchant_id="merch_1",
            actor_type="agent",
            actor_ref="agent_1",
            action="submit_payment",
            amount=999.0,
            currency="USD",
            geo={"country": "US"},
            consent_scopes=[],
            approval_id=None,
        )
    )
    assert d.decision == "allow"


def test_policy_requires_hil_when_enforced_and_threshold_hit():
    svc = GovernanceService(evaluator=DefaultPolicyEvaluator(enforce=True, hil_threshold=100.0))
    d = svc.evaluate(
        PolicyInput(
            merchant_id="merch_1",
            actor_type="agent",
            actor_ref="agent_1",
            action="submit_payment",
            amount=150.0,
            currency="USD",
            geo={"country": "US"},
            consent_scopes=["payments:create"],
            approval_id=None,
        )
    )
    assert d.decision == "require_hil"
    approval = svc.request_hil(intent={"action": "submit_payment", "order_id": "ORD_1"})
    assert approval["approval_id"].startswith("hil_")


def test_policy_hard_hil_threshold_applies_even_when_not_enforced(monkeypatch):
    monkeypatch.setenv("MVP_GOVERNANCE_HARD_HIL", "true")
    monkeypatch.setenv("MVP_HARD_HIL_AMOUNT_THRESHOLD", "10.0")
    svc = GovernanceService(evaluator=DefaultPolicyEvaluator(enforce=False))
    d = svc.evaluate(
        PolicyInput(
            merchant_id="merch_1",
            actor_type="agent",
            actor_ref="agent_1",
            action="submit_payment",
            amount=99.0,
            currency="USD",
            geo={"country": "US"},
            consent_scopes=[],
            approval_id=None,
        )
    )
    assert d.decision == "require_hil"

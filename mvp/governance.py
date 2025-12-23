from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Protocol

from mvp.constants import EVENT_AUDIT, RiskTier, SCHEMA_VERSION, SchemaVersion
from mvp.events import emit_best_effort
from mvp.schemas import AuditActor, AuditConsent, AuditEvent, AuditRequestContext, AuditSubject, HashChain
from mvp.playbooks import risk_thresholds_for_geo
from services.pcs_hash import chain_hash, sha256_json


ConsentScope = Literal[
    "offers:read",
    "quotes:create",
    "orders:create",
    "payments:create",
    "refunds:create",
    "disputes:read",
    "evidence:read",
]


PolicyDecisionType = Literal["allow", "deny", "require_hil"]


@dataclass(frozen=True)
class PolicyInput:
    merchant_id: str
    actor_type: str
    actor_ref: Optional[str]
    action: str
    amount: Optional[float]
    currency: Optional[str]
    geo: Optional[Dict[str, Any]]
    consent_scopes: List[str]
    approval_id: Optional[str]


@dataclass(frozen=True)
class PolicyDecision:
    decision: PolicyDecisionType
    risk_tier: RiskTier
    reason_codes: List[str]
    required_scopes: List[str]


class PolicyEvaluator(Protocol):
    def evaluate(self, inp: PolicyInput) -> PolicyDecision: ...


class DefaultPolicyEvaluator:
    def __init__(self, *, enforce: Optional[bool] = None, hil_threshold: Optional[float] = None):
        self._enforce = enforce
        self._hil_threshold = hil_threshold

    def evaluate(self, inp: PolicyInput) -> PolicyDecision:
        enforce = (
            self._enforce
            if self._enforce is not None
            else os.getenv("MVP_GOVERNANCE_ENFORCE", "false").lower() == "true"
        )
        env_hil_threshold = os.getenv("MVP_HIL_AMOUNT_THRESHOLD")
        hil_threshold = (
            self._hil_threshold
            if self._hil_threshold is not None
            else float(env_hil_threshold) if env_hil_threshold is not None else 250.0
        )
        hard_hil_enabled = os.getenv("MVP_GOVERNANCE_HARD_HIL", "true").lower() == "true"
        env_hard_hil_threshold = os.getenv("MVP_HARD_HIL_AMOUNT_THRESHOLD")
        hard_hil_threshold = float(env_hard_hil_threshold) if env_hard_hil_threshold is not None else 5000.0

        # Playbook overrides (geo-based).
        try:
            country = None
            if isinstance(inp.geo, dict):
                country = inp.geo.get("country")
            overrides = risk_thresholds_for_geo(country=country)
            if self._hil_threshold is None and env_hil_threshold is None and overrides.get("hil_amount_threshold") is not None:
                hil_threshold = float(overrides["hil_amount_threshold"])
            if env_hard_hil_threshold is None and overrides.get("hard_hil_amount_threshold") is not None:
                hard_hil_threshold = float(overrides["hard_hil_amount_threshold"])
        except Exception:
            pass

        required_scopes = _required_scopes_for_action(inp.action)
        missing = [s for s in required_scopes if s not in (inp.consent_scopes or [])]

        # Always-on hard gate for extreme risk tiers (safe default; configurable).
        if (
            hard_hil_enabled
            and _is_high_risk_action(inp.action)
            and (inp.amount or 0.0) >= hard_hil_threshold
            and not inp.approval_id
        ):
            return PolicyDecision(
                decision="require_hil",
                risk_tier="high",
                reason_codes=["hil_required_hard_threshold"],
                required_scopes=required_scopes,
            )

        if enforce and missing:
            return PolicyDecision(
                decision="deny",
                risk_tier="high",
                reason_codes=["missing_consent_scope"],
                required_scopes=required_scopes,
            )

        if not enforce:
            # Even when not enforcing, compute a risk tier for measurement tagging.
            if _is_high_risk_action(inp.action) and (inp.amount or 0.0) >= hil_threshold:
                risk: RiskTier = "high"
            elif _is_high_risk_action(inp.action):
                risk = "medium"
            else:
                risk = "low"
            return PolicyDecision(
                decision="allow",
                risk_tier=risk,
                reason_codes=[],
                required_scopes=required_scopes,
            )

        # Enforcement mode: require HIL for high-risk actions above the threshold unless approval_id present.
        if _is_high_risk_action(inp.action) and (inp.amount or 0.0) >= hil_threshold and not inp.approval_id:
            return PolicyDecision(
                decision="require_hil",
                risk_tier="high",
                reason_codes=["hil_required"],
                required_scopes=required_scopes,
            )

        return PolicyDecision(
            decision="allow",
            risk_tier="medium" if _is_high_risk_action(inp.action) else "low",
            reason_codes=[],
            required_scopes=required_scopes,
        )


class HilProvider(Protocol):
    def request_approval(self, *, intent: Dict[str, Any]) -> Dict[str, Any]: ...
    def record_decision(self, *, approval_id: str, decision: str, decided_by: str, reason: Optional[str]) -> None: ...


class StubHilProvider:
    def request_approval(self, *, intent: Dict[str, Any]) -> Dict[str, Any]:
        return {"approval_id": f"hil_{uuid.uuid4().hex}", "status": "pending", "intent": intent}

    def record_decision(
        self, *, approval_id: str, decision: str, decided_by: str, reason: Optional[str]
    ) -> None:
        return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _required_scopes_for_action(action: str) -> List[str]:
    mapping = {
        "offer_generated": ["quotes:create"],
        "create_order": ["orders:create"],
        "submit_payment": ["payments:create"],
        "submit_payment_intent": ["payments:create"],
        "refund_requested": ["refunds:create"],
        "evidence_read": ["evidence:read"],
        "dispute_read": ["disputes:read"],
    }
    return mapping.get(action, [])


def _is_high_risk_action(action: str) -> bool:
    return action in {"submit_payment", "submit_payment_intent", "refund_requested", "cancel_order"}


class GovernanceService:
    def __init__(
        self,
        *,
        evaluator: Optional[PolicyEvaluator] = None,
        hil: Optional[HilProvider] = None,
    ):
        self.evaluator = evaluator or DefaultPolicyEvaluator()
        self.hil = hil or StubHilProvider()
        self._chain_cursor: Dict[str, str] = {}

    def evaluate(self, inp: PolicyInput) -> PolicyDecision:
        return self.evaluator.evaluate(inp)

    def request_hil(self, *, intent: Dict[str, Any]) -> Dict[str, Any]:
        return self.hil.request_approval(intent=intent)

    def record_audit_event(
        self,
        *,
        merchant_id: str,
        actor_type: str,
        actor_ref: Optional[str],
        action: str,
        subject: Dict[str, Any],
        request_context: Dict[str, Any],
        consent: Dict[str, Any],
        payload: Dict[str, Any],
        risk_tier: RiskTier,
    ) -> AuditEvent:
        occurred_at = _utc_now()
        payload_sha = sha256_json(payload)
        prev = self._chain_cursor.get(merchant_id)
        idempotency_key = request_context.get("request_id") or payload.get("idempotency_key") or str(uuid.uuid4())
        chash = chain_hash(prev, payload_sha, str(idempotency_key), occurred_at.isoformat())
        self._chain_cursor[merchant_id] = chash

        event = AuditEvent(
            schema_version=SCHEMA_VERSION,
            audit_event_id=f"audit_{uuid.uuid4().hex}",
            merchant_id=merchant_id,
            actor=AuditActor(type=actor_type, ref=actor_ref),
            action=action,
            occurred_at=occurred_at,
            request_context=AuditRequestContext(**request_context),
            consent=AuditConsent(**consent),
            subject=AuditSubject(**subject),
            payload_digests={"request_sha256": payload_sha, "response_sha256": None},
            chain=HashChain(prev_chain_hash=prev, chain_hash=chash),
        )

        emit_best_effort(
            event_type=EVENT_AUDIT,
            payload=event.model_dump(mode="json"),
            merchant_id=merchant_id,
            geo=request_context.get("geo"),
            surface=request_context.get("surface") or "backend",
            adapter=request_context.get("adapter"),
            risk_tier=risk_tier,
            idempotency_key=str(idempotency_key),
        )
        return event


governance = GovernanceService()

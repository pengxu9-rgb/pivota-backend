"""Phase 2.3 — /api/audits endpoint tests.

Strategy: spin up a FastAPI app with just the audit_runs router +
override the auth dep + monkey-patch the DB accessors. Validates the
HTTP surface (status codes, body shape, idempotency, cross-tenant
guard) without touching Postgres.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# =====================================================================
# Test app + accessor stub
# =====================================================================


class _AccessorStub:
    """Records every accessor call so tests can assert behavior.
    Each accessor is overridable via the *_returns dicts."""

    def __init__(self):
        self.enqueued: List[Dict[str, Any]] = []
        self.cancelled: List[str] = []
        self.idem_lookups: List[str] = []
        self.debits: List[Dict[str, Any]] = []
        self.credits: List[Dict[str, Any]] = []
        self.rate_limit_checks: List[str] = []
        self.payment_method_checks: List[str] = []
        self.payment_method_error: Optional[Exception] = None
        self.preview_resolves: List[Dict[str, Any]] = []

        # Configurable returns — tests set these before requests.
        self.enqueue_returns: Optional[str] = "run-new-1"
        self.idem_returns: Optional[str] = None
        self.fetch_returns: Optional[Dict[str, Any]] = None
        self.list_returns: List[Dict[str, Any]] = []
        self.cancel_returns: bool = True
        # P1-2: by default the product-key ownership check passes
        # (no keys are missing). Tests that exercise the validation
        # path set missing_keys_returns explicitly.
        self.missing_keys_returns: List[str] = []
        self.balance: Dict[str, Any] = {
            "credits": 10_000,
            "allowance_credits": 18_000,
            "overage_pending_credits": 0,
            "overage_charged_credits": 0,
            "overage_blocked_until_payment": False,
            "usd_cogs_internal": Decimal("12.3456"),
            "plan_tier": "starter",
            "purchased_credits": 0,
            "updated_at": None,
            "version": 0,
        }
        self.preview_sku_keys: List[str] = ["sku-1", "sku-2"]

    async def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        # P0-3: enqueue_audit_run_with_replay returns
        # (run_id, was_existing). Keep the legacy single-string
        # configuration for back-compat; tests that exercise the
        # race-replay path can set enqueue_returns to a tuple.
        if isinstance(self.enqueue_returns, tuple):
            return self.enqueue_returns
        return (self.enqueue_returns, False)

    async def find_idem(self, *, idempotency_key):
        self.idem_lookups.append(idempotency_key)
        return self.idem_returns

    async def fetch(self, *, run_id):
        return self.fetch_returns

    async def cancel(self, *, run_id):
        self.cancelled.append(run_id)
        return self.cancel_returns

    async def recent(self, *, merchant_id, limit, subject_type=None):
        self.recent_subject_type = subject_type
        return self.list_returns

    async def missing_keys(self, *, merchant_id, product_keys):
        return list(self.missing_keys_returns)
    async def get_balance(self, merchant_id):
        return dict(self.balance)

    async def debit(self, merchant_id, kind, amount, idempotency_key, **kwargs):
        self.debits.append({
            "merchant_id": merchant_id,
            "kind": kind,
            "amount": amount,
            "idempotency_key": idempotency_key,
            "usd_cogs": kwargs.get("usd_cogs"),
        })
        available = int(self.balance.get("credits") or 0)
        if available < int(amount):
            if str(self.balance.get("plan_tier") or "free") != "free":
                shortfall = int(amount) - available
                self.balance["credits"] = 0
                self.balance["overage_pending_credits"] = (
                    int(self.balance.get("overage_pending_credits") or 0)
                    + shortfall
                )
                self.balance["usd_cogs_internal"] = (
                    Decimal(str(self.balance.get("usd_cogs_internal") or 0))
                    + Decimal(str(kwargs.get("usd_cogs") or 0))
                )
                self.balance["version"] = int(self.balance.get("version") or 0) + 1
                return {
                    **self.balance,
                    "replay": False,
                    "purchased_credits_debited": int(
                        self.balance.get("purchased_credits") or 0
                    ),
                    "overage_credits_accrued": shortfall,
                }
            from services.merchant_credit_balance_service import (
                InsufficientCreditsError,
            )
            raise InsufficientCreditsError(
                merchant_id, kind, int(amount), available,
            )
        purchased_available = int(self.balance.get("purchased_credits") or 0)
        allowance_available = max(0, available - purchased_available)
        purchased_debited = min(
            purchased_available,
            max(0, int(amount) - allowance_available),
        )
        self.balance["credits"] = available - int(amount)
        self.balance["purchased_credits"] = purchased_available - purchased_debited
        self.balance["usd_cogs_internal"] = (
            Decimal(str(self.balance.get("usd_cogs_internal") or 0))
            + Decimal(str(kwargs.get("usd_cogs") or 0))
        )
        self.balance["version"] = int(self.balance.get("version") or 0) + 1
        return {
            **self.balance,
            "replay": False,
            "purchased_credits_debited": purchased_debited,
        }

    async def credit(self, merchant_id, kind, amount, source_event_id, **kwargs):
        purchased_credits = int(
            kwargs.get("purchased_credits")
            if kwargs.get("purchased_credits") is not None
            else amount
        )
        self.credits.append({
            "merchant_id": merchant_id,
            "kind": kind,
            "amount": amount,
            "source_event_id": source_event_id,
            "usd_cogs": kwargs.get("usd_cogs"),
            "purchased_credits": purchased_credits,
        })
        self.balance["credits"] = int(self.balance.get("credits") or 0) + int(amount)
        self.balance["purchased_credits"] = (
            int(self.balance.get("purchased_credits") or 0) + purchased_credits
        )
        self.balance["usd_cogs_internal"] = max(
            Decimal("0"),
            Decimal(str(self.balance.get("usd_cogs_internal") or 0))
            - Decimal(str(kwargs.get("usd_cogs") or 0)),
        )
        self.balance["version"] = int(self.balance.get("version") or 0) + 1
        return {**self.balance, "replay": False}

    async def rate_limit(self, merchant_id):
        self.rate_limit_checks.append(merchant_id)
        return 1

    async def require_payment_method(self, merchant_id):
        self.payment_method_checks.append(merchant_id)
        if self.payment_method_error is not None:
            raise self.payment_method_error

    async def resolve_preview(self, *, merchant_id, scope):
        self.preview_resolves.append({
            "merchant_id": merchant_id,
            "scope": scope,
        })
        return list(self.preview_sku_keys)


@pytest.fixture
def stub():
    return _AccessorStub()


@pytest.fixture
def client(stub, monkeypatch):
    """Mount the audit_runs router in an isolated app + patch every
    DB accessor + auth dep so tests don't need Postgres."""
    from routes import audit_runs_routes
    from utils import auth as auth_module

    monkeypatch.setattr(
        audit_runs_routes.settings, "deepseek_api_key", None, raising=False,
    )
    monkeypatch.setattr(
        audit_runs_routes, "enqueue_audit_run_with_replay", stub.enqueue,
    )
    monkeypatch.setattr(
        audit_runs_routes, "find_in_flight_by_idempotency_key",
        stub.find_idem,
    )
    monkeypatch.setattr(
        audit_runs_routes, "fetch_audit_run_by_id", stub.fetch,
    )
    monkeypatch.setattr(
        audit_runs_routes, "cancel_audit_run", stub.cancel,
    )
    monkeypatch.setattr(
        audit_runs_routes, "recent_runs_for_merchant", stub.recent,
    )
    monkeypatch.setattr(
        audit_runs_routes,
        "_missing_product_keys_for_merchant", stub.missing_keys,
    )
    monkeypatch.setattr(
        audit_runs_routes, "get_balance", stub.get_balance,
    )
    monkeypatch.setattr(
        audit_runs_routes, "debit", stub.debit,
    )
    monkeypatch.setattr(
        audit_runs_routes, "credit", stub.credit,
    )
    monkeypatch.setattr(
        audit_runs_routes, "_check_audit_rate_limit", stub.rate_limit,
    )
    monkeypatch.setattr(
        audit_runs_routes,
        "require_verified_payment_method",
        stub.require_payment_method,
    )
    monkeypatch.setattr(
        audit_runs_routes, "_resolve_preview_sku_keys", stub.resolve_preview,
    )

    # T2b readiness gate is exercised in test_audit_v3_readiness_gate.py; here it
    # is a no-op so these endpoint tests don't need the readiness DB stubs.
    async def _no_readiness_gate(*_a, **_k):
        return None

    monkeypatch.setattr(
        audit_runs_routes, "_enforce_audit_readiness", _no_readiness_gate,
    )
    audit_runs_routes._PREVIEW_CACHE.clear()

    app = FastAPI()
    app.include_router(audit_runs_routes.router)

    # Auth override: every request authenticates as merch-A.
    app.dependency_overrides[auth_module.get_current_merchant] = (
        lambda: "merch-A"
    )
    return TestClient(app)


# =====================================================================
# POST /api/audits
# =====================================================================


def json_dumps_lower(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str).lower()


def test_free_plan_with_credits_runs_premium_no_paywall(client, stub):
    # ADR-005: premium providers (ChatGPT/Claude) are gated by credit BALANCE,
    # not plan tier. A free-plan merchant with enough credits can run them — the
    # old `premium_provider_subscription_required` paywall was removed. (Plenty
    # of credits so the only thing that could block is the deleted plan-gate.)
    stub.balance = {
        **stub.balance,
        "plan_tier": "free",
        "credits": 1_000_000,
        "allowance_credits": 0,
        "purchased_credits": 1_000_000,
    }
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1", "pk-2"],
            "providers": ["gemini", "chatgpt"],
        },
    )
    assert res.status_code == 202, res.text
    assert "premium_provider_subscription_required" not in res.text


def test_post_enqueues_and_returns_202(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1", "pk-2"],
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body["run_id"] == "run-new-1"
    assert body["stage"] == "queued"
    assert body["idempotent_replay"] is False
    assert len(stub.enqueued) == 1
    assert stub.enqueued[0]["merchant_id"] == "merch-A"
    assert stub.enqueued[0]["product_keys"] == ["pk-1", "pk-2"]
    # Idempotency lookup happened (default force=False).
    assert len(stub.idem_lookups) == 1
    assert stub.balance["credits"] == 9648
    assert len(stub.debits) == 1
    assert stub.debits[0]["kind"] == "audit"
    assert stub.debits[0]["amount"] == 352
    launch = stub.enqueued[0]["request_options_jsonb"]["launch"]
    # Default profile is now pilot_gemini (free-tier = Gemini only); ChatGPT is
    # premium/opt-in, so the default run is Gemini-only and ~half the cost.
    assert launch["coverage_profile"] == "pilot_gemini"
    assert launch["providers"] == ["gemini"]
    assert launch["provider_models"]["gemini"]["model"] == "gemini-2.5-flash"
    assert launch["provider_models"]["gemini"]["model_is_override"] is False
    assert launch["verify_providers"] == []
    assert launch["verify_skipped"] == [
        {"provider": "deepseek", "reason": "missing_deepseek_api_key"},
    ]
    assert launch["pending_engine_support"] == []


def test_post_returns_existing_run_on_idempotent_replay(client, stub):
    stub.idem_returns = "run-already-running"
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body["run_id"] == "run-already-running"
    assert body["idempotent_replay"] is True
    # Worker did NOT enqueue — replayed instead.
    assert stub.enqueued == []
    assert stub.debits == []


def test_post_force_skips_idempotency_dedupe(client, stub):
    stub.idem_returns = "run-already-running"
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
            "force": True,
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body["idempotent_replay"] is False
    # No idempotency lookup happened.
    assert stub.idem_lookups == []
    # Worker enqueued with idempotency_key=None.
    assert len(stub.enqueued) == 1
    assert stub.enqueued[0]["idempotency_key"] is None
    assert len(stub.debits) == 1


def test_post_legacy_explicit_provider_uses_single_provider_profile(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
            "providers": ["gemini"],
        },
    )
    assert res.status_code == 202
    assert stub.debits[0]["amount"] == 176
    launch = stub.enqueued[0]["request_options_jsonb"]["launch"]
    assert launch["coverage_profile"] == "explicit"
    assert launch["providers"] == ["gemini"]


def test_post_accepts_chatgpt_model_override(client, stub):
    # ChatGPT is no longer in the default profile, so request a profile that
    # includes it. Default stub tier is "starter" (paid), which clears the
    # premium-provider entitlement gate.
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
            "coverage_profile": "us_shopper",
            "model_overrides": {"chatgpt": "gpt-5.5-mini"},
        },
    )
    assert res.status_code == 202
    launch = stub.enqueued[0]["request_options_jsonb"]["launch"]
    assert launch["provider_models"]["chatgpt"] == {
        "model": "gpt-5.5-mini",
        "default_model": "chat-latest",
        "model_is_override": True,
    }
    assert launch["model_overrides"] == {"chatgpt": "gpt-5.5-mini"}
    # Provider-level credit metering stays unchanged by model overrides.
    # 644 (was 368) after the audit-billing-cogs fix (#1506): grounded ChatGPT
    # is priced on ~15k measured input tokens/probe, not the flat 2000.
    assert stub.debits[0]["amount"] == 644


def test_post_rejects_cross_tenant_merchant_id(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-OTHER",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 403
    assert stub.enqueued == []


def test_post_rejects_cold_start_subject_for_merchant_auth(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
            "subject_type": "cold_start",
        },
    )
    assert res.status_code == 403
    assert stub.enqueued == []


def test_post_rejects_too_many_products(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": [f"pk-{idx}" for idx in range(51)],
        },
    )
    assert res.status_code == 422


def test_post_returns_503_on_persistence_failure(client, stub):
    stub.enqueue_returns = None
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 503
    assert stub.balance["credits"] == 10_000
    assert stub.credits[0]["kind"] == "audit"


def test_post_returns_402_when_credits_insufficient(client, stub):
    stub.balance["plan_tier"] = "free"
    stub.balance["credits"] = 100
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1", "pk-2"],
        },
    )
    assert res.status_code == 402
    detail = res.json()["detail"]
    assert detail == {
        "error": "insufficient_credits",
        "kind": "credits",
        "required": 352,
        "available": 100,
        "preview_url": "/api/audits/preview",
    }
    assert stub.balance["credits"] == 100
    assert stub.debits == []


def test_post_debits_prompt_credits_for_custom_prompts(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
            "custom_prompts": ["compare with refill packs", "is it oily?"],
        },
    )
    assert res.status_code == 202
    assert stub.balance["credits"] == 9822
    assert [d["kind"] for d in stub.debits] == ["audit", "prompt"]
    assert [d["amount"] for d in stub.debits] == [176, 2]


def test_post_total_credit_gap_returns_402_before_any_debit(client, stub):
    stub.balance["plan_tier"] = "free"
    stub.balance["credits"] = 176
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
            "custom_prompts": ["extra merchant prompt"],
        },
    )
    assert res.status_code == 402
    detail = res.json()["detail"]
    assert detail["kind"] == "credits"
    assert detail["required"] == 177
    assert detail["available"] == 176
    assert stub.balance["credits"] == 176
    assert stub.debits == []
    assert stub.credits == []
    assert stub.enqueued == []


def test_post_free_tier_applies_rate_limit_and_credits(client, stub):
    stub.balance["plan_tier"] = "free"
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 202
    assert stub.rate_limit_checks == ["merch-A"]
    assert stub.balance["credits"] == 9824


def test_post_paid_tier_skips_rate_limit(client, stub):
    stub.balance["plan_tier"] = "growth"
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 202
    assert stub.rate_limit_checks == []
    assert stub.payment_method_checks == ["merch-A"]


def test_post_paid_tier_without_verified_payment_method_is_blocked(client, stub):
    from services.merchant_credit_balance_service import (
        MissingVerifiedPaymentMethodError,
    )

    stub.balance["plan_tier"] = "growth"
    stub.payment_method_error = MissingVerifiedPaymentMethodError(
        "merch-A",
        "no_default_pm",
    )
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 402
    detail = res.json()["detail"]
    assert detail["error"] == "missing_verified_payment_method"
    assert detail["reason"] == "no_default_pm"
    assert stub.payment_method_checks == ["merch-A"]
    assert stub.debits == []
    assert stub.enqueued == []


def test_post_paid_tier_with_verified_payment_method_is_allowed(client, stub):
    stub.balance["plan_tier"] = "growth"
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 202
    assert stub.payment_method_checks == ["merch-A"]
    assert len(stub.debits) == 1


def test_post_paid_tier_overage_is_allowed_after_verified_card(client, stub):
    stub.balance["plan_tier"] = "growth"
    stub.balance["credits"] = 100
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1", "pk-2"],
        },
    )
    assert res.status_code == 202
    assert stub.payment_method_checks == ["merch-A"]
    assert stub.balance["credits"] == 0
    assert stub.balance["overage_pending_credits"] == 252
    assert len(stub.debits) == 1
    assert len(stub.enqueued) == 1


def test_post_overage_blocked_merchant_gets_402(client, stub):
    stub.balance["plan_tier"] = "growth"
    stub.balance["overage_blocked_until_payment"] = True
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 402
    detail = res.json()["detail"]
    assert detail["error"] == "overage_payment_failed"
    assert stub.payment_method_checks == []
    assert stub.debits == []
    assert stub.enqueued == []


def test_post_free_tier_is_exempt_from_verified_payment_method(client, stub):
    from services.merchant_credit_balance_service import (
        MissingVerifiedPaymentMethodError,
    )

    stub.balance["plan_tier"] = "free"
    stub.payment_method_error = MissingVerifiedPaymentMethodError(
        "merch-A",
        "missing_default_payment_method",
    )
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 202
    assert stub.payment_method_checks == []
    assert stub.rate_limit_checks == ["merch-A"]


def test_post_relaunch_existing_run_does_not_double_debit(client, stub):
    stub.idem_returns = "run-already-running"
    stub.balance["credits"] = 0
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 202
    assert res.json()["run_id"] == "run-already-running"
    assert stub.debits == []


# =====================================================================
# POST /api/audits/preview
# =====================================================================


def test_preview_returns_cost_balance_and_sufficiency(client, stub):
    stub.preview_sku_keys = [f"sku-{i}" for i in range(10)]
    stub.balance.update({
        "credits": 5000,
        "allowance_credits": 18_000,
        "usd_cogs_internal": Decimal("99.9900"),
        "plan_tier": "growth",
    })
    res = client.post(
        "/api/audits/preview",
        json={
            "merchant_id": "merch-A",
            "scope": {"select_top_n_by_revenue": 10},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["audit_run_id_preview"].startswith("preview_")
    assert body["merchant_id"] == "merch-A"
    assert body["sku_count"] == 10
    assert body["prompts_per_sku"] == 40
    assert body["total_prompts"] == 400
    assert body["estimated_cache_savings"] == {
        "prompts_cached": 80,
        "cache_hit_rate": 0.2,
    }
    assert body["coverage_profile"] == "pilot_gemini"
    assert body["providers"] == ["gemini"]
    assert body["requested_providers"] == ["gemini"]
    assert body["verify_providers"] == []
    assert body["verify_skipped"] == [
        {"provider": "deepseek", "reason": "missing_deepseek_api_key"},
    ]
    assert body["pending_engine_support"] == []
    assert body["estimated_audit_credits"] == 1760
    assert body["estimated_prompt_credits"] == 0
    assert body["estimated_execution_credits"] == 0
    assert body["current_balance"] == {
        "credits": 5000,
        "allowance_credits": 18_000,
        "plan_tier": "growth",
    }
    assert body["sufficient"] is True
    assert body["gaps"] == []
    assert stub.debits == []


def test_preview_paid_tier_can_overage_is_sufficient(client, stub):
    # A PAID tier (default stub plan_tier='starter') can launch on overage, so
    # a selected audit that costs more than the current balance must still
    # report sufficient=True (mirrors the launch gate `if gaps and not paid_tier`)
    # — otherwise the portal blocks the Run button and tells a paying merchant to
    # top up. The gap is still surfaced + will_overage flags the overage.
    stub.preview_sku_keys = ["sku-1", "sku-2", "sku-3"]
    stub.balance.update({"credits": 100})
    res = client.post(
        "/api/audits/preview",
        json={
            "merchant_id": "merch-A",
            "scope": {"sku_keys": ["sku-1", "sku-2", "sku-3"]},
            "custom_prompts": ["one"],
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sufficient"] is True
    assert body["will_overage"] is True
    assert body["gaps"] == [
        {"kind": "credits", "required": 529, "available": 100, "short": 429},
    ]


def test_preview_free_tier_with_gaps_is_insufficient(client, stub):
    # The FREE tier is genuinely hard-blocked (no overage) — sufficient=False.
    stub.preview_sku_keys = ["sku-1", "sku-2", "sku-3"]
    stub.balance.update({"credits": 100, "plan_tier": "free"})
    res = client.post(
        "/api/audits/preview",
        json={
            "merchant_id": "merch-A",
            "scope": {"sku_keys": ["sku-1", "sku-2", "sku-3"]},
            "custom_prompts": ["one"],
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sufficient"] is False
    assert body["will_overage"] is False
    assert body["gaps"][0]["short"] == 429


def test_preview_us_shopper_sums_gemini_and_chatgpt_per_prompt(client, stub):
    stub.preview_sku_keys = ["sku-1"]
    res = client.post(
        "/api/audits/preview",
        json={
            "merchant_id": "merch-A",
            "scope": {"sku_keys": ["sku-1"]},
            "prompts_per_sku": 40,
            "coverage_profile": "us_shopper",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["providers"] == ["gemini", "chatgpt"]
    # 644 (was 368) — grounded ChatGPT priced on ~15k measured input tokens (#1506).
    assert body["estimated_audit_credits"] == 644


def test_preview_dedups_cost_computation_for_same_scope(client, stub):
    stub.preview_sku_keys = ["sku-1", "sku-2"]
    payload = {
        "merchant_id": "merch-A",
        "scope": {"sku_keys": ["sku-2", "sku-1"]},
        "prompts_per_sku": 40,
        "providers": ["gemini"],
    }
    first = client.post("/api/audits/preview", json=payload)
    second = client.post("/api/audits/preview", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["audit_run_id_preview"] == (
        second.json()["audit_run_id_preview"]
    )


def test_brand_facing_routes_do_not_expose_internal_usd(client, stub):
    preview = client.post(
        "/api/audits/preview",
        json={
            "merchant_id": "merch-A",
            "scope": {"sku_keys": ["sku-1"]},
        },
    )
    assert preview.status_code == 200
    preview_text = json_dumps_lower(preview.json())
    assert "usd" not in preview_text
    assert "credit_to_usd" not in preview_text
    assert "provider_cost_fraction" not in preview_text

    row = _detail_row()
    row["cost_summary_jsonb"] = {
        "estimated_cost_usd": 1.23,
        "providers": [{"provider": "gemini", "cost_usd": 1.23}],
        "credit_to_usd": 0.01,
        "provider_cost_fraction": 0.65,
        "total_input_tokens": 2000,
    }
    row["report_jsonb"] = {"merchant_name": "Test", "usd_cogs_internal": 99}
    stub.fetch_returns = row
    detail = client.get("/api/audits/r-1")
    assert detail.status_code == 200
    detail_text = json_dumps_lower(detail.json())
    assert "usd" not in detail_text
    assert "credit_to_usd" not in detail_text
    assert "provider_cost_fraction" not in detail_text


def test_post_422_when_any_product_key_missing(client, stub):
    """P1-2 regression: when one or more product_keys are not owned by
    the authenticated merchant (or don't exist), POST returns 422
    with the missing keys listed — does NOT enqueue a doomed run."""
    stub.missing_keys_returns = ["pk-foreign", "pk-typo"]
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1", "pk-foreign", "pk-typo"],
        },
    )
    assert res.status_code == 422
    detail = res.json().get("detail") or {}
    assert "missing_product_keys" in detail
    assert set(detail["missing_product_keys"]) == {"pk-foreign", "pk-typo"}
    # The route must NOT have enqueued anything.
    assert stub.enqueued == [], (
        "422 path must short-circuit before enqueue"
    )


def test_post_validation_runs_before_idempotency_lookup(client, stub):
    """Ordering guard: cross-tenant guard already runs first, but the
    product-key ownership check must also fire BEFORE the idempotency
    lookup. Otherwise a typo would still bump the daily-cap counter
    or hit the idempotency table on every retry."""
    stub.missing_keys_returns = ["pk-not-owned"]
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-not-owned"],
        },
    )
    assert res.status_code == 422
    assert stub.idem_lookups == [], (
        "Ownership-422 must short-circuit before find_in_flight is "
        "called — otherwise typos pollute the idempotency lookups"
    )


def test_post_happy_path_with_valid_keys_passes_ownership_check(client, stub):
    """Sanity: when missing_keys returns empty, the route proceeds
    to enqueue as before."""
    stub.missing_keys_returns = []
    res = client.post(
        "/api/audits",
        json={"merchant_id": "merch-A", "product_keys": ["pk-1"]},
    )
    assert res.status_code == 202
    assert len(stub.enqueued) == 1


# =====================================================================
# GET /api/audits/{run_id}
# =====================================================================


def _detail_row(run_id: str = "r-1", stage: str = "completed",
                merchant_id: str = "merch-A") -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "merchant_id": merchant_id,
        "subject_type": "merchant",
        "stage": stage,
        "stage_updated_at": "2026-05-09T12:00:00+00:00",
        "requested_at": "2026-05-09T11:55:00+00:00",
        "completed_at": "2026-05-09T12:00:00+00:00",
        "cancelled_at": None,
        "product_keys": ["pk-1"],
        "verdict_labels": ["VISIBLE VIA RETAILERS"],
        "visibility_score_avg": 67,
        "attribution_score_avg": 25,
        "category_visibility_score_avg": 60,
        "audited_via_pivota_canonical": [],
        "partial_result_jsonb": None,
        "report_jsonb": {"merchant_name": "Test"},
        "cost_summary_jsonb": None,
        "error_jsonb": None,
        "error_message": None,
        "idempotency_key": "idem-x",
    }


def test_get_returns_canonical_shape(client, stub):
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1")
    assert res.status_code == 200
    body = res.json()
    assert body["run_id"] == "r-1"
    assert body["stage"] == "completed"
    assert body["report_jsonb"]["merchant_name"] == "Test"


def test_get_404_when_not_found(client, stub):
    stub.fetch_returns = None
    res = client.get("/api/audits/nonexistent")
    assert res.status_code == 404


def test_get_404_for_cross_tenant_run(client, stub):
    stub.fetch_returns = _detail_row(merchant_id="merch-OTHER")
    res = client.get("/api/audits/r-1")
    # Don't leak existence — cross-tenant looks identical to not-found.
    assert res.status_code == 404


# =====================================================================
# P0-4: audience auth restrictions on /api/audits/{run_id}?audience=
# =====================================================================


def test_get_merchant_audience_allowed(client, stub, monkeypatch):
    """Merchant JWT + ?audience=merchant — the one allowed projection."""
    stub.fetch_returns = _detail_row()
    from routes import audit_runs_routes  # noqa: F401

    async def fake_fetch_projection(*, audit_run_id, audience):
        return {"payload_jsonb": {"audience": "merchant",
                                  "action_queue": []}}

    from db import audit_evidence
    monkeypatch.setattr(
        audit_evidence, "fetch_projection", fake_fetch_projection,
    )
    res = client.get("/api/audits/r-1?audience=merchant")
    assert res.status_code == 200
    body = res.json()
    assert body.get("audience") == "merchant"


def test_get_rejects_internal_ops_audience_for_merchant_jwt(client, stub):
    """The bug: merchant JWT could fetch internal_ops projection of
    their own audit. Must now return 403 — not 200, not 404."""
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=internal_ops")
    assert res.status_code == 403
    detail = (res.json() or {}).get("detail") or ""
    assert "employee or admin" in detail.lower() or \
        "merchant jwts may only read" in detail.lower(), (
            f"403 detail should explain the auth requirement; got {detail}"
        )


def test_get_rejects_employee_bd_audience_for_merchant_jwt(client, stub):
    """employee_bd projection includes full evidence + cost detail.
    Must not be reachable via a merchant JWT."""
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=employee_bd")
    assert res.status_code == 403


def test_get_rejects_pivota_pdp_feed_audience_for_merchant_jwt(client, stub):
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=pivota_pdp_feed")
    assert res.status_code == 403


def test_get_rejects_frontend_agent_feed_audience_for_merchant_jwt(client, stub):
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=frontend_agent_feed")
    assert res.status_code == 403


def test_get_unknown_audience_returns_422_not_403(client, stub):
    """Schema validation runs BEFORE the role check — an unknown
    audience is a client error (422), not a permission error (403).
    Ordering matters so callers see a clear "fix your audience param"
    signal instead of a misleading 'employee auth required'."""
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=nonsense_audience")
    assert res.status_code == 422


def test_get_cross_tenant_with_internal_audience_still_returns_404(client, stub):
    """Cross-tenant + internal audience: the cross-tenant 404 must
    still win (don't leak existence). The audience-based 403 only
    fires for runs the merchant DOES own."""
    stub.fetch_returns = _detail_row(merchant_id="merch-OTHER")
    res = client.get("/api/audits/r-1?audience=internal_ops")
    assert res.status_code == 404


# =====================================================================
# POST /api/audits/{run_id}/cancel
# =====================================================================


def test_cancel_active_run_succeeds(client, stub):
    stub.fetch_returns = _detail_row(stage="probing")
    res = client.post("/api/audits/r-1/cancel")
    assert res.status_code == 202
    body = res.json()
    assert body["cancellation_requested"] is True
    assert body["current_stage"] == "probing"
    assert stub.cancelled == ["r-1"]


def test_cancel_terminal_run_is_noop(client, stub):
    stub.fetch_returns = _detail_row(stage="completed")
    res = client.post("/api/audits/r-1/cancel")
    assert res.status_code == 202
    body = res.json()
    assert body["cancellation_requested"] is False
    assert "terminal" in body["reason"].lower()
    assert stub.cancelled == []


def test_cancel_404_for_cross_tenant_run(client, stub):
    stub.fetch_returns = _detail_row(merchant_id="merch-OTHER",
                                      stage="probing")
    res = client.post("/api/audits/r-1/cancel")
    assert res.status_code == 404
    assert stub.cancelled == []


# =====================================================================
# GET /api/audits (list)
# =====================================================================


def test_list_returns_recent_runs(client, stub):
    stub.list_returns = [
        {"run_id": "r-1", "status": "succeeded"},
        {"run_id": "r-2", "status": "running"},
    ]
    res = client.get("/api/audits")
    assert res.status_code == 200
    assert len(res.json()) == 2


def test_list_rejects_invalid_limit(client, stub):
    res = client.get("/api/audits?limit=999")
    assert res.status_code == 422
    res = client.get("/api/audits?limit=0")
    assert res.status_code == 422


def test_list_threads_subject_type_filter(client, stub):
    # ?subject_type= scopes the history to one run kind so each surface lists
    # only the runs it can open (per-SKU = "merchant", URL wedge = "merchant_url").
    stub.list_returns = [{"run_id": "r-1", "status": "succeeded"}]
    res = client.get("/api/audits?subject_type=merchant_url")
    assert res.status_code == 200
    assert stub.recent_subject_type == "merchant_url"


def test_list_subject_type_defaults_to_all(client, stub):
    stub.list_returns = []
    res = client.get("/api/audits")
    assert res.status_code == 200
    assert stub.recent_subject_type is None

"""
Tier-2 prototype-belt deletions (2026-08-11) — these tests pin the removals so
the fabrication belt cannot quietly grow back:

- orchestrator/payment_executor.py (fabricated Adyen success) and
  adapters/ap2_payment_adapter.py (unwired, broken ctor) are DELETED modules.
- adapters/stripe_adapter.py no longer charges under the process-global
  PLATFORM key (Pivota-as-MoR hazard); only webhook-signature verification
  remains.
- /agent/pay and /agent/pay-simple keep answering an actionable 410 (never a
  404), with the coin-flip simulation belt behind them gone.
- ProtocolAdapterService no longer registers the fictional X402Adapter and no
  adapter advertises fictional endpoint maps.
- The promotions lane is deleted end-to-end (ADR-022): the internal promotions
  API answers 404, and none of the promo modules import. The earlier gate
  (manual FLASH_SALE / FREE_SHIPPING refused with PROMO_TYPE_NOT_APPLIED_AT_QUOTE)
  is superseded by the deletion.
"""
import importlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app
from utils.auth import create_access_token


@pytest.fixture
def client():
    return TestClient(app)


class TestDeletedModulesStayDeleted:
    def test_payment_executor_is_gone(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("orchestrator.payment_executor")

    def test_ap2_payment_adapter_is_gone(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("adapters.ap2_payment_adapter")


class TestStripeAdapterSurface:
    def test_only_webhook_verification_remains(self):
        mod = importlib.import_module("adapters.stripe_adapter")
        public = {n for n in dir(mod) if not n.startswith("_") and callable(getattr(mod, n))}
        # Allowlist: the ONE legitimate export. create/get/confirm_payment_intent
        # (platform-key charges) must not come back.
        assert public == {"verify_webhook_signature"}

    def test_import_does_not_arm_the_platform_key(self):
        import stripe

        from config.settings import settings

        mod = importlib.import_module("adapters.stripe_adapter")
        prior = stripe.api_key
        # Review hardening: with no STRIPE_SECRET_KEY in the test env, the OLD
        # `stripe.api_key = settings.stripe_secret_key` also left the global
        # None, so the assertion held vacuously. Plant a sentinel so a revived
        # import-time assignment is caught red-handed.
        try:
            with patch.object(settings, "stripe_secret_key", "sk_test_sentinel_do_not_arm"):
                stripe.api_key = None
                importlib.reload(mod)
                # The old module set stripe.api_key = settings.stripe_secret_key
                # at import time — a process-global platform key any later charge
                # would silently use. Importing must leave the global untouched.
                assert stripe.api_key is None
        finally:
            stripe.api_key = prior


class TestDeprecatedPayRoutesStillAnswer410:
    def _headers(self):
        token = create_access_token(
            {"sub": "user_test", "email": "t@example.com", "role": "admin"}
        )
        return {"Authorization": f"Bearer {token}"}

    def test_pay_is_410_not_404(self, client):
        resp = client.post(
            "/agent/pay",
            headers=self._headers(),
            json={"agent_id": "a", "merchant_id": "m", "items": [], "amount": 10.0, "currency": "USD"},
        )
        assert resp.status_code == 410
        assert resp.json()["detail"]["error"] == "QUOTE_REQUIRED_BEFORE_PURCHASE"

    def test_pay_simple_is_410_not_404(self, client):
        resp = client.post(
            "/agent/pay-simple",
            headers=self._headers(),
            json={"agent_id": "a", "order_id": "o", "amount": 10.0, "currency": "USD"},
        )
        assert resp.status_code == 410
        assert resp.json()["detail"]["error"] == "QUOTE_REQUIRED_BEFORE_PURCHASE"


class TestProtocolAdapterRegistryIsHonest:
    def test_registry_allowlist_and_no_fictional_endpoints(self):
        from services.protocol_adapter_service import ProtocolAdapterService

        svc = ProtocolAdapterService(None)
        assert sorted(svc.adapters.keys()) == ["ACP", "AP2"]
        for adapter in svc.adapters.values():
            assert not hasattr(adapter, "get_endpoints")

    @pytest.mark.asyncio
    async def test_x402_is_refused_as_unknown(self):
        from services.protocol_adapter_service import ProtocolAdapterService

        svc = ProtocolAdapterService(None)
        ok, err = await svc.validate_request("X-402", {"anything": 1})
        assert ok is False
        assert err == "Unknown protocol: X-402"


class TestPromotionsLaneStaysDeleted:
    """ADR-022: the merchant-promotions lane is deleted end-to-end.

    Prod held exactly 17 rows, all PIVOTA_AUDIT_20260421 Shopify-synced fixtures
    for one test merchant. These tests pin the deletion the same way the belt
    deletions above are pinned, so the lane cannot quietly grow back. The
    rebuild design (enforcement follows checkout authority; agent-applied
    Shopify codes) lives in ADR-022.
    """

    ADMIN = {"X-ADMIN-KEY": "test-admin-key"}

    @pytest.mark.parametrize(
        "module",
        [
            "services.promotions_service",
            "services.shopify_promotions_sync",
            "services.store_discount_evidence_service",
            "services.shopify_discount_fixture_service",
            "routes.merchant_promotions_api",
            "routes.agent_promotions",
            "routes.shopify_promotions_sync_api",
        ],
    )
    def test_promo_modules_are_gone(self, module):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/agent/internal/promotions"),
            ("post", "/agent/internal/promotions"),
            ("get", "/agent/v1/promotions/active"),
            ("post", "/agent/internal/shopify/promotions/sync/merch_t"),
        ],
    )
    def test_promo_routes_answer_404(self, client, monkeypatch, method, path):
        monkeypatch.setenv("PROMOTIONS_ADMIN_KEY", "test-admin-key")
        resp = getattr(client, method)(path, headers=self.ADMIN)
        assert resp.status_code == 404

    def test_quote_service_has_no_promo_applier(self):
        from services.quote_service import QuoteService

        assert not hasattr(QuoteService, "_apply_infra_promotions_best_effort")

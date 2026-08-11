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
- Manual FLASH_SALE / FREE_SHIPPING promotions are refused at the internal API
  (the quote engine applies only MULTI_BUY_DISCOUNT), and the quote engine
  records an evidence decision for any promo type it skips.
"""
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

        mod = importlib.import_module("adapters.stripe_adapter")
        stripe.api_key = None
        importlib.reload(mod)
        # The old module set stripe.api_key = settings.stripe_secret_key at
        # import time — a process-global platform key any later charge would
        # silently use. Importing must leave the global untouched.
        assert stripe.api_key is None


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


class TestManualPromoTrapdoorClosed:
    ADMIN = {"X-ADMIN-KEY": "test-admin-key"}

    def _payload(self, ptype, config):
        return {
            "merchantId": "merch_t",
            "name": "t",
            "type": ptype,
            "startAt": "2026-01-01T00:00:00Z",
            "endAt": "2027-01-01T00:00:00Z",
            "channels": ["creator_agents"],
            "scope": {"global": True},
            "config": config,
        }

    def test_manual_flash_sale_is_rejected(self, client, monkeypatch):
        monkeypatch.setenv("PROMOTIONS_ADMIN_KEY", "test-admin-key")
        resp = client.post(
            "/agent/internal/promotions",
            headers=self.ADMIN,
            json=self._payload("FLASH_SALE", {"kind": "FLASH_SALE", "flashPrice": 5}),
        )
        # 400 (not 422): the error middleware rewrites 422s into a generic
        # INVALID_REQUEST, so the named refusal rides a 400.
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "PROMO_TYPE_NOT_APPLIED_AT_QUOTE"

    def test_manual_free_shipping_is_rejected(self, client, monkeypatch):
        monkeypatch.setenv("PROMOTIONS_ADMIN_KEY", "test-admin-key")
        resp = client.post(
            "/agent/internal/promotions",
            headers=self.ADMIN,
            json=self._payload("FREE_SHIPPING", {"kind": "FREE_SHIPPING"}),
        )
        # 400 (not 422): the error middleware rewrites 422s into a generic
        # INVALID_REQUEST, so the named refusal rides a 400.
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "PROMO_TYPE_NOT_APPLIED_AT_QUOTE"

    def test_multi_buy_still_passes_the_gate(self, client, monkeypatch):
        monkeypatch.setenv("PROMOTIONS_ADMIN_KEY", "test-admin-key")
        fake_promo = SimpleNamespace(dict=lambda: {"id": "promo_1", "type": "MULTI_BUY_DISCOUNT"})
        with patch(
            "routes.merchant_promotions_api.create_promotion",
            AsyncMock(return_value=fake_promo),
        ) as created:
            resp = client.post(
                "/agent/internal/promotions",
                headers=self.ADMIN,
                json=self._payload(
                    "MULTI_BUY_DISCOUNT",
                    {"kind": "MULTI_BUY_DISCOUNT", "thresholdQuantity": 3, "discountPercent": 10},
                ),
            )
        assert resp.status_code == 201
        assert created.await_count == 1


class TestQuoteRecordsSkippedPromoTypes:
    @pytest.mark.asyncio
    async def test_unsupported_type_leaves_an_evidence_decision(self, monkeypatch):
        from services.quote_service import QuoteService

        monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")
        flash = SimpleNamespace(
            id="promo_flash", type="FLASH_SALE", scope={"global": True}, config={"kind": "FLASH_SALE"}
        )
        evidence = {}
        with patch(
            "services.quote_service.list_promotions",
            AsyncMock(return_value=([flash], 1)),
        ):
            await QuoteService._apply_infra_promotions_best_effort(
                SimpleNamespace(),
                merchant_id="merch_t",
                items=[],
                pricing={},
                line_items=[],
                promotion_lines=[],
                discount_evidence=evidence,
                creator_id=None,
            )
        assert evidence["decisions"] == [
            {
                "promotion_id": "promo_flash",
                "decision": "skipped",
                "reason": "promo_type_not_applied_at_quote",
                "promo_type": "FLASH_SALE",
                "source": "pivota_infra",
            }
        ]

"""The /agent/v1/payments lane must honor the order-level test-processor probe.

The gateway stamps `metadata.allow_test_psp_surfaces` onto an order at
create_order, where routes.order_routes._resolve_order_live_readiness_requirement
gates the stamp on ALLOW_TEST_PSP_PROBE + TEST_PSP_PROBE_MERCHANTS before
relaxing live-readiness. The payments lane charges that same order later and
must resolve the SAME bypass from the order's persisted metadata — otherwise the
probe merchant's test-mode PSP is refused at Pay with "configured for test, not
live" (observed live 2026-08-28) while a live-mode PSP would charge real money.

Every test drives the real route function into create_payment_with_failover and
pins the exact `enforce_live_readiness` kwarg the route delivered; the bypass
must stay allowlist-gated, so three of the four tests exist to prove the stamp
alone buys nothing.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

REAL_MERCHANT = "merch_test_123"


@pytest.fixture(autouse=True)
def _payment_route_collaborators(monkeypatch: pytest.MonkeyPatch):
    """Patch route collaborators (on the module reference, never the source
    module) so create_payment runs to the PSP-failover call, and start every
    test with BOTH probe env vars absent — ambient prod-parallel env must not
    decide a verdict here."""
    import mvp.events as mvp_events
    import mvp.governance as mvp_governance
    import routes.agent_payment_sdk as payment_module
    from db.database import database as database_obj

    monkeypatch.delenv("ALLOW_TEST_PSP_PROBE", raising=False)
    monkeypatch.delenv("TEST_PSP_PROBE_MERCHANTS", raising=False)

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "shopify", "store_id": "store_test"}

    class _Decision:
        decision = "allow"
        reason_codes: list = []
        required_scopes: list = []
        risk_tier = "low"

    class _FakeRoutingService:
        def __init__(self, _database: Any) -> None:
            pass

        async def select_psp(self, **_kwargs: Any):
            return "stripe", {}

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id}

    async def fake_fetch_one(_query: Any, _values: Optional[Dict[str, Any]] = None):
        return None

    monkeypatch.setattr(payment_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(payment_module, "PaymentRoutingService", _FakeRoutingService)
    monkeypatch.setattr(payment_module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(database_obj, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_: None)


def _order_metadata(*, stamped: bool) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "pricing_quote": {
            "quote_id": "q_test_psp_stamp",
            "live_validation": {"status": "validated"},
            "expires_at": "2099-01-01T00:00:00Z",
        },
    }
    if stamped:
        metadata["allow_test_psp_surfaces"] = True
    return metadata


async def _drive_to_failover(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stamped: bool,
    merchant_id: str = REAL_MERCHANT,
) -> Dict[str, Any]:
    """Run the real route into create_payment_with_failover; return its kwargs.

    The stamp travels ONLY on the DB-loaded order row — the request body carries
    nothing but order_id + payment_method, so a passing bypass proves the route
    read the persisted metadata, not caller input.
    """
    import routes.agent_payment_sdk as payment_module
    from fastapi import BackgroundTasks

    async def fake_get_order(order_id: str) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "merchant_id": merchant_id,
            "payment_status": "unpaid",
            "total": 33.0,
            "currency": "USD",
            "items": [{"product_id": "prod_1", "merchant_id": merchant_id}],
            "metadata": _order_metadata(stamped=stamped),
        }

    captured: Dict[str, Any] = {}

    async def fake_create_payment_with_failover(*_args: Any, **kwargs: Any):
        captured.update(kwargs)
        return False, None, "stopped-by-test", "none"

    monkeypatch.setattr(payment_module, "get_order", fake_get_order)
    monkeypatch.setattr(
        payment_module, "create_payment_with_failover", fake_create_payment_with_failover
    )

    class _Context:
        agent_id = "agent_test"
        session_id = "sess_test"

        def can_access_merchant(self, mid: Optional[str]) -> bool:
            return mid == merchant_id

    with pytest.raises(payment_module.HTTPException) as exc_info:
        await payment_module.create_payment(
            payment_module.PaymentRequest(
                order_id="ORD_TEST_STAMP",
                payment_method=payment_module.PaymentMethod(type="dynamic"),
            ),
            BackgroundTasks(),
            context=_Context(),
        )

    # The double failed the charge, so the route must surface OUR failure; any
    # other refusal means the flow never reached the failover call and the
    # captured kwargs (the thing under test) would be vacuous.
    assert exc_info.value.status_code == 500
    assert "stopped-by-test" in str(exc_info.value.detail)
    assert "enforce_live_readiness" in captured
    return captured


@pytest.mark.asyncio
async def test_stamped_order_bypasses_live_readiness_when_probe_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", REAL_MERCHANT)

    captured = await _drive_to_failover(monkeypatch, stamped=True)

    assert captured["enforce_live_readiness"] is False


@pytest.mark.asyncio
async def test_stamp_alone_buys_nothing_without_the_server_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", REAL_MERCHANT)

    captured = await _drive_to_failover(monkeypatch, stamped=True)

    assert captured["enforce_live_readiness"] is True


@pytest.mark.asyncio
async def test_stamp_does_not_bypass_for_a_merchant_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", "merch_someone_else")

    captured = await _drive_to_failover(monkeypatch, stamped=True)

    assert captured["enforce_live_readiness"] is True


@pytest.mark.asyncio
async def test_unstamped_order_enforces_even_with_probe_fully_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", REAL_MERCHANT)

    captured = await _drive_to_failover(monkeypatch, stamped=False)

    assert captured["enforce_live_readiness"] is True

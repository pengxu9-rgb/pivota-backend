from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


def _adyen_escape_component(value: object) -> str:
    return str("" if value is None else value).replace("\\", "\\\\").replace(":", "\\:")


def _adyen_hmac_signature(notification: Dict[str, Any], secret: str) -> str:
    amount = notification.get("amount") or {}
    parts = [
        notification.get("pspReference"),
        notification.get("originalReference"),
        notification.get("merchantAccountCode"),
        notification.get("merchantReference"),
        amount.get("value"),
        amount.get("currency"),
        notification.get("eventCode"),
        notification.get("success"),
    ]
    signing_string = ":".join(_adyen_escape_component(part) for part in parts)
    key = bytes.fromhex(secret)
    digest = hmac.new(key, signing_string.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _adyen_payload(
    order_id: str,
    *,
    psp_reference: str,
    success: str,
    event_code: str = "AUTHORISATION",
    amount_value: int = 4520,
    amount_currency: str = "USD",
    merchant_account_code: str = "WoopayECOM",
    original_reference: str | None = None,
    hmac_signature: str | None = None,
) -> Dict[str, Any]:
    notification = {
        "eventCode": event_code,
        "success": success,
        "pspReference": psp_reference,
        "originalReference": original_reference,
        "merchantReference": order_id,
        "merchantAccountCode": merchant_account_code,
        "amount": {
            "value": amount_value,
            "currency": amount_currency,
        },
        "additionalData": {},
    }
    if hmac_signature is not None:
        notification["additionalData"]["hmacSignature"] = hmac_signature
    return {
        "notificationItems": [
            {
                "NotificationRequestItem": notification,
            }
        ]
    }


def test_adyen_official_hmac_canonical_vector_still_verifies() -> None:
    import routes.psp_routes as psp_routes_module

    secret = "44782DEF547AAA06C910C43932B1EB0C71FC68D9D0C057550C48EC2ACF6BA056"
    notification = {
        "pspReference": "7914073381342284",
        "merchantAccountCode": "TestMerchant",
        "merchantReference": "TestPayment-1407325143704",
        "amount": {
            "value": 1130,
            "currency": "EUR",
        },
        "eventCode": "AUTHORISATION",
        "success": "true",
        "additionalData": {
            "hmacSignature": "coqCmt/IZ4E3CzPvMY8zTjQVL5hYJUiBRg8UU+iCWo0=",
        },
    }

    assert psp_routes_module._verify_adyen_notification_hmac(notification, secret) is True
    assert (
        psp_routes_module._adyen_notification_signing_string(notification)
        == "7914073381342284::TestMerchant:TestPayment-1407325143704:1130:EUR:AUTHORISATION:true"
    )


def test_adyen_zero_amount_hmac_signing_string_keeps_zero_value() -> None:
    import routes.psp_routes as psp_routes_module

    secret = "44782DEF547AAA06C910C43932B1EB0C71FC68D9D0C057550C48EC2ACF6BA056"
    notification = {
        "pspReference": "WMKKJTS2T75GW7V5",
        "merchantAccountCode": "WoopayECOM",
        "merchantReference": "testMerchantRef1",
        "amount": {
            "value": 0,
            "currency": "EUR",
        },
        "eventCode": "AUTHENTICATION",
        "success": "true",
        "additionalData": {},
    }
    signing_string = psp_routes_module._adyen_notification_signing_string(notification)

    assert signing_string == "WMKKJTS2T75GW7V5::WoopayECOM:testMerchantRef1:0:EUR:AUTHENTICATION:true"
    digest = hmac.new(bytes.fromhex(secret), signing_string.encode("utf-8"), hashlib.sha256).digest()
    notification["additionalData"]["hmacSignature"] = base64.b64encode(digest).decode("utf-8")

    assert psp_routes_module._verify_adyen_notification_hmac(notification, secret) is True


def test_adyen_zero_amount_signing_string_has_no_empty_amount_segment() -> None:
    import routes.psp_routes as psp_routes_module

    notification = {
        "pspReference": "WMKKJTS2T75GW7V5",
        "merchantAccountCode": "WoopayECOM",
        "merchantReference": "testMerchantRef1",
        "amount": {
            "value": 0,
            "currency": "EUR",
        },
        "eventCode": "AUTHENTICATION",
        "success": "true",
        "additionalData": {},
    }
    signing_string = psp_routes_module._adyen_notification_signing_string(notification)

    assert ":::" not in signing_string
    assert ":testMerchantRef1::EUR:" not in signing_string
    assert ":testMerchantRef1:0:EUR:" in signing_string


@pytest.mark.asyncio
async def test_adyen_webhook_rejects_invalid_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=_adyen_payload("ORD_BAD_AUTH", psp_reference="PSP_BAD_AUTH", success="true"),
            auth=("wrong_user", "wrong_pass"),
        )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


@pytest.mark.asyncio
async def test_webhooks_adyen_alias_uses_real_basic_auth_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/adyen",
            json=_adyen_payload("ORD_ALIAS_BAD_AUTH", psp_reference="PSP_ALIAS_BAD_AUTH", success="true"),
            auth=("wrong_user", "wrong_pass"),
        )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


@pytest.mark.asyncio
async def test_adyen_webhook_rejects_invalid_hmac_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(
        psp_routes_module.settings,
        "adyen_webhook_secret",
        "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
        raising=False,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=_adyen_payload(
                "ORD_BAD_SIG",
                psp_reference="PSP_BAD_SIG",
                success="true",
                hmac_signature="bad_signature",
            ),
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid HMAC signature"


@pytest.mark.asyncio
async def test_adyen_webhook_rejects_missing_hmac_signature_when_secret_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(
        psp_routes_module.settings,
        "adyen_webhook_secret",
        "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
        raising=False,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=_adyen_payload(
                "ORD_MISSING_SIG",
                psp_reference="PSP_MISSING_SIG",
                success="true",
            ),
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid HMAC signature"


@pytest.mark.asyncio
async def test_adyen_webhook_authorisation_success_marks_order_paid_and_syncs_shopify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as order_routes_module
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    merchant_webhook_calls: list[Dict[str, Any]] = []
    paid_calls: list[str] = []
    payment_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []
    shopify_calls: list[str] = []
    scheduled_tasks: list[asyncio.Task[Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_SUCCESS",
        psp_reference="PSP_ADYEN_SUCCESS",
        success="true",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return "ORD_ADYEN_SUCCESS"

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        assert order_id == "ORD_ADYEN_SUCCESS"
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "payment_status": "pending",
        }

    async def fake_mark_order_paid(order_id: str) -> bool:
        paid_calls.append(order_id)
        return True

    async def fake_update_payment_info(
        order_id: str,
        payment_intent_id: str,
        client_secret: str,
        payment_status: str = "processing",
        psp_used: str | None = None,
    ) -> bool:
        payment_updates.append(
            {
                "order_id": order_id,
                "payment_intent_id": payment_intent_id,
                "client_secret": client_secret,
                "payment_status": payment_status,
                "psp_used": psp_used,
            }
        )
        return True

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_emit_merchant_webhook_event(
        merchant_id: str,
        *,
        event_type: str,
        payload,
        request_id=None,
        force_delivery: bool = False,
    ) -> Dict[str, Any]:
        merchant_webhook_calls.append(
            {
                "merchant_id": merchant_id,
                "event_type": event_type,
                "payload": dict(payload),
                "request_id": request_id,
                "force_delivery": force_delivery,
            }
        )
        return {"status": "delivered"}

    async def fake_create_shopify_order(order_id: str) -> bool:
        shopify_calls.append(order_id)
        return True

    real_create_task = asyncio.create_task

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(psp_routes_module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(psp_routes_module, "emit_merchant_webhook_event", fake_emit_merchant_webhook_event)
    monkeypatch.setattr(psp_routes_module.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    if scheduled_tasks:
        await asyncio.gather(*scheduled_tasks)

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_SUCCESS", "succeeded", "adyen", "PSP_ADYEN_SUCCESS")]
    assert paid_calls == ["ORD_ADYEN_SUCCESS"]
    assert payment_updates == [
        {
            "order_id": "ORD_ADYEN_SUCCESS",
            "payment_intent_id": "PSP_ADYEN_SUCCESS",
            "client_secret": "",
            "payment_status": "paid",
            "psp_used": "adyen",
        }
    ]
    assert shopify_calls == ["ORD_ADYEN_SUCCESS"]
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "payment_confirmed_webhook"
    assert order_events[0]["order_id"] == "ORD_ADYEN_SUCCESS"
    assert order_events[0]["merchant_id"] == "m_adyen"
    assert order_events[0]["metadata"]["psp"] == "adyen"
    assert merchant_webhook_calls == [
        {
            "merchant_id": "m_adyen",
            "event_type": "payment.completed",
            "payload": {
                "order_id": "ORD_ADYEN_SUCCESS",
                "merchant_id": "m_adyen",
                "payment_id": "PSP_ADYEN_SUCCESS",
                "transaction_id": "PSP_ADYEN_SUCCESS",
                "amount": 45.2,
                "currency": "USD",
                "psp_used": "adyen",
                "status": "paid",
                "customer_email": None,
            },
            "request_id": None,
            "force_delivery": False,
        }
    ]


@pytest.mark.asyncio
async def test_adyen_webhook_handles_multiple_notification_items_without_partial_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as order_routes_module
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    paid_calls: list[str] = []
    payment_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []
    shopify_calls: list[str] = []
    scheduled_tasks: list[asyncio.Task[Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = {
        "notificationItems": [
            {
                "NotificationRequestItem": _adyen_payload(
                    "ORD_BATCH_SUCCESS",
                    psp_reference="PSP_BATCH_SUCCESS",
                    success="true",
                )["notificationItems"][0]["NotificationRequestItem"]
            },
            {
                "NotificationRequestItem": _adyen_payload(
                    "ORD_BATCH_FAIL",
                    psp_reference="PSP_BATCH_FAIL",
                    success="false",
                )["notificationItems"][0]["NotificationRequestItem"]
            },
        ]
    }
    for item in payload["notificationItems"]:
        notification = item["NotificationRequestItem"]
        notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        if order_id == "ORD_BATCH_SUCCESS":
            return {
                "order_id": order_id,
                "merchant_id": "m_adyen",
                "payment_status": "pending",
            }
        if order_id == "ORD_BATCH_FAIL":
            return {
                "order_id": order_id,
                "merchant_id": "m_adyen",
                "payment_status": "pending",
            }
        return None

    async def fake_mark_order_paid(order_id: str) -> bool:
        paid_calls.append(order_id)
        return True

    async def fake_update_payment_info(
        order_id: str,
        payment_intent_id: str,
        client_secret: str,
        payment_status: str = "processing",
        psp_used: str | None = None,
    ) -> bool:
        payment_updates.append(
            {
                "order_id": order_id,
                "payment_intent_id": payment_intent_id,
                "client_secret": client_secret,
                "payment_status": payment_status,
                "psp_used": psp_used,
            }
        )
        return True

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_create_shopify_order(order_id: str) -> bool:
        shopify_calls.append(order_id)
        return True

    real_create_task = asyncio.create_task

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(psp_routes_module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(psp_routes_module.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    if scheduled_tasks:
        await asyncio.gather(*scheduled_tasks)

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [
        ("ORD_BATCH_SUCCESS", "succeeded", "adyen", "PSP_BATCH_SUCCESS"),
        ("ORD_BATCH_FAIL", "failed", "adyen", "PSP_BATCH_FAIL"),
    ]
    assert paid_calls == ["ORD_BATCH_SUCCESS"]
    assert payment_updates == [
        {
            "order_id": "ORD_BATCH_SUCCESS",
            "payment_intent_id": "PSP_BATCH_SUCCESS",
            "client_secret": "",
            "payment_status": "paid",
            "psp_used": "adyen",
        }
    ]
    assert shopify_calls == ["ORD_BATCH_SUCCESS"]
    assert len(order_events) == 1
    assert order_events[0]["order_id"] == "ORD_BATCH_SUCCESS"
    assert order_events[0]["event_type"] == "payment_confirmed_webhook"


@pytest.mark.asyncio
async def test_adyen_webhook_authorisation_failure_records_failed_callback_without_marking_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_FAIL",
        psp_reference="PSP_ADYEN_FAIL",
        success="false",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return "ORD_ADYEN_FAIL"

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("failed AUTHORISATION should not mark order paid")

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_FAIL", "failed", "adyen", "PSP_ADYEN_FAIL")]


@pytest.mark.asyncio
async def test_adyen_webhook_already_paid_order_remains_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as order_routes_module
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_ALREADY_PAID",
        psp_reference="PSP_ADYEN_ALREADY_PAID",
        success="true",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "payment_status": "paid",
        }

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("already-paid Adyen webhook should not mutate order state again")

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fail_if_called)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_ALREADY_PAID", "succeeded", "adyen", "PSP_ADYEN_ALREADY_PAID")]


@pytest.mark.asyncio
async def test_adyen_webhook_refund_success_reconciles_partial_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    merchant_webhook_calls: list[Dict[str, Any]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_REFUND",
        psp_reference="PSP_ADYEN_REFUND",
        success="true",
        event_code="REFUND",
        amount_value=1200,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "paid",
            "payment_status": "paid",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "metadata": {},
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_emit_merchant_webhook_event(
        merchant_id: str,
        *,
        event_type: str,
        payload,
        request_id=None,
        force_delivery: bool = False,
    ) -> Dict[str, Any]:
        merchant_webhook_calls.append(
            {
                "merchant_id": merchant_id,
                "event_type": event_type,
                "payload": dict(payload),
                "request_id": request_id,
                "force_delivery": force_delivery,
            }
        )
        return {"status": "delivered"}

    async def fake_fetch_one(query: str, values: Dict[str, Any] | None = None):
        assert "FROM refund_records" in query
        assert values is not None
        assert values["order_id"] == "ORD_ADYEN_REFUND"
        assert values["merchant_id"] == "m_adyen"
        assert values["psp_reference"] == "PSP_ADYEN_REFUND"
        return {"refund_id": "REF_ADYEN_1"}

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(psp_routes_module, "emit_merchant_webhook_event", fake_emit_merchant_webhook_event)
    monkeypatch.setattr(psp_routes_module.database, "fetch_one", fake_fetch_one)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_REFUND", "refunded", "adyen", "PSP_ADYEN_REFUND")]
    assert len(status_updates) == 1
    assert status_updates[0]["order_id"] == "ORD_ADYEN_REFUND"
    assert status_updates[0]["status"] == "partially_refunded"
    assert status_updates[0]["payment_status"] == "partially_refunded"
    assert str(status_updates[0]["total_refunded"]) == "12.00"
    assert status_updates[0]["metadata"]["adyen_refund_psp_refs"] == ["PSP_ADYEN_REFUND"]
    assert status_updates[0]["metadata"]["adyen_last_refund"]["currency"] == "USD"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_processed_webhook"
    assert order_events[0]["order_id"] == "ORD_ADYEN_REFUND"
    assert order_events[0]["metadata"]["psp"] == "adyen"
    assert order_events[0]["metadata"]["refund_amount"] == "12"
    assert merchant_webhook_calls == [
        {
            "merchant_id": "m_adyen",
            "event_type": "refund.processed",
            "payload": {
                "order_id": "ORD_ADYEN_REFUND",
                "merchant_id": "m_adyen",
                "refund_id": "REF_ADYEN_1",
                "amount": 12.0,
                "currency": "USD",
                "is_partial": True,
                "status": "partially_refunded",
            },
            "request_id": None,
            "force_delivery": False,
        }
    ]


@pytest.mark.asyncio
async def test_adyen_webhook_duplicate_refund_psp_reference_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_REFUND_DUP",
        psp_reference="PSP_ADYEN_REFUND_DUP",
        success="true",
        event_code="REFUND",
        amount_value=500,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "partially_refunded",
            "payment_status": "partially_refunded",
            "total": "20.00",
            "total_refunded": "5.00",
            "currency": "USD",
            "metadata": {
                "adyen_refund_psp_refs": ["PSP_ADYEN_REFUND_DUP"],
            },
        }

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("duplicate Adyen refund webhook should not mutate order state again")

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_REFUND_DUP", "refunded", "adyen", "PSP_ADYEN_REFUND_DUP")]


@pytest.mark.asyncio
async def test_adyen_webhook_cancellation_success_marks_order_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_CANCEL",
        psp_reference="PSP_ADYEN_CANCEL",
        success="true",
        event_code="CANCELLATION",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "pending",
            "payment_status": "pending",
            "metadata": {},
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_CANCEL", "cancelled", "adyen", "PSP_ADYEN_CANCEL")]
    assert len(status_updates) == 1
    assert status_updates[0]["order_id"] == "ORD_ADYEN_CANCEL"
    assert status_updates[0]["status"] == "cancelled"
    assert status_updates[0]["payment_status"] == "cancelled"
    assert status_updates[0]["metadata"]["adyen_last_cancellation"]["psp_reference"] == "PSP_ADYEN_CANCEL"
    assert status_updates[0]["cancelled_at"] is not None
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "order_cancelled_webhook"
    assert order_events[0]["order_id"] == "ORD_ADYEN_CANCEL"
    assert order_events[0]["metadata"]["psp"] == "adyen"


@pytest.mark.asyncio
async def test_adyen_webhook_already_cancelled_order_remains_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_CANCELLED",
        psp_reference="PSP_ADYEN_CANCELLED",
        success="true",
        event_code="CANCELLATION",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "cancelled",
            "payment_status": "cancelled",
            "metadata": {},
        }

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("already-cancelled Adyen webhook should not mutate order state again")

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_CANCELLED", "cancelled", "adyen", "PSP_ADYEN_CANCELLED")]


@pytest.mark.asyncio
async def test_adyen_webhook_refund_failed_rolls_back_applied_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_REFUND_FAILED",
        psp_reference="PSP_ADYEN_REFUND_FAILED",
        success="true",
        event_code="REFUND_FAILED",
        amount_value=1200,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "partially_refunded",
            "payment_status": "partially_refunded",
            "total": "20.00",
            "total_refunded": "12.00",
            "currency": "USD",
            "metadata": {
                "adyen_refund_psp_refs": ["PSP_ADYEN_REFUND_FAILED"],
                "adyen_refund_records": {
                    "PSP_ADYEN_REFUND_FAILED": {
                        "psp_reference": "PSP_ADYEN_REFUND_FAILED",
                        "amount_minor": "1200",
                        "amount": "12.00",
                        "currency": "USD",
                    }
                },
                "adyen_last_refund": {
                    "psp_reference": "PSP_ADYEN_REFUND_FAILED",
                    "amount_minor": "1200",
                    "amount": "12.00",
                    "currency": "USD",
                },
            },
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_REFUND_FAILED", "failed", "adyen", "PSP_ADYEN_REFUND_FAILED")]
    assert len(status_updates) == 1
    assert status_updates[0]["status"] == "paid"
    assert status_updates[0]["payment_status"] == "paid"
    assert str(status_updates[0]["total_refunded"]) == "0.00"
    assert status_updates[0]["metadata"]["adyen_refund_psp_refs"] == []
    assert status_updates[0]["metadata"]["adyen_refund_records"] == {}
    assert status_updates[0]["metadata"]["adyen_last_refund_failure"]["rolled_back_ref"] == "PSP_ADYEN_REFUND_FAILED"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_failed_webhook"
    assert order_events[0]["metadata"]["refund_amount"] == "12.00"


@pytest.mark.asyncio
async def test_adyen_webhook_refunded_reversed_rolls_back_last_refund_when_event_has_new_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_REFUND_REVERSED",
        psp_reference="PSP_ADYEN_REFUND_REVERSED_EVENT",
        original_reference="PSP_ADYEN_REFUND_ORIG",
        success="true",
        event_code="REFUNDED_REVERSED",
        amount_value=1200,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "partially_refunded",
            "payment_status": "partially_refunded",
            "total": "20.00",
            "total_refunded": "12.00",
            "currency": "USD",
            "metadata": {
                "adyen_refund_psp_refs": ["PSP_ADYEN_REFUND_ORIG"],
                "adyen_refund_records": {
                    "PSP_ADYEN_REFUND_ORIG": {
                        "psp_reference": "PSP_ADYEN_REFUND_ORIG",
                        "amount_minor": "1200",
                        "amount": "12.00",
                        "currency": "USD",
                    }
                },
                "adyen_last_refund": {
                    "psp_reference": "PSP_ADYEN_REFUND_ORIG",
                    "amount_minor": "1200",
                    "amount": "12.00",
                    "currency": "USD",
                },
            },
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_REFUND_REVERSED", "reversed", "adyen", "PSP_ADYEN_REFUND_REVERSED_EVENT")]
    assert len(status_updates) == 1
    assert status_updates[0]["status"] == "paid"
    assert status_updates[0]["payment_status"] == "paid"
    assert str(status_updates[0]["total_refunded"]) == "0.00"
    assert status_updates[0]["metadata"]["adyen_refund_psp_refs"] == []
    assert status_updates[0]["metadata"]["adyen_refund_records"] == {}
    assert status_updates[0]["metadata"]["adyen_last_refund_reversal"]["rolled_back_ref"] == "PSP_ADYEN_REFUND_ORIG"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_reversed_webhook"
    assert order_events[0]["metadata"]["rolled_back_ref"] == "PSP_ADYEN_REFUND_ORIG"


@pytest.mark.asyncio
async def test_adyen_webhook_refund_rejection_logs_without_mutating_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_REFUND_REJECTED",
        psp_reference="PSP_ADYEN_REFUND_REJECTED",
        success="false",
        event_code="REFUND",
        amount_value=1200,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["reason"] = "Requested refund amount too high"
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "paid",
            "payment_status": "paid",
            "metadata": {},
        }

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("rejected Adyen refund should not mutate order state")

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_REFUND_REJECTED", "failed", "adyen", "PSP_ADYEN_REFUND_REJECTED")]
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_rejected_webhook"
    assert order_events[0]["metadata"]["reason"] == "Requested refund amount too high"


@pytest.mark.asyncio
async def test_adyen_webhook_refund_with_data_success_reconciles_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_REFUND_WITH_DATA",
        psp_reference="PSP_ADYEN_REFUND_WITH_DATA",
        success="true",
        event_code="REFUND_WITH_DATA",
        amount_value=1200,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "paid",
            "payment_status": "paid",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "metadata": {},
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_REFUND_WITH_DATA", "refunded", "adyen", "PSP_ADYEN_REFUND_WITH_DATA")]
    assert len(status_updates) == 1
    assert status_updates[0]["status"] == "partially_refunded"
    assert status_updates[0]["payment_status"] == "partially_refunded"
    assert str(status_updates[0]["total_refunded"]) == "12.00"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_processed_webhook"
    assert order_events[0]["metadata"]["event_code"] == "REFUND_WITH_DATA"


@pytest.mark.asyncio
async def test_adyen_webhook_refund_with_data_rejection_logs_without_mutating_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_REFUND_WITH_DATA_REJECTED",
        psp_reference="PSP_ADYEN_REFUND_WITH_DATA_REJECTED",
        success="false",
        event_code="REFUND_WITH_DATA",
        amount_value=1200,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["reason"] = "Authorisation for refund failed"
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "paid",
            "payment_status": "paid",
            "metadata": {},
        }

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("rejected REFUND_WITH_DATA should not mutate order state")

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [
        (
            "ORD_ADYEN_REFUND_WITH_DATA_REJECTED",
            "failed",
            "adyen",
            "PSP_ADYEN_REFUND_WITH_DATA_REJECTED",
        )
    ]
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_rejected_webhook"
    assert order_events[0]["metadata"]["event_code"] == "REFUND_WITH_DATA"


@pytest.mark.asyncio
async def test_adyen_webhook_cancel_or_refund_refunds_captured_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_CANCEL_OR_REFUND_REFUND",
        psp_reference="PSP_ADYEN_CANCEL_OR_REFUND_REFUND",
        success="true",
        event_code="CANCEL_OR_REFUND",
        amount_value=2000,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["reason"] = "Processed successfully"
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "paid",
            "payment_status": "paid",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "metadata": {},
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [
        (
            "ORD_ADYEN_CANCEL_OR_REFUND_REFUND",
            "refunded",
            "adyen",
            "PSP_ADYEN_CANCEL_OR_REFUND_REFUND",
        )
    ]
    assert len(status_updates) == 1
    assert status_updates[0]["status"] == "refunded"
    assert status_updates[0]["payment_status"] == "refunded"
    assert str(status_updates[0]["total_refunded"]) == "20.00"
    assert status_updates[0]["metadata"]["adyen_last_cancel_or_refund"]["outcome"] == "refunded"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "cancel_or_refund_webhook"
    assert order_events[0]["metadata"]["outcome"] == "refunded"


@pytest.mark.asyncio
async def test_adyen_webhook_cancel_or_refund_cancels_uncaptured_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_CANCEL_OR_REFUND_CANCEL",
        psp_reference="PSP_ADYEN_CANCEL_OR_REFUND_CANCEL",
        success="true",
        event_code="CANCEL_OR_REFUND",
        amount_value=0,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["reason"] = "Cancelled before capture"
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "pending",
            "payment_status": "authorized",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "metadata": {},
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [
        (
            "ORD_ADYEN_CANCEL_OR_REFUND_CANCEL",
            "cancelled",
            "adyen",
            "PSP_ADYEN_CANCEL_OR_REFUND_CANCEL",
        )
    ]
    assert len(status_updates) == 1
    assert status_updates[0]["status"] == "cancelled"
    assert status_updates[0]["payment_status"] == "cancelled"
    assert status_updates[0]["cancelled_at"] is not None
    assert status_updates[0]["metadata"]["adyen_last_cancel_or_refund"]["outcome"] == "cancelled"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "cancel_or_refund_webhook"
    assert order_events[0]["metadata"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_adyen_webhook_cancel_or_refund_rejected_logs_without_mutating_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_CANCEL_OR_REFUND_REJECTED",
        psp_reference="PSP_ADYEN_CANCEL_OR_REFUND_REJECTED",
        success="false",
        event_code="CANCEL_OR_REFUND",
        amount_value=2000,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["reason"] = "Authorisation for refund failed"
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "paid",
            "payment_status": "paid",
            "metadata": {},
        }

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("rejected cancel_or_refund should not mutate order state")

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [
        (
            "ORD_ADYEN_CANCEL_OR_REFUND_REJECTED",
            "failed",
            "adyen",
            "PSP_ADYEN_CANCEL_OR_REFUND_REJECTED",
        )
    ]
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "cancel_or_refund_rejected_webhook"
    assert order_events[0]["metadata"]["reason"] == "Authorisation for refund failed"


@pytest.mark.asyncio
async def test_adyen_webhook_capture_failed_marks_order_payment_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_CAPTURE_FAILED",
        psp_reference="PSP_ADYEN_CAPTURE_FAILED",
        original_reference="PSP_ADYEN_AUTH_ORIG",
        success="true",
        event_code="CAPTURE_FAILED",
        amount_value=2000,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["reason"] = "Capture failed after authorisation"
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "paid",
            "payment_status": "paid",
            "metadata": {},
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [("ORD_ADYEN_CAPTURE_FAILED", "failed", "adyen", "PSP_ADYEN_CAPTURE_FAILED")]
    assert len(status_updates) == 1
    assert status_updates[0]["status"] == "payment_failed"
    assert status_updates[0]["payment_status"] == "payment_failed"
    assert status_updates[0]["metadata"]["adyen_last_capture_failed"]["original_reference"] == "PSP_ADYEN_AUTH_ORIG"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "capture_failed_webhook"
    assert order_events[0]["metadata"]["reason"] == "Capture failed after authorisation"


@pytest.mark.asyncio
async def test_adyen_webhook_capture_failed_already_failed_order_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    webhook_calls: list[tuple[str, str, str, str]] = []
    order_events: list[Dict[str, Any]] = []

    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    payload = _adyen_payload(
        "ORD_ADYEN_CAPTURE_FAILED_IDEMPOTENT",
        psp_reference="PSP_ADYEN_CAPTURE_FAILED_IDEMPOTENT",
        original_reference="PSP_ADYEN_AUTH_ORIG_2",
        success="true",
        event_code="CAPTURE_FAILED",
        amount_value=2000,
        amount_currency="USD",
    )
    notification = payload["notificationItems"][0]["NotificationRequestItem"]
    notification["reason"] = "Capture failed after retry"
    notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_adyen",
            "status": "payment_failed",
            "payment_status": "payment_failed",
            "metadata": {},
        }

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("already failed capture_failed webhook should not mutate order state again")

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/psp/webhook/adyen",
            json=payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"notificationResponse": "[accepted]"}
    assert webhook_calls == [
        (
            "ORD_ADYEN_CAPTURE_FAILED_IDEMPOTENT",
            "failed",
            "adyen",
            "PSP_ADYEN_CAPTURE_FAILED_IDEMPOTENT",
        )
    ]
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "capture_failed_webhook"
    assert order_events[0]["metadata"]["original_reference"] == "PSP_ADYEN_AUTH_ORIG_2"

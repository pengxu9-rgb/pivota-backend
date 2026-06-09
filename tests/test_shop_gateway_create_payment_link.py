"""Contract test for the shop-gateway create_payment_link operation.

Regression guard for the 2026-06-09 gap: the safety-kernel hosted-checkout executor calls
`upstream('create_payment_link', {...})` -> backend POST /agent/shop/v1/invoke with
operation 'create_payment_link'. Before this fix the gateway had no handler and returned
HTTP 400 "Unsupported operation", which the kernel mapped to MERCHANT_UNAVAILABLE. This test
pins the handler: it must proxy to /agent/v2/payments/checkout-sessions (which mints a HOSTED
Stripe Checkout page, NO charge) and pass the order/email/address/return_url + user_ref->buyer_ref.
"""
from __future__ import annotations

import asyncio

import routes.agent_shop_gateway as gw


def _run(coro):
    return asyncio.run(coro)


def test_create_payment_link_payload_parses_flat_kernel_shape() -> None:
    # The executor sends a FLAT payload (no nested wrapper) — it must parse, and unknown
    # optionals default to None.
    p = gw.CreatePaymentLinkPayload(
        **{
            "order_id": "ORD_1",
            "customer_email": "guest@example.com",
            "shipping_address": {"country": "US", "city": "SF"},
            "return_url": "https://shop.example/return",
            "user_ref": "user_42",
        }
    )
    assert p.order_id == "ORD_1"
    assert p.customer_email == "guest@example.com"
    assert p.shipping_address == {"country": "US", "city": "SF"}
    assert p.return_url == "https://shop.example/return"
    assert p.user_ref == "user_42"

    minimal = gw.CreatePaymentLinkPayload(**{"order_id": "ORD_2"})
    assert minimal.order_id == "ORD_2"
    assert minimal.customer_email is None and minimal.shipping_address is None


def test_create_payment_link_proxies_to_hosted_checkout(monkeypatch) -> None:
    captured = {}

    async def fake_proxy(method, path, body, *, checkout_token=None):
        captured.update(method=method, path=path, body=body, checkout_token=checkout_token)
        # Mirror the real /agent/v2/payments/checkout-sessions response shape.
        return {
            "status": "success",
            "checkout_session": {
                "checkout_session_id": "cs_test_1",
                "order_id": body["order_id"],
                "state": "created",
                "hosted_url": "https://checkout.stripe.com/c/pay/cs_test_1",
                "provider": "pivota_hosted_checkout",
            },
        }

    monkeypatch.setattr(gw, "_proxy_agent_api", fake_proxy)

    payload = gw.CreatePaymentLinkPayload(
        **{
            "order_id": "ORD_1",
            "customer_email": "guest@example.com",
            "shipping_address": {"country": "US", "city": "SF"},
            "return_url": "https://shop.example/return",
            "user_ref": "user_42",
        }
    )
    out = _run(gw._handle_create_payment_link(payload, checkout_token="ctok_abc"))

    # Proxied to the hosted-checkout endpoint (NOT the charging /payments path).
    assert captured["method"] == "POST"
    assert captured["path"] == "/agent/v2/payments/checkout-sessions"
    assert captured["checkout_token"] == "ctok_abc"
    body = captured["body"]
    assert body["order_id"] == "ORD_1"
    assert body["customer_email"] == "guest@example.com"
    assert body["shipping_address"] == {"country": "US", "city": "SF"}
    assert body["return_url"] == "https://shop.example/return"
    assert body["buyer_ref"] == "user_42"  # user_ref is mapped to buyer_ref for the session

    # The kernel executor reads checkout_session.hosted_url verbatim → must be an https URL.
    assert out["checkout_session"]["hosted_url"].startswith("https://")


def test_create_payment_link_omits_absent_optionals(monkeypatch) -> None:
    captured = {}

    async def fake_proxy(method, path, body, *, checkout_token=None):
        captured["body"] = body
        return {"checkout_session": {"hosted_url": "https://checkout.example/x"}}

    monkeypatch.setattr(gw, "_proxy_agent_api", fake_proxy)

    payload = gw.CreatePaymentLinkPayload(**{"order_id": "ORD_3"})
    _run(gw._handle_create_payment_link(payload, checkout_token=None))

    # Only order_id is sent; no empty/None optionals leak into the upstream body.
    assert captured["body"] == {"order_id": "ORD_3"}

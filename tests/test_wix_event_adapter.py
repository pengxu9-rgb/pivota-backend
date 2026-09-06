"""Wix native telemetry: JWT verification, the mapper, and the receiver.

Fixtures use Wix's documented shapes only — the domain-event envelope
(`entityFqdn`/`slug`/`createdEvent.entity` | `updatedEvent.currentEntity` |
`actionEvent.body`), decimal-string `Price` objects, the `paymentStatus` and
`status` enums, and the double-JSON-encoded `data` claim the reference handler
parses twice. Nothing is invented; a field this bridge does not read is absent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient


STORE_ID = "store-wix"
MERCHANT_ID = "merchant-wix"
INSTANCE_ID = "d2b4e0a1-1f3c-4a55-9a2e-4c1b7a55e001"
SITE_ID = "3c76e2aa-1111-2222-3333-444455556666"
ORDER_ID = "a4738c5d-98d6-45e4-bc88-4e5940acacfd"
EVENT_ID = "89db87be-f1b9-40a4-9bd4-cc4ef0804824"


# ---- keys -------------------------------------------------------------------


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


WIX_PRIVATE_PEM, WIX_PUBLIC_PEM = _keypair()
OTHER_PRIVATE_PEM, OTHER_PUBLIC_PEM = _keypair()


# ---- documented payloads ----------------------------------------------------


def _order(**overrides):
    """The Order entity as the Order Created reference publishes it."""
    order = {
        "id": ORDER_ID,
        "number": 10133,
        "createdDate": "2023-12-05T10:48:58.241Z",
        "updatedDate": "2023-12-05T11:02:11.900Z",
        "status": "APPROVED",
        "paymentStatus": "NOT_PAID",
        "fulfillmentStatus": "NOT_FULFILLED",
        "priceSummary": {
            "subtotal": {"amount": "36.00", "formattedAmount": "$36.00"},
            "shipping": {"amount": "5.0", "formattedAmount": "$5.00"},
            "tax": {"amount": "1.56", "formattedAmount": "$1.56"},
            "discount": {"amount": "2", "formattedAmount": "$2.00"},
            "total": {"amount": "40.56", "formattedAmount": "$40.56"},
        },
        "currency": "USD",
        "buyerInfo": {
            "contactId": "f61f30cd-7474-47b7-95a2-339c0fcacbd3",
            "memberId": "f61f30cd-7474-47b7-95a2-339c0fcacbd3",
            "email": "janedoe@gmail.com",
        },
        "checkoutId": "6f1204d5-3923-4709-869e-51680a1b5530",
        "channelInfo": {"type": "WEB"},
        "archived": False,
        "taxIncludedInPrices": False,
        "buyerLanguage": "en",
        "weightUnit": "LB",
    }
    order.update(overrides)
    return order


def _inner_order_event(slug="created", order=None, event_id=EVENT_ID):
    """The domain event, with the envelope key each slug actually uses."""
    order = _order() if order is None else order
    inner = {
        "id": event_id,
        "entityFqdn": "wix.ecom.v1.order",
        "slug": slug,
        "entityId": ORDER_ID,
        "eventTime": "2023-12-05T10:48:58.278491Z",
        "triggeredByAnonymizeRequest": False,
    }
    if slug == "created":
        inner["createdEvent"] = {"entity": order}
    elif slug == "updated":
        inner["updatedEvent"] = {"currentEntity": order}
    elif slug == "payment_status_updated":
        inner["actionEvent"] = {
            "body": {"order": order, "previousPaymentStatus": "NOT_PAID"}
        }
    elif slug == "canceled":
        inner["actionEvent"] = {
            "body": {"order": order, "restockAllItems": False, "sendOrderCanceledEmail": False}
        }
    else:
        inner["actionEvent"] = {"body": {"order": order}}
    return inner


def _refund(refund_id, amount, created, status="SUCCEEDED"):
    """A Refund as Order Transactions documents it: no top-level amount."""
    return {
        "id": refund_id,
        "transactions": [
            {
                "paymentId": "pay-1",
                "amount": {"amount": amount, "formattedAmount": f"${amount}"},
                "refundStatus": status,
            }
        ],
        "details": {"items": [], "shippingIncluded": False},
        "createdDate": created,
        "summary": {
            "requestedRefund": {"amount": amount},
            "refunded": {"amount": amount if status == "SUCCEEDED" else "0"},
            "pending": False,
        },
    }


def _inner_transactions_event(slug="refund_completed", refunds=None, payments=None, completed=None):
    refunds = [] if refunds is None else refunds
    body = {
        "orderTransactions": {
            "orderId": ORDER_ID,
            "payments": [] if payments is None else payments,
            "refunds": refunds,
        }
    }
    if slug == "refund_completed":
        body["orderId"] = ORDER_ID
        body["refund"] = completed if completed is not None else (refunds[-1] if refunds else {})
        body["sideEffects"] = {"sendOrderRefundedEmail": False}
    else:
        body["paymentIds"] = []
        body["refundIds"] = [r["id"] for r in refunds]
    return {
        "id": "03756f6a-62c6-4f85-8356-b171adb7e4f3",
        "entityFqdn": "wix.ecom.v1.order_transactions",
        "slug": slug,
        "entityId": ORDER_ID,
        "eventTime": "2022-12-21T13:37:43.471496Z",
        "actionEvent": {"body": body},
        "triggeredByAnonymizeRequest": False,
    }


def _claim(inner, event_type="wix.ecom.v1.order_created", instance_id=INSTANCE_ID):
    """The outer `data` claim: `data` and `identity` are JSON STRINGS."""
    return {
        "eventType": event_type,
        "instanceId": instance_id,
        "data": json.dumps(inner),
        "identity": json.dumps(
            {"identityType": "ANONYMOUS_VISITOR", "anonymousVisitorId": "vis-1"}
        ),
    }


def _token(claim, *, private_pem=WIX_PRIVATE_PEM, algorithm="RS256", exp=None, **registered):
    """The delivery: a JWT whose `data` claim is the JSON-encoded event.

    `registered` carries any RFC 7519 claim (`aud`, `nbf`, `iat`, `iss`, ...).
    The Wix docs never show the registered claims, so this receiver must not
    turn one it did not expect into a refusal.
    """
    payload = {"data": json.dumps(claim)}
    if exp is not None:
        payload["exp"] = int(exp.timestamp())
    payload.update(registered)
    return jwt.encode(payload, private_pem, algorithm=algorithm)


# ---- JWT verification -------------------------------------------------------


def test_a_valid_rs256_token_returns_the_decoded_data_claim():
    from services.wix_webhook_auth import verify_wix_webhook_jwt

    token = _token(_claim(_inner_order_event()))
    event = verify_wix_webhook_jwt(token.encode(), public_key_pem=WIX_PUBLIC_PEM)

    assert event["eventType"] == "wix.ecom.v1.order_created"
    assert event["instanceId"] == INSTANCE_ID
    # Still a STRING: the inner parse belongs to the mapper.
    assert isinstance(event["data"], str)
    assert json.loads(event["data"])["slug"] == "created"


def test_a_token_signed_by_another_key_is_refused():
    from services.wix_webhook_auth import (
        WixWebhookVerificationError,
        verify_wix_webhook_jwt,
    )

    token = _token(_claim(_inner_order_event()), private_pem=OTHER_PRIVATE_PEM)
    with pytest.raises(WixWebhookVerificationError):
        verify_wix_webhook_jwt(token.encode(), public_key_pem=WIX_PUBLIC_PEM)


def test_an_expired_token_is_refused():
    from services.wix_webhook_auth import (
        WixWebhookVerificationError,
        verify_wix_webhook_jwt,
    )

    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    token = _token(_claim(_inner_order_event()), exp=now - timedelta(minutes=5))
    with pytest.raises(WixWebhookVerificationError):
        verify_wix_webhook_jwt(token.encode(), public_key_pem=WIX_PUBLIC_PEM, now=now)


def test_an_unexpired_token_passes_the_same_clock():
    from services.wix_webhook_auth import verify_wix_webhook_jwt

    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    token = _token(_claim(_inner_order_event()), exp=now + timedelta(minutes=5))
    event = verify_wix_webhook_jwt(token.encode(), public_key_pem=WIX_PUBLIC_PEM, now=now)
    assert event["instanceId"] == INSTANCE_ID


def test_a_token_carrying_the_registered_claims_wix_may_send_is_accepted():
    """PyJWT 2.12 turns `verify_aud`/`verify_nbf`/`verify_iat`/`verify_iss` ON.

    `verify_aud` is the live hazard: with no `audience=` argument, ANY token
    that carries `aud` raises `InvalidAudienceError` — so if Wix stamps the
    app id as the audience, every delivery 401s forever. The Wix docs never
    show the registered claims, so none of them may become a refusal.
    """
    from services.wix_webhook_auth import verify_wix_webhook_jwt

    now = datetime.now(timezone.utc)
    token = _token(
        _claim(_inner_order_event()),
        aud="wix-app-id-9f3c",
        iss="wix.com",
        nbf=int((now - timedelta(minutes=5)).timestamp()),
        iat=int((now - timedelta(minutes=5)).timestamp()),
        sub="instance-subject",
        jti="delivery-1",
    )

    event = verify_wix_webhook_jwt(token.encode(), public_key_pem=WIX_PUBLIC_PEM)
    assert event["instanceId"] == INSTANCE_ID


def test_a_clock_skewed_iat_or_nbf_does_not_refuse_a_legitimate_delivery():
    """`iat` is a timestamp, not a validity window, and PyJWT applies NO leeway.

    A Wix signer a few seconds ahead of our clock is not a forgery, and there
    is nothing to retry into: a 401 is final for that delivery.
    """
    from services.wix_webhook_auth import verify_wix_webhook_jwt

    ahead = int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp())

    for claim in ("iat", "nbf"):
        token = _token(_claim(_inner_order_event()), **{claim: ahead})
        event = verify_wix_webhook_jwt(token.encode(), public_key_pem=WIX_PUBLIC_PEM)
        assert event["instanceId"] == INSTANCE_ID, claim


def test_the_signature_and_expiry_refusals_survive_the_relaxed_claim_options():
    """Relaxing the registered claims must not relax anything that matters."""
    from services.wix_webhook_auth import (
        WixWebhookVerificationError,
        verify_wix_webhook_jwt,
    )

    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    # ...a wrong signature, even with a perfectly good `aud`.
    with pytest.raises(WixWebhookVerificationError):
        verify_wix_webhook_jwt(
            _token(
                _claim(_inner_order_event()),
                private_pem=OTHER_PRIVATE_PEM,
                aud="wix-app-id-9f3c",
            ).encode(),
            public_key_pem=WIX_PUBLIC_PEM,
        )
    # ...an expired token, checked by _reject_expired against OUR clock.
    with pytest.raises(WixWebhookVerificationError):
        verify_wix_webhook_jwt(
            _token(
                _claim(_inner_order_event()),
                exp=now - timedelta(minutes=5),
                aud="wix-app-id-9f3c",
            ).encode(),
            public_key_pem=WIX_PUBLIC_PEM,
            now=now,
        )
    # ...and `alg: none`.
    unsigned = jwt.encode(
        {"data": json.dumps(_claim(_inner_order_event())), "aud": "wix-app-id-9f3c"},
        key="",
        algorithm="none",
    )
    with pytest.raises(WixWebhookVerificationError):
        verify_wix_webhook_jwt(unsigned.encode(), public_key_pem=WIX_PUBLIC_PEM)


def test_a_broken_public_key_is_a_configuration_error_not_a_rejection():
    """OUR key being unparseable is a 503, never a 401 blaming the delivery.

    PyJWT parses the key inside `jwt.decode`, so `InvalidKeyError` arrives
    from the same call as `InvalidSignatureError`; a blanket `except` there
    answered a perfectly good delivery with 401 and Wix dropped the event
    after ~48h of retries.
    """
    from services.wix_webhook_auth import (
        WixWebhookKeyNotConfigured,
        verify_wix_webhook_jwt,
    )

    truncated = "\n".join(WIX_PUBLIC_PEM.strip().splitlines()[:2] + ["-----END PUBLIC KEY-----"])
    token = _token(_claim(_inner_order_event())).encode()

    with pytest.raises(WixWebhookKeyNotConfigured):
        verify_wix_webhook_jwt(token, public_key_pem=truncated)
    # A PRIVATE key pasted into the public-key env var is the same class of
    # operator error, and must not read as a bad delivery either.
    with pytest.raises(WixWebhookKeyNotConfigured):
        verify_wix_webhook_jwt(token, public_key_pem=WIX_PRIVATE_PEM)


def test_alg_none_is_refused():
    from services.wix_webhook_auth import (
        WixWebhookVerificationError,
        verify_wix_webhook_jwt,
    )

    unsigned = jwt.encode(
        {"data": json.dumps(_claim(_inner_order_event()))}, key="", algorithm="none"
    )
    with pytest.raises(WixWebhookVerificationError):
        verify_wix_webhook_jwt(unsigned.encode(), public_key_pem=WIX_PUBLIC_PEM)


def test_an_hs256_token_signed_with_the_public_key_is_refused():
    """The classic confusion attack: the public key handed back as an HMAC key."""
    from services.wix_webhook_auth import (
        WixWebhookVerificationError,
        verify_wix_webhook_jwt,
    )

    # PyJWT refuses to ENCODE this, so the attacker's token is forged by hand
    # exactly as an attacker would have to.
    import base64
    import hashlib
    import hmac as hmac_module

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"data": json.dumps(_claim(_inner_order_event()))}).encode())
    signing_input = header + b"." + body
    signature = b64(
        hmac_module.new(
            WIX_PUBLIC_PEM.encode(), signing_input, hashlib.sha256
        ).digest()
    )
    forged = signing_input + b"." + signature

    with pytest.raises(WixWebhookVerificationError):
        verify_wix_webhook_jwt(forged, public_key_pem=WIX_PUBLIC_PEM)


def test_a_ps256_token_signed_by_the_REAL_wix_key_is_still_refused():
    """The algorithm allow-list has to do work PyJWT's key guard cannot.

    `alg: none` and the HS256 confusion attack are ALSO refused by PyJWT
    itself, which will not use an asymmetric PEM as an HMAC secret — so those
    two tests pass even with the allow-list widened, and neither of them proves
    the pinning does anything. PS256 is the case that isolates it: RSA-PSS
    verifies against the very same RSA public key, so PyJWT is perfectly happy
    to check it and ONLY `algorithms=["RS256"]` turns it away.
    """
    from services.wix_webhook_auth import (
        WixWebhookVerificationError,
        verify_wix_webhook_jwt,
    )

    substituted = _token(_claim(_inner_order_event()), algorithm="PS256")
    with pytest.raises(WixWebhookVerificationError):
        verify_wix_webhook_jwt(substituted.encode(), public_key_pem=WIX_PUBLIC_PEM)


def test_a_missing_public_key_is_a_configuration_error_not_a_rejection():
    from services.wix_webhook_auth import (
        WixWebhookKeyNotConfigured,
        verify_wix_webhook_jwt,
    )

    token = _token(_claim(_inner_order_event()))
    with pytest.raises(WixWebhookKeyNotConfigured):
        verify_wix_webhook_jwt(token.encode(), public_key_pem="")


def test_an_escaped_newline_pem_is_accepted():
    from services.wix_webhook_auth import verify_wix_webhook_jwt

    escaped = WIX_PUBLIC_PEM.replace("\n", "\\n")
    token = _token(_claim(_inner_order_event()))
    assert verify_wix_webhook_jwt(token.encode(), public_key_pem=escaped)["instanceId"] == (
        INSTANCE_ID
    )


def test_a_token_without_a_data_claim_is_refused():
    from services.wix_webhook_auth import (
        WixWebhookVerificationError,
        verify_wix_webhook_jwt,
    )

    token = jwt.encode({"iss": "wix.com"}, WIX_PRIVATE_PEM, algorithm="RS256")
    with pytest.raises(WixWebhookVerificationError):
        verify_wix_webhook_jwt(token.encode(), public_key_pem=WIX_PUBLIC_PEM)


# ---- the mapper -------------------------------------------------------------


def _map(inner, *, event_type=None, order=None):
    from services.wix_event_adapter import map_wix_event

    slug = inner["slug"]
    entity = inner["entityFqdn"].rsplit(".", 1)[-1]
    return map_wix_event(
        _claim(inner, event_type=event_type or f"wix.ecom.v1.{entity}_{slug}"),
        store_id=STORE_ID,
        order=order,
    )


def test_order_created_maps_to_order_created_at_the_native_created_date():
    batch = _map(_inner_order_event("created"))

    assert [event.event_type for event in batch.events] == ["order.created"]
    event = batch.events[0]
    assert event.order_id == ORDER_ID
    assert event.order_ref == f"wix:{ORDER_ID}"
    assert event.amount_cents == 4056
    assert event.currency == "USD"
    assert event.buyer_id == "f61f30cd-7474-47b7-95a2-339c0fcacbd3"
    assert event.occurred_at == datetime(2023, 12, 5, 10, 48, 58, 241000, tzinfo=timezone.utc)
    assert event.metadata["native_topic"] == "wix.ecom.v1.order_created"
    # No email, no name, no address ever reaches metadata.
    assert "janedoe@gmail.com" not in json.dumps(event.metadata)


def test_a_paid_order_also_emits_order_paid_at_the_updated_date():
    batch = _map(
        _inner_order_event("payment_status_updated", order=_order(paymentStatus="PAID"))
    )

    assert [event.event_type for event in batch.events] == ["order.created", "order.paid"]
    paid = batch.events[1]
    assert paid.amount_cents == 4056
    assert paid.occurred_at == datetime(2023, 12, 5, 11, 2, 11, 900000, tzinfo=timezone.utc)


@pytest.mark.parametrize("payment_status", ["PARTIALLY_REFUNDED", "FULLY_REFUNDED"])
def test_a_refunded_order_is_still_paid_because_a_refund_presupposes_a_capture(payment_status):
    batch = _map(_inner_order_event("updated", order=_order(paymentStatus=payment_status)))
    assert "order.paid" in [event.event_type for event in batch.events]


@pytest.mark.parametrize(
    "payment_status",
    ["NOT_PAID", "PARTIALLY_PAID", "PENDING", "PENDING_MERCHANT", "CANCELED", "UNSPECIFIED"],
)
def test_an_uncaptured_payment_status_never_emits_order_paid(payment_status):
    batch = _map(_inner_order_event("updated", order=_order(paymentStatus=payment_status)))
    assert [event.event_type for event in batch.events] == ["order.created"]


def test_a_canceled_order_emits_order_cancelled():
    batch = _map(
        _inner_order_event(
            "canceled", order=_order(status="CANCELED", paymentStatus="PAID")
        )
    )
    assert [event.event_type for event in batch.events] == [
        "order.created",
        "order.paid",
        "order.cancelled",
    ]


def test_a_declined_order_emits_payment_failed():
    batch = _map(
        _inner_order_event("payment_status_updated", order=_order(paymentStatus="DECLINED"))
    )
    assert [event.event_type for event in batch.events] == ["order.created", "payment.failed"]


def test_a_repeated_delivery_produces_identical_event_ids():
    """The identity must survive a CHANGED order, not just a re-sent one.

    Re-mapping the same object proves nothing: `updatedDate` moves on every
    Wix order update and `order.paid` is stamped AT `updatedDate`, so folding
    it into the event id would make every later delivery of an order we have
    already recorded a second `order.paid`. The second delivery here is the
    real one — a later `updatedDate`, a moved `paymentStatus`, a new envelope
    event id — and must still be the same two ledger facts.
    """
    first = _map(_inner_order_event("updated", order=_order(paymentStatus="PAID")))
    second = _map(
        _inner_order_event(
            "updated",
            order=_order(
                paymentStatus="FULLY_REFUNDED",
                updatedDate="2023-12-09T08:15:42.000Z",
            ),
            event_id="a-different-delivery",
        )
    )

    assert [event.event_type for event in first.events] == ["order.created", "order.paid"]
    assert [event.event_type for event in second.events] == [
        event.event_type for event in first.events
    ]
    assert [event.event_id for event in first.events] == [
        event.event_id for event in second.events
    ]
    # The fixture really did move the field the mutant would read.
    assert first.events[1].occurred_at != second.events[1].occurred_at


def test_the_same_order_reported_by_two_different_events_shares_one_identity():
    created = _map(_inner_order_event("created"))
    approved = _map(_inner_order_event("approved"))
    assert created.events[0].event_id == approved.events[0].event_id


def test_a_zero_decimal_currency_keeps_whole_units():
    order = _order(
        currency="JPY",
        paymentStatus="PAID",
        priceSummary={"total": {"amount": "4056", "formattedAmount": "¥4,056"}},
    )
    batch = _map(_inner_order_event("updated", order=order))
    assert [event.amount_cents for event in batch.events] == [4056, 4056]


def test_a_pivota_written_back_order_is_recovered_under_the_pivota_namespace():
    """`channelInfo.externalOrderId` is what our writeback stamps structurally."""
    order = _order(
        channelInfo={
            "type": "OTHER_PLATFORM",
            "channelName": "Pivota",
            "externalOrderId": "ord_123",
        }
    )
    batch = _map(_inner_order_event("created", order=order))
    assert batch.events[0].order_ref == "pivota:ord_123"


def test_a_buyer_note_claiming_a_pivota_order_is_never_believed():
    """`buyerNote` is buyer free text; reading it would let a shopper forge a merge."""
    order = _order(buyerNote="Pivota Order ID: ord_999")
    batch = _map(_inner_order_event("created", order=order))
    assert batch.events[0].order_ref == f"wix:{ORDER_ID}"


def test_an_external_order_id_without_the_external_channel_type_is_not_trusted():
    order = _order(channelInfo={"type": "WEB", "externalOrderId": "ord_999"})
    batch = _map(_inner_order_event("created", order=order))
    assert batch.events[0].order_ref == f"wix:{ORDER_ID}"


def test_two_partial_refunds_map_to_two_distinct_refund_events():
    refunds = [
        _refund("refund-901", "10.50", "2022-12-21T14:00:00.000Z"),
        _refund("refund-902", "5.00", "2022-12-21T15:00:00.000Z"),
    ]
    batch = _map(
        _inner_transactions_event(refunds=refunds),
        order=_order(paymentStatus="PARTIALLY_REFUNDED"),
    )

    assert [event.event_type for event in batch.events] == [
        "refund.succeeded",
        "refund.succeeded",
    ]
    assert sorted(event.refund_id for event in batch.events) == ["refund-901", "refund-902"]
    assert sorted(event.amount_cents for event in batch.events) == [500, 1050]
    assert len({event.event_id for event in batch.events}) == 2
    assert all(event.order_id == ORDER_ID for event in batch.events)
    assert all(event.currency == "USD" for event in batch.events)
    # Each refund carries its OWN date, not the delivery's or its sibling's.
    by_id = {event.refund_id: event for event in batch.events}
    assert by_id["refund-901"].occurred_at == datetime(
        2022, 12, 21, 14, 0, tzinfo=timezone.utc
    )
    assert by_id["refund-902"].occurred_at == datetime(
        2022, 12, 21, 15, 0, tzinfo=timezone.utc
    )
    assert by_id["refund-901"].amount_cents == 1050
    assert by_id["refund-902"].amount_cents == 500


def test_a_refund_event_id_is_keyed_on_the_refund_not_the_order():
    one = _map(
        _inner_transactions_event(
            refunds=[_refund("refund-901", "10.50", "2022-12-21T14:00:00.000Z")]
        ),
        order=_order(),
    )
    two = _map(
        _inner_transactions_event(
            refunds=[_refund("refund-902", "10.50", "2022-12-21T14:00:00.000Z")]
        ),
        order=_order(),
    )
    assert one.events[0].event_id != two.events[0].event_id


def test_a_refund_with_nothing_settled_is_not_reported_as_succeeded():
    from services.wix_event_adapter import NoWixCanonicalEvents

    pending = {
        "id": "refund-903",
        "transactions": [
            {
                "paymentId": "pay-1",
                "amount": {"amount": "10.00"},
                "refundStatus": "PENDING",
            }
        ],
        "createdDate": "2022-12-21T14:00:00.000Z",
    }
    with pytest.raises(NoWixCanonicalEvents):
        _map(
            _inner_transactions_event(slug="details_updated", refunds=[pending]),
            order=_order(),
        )


def test_a_partly_settled_refund_reports_what_settled_not_what_was_requested():
    """`requestedRefund` is a REQUEST; `summary.refunded` is the money moved."""
    partial = {
        "id": "refund-904",
        "transactions": [
            {
                "paymentId": "pay-1",
                "amount": {"amount": "4.00", "formattedAmount": "$4.00"},
                "refundStatus": "SUCCEEDED",
            }
        ],
        "createdDate": "2022-12-21T14:00:00.000Z",
        "summary": {
            "requestedRefund": {"amount": "10.50", "formattedAmount": "$10.50"},
            "refunded": {"amount": "4.00", "formattedAmount": "$4.00"},
            "pending": False,
        },
    }
    batch = _map(
        _inner_transactions_event(refunds=[partial]),
        order=_order(paymentStatus="PARTIALLY_REFUNDED"),
    )
    assert [event.event_type for event in batch.events] == ["refund.succeeded"]
    assert batch.events[0].amount_cents == 400


def test_a_zero_refunded_summary_falls_through_to_the_succeeded_transactions():
    """Zero settled is the ABSENCE of an answer, not an answer of nothing.

    Settling for it emitted `refund.succeeded` at amount 0 under the refund's
    own id — and since the event id is keyed on the refund id alone, the real
    `refund_completed` delivery then deduped against that shadow and the
    refund was lost. Zero must fall through.
    """
    lagging_summary = {
        "id": "refund-905",
        "transactions": [
            {
                "paymentId": "pay-1",
                "amount": {"amount": "10.50", "formattedAmount": "$10.50"},
                "refundStatus": "SUCCEEDED",
            }
        ],
        "createdDate": "2022-12-21T14:00:00.000Z",
        "summary": {"requestedRefund": {"amount": "10.50"}, "refunded": {"amount": "0"}},
    }
    batch = _map(
        _inner_transactions_event(refunds=[lagging_summary]),
        order=_order(paymentStatus="PARTIALLY_REFUNDED"),
    )
    assert [event.event_type for event in batch.events] == ["refund.succeeded"]
    assert batch.events[0].amount_cents == 1050


def test_a_zero_refunded_summary_with_nothing_succeeded_emits_no_refund():
    from services.wix_event_adapter import NoWixCanonicalEvents

    unsettled = {
        "id": "refund-903",
        "transactions": [
            {
                "paymentId": "pay-1",
                "amount": {"amount": "10.50", "formattedAmount": "$10.50"},
                "refundStatus": "PENDING",
            }
        ],
        "createdDate": "2022-12-21T14:00:00.000Z",
        "summary": {"requestedRefund": {"amount": "10.50"}, "refunded": {"amount": "0"}},
    }
    with pytest.raises(NoWixCanonicalEvents):
        _map(_inner_transactions_event(refunds=[unsettled]), order=_order())


def test_a_succeeded_transaction_of_zero_is_also_nothing_settled():
    from services.wix_event_adapter import NoWixCanonicalEvents

    zero = {
        "id": "refund-906",
        "transactions": [
            {
                "paymentId": "pay-1",
                "amount": {"amount": "0", "formattedAmount": "$0.00"},
                "refundStatus": "SUCCEEDED",
            }
        ],
        "createdDate": "2022-12-21T14:00:00.000Z",
    }
    with pytest.raises(NoWixCanonicalEvents):
        _map(_inner_transactions_event(refunds=[zero]), order=_order())


def test_a_declined_payment_in_the_transactions_payload_is_a_payment_failure():
    payments = [
        {
            "id": "pay-declined",
            "amount": {"amount": "40.56"},
            "status": "DECLINED",
            "createdDate": "2022-12-21T13:37:00.000Z",
            "updatedDate": "2022-12-21T13:38:00.000Z",
        }
    ]
    batch = _map(
        _inner_transactions_event(slug="details_updated", payments=payments),
        order=_order(),
    )
    assert [event.event_type for event in batch.events] == ["payment.failed"]
    assert batch.events[0].payment_id == "pay-declined"
    assert batch.events[0].amount_cents == 4056


def test_a_transactions_event_needs_the_order_but_an_order_event_does_not():
    from services.wix_event_adapter import needs_wix_order_fetch

    assert needs_wix_order_fetch(_claim(_inner_transactions_event())) is True
    assert needs_wix_order_fetch(_claim(_inner_order_event("created"))) is False


def test_an_unsupported_event_is_refused():
    from services.wix_event_adapter import UnsupportedWixEvent, map_wix_event

    inner = _inner_order_event("created")
    inner["entityFqdn"] = "wix.stores.v1.product"
    inner["slug"] = "changed"
    with pytest.raises(UnsupportedWixEvent):
        map_wix_event(
            _claim(inner, event_type="wix.stores.v1.product_changed"), store_id=STORE_ID
        )


def test_an_order_event_without_an_order_entity_is_a_value_error():
    from services.wix_event_adapter import map_wix_event

    inner = _inner_order_event("created")
    inner["createdEvent"] = {}
    with pytest.raises(ValueError):
        map_wix_event(_claim(inner), store_id=STORE_ID)


# ---- the receiver -----------------------------------------------------------


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def fetch_all(self, query, values=None):
        self.queries.append((query, values))
        return list(self.rows)


def _store_row(**overrides):
    credentials = {
        "api_key": "wix-api-key",
        "site_id": SITE_ID,
        "instance_id": INSTANCE_ID,
    }
    credentials.update(overrides.pop("credentials", {}))
    row = {
        "store_id": STORE_ID,
        "merchant_id": MERCHANT_ID,
        "domain": SITE_ID,
        "api_key": json.dumps(credentials),
    }
    row.update(overrides)
    return row


def _client(monkeypatch, *, rows, fetch=None, fetch_error=None, public_key=WIX_PUBLIC_PEM):
    """A one-route app around the real receiver, with DB and fetch stubbed."""
    from routes import wix_webhooks as route

    calls = {"fetch": [], "ingest": [], "db": []}
    db = _FakeDB(rows)
    monkeypatch.setattr(route, "database", db)
    calls["db"] = db.queries
    monkeypatch.setenv("WIX_APP_PUBLIC_KEY", public_key)

    async def _fetch(**kwargs):
        calls["fetch"].append(kwargs)
        if fetch_error is not None:
            raise fetch_error
        return fetch if fetch is not None else _order()

    async def _ingest(**kwargs):
        calls["ingest"].append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "fetch_wix_order", _fetch)
    monkeypatch.setattr(route, "ingest_merchant_event_batch", _ingest)

    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app), calls


def _post(client, token):
    return client.post(
        "/webhooks/wix",
        content=token,
        headers={"Content-Type": "application/jwt"},
    )


def test_route_accepts_a_valid_delivery_and_stamps_the_write_path(monkeypatch):
    client, calls = _client(monkeypatch, rows=[_store_row()])

    response = _post(
        client,
        _token(
            _claim(_inner_order_event("payment_status_updated", order=_order(paymentStatus="PAID")))
        ),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "recorded"
    assert response.json()["platform"] == "wix"

    assert len(calls["ingest"]) == 1
    ingested = calls["ingest"][0]
    assert ingested["write_path"] == "wix_webhook"
    assert ingested["agent_identity_confidence"] == "platform_asserted"
    assert ingested["merchant_id"] == MERCHANT_ID
    assert [event.event_type for event in ingested["batch"].events] == [
        "order.created",
        "order.paid",
    ]
    # An order-domain delivery is self-contained: no Wix API call.
    assert calls["fetch"] == []


def test_a_token_signed_by_another_key_is_401_and_never_looks_up_a_store(monkeypatch):
    client, calls = _client(monkeypatch, rows=[_store_row()])

    response = _post(
        client, _token(_claim(_inner_order_event()), private_pem=OTHER_PRIVATE_PEM)
    )

    assert response.status_code == 401
    assert calls["db"] == []
    assert calls["ingest"] == []


def test_an_unsigned_token_is_401_and_never_looks_up_a_store(monkeypatch):
    client, calls = _client(monkeypatch, rows=[_store_row()])

    unsigned = jwt.encode(
        {"data": json.dumps(_claim(_inner_order_event()))}, key="", algorithm="none"
    )
    response = _post(client, unsigned)

    assert response.status_code == 401
    assert calls["db"] == []


def test_an_expired_token_is_401(monkeypatch):
    client, calls = _client(monkeypatch, rows=[_store_row()])

    response = _post(
        client,
        _token(
            _claim(_inner_order_event()),
            exp=datetime.now(timezone.utc) - timedelta(hours=1),
        ),
    )

    assert response.status_code == 401
    assert calls["db"] == []


def test_an_unknown_instance_is_404_and_never_ingests(monkeypatch):
    client, calls = _client(monkeypatch, rows=[])

    response = _post(client, _token(_claim(_inner_order_event())))

    assert response.status_code == 404
    assert calls["ingest"] == []


def test_a_store_whose_stored_instance_only_contains_the_id_is_not_resolved(monkeypatch):
    """The LIKE narrows the scan; only an EXACT match may resolve a store."""
    row = _store_row(credentials={"instance_id": f"{INSTANCE_ID}-other"})
    client, calls = _client(monkeypatch, rows=[row])

    response = _post(client, _token(_claim(_inner_order_event())))

    assert response.status_code == 404
    assert calls["ingest"] == []


def test_two_stores_claiming_one_instance_id_are_refused_rather_than_guessed(
    monkeypatch, caplog
):
    """`merchant_stores` has NO uniqueness on the instance id.

    Answering with whichever row the database returned first is a
    cross-merchant leak: merchant B types merchant A's instance id and starts
    receiving A's signed order and refund events, silently and forever. An
    ambiguous instance is refused, so a hijack is a visible outage plus a
    logged warning instead.
    """
    import logging

    victim = _store_row()
    hijacker = _store_row(
        store_id="store-attacker",
        merchant_id="merchant-attacker",
        api_key=json.dumps(
            {"api_key": "attacker-key", "site_id": SITE_ID, "instance_id": INSTANCE_ID}
        ),
    )
    client, calls = _client(monkeypatch, rows=[victim, hijacker])

    with caplog.at_level(logging.WARNING, logger="routes.wix_webhooks"):
        response = _post(
            client,
            _token(
                _claim(
                    _inner_order_event(
                        "payment_status_updated", order=_order(paymentStatus="PAID")
                    )
                )
            ),
        )

    assert response.status_code == 404
    assert calls["ingest"] == [], "an ambiguous instance must not reach the ledger"
    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any(INSTANCE_ID in message for message in warnings), warnings
    # The warning names the instance, never a credential.
    assert not any("attacker-key" in message or "wix-api-key" in message for message in warnings)


def test_an_instance_id_carrying_a_like_wildcard_never_reaches_the_database(monkeypatch):
    """`_` is a single-character LIKE wildcard, so it must not survive the
    shape check either — a Wix instance id is a GUID and needs neither it nor
    `%`. Refused before the query, so no scan can be widened by a delivery."""
    client, calls = _client(monkeypatch, rows=[_store_row()])

    for forged in (INSTANCE_ID.replace("-", "_", 1), INSTANCE_ID[:8] + "%"):
        response = _post(
            client, _token(_claim(_inner_order_event(), instance_id=forged))
        )
        assert response.status_code == 404, forged
    assert calls["db"] == []
    assert calls["ingest"] == []


def test_an_inactive_store_is_404_because_the_lookup_filters_status(monkeypatch):
    client, calls = _client(monkeypatch, rows=[])

    response = _post(client, _token(_claim(_inner_order_event())))

    assert response.status_code == 404
    query, values = calls["db"][0]
    assert "lower(COALESCE(status, 'active')) IN ('active', 'connected')" in query
    assert values == {"needle": f"%{INSTANCE_ID}%"}


def test_an_unsupported_event_is_200_ignored_and_never_ingests(monkeypatch):
    client, calls = _client(monkeypatch, rows=[_store_row()])

    inner = _inner_order_event("created")
    inner["entityFqdn"] = "wix.stores.v1.product"
    inner["slug"] = "changed"
    response = _post(
        client, _token(_claim(inner, event_type="wix.stores.v1.product_changed"))
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert calls["ingest"] == []
    assert calls["fetch"] == []


def test_an_unconfigured_public_key_is_503(monkeypatch):
    client, calls = _client(monkeypatch, rows=[_store_row()], public_key="")

    response = _post(client, _token(_claim(_inner_order_event())))

    assert response.status_code == 503
    assert calls["db"] == []


def test_a_truncated_public_key_is_503_not_401(monkeypatch):
    """A broken key of OURS must not be reported as a bad delivery.

    401 is final for that event; 503 is what Wix retries, and it is the status
    that tells an operator to go and look at `WIX_APP_PUBLIC_KEY`.
    """
    truncated = "\n".join(WIX_PUBLIC_PEM.strip().splitlines()[:2] + ["-----END PUBLIC KEY-----"])
    client, calls = _client(monkeypatch, rows=[_store_row()], public_key=truncated)

    response = _post(client, _token(_claim(_inner_order_event())))

    assert response.status_code == 503, response.text
    assert calls["db"] == []
    assert calls["ingest"] == []


def test_an_oauth_written_blob_still_fetches_with_a_real_credential(monkeypatch):
    """The route reads the credential with `normalize_wix_api_key`, the same
    reader every other Wix caller uses. Re-deriving the precedence here missed
    `wix_access_token`, so an OAuth-written blob would have called Wix with an
    empty Authorization header and 401'd the read-back into a 503 loop."""
    row = _store_row(
        api_key=json.dumps(
            {
                "auth_mode": "oauth",
                "wix_access_token": "oauth-access-token",
                "site_id": SITE_ID,
                "instance_id": INSTANCE_ID,
            }
        )
    )
    client, calls = _client(
        monkeypatch, rows=[row], fetch=_order(paymentStatus="PARTIALLY_REFUNDED")
    )

    response = _post(
        client,
        _token(
            _claim(
                _inner_transactions_event(
                    refunds=[_refund("refund-901", "10.50", "2022-12-21T14:00:00.000Z")]
                ),
                event_type="wix.ecom.v1.order_transactions_refund_completed",
            )
        ),
    )

    assert response.status_code == 200, response.text
    assert calls["fetch"][0]["api_key"] == "oauth-access-token"


def test_a_transactions_delivery_reads_the_order_back_with_the_store_credential(monkeypatch):
    client, calls = _client(
        monkeypatch,
        rows=[_store_row()],
        fetch=_order(paymentStatus="PARTIALLY_REFUNDED"),
    )

    refunds = [_refund("refund-901", "10.50", "2022-12-21T14:00:00.000Z")]
    response = _post(
        client,
        _token(
            _claim(
                _inner_transactions_event(refunds=refunds),
                event_type="wix.ecom.v1.order_transactions_refund_completed",
            )
        ),
    )

    assert response.status_code == 200, response.text
    assert len(calls["fetch"]) == 1
    assert calls["fetch"][0] == {
        "api_key": "wix-api-key",
        "site_id": SITE_ID,
        "order_id": ORDER_ID,
    }
    ingested = calls["ingest"][0]["batch"].events
    assert [event.event_type for event in ingested] == ["refund.succeeded"]
    assert ingested[0].currency == "USD"


def test_a_failed_order_read_is_503_and_never_ingests(monkeypatch):
    from services.wix_order_fetch import WixOrderFetchError

    client, calls = _client(
        monkeypatch,
        rows=[_store_row()],
        fetch_error=WixOrderFetchError("Wix order fetch failed with HTTP 500"),
    )

    response = _post(
        client,
        _token(
            _claim(
                _inner_transactions_event(
                    refunds=[_refund("refund-901", "10.50", "2022-12-21T14:00:00.000Z")]
                ),
                event_type="wix.ecom.v1.order_transactions_refund_completed",
            )
        ),
    )

    assert response.status_code == 503
    assert calls["ingest"] == []


def test_a_body_over_one_megabyte_is_refused(monkeypatch):
    client, calls = _client(monkeypatch, rows=[_store_row()])

    response = _post(client, "x" * 1_100_000)

    assert response.status_code == 413
    assert calls["db"] == []


def test_the_route_is_wrapped_and_charges_the_platform_tier():
    """The ingress ratchet asserts this too; pinning it here keeps failures local."""
    import ast
    from pathlib import Path

    import routes.wix_webhooks as route

    tree = ast.parse(Path(route.__file__).read_text(encoding="utf-8"))
    decorators = [
        decorator
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and getattr(decorator.func, "id", None) == "telemetry_ingress_route"
    ]
    assert [decorator.args[0].value for decorator in decorators] == ["wix_webhook"]
    tiers = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "enforce_rate_limit"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert tiers == {"platform"}


def test_the_receiver_never_reads_an_instance_id_out_of_the_unverified_body():
    """Store resolution must come from the verified claim, and only from it."""
    import ast
    from pathlib import Path

    import routes.wix_webhooks as route

    source = Path(route.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "receive_wix_webhook"
    )
    # The only `instanceId` read in the handler is off the verified `event`.
    reads = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "instanceId"
    ]
    assert reads, "the handler must read instanceId"
    assert all(getattr(node.func.value, "id", None) == "event" for node in reads)
    # `raw` is the unparsed body: it may be measured and verified, never parsed.
    assert "json.loads(raw" not in source


# ---- the connect route: what actually gets persisted ------------------------
#
# `POST /integrations/wix/connect` is the ONLY writer of the `instance_id`
# the receiver above resolves a store by, and `merchant_stores` has no
# uniqueness on it. These tests pin the credential bytes it writes and the
# claim it refuses.


class _ConnectDB:
    """The Wix rows a connect call sees: the instance-id scan, then the
    per-merchant store lookup. Records every write."""

    def __init__(self, rows=(), existing=None):
        self.rows = list(rows)
        self.existing = existing
        self.writes = []
        self.queries = []

    async def fetch_all(self, query, values=None):
        self.queries.append((str(query), values))
        return list(self.rows)

    async def fetch_one(self, query, values=None):
        self.queries.append((str(query), values))
        return dict(self.existing) if self.existing else None

    async def execute(self, query, values=None):
        self.writes.append((str(query), dict(values or {})))
        return None


def _connect_client(monkeypatch, db, *, user=None):
    from routes import merchant_store_connections as route
    from utils.auth import get_current_user

    monkeypatch.setattr(route, "database", db)

    async def _validate(site_id, api_key):
        return {"site_id": SITE_ID, "api_key": str(api_key), "status_code": 200}

    async def _sync_status(merchant_id, reason=""):
        return None

    monkeypatch.setattr(route, "validate_wix_catalog_access", _validate)
    monkeypatch.setattr(route, "sync_catalog_merchant_status", _sync_status)

    app = FastAPI()
    app.include_router(route.router)

    async def fake_user():
        return user or {"role": "merchant", "merchant_id": MERCHANT_ID}

    app.dependency_overrides[get_current_user] = fake_user
    return TestClient(app)


def _connect(client, **body):
    payload = {"merchant_id": MERCHANT_ID, "site_id": SITE_ID, "api_key": "wix-api-key"}
    payload.update(body)
    return client.post("/integrations/wix/connect", json=payload)


def _written_credential(db):
    assert len(db.writes) == 1, db.writes
    return db.writes[0][1]["token"]


def test_connect_with_an_instance_id_persists_the_credential_blob(monkeypatch):
    db = _ConnectDB()
    response = _connect(_connect_client(monkeypatch, db), instance_id=INSTANCE_ID)

    assert response.status_code == 200, response.text
    stored = json.loads(_written_credential(db))
    assert stored == {
        "api_key": "wix-api-key",
        "site_id": SITE_ID,
        "instance_id": INSTANCE_ID,
    }
    # And the receiver's own reader finds it again.
    from services.wix_connection import normalize_wix_api_key, stored_wix_instance_id

    assert stored_wix_instance_id(_written_credential(db)) == INSTANCE_ID
    assert normalize_wix_api_key(_written_credential(db)) == "wix-api-key"


def test_connect_without_an_instance_id_writes_the_bare_key_exactly_as_before(monkeypatch):
    """A connect that does not opt into telemetry must be byte-identical."""
    db = _ConnectDB()
    response = _connect(_connect_client(monkeypatch, db))

    assert response.status_code == 200, response.text
    assert _written_credential(db) == "wix-api-key"


def test_a_reconnect_without_an_instance_id_keeps_the_stored_one(monkeypatch):
    """Rotating the API key must not silently switch telemetry off.

    The UPDATE branch wrote the bare key over the blob, so the next credential
    rotation erased the `instance_id` and every later delivery 404'd — with
    nothing anywhere saying why.
    """
    db = _ConnectDB(
        existing={
            "store_id": STORE_ID,
            "api_key": json.dumps(
                {"api_key": "old-key", "site_id": SITE_ID, "instance_id": INSTANCE_ID}
            ),
        }
    )
    response = _connect(_connect_client(monkeypatch, db), api_key="rotated-key")

    assert response.status_code == 200, response.text
    stored = json.loads(_written_credential(db))
    assert stored["instance_id"] == INSTANCE_ID
    assert stored["site_id"] == SITE_ID
    assert stored["api_key"] == "rotated-key"


def test_a_reconnect_to_a_store_that_never_had_a_blob_still_writes_the_bare_key(monkeypatch):
    db = _ConnectDB(existing={"store_id": STORE_ID, "api_key": "old-bare-key"})
    response = _connect(_connect_client(monkeypatch, db), api_key="rotated-key")

    assert response.status_code == 200, response.text
    assert _written_credential(db) == "rotated-key"


def test_connect_refuses_an_instance_id_another_merchant_already_claims(monkeypatch):
    """Otherwise merchant B connects with merchant A's instance id and starts
    receiving A's signed order and refund events."""
    victim = {
        "store_id": STORE_ID,
        "merchant_id": "merchant-victim",
        "domain": SITE_ID,
        "api_key": json.dumps(
            {"api_key": "victim-key", "site_id": SITE_ID, "instance_id": INSTANCE_ID}
        ),
    }
    db = _ConnectDB(rows=[victim])
    response = _connect(
        _connect_client(monkeypatch, db, user={"role": "merchant", "merchant_id": "merchant-attacker"}),
        merchant_id="merchant-attacker",
        instance_id=INSTANCE_ID,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "WIX_INSTANCE_ID_ALREADY_CLAIMED"
    assert db.writes == [], "the refused claim must persist nothing"


def test_a_merchant_may_reclaim_their_own_instance_id(monkeypatch):
    """The check is about ANOTHER merchant; re-connecting your own site is not
    a hijack, and refusing it would break every credential rotation."""
    mine = {
        "store_id": STORE_ID,
        "merchant_id": MERCHANT_ID,
        "domain": SITE_ID,
        "api_key": json.dumps(
            {"api_key": "old-key", "site_id": SITE_ID, "instance_id": INSTANCE_ID}
        ),
    }
    db = _ConnectDB(rows=[mine], existing={"store_id": STORE_ID, "api_key": mine["api_key"]})
    response = _connect(
        _connect_client(monkeypatch, db), instance_id=INSTANCE_ID, api_key="rotated-key"
    )

    assert response.status_code == 200, response.text
    assert json.loads(_written_credential(db))["instance_id"] == INSTANCE_ID


@pytest.mark.parametrize(
    "forged",
    [
        "d2b4e0a1_1f3c_4a55_9a2e_4c1b7a55e001",  # `_` is a LIKE single-char wildcard
        "%",
        "d2b4e0a1%",
        "short",
        "-leading-hyphen",
    ],
)
def test_connect_refuses_an_instance_id_the_receiver_could_never_resolve(monkeypatch, forged):
    db = _ConnectDB()
    response = _connect(_connect_client(monkeypatch, db), instance_id=forged)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "WIX_INSTANCE_ID_INVALID"
    assert db.writes == []
    assert db.queries == [], "a wildcard must never reach the database"


def test_a_foreign_merchant_cannot_connect_a_wix_store(monkeypatch):
    db = _ConnectDB()
    client = _connect_client(
        monkeypatch, db, user={"role": "merchant", "merchant_id": "merchant-other"}
    )
    response = _connect(client, instance_id=INSTANCE_ID)

    assert response.status_code == 403
    assert db.writes == []


# ---- connect-sync: the other writer of the same column ----------------------


def _connect_sync_client(monkeypatch, db):
    from routes import wix_sync as route
    from utils.auth import get_current_user

    monkeypatch.setattr(route, "database", db)

    async def _validate(site_id, api_key):
        return {"site_id": SITE_ID, "api_key": str(api_key), "status_code": 200}

    monkeypatch.setattr(route, "validate_wix_catalog_access", _validate)

    app = FastAPI()
    app.include_router(route.router)

    async def fake_user():
        return {"role": "merchant", "merchant_id": MERCHANT_ID}

    app.dependency_overrides[get_current_user] = fake_user
    return TestClient(app)


class _ConnectSyncDB(_ConnectDB):
    """`connect-sync` reads the merchant row first, then the store row."""

    def __init__(self, existing=None):
        super().__init__(existing=existing)
        self._answers = [{"merchant_id": MERCHANT_ID}]

    async def fetch_one(self, query, values=None):
        self.queries.append((str(query), values))
        if "merchant_onboarding" in str(query):
            return dict(self._answers[0])
        return dict(self.existing) if self.existing else None


def _post_connect_sync(client, *, api_key="rotated-key"):
    return client.post(
        "/integrations/wix/connect-sync",
        params={"merchant_id": MERCHANT_ID, "api_key": api_key, "site_id": SITE_ID},
    )


def test_connect_sync_keeps_the_instance_id_it_did_not_write(monkeypatch):
    """`normalize_wix_api_key` hands back the BARE key out of the blob, so
    writing it back plain erased `instance_id` and killed telemetry for a
    store this endpoint knows nothing about."""
    db = _ConnectSyncDB(
        existing={
            "store_id": STORE_ID,
            "api_key": json.dumps(
                {"api_key": "old-key", "site_id": SITE_ID, "instance_id": INSTANCE_ID}
            ),
        }
    )
    response = _post_connect_sync(_connect_sync_client(monkeypatch, db))

    assert response.status_code == 200, response.text
    stored = json.loads(db.writes[0][1]["api_key"])
    assert stored["instance_id"] == INSTANCE_ID
    assert stored["api_key"] == "rotated-key"


def test_connect_sync_on_a_bare_key_store_still_writes_the_bare_key(monkeypatch):
    db = _ConnectSyncDB(existing={"store_id": STORE_ID, "api_key": "old-bare-key"})
    response = _post_connect_sync(_connect_sync_client(monkeypatch, db))

    assert response.status_code == 200, response.text
    assert db.writes[0][1]["api_key"] == "rotated-key"

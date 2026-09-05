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


def _token(claim, *, private_pem=WIX_PRIVATE_PEM, algorithm="RS256", exp=None):
    payload = {"data": json.dumps(claim)}
    if exp is not None:
        payload["exp"] = int(exp.timestamp())
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
    first = _map(_inner_order_event("updated", order=_order(paymentStatus="PAID")))
    # A redelivery carries a NEW envelope event id; the ledger identity must
    # not move with it, or every Wix retry would double-count.
    second = _map(
        _inner_order_event(
            "updated", order=_order(paymentStatus="PAID"), event_id="a-different-delivery"
        )
    )
    assert [event.event_id for event in first.events] == [
        event.event_id for event in second.events
    ]


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

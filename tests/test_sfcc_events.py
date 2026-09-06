import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _event(event_type="order.created"):
    return {
        "event_id": "native-event-1",
        "type": event_type,
        "occurred_at": "2026-08-28T10:00:00Z",
        "site_id": "RefArchGlobal",
        "basket_id": "basket-1",
        "checkout_id": "checkout-1",
        "order_id": "order-44",
        "payment_id": "payment-44",
        "refund_id": "refund-44",
        "customer_id": "customer-8",
        "session_id": "session-8",
        "click_id": "clk_abcdef1234",
        "amount": "25.50",
        "currency": "USD",
        "status": "created",
        "items": [
            {
                "id": "item-1",
                "product_id": "product-10",
                "variant_id": "variant-11",
                "sku": "SKU-11",
                "quantity": 2,
                "price": "12.75",
                "total": "25.50",
                "product_name": "Do not retain",
            }
        ],
        "billing_address": {"email": "buyer@example.com"},
    }


def test_sfcc_event_maps_to_platform_neutral_ledger_without_pii():
    from services.sfcc_event_adapter import map_sfcc_integration_event

    event = map_sfcc_integration_event(
        _event(),
        store_id="store-sfcc",
        delivery_id="delivery-1",
    )
    retry = map_sfcc_integration_event(
        _event(),
        store_id="store-sfcc",
        delivery_id="delivery-2",
    )

    assert event.event_type == "order.created"
    assert event.platform == "salesforce_commerce_cloud"
    assert event.source == "sfcc_cartridge_outbox"
    assert event.order_id == "order-44"
    assert event.click_id == "clk_abcdef1234"
    assert event.amount_cents == 2550
    assert event.event_id == retry.event_id
    assert event.metadata["native_site_id"] == "RefArchGlobal"
    assert event.metadata["native_line_items"] == [
        {
            "id": "item-1",
            "product_id": "product-10",
            "variant_id": "variant-11",
            "sku": "SKU-11",
            "quantity": 2,
            "price": "12.75",
            "total": "25.50",
        }
    ]
    serialized = event.model_dump_json()
    assert "buyer@example.com" not in serialized
    assert "Do not retain" not in serialized


@pytest.mark.parametrize(
    ("native_type", "canonical_type"),
    [
        ("basket.created", "cart.created"),
        ("basket.item_added", "cart.item_added"),
        ("checkout.submitted", "checkout.submitted"),
        ("payment.authorized", "payment.authorized"),
        ("payment.declined", "payment.declined"),
        ("order.cancelled", "order.cancelled"),
        ("refund.succeeded", "refund.succeeded"),
    ],
)
def test_sfcc_lifecycle_mapping(native_type, canonical_type):
    from services.sfcc_event_adapter import map_sfcc_integration_event

    event = map_sfcc_integration_event(
        _event(native_type),
        store_id="store-sfcc",
    )
    assert event.event_type == canonical_type


def test_sfcc_event_requires_native_id_and_entity_stitch_key():
    from services.sfcc_event_adapter import map_sfcc_integration_event

    missing_id = _event()
    missing_id.pop("event_id")
    with pytest.raises(ValueError, match="event_id"):
        map_sfcc_integration_event(missing_id, store_id="store-sfcc")

    missing_order = _event()
    missing_order.pop("order_id")
    with pytest.raises(ValueError, match="order_id"):
        map_sfcc_integration_event(missing_order, store_id="store-sfcc")


def test_sfcc_webhook_requires_fresh_body_signature_and_exact_site(monkeypatch):
    from routes import sfcc_events as route

    ingested = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(
                    {
                        "site_id": "RefArchGlobal",
                        "telemetry_signing_secret": "hook-secret",
                    }
                ),
            }

    async def fake_ingest(**kwargs):
        ingested.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(route.time, "time", lambda: 2_000_000_000)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    raw = json.dumps({"events": [_event()]}, separators=(",", ":")).encode("utf-8")
    timestamp = "2000000000"
    digest = hmac.new(
        b"hook-secret",
        timestamp.encode("ascii") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Pivota-SFCC-Signature": f"sha256={digest}",
        "X-Pivota-SFCC-Timestamp": timestamp,
        "X-Pivota-SFCC-Delivery-Id": "delivery-1",
        "X-Pivota-SFCC-Site-Id": "RefArchGlobal",
    }

    response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert ingested[0]["merchant_id"] == "merchant-1"
    assert ingested[0]["agent_identity_confidence"] == "platform_asserted"
    assert ingested[0]["write_path"] == "sfcc_cartridge"

    tampered = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw + b" ",
        headers=headers,
    )
    assert tampered.status_code == 401

    wrong_site = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={**headers, "X-Pivota-SFCC-Site-Id": "OtherSite"},
    )
    assert wrong_site.status_code == 401

    for body_site_id in ("OtherSite", None):
        body_event = _event()
        body_event["site_id"] = body_site_id
        body_raw = json.dumps(
            {"events": [body_event]}, separators=(",", ":")
        ).encode("utf-8")
        body_digest = hmac.new(
            b"hook-secret",
            timestamp.encode("ascii") + b"." + body_raw,
            hashlib.sha256,
        ).hexdigest()
        body_site_response = client.post(
            "/webhooks/salesforce-commerce-cloud/store-sfcc",
            content=body_raw,
            headers={
                **headers,
                "X-Pivota-SFCC-Signature": f"sha256={body_digest}",
            },
        )
        assert body_site_response.status_code == 401

    expired = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={**headers, "X-Pivota-SFCC-Timestamp": "1999999000"},
    )
    assert expired.status_code == 401


def test_sfcc_webhook_ignores_future_event_without_dropping_supported_batch_items(monkeypatch):
    from routes import sfcc_events as route

    ingested = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(
                    {
                        "site_id": "RefArchGlobal",
                        "telemetry_signing_secret": "hook-secret",
                    }
                ),
            }

    async def fake_ingest(**kwargs):
        ingested.extend(kwargs["batch"].events)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(route.time, "time", lambda: 2_000_000_000)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    future = _event()
    future["event_id"] = "future-1"
    future["type"] = "shipment.dispatched"
    raw = json.dumps({"events": [future, _event()]}, separators=(",", ":")).encode()
    timestamp = "2000000000"
    digest = hmac.new(
        b"hook-secret", timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Pivota-SFCC-Signature": f"sha256={digest}",
            "X-Pivota-SFCC-Timestamp": timestamp,
            "X-Pivota-SFCC-Site-Id": "RefArchGlobal",
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["ignored"] == 1
    assert [event.event_type for event in ingested] == ["order.created"]


def test_sfcc_webhook_drops_malformed_supported_event_without_poisoning_valid_sibling(
    monkeypatch,
):
    from routes import sfcc_events as route

    ingested = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(
                    {
                        "site_id": "RefArchGlobal",
                        "telemetry_signing_secret": "hook-secret",
                    }
                ),
            }

    async def fake_ingest(**kwargs):
        ingested.extend(kwargs["batch"].events)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(route.time, "time", lambda: 2_000_000_000)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    malformed = _event()
    malformed["event_id"] = "malformed-1"
    malformed.pop("order_id")
    valid = _event()
    valid["event_id"] = "valid-1"
    raw = json.dumps({"events": [malformed, valid]}, separators=(",", ":")).encode()
    timestamp = "2000000000"
    digest = hmac.new(
        b"hook-secret", timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()

    response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Pivota-SFCC-Signature": f"sha256={digest}",
            "X-Pivota-SFCC-Timestamp": timestamp,
            "X-Pivota-SFCC-Site-Id": "RefArchGlobal",
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["rejected"] == 1
    assert len(ingested) == 1
    assert ingested[0].order_id == "order-44"


@pytest.mark.asyncio
async def test_sfcc_telemetry_provision_returns_secret_once_and_supports_rotation(monkeypatch):
    from routes import sfcc_integration as route

    writes = []
    credentials = {
        "site_id": "RefArchGlobal",
        "client_id": "client-123",
        "client_secret": "slas-secret",
    }

    class FakeDB:
        async def fetch_one(self, query, values):
            if query.lstrip().startswith("UPDATE"):
                assert values["expected_api_key"] == json.dumps(credentials)
                writes.append(values)
                credentials.clear()
                credentials.update(json.loads(values["api_key"]))
                return {"store_id": "store-sfcc"}
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(credentials),
            }

    generated = iter(["first-signing-secret", "rotated-signing-secret"])
    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route.secrets, "token_urlsafe", lambda _: next(generated))
    user = {"role": "merchant", "merchant_id": "merchant-1"}

    first = await route.provision_salesforce_commerce_cloud_telemetry(
        "store-sfcc",
        route.SalesforceCommerceCloudTelemetryProvisionRequest(),
        current_user=user,
    )
    assert first["status"] == "provisioned"
    assert first["signing_secret"] == "first-signing-secret"
    assert credentials["client_secret"] == "slas-secret"

    existing = await route.provision_salesforce_commerce_cloud_telemetry(
        "store-sfcc",
        route.SalesforceCommerceCloudTelemetryProvisionRequest(),
        current_user=user,
    )
    assert existing["status"] == "already_configured"
    assert "signing_secret" not in existing

    rotated = await route.provision_salesforce_commerce_cloud_telemetry(
        "store-sfcc",
        route.SalesforceCommerceCloudTelemetryProvisionRequest(rotate=True),
        current_user=user,
    )
    assert rotated["status"] == "rotated"
    assert rotated["signing_secret"] == "rotated-signing-secret"
    assert len(writes) == 2


@pytest.mark.asyncio
async def test_sfcc_first_provision_cas_loser_does_not_return_a_stale_secret(monkeypatch):
    from routes import sfcc_integration as route

    base = {
        "site_id": "RefArchGlobal",
        "client_id": "client-123",
        "client_secret": "slas-secret",
    }
    concurrent = {**base, "telemetry_signing_secret": "concurrent-winner"}
    selects = 0

    class FakeDB:
        async def fetch_one(self, query, values):
            nonlocal selects
            if query.lstrip().startswith("UPDATE"):
                return None
            selects += 1
            credentials = base if selects == 1 else concurrent
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(credentials),
            }

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route.secrets, "token_urlsafe", lambda _: "cas-loser-secret")
    result = await route.provision_salesforce_commerce_cloud_telemetry(
        "store-sfcc",
        route.SalesforceCommerceCloudTelemetryProvisionRequest(),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    assert result["status"] == "already_configured"
    assert "signing_secret" not in result
    assert "cas-loser-secret" not in json.dumps(result)


# ---- the settlement sweep's events ---------------------------------------------


def _sweep_event(event_type, **overrides):
    """One event exactly as `SweepPivotaSettlements.js` enqueues it.

    The sweep sends a DETERMINISTIC `event_id` (unlike the shopper hooks, which
    mint a UUID), no line items, and the order's `lastModified` as the time.
    """
    event = {
        "event_id": f"{event_type}:order-44",
        "type": event_type,
        "occurred_at": "2026-08-28T10:00:00Z",
        "site_id": "RefArchGlobal",
        "basket_id": None,
        "checkout_id": None,
        "order_id": "order-44",
        "payment_id": None,
        "customer_id": "customer-8",
        "amount": "25.50",
        "currency": "USD",
        "status": "PAID",
        "items": [],
    }
    event.update(overrides)
    return event


def test_sfcc_order_paid_names_the_amount_basis_and_keeps_its_native_id_stable():
    from services.sfcc_event_adapter import map_sfcc_integration_event

    event = map_sfcc_integration_event(_sweep_event("order.paid"), store_id="store-sfcc")
    replay = map_sfcc_integration_event(
        _sweep_event("order.paid"), store_id="store-sfcc", delivery_id="a-later-batch"
    )

    assert event.event_type == "order.paid"
    assert event.amount_cents == 2550
    # `Order.totalGrossPrice`, not a capture. A divergence from the PSP's own
    # figure has to be diagnosable rather than invisible.
    assert event.metadata["native_amount_semantics"] == "order_total_gross"
    # The sweep's deterministic id is what makes a redelivery dedupe in the
    # ledger instead of writing a second paid row.
    assert event.event_id == replay.event_id


def test_sfcc_refund_is_keyed_on_the_invoice_so_two_credits_stay_two_events():
    from services.sfcc_event_adapter import map_sfcc_integration_event

    first = map_sfcc_integration_event(
        _sweep_event(
            "refund.succeeded",
            event_id="refund.succeeded:INV-1",
            refund_id="INV-1",
            amount="10.00",
        ),
        store_id="store-sfcc",
    )
    second = map_sfcc_integration_event(
        _sweep_event(
            "refund.succeeded",
            event_id="refund.succeeded:INV-2",
            refund_id="INV-2",
            amount="5.00",
        ),
        store_id="store-sfcc",
    )

    assert first.refund_id == "INV-1"
    assert second.refund_id == "INV-2"
    assert first.event_id != second.event_id
    assert (first.amount_cents, second.amount_cents) == (1000, 500)
    assert first.order_id == second.order_id == "order-44"
    # A refund's amount is neither the order total nor the invoice's cumulative
    # figure: it is the DELTA that observation added. Reading a row as the
    # invoice total would over-report an invoice refunded twice, so the basis is
    # named on the row.
    assert first.metadata["native_amount_semantics"] == "invoice_cumulative_delta"
    assert first.metadata["native_amount_semantics"] != "order_total_gross"


def test_sfcc_two_partial_refunds_on_one_invoice_stay_two_events():
    """`Invoice.refundedAmount` is cumulative per INVOICE.

    A second partial refund raises it instead of creating a second invoice, so
    a `refund_id` of the bare invoice number made the second refund a duplicate
    of the first and lost it for good. The sweep qualifies the id with the
    cumulative total the invoice reached and sends the difference.
    """
    from services.sfcc_event_adapter import map_sfcc_integration_event

    first = map_sfcc_integration_event(
        _sweep_event(
            "refund.succeeded",
            event_id="refund.succeeded:INV-1:10.00",
            refund_id="INV-1:10.00",
            amount="10.00",
        ),
        store_id="store-sfcc",
    )
    second = map_sfcc_integration_event(
        _sweep_event(
            "refund.succeeded",
            event_id="refund.succeeded:INV-1:25.00",
            refund_id="INV-1:25.00",
            amount="15.00",
        ),
        store_id="store-sfcc",
    )

    # Same invoice, two ledger rows — and the funnel sums distinct refund ids
    # inside one authority, so the two deltas report the cumulative 25.00.
    assert first.event_id != second.event_id
    assert (first.refund_id, second.refund_id) == ("INV-1:10.00", "INV-1:25.00")
    assert (first.amount_cents, second.amount_cents) == (1000, 1500)
    # A redelivery of either is still the same key.
    replay = map_sfcc_integration_event(
        _sweep_event(
            "refund.succeeded",
            event_id="refund.succeeded:INV-1:25.00",
            refund_id="INV-1:25.00",
            amount="15.00",
        ),
        store_id="store-sfcc",
    )
    assert replay.event_id == second.event_id


@pytest.mark.parametrize("event_type", ["order.paid", "payment.succeeded", "refund.succeeded"])
@pytest.mark.parametrize("amount", ["0", "0.00", "", None])
def test_sfcc_money_event_without_a_positive_amount_is_rejected(event_type, amount):
    """A zero-amount money event under a native id is a PERMANENT SHADOW.

    The ledger dedupes first-write-wins on the key derived from the native id,
    which for these three is the order or the credit invoice. A zero row would
    make the real figure for that same order/invoice unwritable forever, so the
    mapper refuses it and the receiver counts it `rejected`.
    """
    from services.sfcc_event_adapter import map_sfcc_integration_event

    payload = _sweep_event(event_type, refund_id="INV-1", amount=amount)
    with pytest.raises(ValueError, match="positive settled amount"):
        map_sfcc_integration_event(payload, store_id="store-sfcc")


def test_sfcc_money_event_without_a_currency_is_rejected():
    """The funnel drops a money row with no currency, but the dedupe key is
    already occupied by then — so it is refused here instead."""
    from services.sfcc_event_adapter import map_sfcc_integration_event

    with pytest.raises(ValueError, match="missing currency"):
        map_sfcc_integration_event(
            _sweep_event("order.paid", currency=""), store_id="store-sfcc"
        )


@pytest.mark.parametrize(
    "event_type", ["order.created", "order.cancelled", "payment.authorized", "payment.failed"]
)
def test_sfcc_non_money_events_still_map_without_an_amount(event_type):
    """The positive-amount rule must apply to the three money events and to
    nothing else: a cancellation moves no money and still has to land."""
    from services.sfcc_event_adapter import map_sfcc_integration_event

    event = map_sfcc_integration_event(
        _sweep_event(event_type, amount=None, payment_id="payment-44", status="CANCELLED"),
        store_id="store-sfcc",
    )
    assert event.amount_cents is None
    assert "native_amount_semantics" not in event.metadata


def test_sfcc_event_metadata_stays_inside_the_shared_allowlist():
    from services.merchant_event_ingest_service import ALLOWED_MERCHANT_METADATA_KEYS
    from services.sfcc_event_adapter import map_sfcc_integration_event

    for event_type in ("order.paid", "order.cancelled", "refund.succeeded"):
        event = map_sfcc_integration_event(
            _sweep_event(event_type, refund_id="INV-1"),
            store_id="store-sfcc",
            delivery_id="delivery-1",
        )
        assert set(event.metadata) <= set(ALLOWED_MERCHANT_METADATA_KEYS)


# ---- receiver hardening ---------------------------------------------------------


def _route_client(monkeypatch, ingested):
    from routes import sfcc_events as route

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(
                    {
                        "site_id": "RefArchGlobal",
                        "telemetry_signing_secret": "hook-secret",
                    }
                ),
            }

    async def fake_ingest(**kwargs):
        ingested.extend(kwargs["batch"].events)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(route.time, "time", lambda: 2_000_000_000)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _signed(events):
    raw = json.dumps({"events": events}, separators=(",", ":")).encode("utf-8")
    timestamp = "2000000000"
    digest = hmac.new(
        b"hook-secret", timestamp.encode("ascii") + b"." + raw, hashlib.sha256
    ).hexdigest()
    return raw, {
        "Content-Type": "application/json",
        "X-Pivota-SFCC-Signature": f"sha256={digest}",
        "X-Pivota-SFCC-Timestamp": timestamp,
        "X-Pivota-SFCC-Delivery-Id": "delivery-1",
        "X-Pivota-SFCC-Site-Id": "RefArchGlobal",
    }


def test_sfcc_non_ascii_signature_header_is_a_401_not_an_unauthenticated_500(monkeypatch):
    """`hmac.compare_digest` raises TypeError on a str holding a code point
    above U+00FF, and Starlette decodes header bytes as latin-1 — so
    `sha256=\xe9…` used to reach the comparison as a str it could not accept and
    became an UNAUTHENTICATED 500. Both sides are bytes now."""
    client = _route_client(monkeypatch, [])
    raw, headers = _signed([_sweep_event("order.paid")])

    response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={
            **headers,
            # Sent as raw bytes: this is exactly what a cartridge with a
            # mangled credential field puts on the wire.
            "X-Pivota-SFCC-Signature": "sha256=\xe9deadbeef".encode("latin-1"),
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Invalid SFCC event signature"


def test_sfcc_non_ascii_site_id_is_a_401_in_the_header_and_in_the_signed_body(monkeypatch):
    """The same TypeError shape, twice more: the site-id header is latin-1
    decoded, and the per-event `site_id` comes out of JSON, which may hold any
    code point at all."""
    client = _route_client(monkeypatch, [])
    raw, headers = _signed([_sweep_event("order.paid")])

    header_response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={**headers, "X-Pivota-SFCC-Site-Id": "RefArchGlob\xe9l".encode("latin-1")},
    )
    assert header_response.status_code == 401, header_response.text

    # The body is JSON, so it can carry a code point latin-1 cannot even hold.
    body_raw, body_headers = _signed(
        [_sweep_event("order.paid", site_id="RefArchGlob\u4e2d")]
    )
    body_response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=body_raw,
        headers={**body_headers, "X-Pivota-SFCC-Site-Id": "RefArchGlobal"},
    )
    assert body_response.status_code == 401
    assert body_response.json()["detail"] == "Invalid SFCC event site"


def test_sfcc_all_rejected_batch_reports_rejected_and_the_counter_sees_it(monkeypatch):
    """`TelemetryIngress.record_result` short-circuits on `status: "ignored"`
    and records exactly ONE `ignored` event. A delivery whose every event was
    REJECTED therefore counted as ignored and the rejections vanished from the
    metrics. The summary shape with `accepted = 0` makes the ingress walk its
    accepted/duplicate/ignored/rejected fields instead."""
    from observability import commerce_telemetry_metrics as metrics

    def counter(outcome):
        value = metrics.counter_value(
            "events", write_path="sfcc_cartridge", outcome=outcome
        )
        assert value is not None, "prometheus_client is required for this test"
        return value

    ingested = []
    client = _route_client(monkeypatch, ingested)
    # Two events the mapper refuses: a zero-amount refund (the permanent-shadow
    # rule) and an order event with no order id.
    zero_refund = _sweep_event(
        "refund.succeeded", event_id="refund.succeeded:INV-9", refund_id="INV-9", amount="0"
    )
    no_order = _sweep_event("order.paid", event_id="order.paid:none")
    no_order.pop("order_id")
    raw, headers = _signed([zero_refund, no_order])

    before_rejected = counter("rejected")
    before_ignored = counter("ignored")
    response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc", content=raw, headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["accepted"] == 0
    assert body["rejected"] == 2
    assert body["ignored"] == 0
    assert ingested == []
    assert counter("rejected") == before_rejected + 2
    # …and NOT the single blanket `ignored` the old shape recorded.
    assert counter("ignored") == before_ignored


def test_sfcc_all_ignored_batch_still_reports_ignored(monkeypatch):
    """The `rejected` branch must not swallow the plain unsupported-event case:
    a batch of events this bridge does not map is still `ignored`."""
    from observability import commerce_telemetry_metrics as metrics

    client = _route_client(monkeypatch, [])
    unknown = _sweep_event("shipment.dispatched", event_id="shipment-1")
    raw, headers = _signed([unknown])

    before = metrics.counter_value(
        "events", write_path="sfcc_cartridge", outcome="ignored"
    )
    response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc", content=raw, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["ignored"] == 1
    assert response.json()["rejected"] == 0
    assert metrics.counter_value(
        "events", write_path="sfcc_cartridge", outcome="ignored"
    ) == before + 1

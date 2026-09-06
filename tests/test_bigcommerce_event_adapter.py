"""BigCommerce native telemetry: mapper, receiver, and the SQLite ledger path.

Fixtures use BigCommerce's documented shapes only — RFC-2822 order dates, the
`payment_status` enum, integer `status_id`, and the v3 refund object inside a
`{"data": [...]}` wrapper. Nothing is invented; a field this bridge does not
read is simply absent.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


STORE_ID = "store-bc"
MERCHANT_ID = "merchant-bc"
STORE_HASH = "abcde"
WEBHOOK_SECRET = "bc-webhook-secret"
ORDER_ID = 250


def _order(**overrides):
    order = {
        "id": ORDER_ID,
        "customer_id": 8,
        "date_created": "Tue, 05 Mar 2019 21:40:11 +0000",
        "date_modified": "Wed, 06 Mar 2019 09:15:00 +0000",
        "status_id": 10,
        "status": "Completed",
        "payment_status": "captured",
        "payment_method": "Credit Card",
        "payment_provider_id": "txn-bc-250",
        "total_inc_tax": "49.99",
        "total_tax": "3.99",
        "refunded_amount": "0.0000",
        "currency_code": "USD",
        "cart_id": "cart-bc-250",
    }
    order.update(overrides)
    return order


def _refund(refund_id, total_amount, created="2019-03-07T11:00:00Z"):
    return {
        "id": refund_id,
        "order_id": ORDER_ID,
        "user_id": 1,
        "created": created,
        "reason": "buyer changed their mind",
        "total_amount": total_amount,
        "total_tax": "0.00",
        "items": [{"item_type": "PRODUCT", "item_id": 1, "quantity": 1}],
    }


def _delivery(scope="store/order/updated", *, producer=f"stores/{STORE_HASH}", data=None):
    return {
        "scope": scope,
        "store_id": "1025646",
        "data": data if data is not None else {"type": "order", "id": ORDER_ID},
        "hash": "3f9ea420af83450d7ef9f78b08c8af25b2213637",
        "producer": producer,
    }


def _map(order=None, refunds=None, *, scope="store/order/updated", delivery_hash="hash-1"):
    from services.bigcommerce_event_adapter import map_bigcommerce_order

    return map_bigcommerce_order(
        order if order is not None else _order(),
        refunds or [],
        scope=scope,
        delivery_hash=delivery_hash,
        store_id=STORE_ID,
    )


# ---- mapper -----------------------------------------------------------------


def test_captured_order_maps_to_created_and_paid_with_native_dates():
    batch = _map()

    assert [event.event_type for event in batch.events] == ["order.created", "order.paid"]
    created, paid = batch.events
    assert created.order_id == "250"
    assert created.order_ref == "bigcommerce:250"
    assert created.buyer_id == "8"
    assert created.payment_id == "txn-bc-250"
    assert created.cart_id == "cart-bc-250"
    assert created.amount_cents == 4999
    assert created.currency == "USD"
    # RFC-2822 in, UTC out.
    assert created.occurred_at.isoformat() == "2019-03-05T21:40:11+00:00"
    assert paid.occurred_at.isoformat() == "2019-03-06T09:15:00+00:00"
    assert created.metadata["native_status"] == "Completed"
    assert created.metadata["native_payment_method"] == "Credit Card"
    assert created.metadata["webhook_delivery_id"] == "hash-1"
    # The v2 order body carries buyer PII in fields this mapper never reads.
    assert "email" not in json.dumps(created.metadata)
    # No BigCommerce channel carries a Pivota click id today.
    assert created.click_id is None


def test_repeated_delivery_produces_identical_event_ids():
    first = _map(scope="store/order/created", delivery_hash="hash-a")
    second = _map(scope="store/order/updated", delivery_hash="hash-b")

    assert [event.event_id for event in first.events] == [
        event.event_id for event in second.events
    ]
    # The delivery hash rides in metadata/trace, never in the identity.
    assert first.events[0].trace_id != second.events[0].trace_id


def test_authorized_order_is_created_but_never_paid():
    batch = _map(_order(payment_status="authorized", status_id=11, status="Awaiting Fulfillment"))

    assert [event.event_type for event in batch.events] == ["order.created"]


@pytest.mark.parametrize("payment_status", ["pending", "capture pending", "void", ""])
def test_uncaptured_payment_statuses_never_emit_order_paid(payment_status):
    batch = _map(_order(payment_status=payment_status, status_id=1, status="Pending"))

    assert "order.paid" not in [event.event_type for event in batch.events]


def test_cancelled_status_id_five_maps_to_order_cancelled():
    batch = _map(_order(status_id=5, status="Cancelled", payment_status="void"))

    types = [event.event_type for event in batch.events]
    assert types == ["order.created", "order.cancelled"]
    cancelled = batch.events[1]
    assert cancelled.occurred_at.isoformat() == "2019-03-06T09:15:00+00:00"


def test_declined_status_id_six_maps_to_payment_failed():
    batch = _map(_order(status_id=6, status="Declined", payment_status="declined"))

    assert [event.event_type for event in batch.events] == ["order.created", "payment.failed"]


def test_two_partial_refunds_map_to_two_distinct_refund_events():
    order = _order(status_id=14, status="Partially Refunded", payment_status="partially refunded")
    batch = _map(
        order,
        [_refund(901, "10.50"), _refund(902, "5.00", created="2019-03-08T11:00:00Z")],
        scope="store/order/refund/created",
    )

    refunds = [event for event in batch.events if event.event_type == "refund.succeeded"]
    assert len(refunds) == 2
    assert sorted(event.refund_id for event in refunds) == ["901", "902"]
    assert sorted(event.amount_cents for event in refunds) == [500, 1050]
    # The event id is keyed on the native refund id, not the order id.
    assert len({event.event_id for event in refunds}) == 2
    assert refunds[0].occurred_at.isoformat() == "2019-03-07T11:00:00+00:00"
    assert refunds[1].occurred_at.isoformat() == "2019-03-08T11:00:00+00:00"
    # A refund presupposes a capture, so the order still reports as paid.
    assert "order.paid" in [event.event_type for event in batch.events]
    # `reason` is merchant free text and never reaches canonical metadata.
    assert "changed their mind" not in json.dumps(refunds[0].metadata)


def test_zero_decimal_currency_keeps_whole_units():
    order = _order(currency_code="JPY", total_inc_tax="4999", payment_status="partially refunded")
    batch = _map(order, [_refund(901, "1050")], scope="store/order/refund/created")

    assert batch.events[0].amount_cents == 4999
    refund = [event for event in batch.events if event.event_type == "refund.succeeded"][0]
    assert refund.amount_cents == 1050
    assert refund.currency == "JPY"


def test_malformed_refund_entries_never_suppress_valid_siblings():
    batch = _map(
        _order(payment_status="partially refunded"),
        [
            "not-an-object",
            {"total_amount": "1.00"},  # no id: cannot be made idempotent
            _refund(901, "10.50"),
            _refund(901, "10.50"),  # repeated in the same list
            {"id": 902, "created": "2019-03-08T11:00:00Z", "total_amount": "oops"},
        ],
        scope="store/order/refund/created",
    )

    refunds = [event for event in batch.events if event.event_type == "refund.succeeded"]
    assert sorted(event.refund_id for event in refunds) == ["901", "902"]
    assert [event.amount_cents for event in refunds if event.refund_id == "901"] == [1050]
    # An unparseable amount keeps the event and drops only the number.
    assert [event.amount_cents for event in refunds if event.refund_id == "902"] == [None]


def test_zero_customer_id_is_a_guest_not_a_buyer():
    batch = _map(_order(customer_id=0))

    assert batch.events[0].buyer_id is None


def test_unsupported_scope_is_refused():
    from services.bigcommerce_event_adapter import UnsupportedBigCommerceEvent

    with pytest.raises(UnsupportedBigCommerceEvent):
        _map(scope="store/product/updated")
    with pytest.raises(UnsupportedBigCommerceEvent):
        _map(scope="")


def test_an_order_without_an_id_is_a_value_error():
    with pytest.raises(ValueError):
        _map(_order(id=None))


def test_refunds_are_relevant_only_on_the_refund_scope_or_a_refunded_order():
    from services.bigcommerce_event_adapter import refunds_are_relevant

    assert refunds_are_relevant("store/order/refund/created", _order()) is True
    assert refunds_are_relevant("store/order/updated", _order()) is False
    assert (
        refunds_are_relevant("store/order/updated", _order(payment_status="refunded")) is True
    )
    assert (
        refunds_are_relevant(
            "store/order/statusUpdated", _order(payment_status="partially refunded")
        )
        is True
    )


# ---- the receiver -----------------------------------------------------------


class _FakeDB:
    def __init__(self, row):
        self.row = row

    async def fetch_one(self, *args, **kwargs):
        return self.row


def _store_row(**overrides):
    credentials = {
        "access_token": "bc-access-token",
        "client_id": "bc-client",
        "store_hash": STORE_HASH,
        "webhook_secret": WEBHOOK_SECRET,
    }
    credentials.update(overrides.pop("credentials", {}))
    row = {
        "store_id": STORE_ID,
        "merchant_id": MERCHANT_ID,
        "domain": f"{STORE_HASH}.mybigcommerce.com",
        "api_key": json.dumps(credentials),
    }
    row.update(overrides)
    return row


def _client(monkeypatch, *, row, fetch=None, fetch_error=None):
    """A one-route app around the real receiver with the DB and fetch stubbed."""
    from routes import bigcommerce_webhooks as route
    from services.bigcommerce_order_fetch import BigCommerceOrderContext

    calls = {"fetch": [], "ingest": []}
    monkeypatch.setattr(route, "database", _FakeDB(row))

    async def _fetch(**kwargs):
        calls["fetch"].append(kwargs)
        if fetch_error is not None:
            raise fetch_error
        order, refunds = fetch if fetch is not None else (_order(), [])
        return BigCommerceOrderContext(order=order, refunds=refunds)

    async def _ingest(**kwargs):
        calls["ingest"].append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "fetch_bigcommerce_order_context", _fetch)
    monkeypatch.setattr(route, "ingest_merchant_event_batch", _ingest)

    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app), calls


def _post(client, payload, *, secret=WEBHOOK_SECRET, store_id=STORE_ID):
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Pivota-Webhook-Secret"] = secret
    return client.post(
        f"/webhooks/bigcommerce/{store_id}",
        content=json.dumps(payload),
        headers=headers,
    )


def test_route_accepts_a_valid_delivery_and_stamps_the_write_path(monkeypatch):
    client, calls = _client(monkeypatch, row=_store_row())

    response = _post(client, _delivery())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "recorded"
    assert body["platform"] == "bigcommerce"

    assert len(calls["fetch"]) == 1
    fetched = calls["fetch"][0]
    assert fetched["store_hash"] == STORE_HASH
    assert fetched["access_token"] == "bc-access-token"
    assert fetched["client_id"] == "bc-client"
    assert fetched["order_id"] == str(ORDER_ID)
    assert fetched["scope"] == "store/order/updated"

    assert len(calls["ingest"]) == 1
    ingested = calls["ingest"][0]
    assert ingested["write_path"] == "bigcommerce_webhook"
    assert ingested["agent_identity_confidence"] == "platform_asserted"
    assert ingested["merchant_id"] == MERCHANT_ID
    assert [event.event_type for event in ingested["batch"].events] == [
        "order.created",
        "order.paid",
    ]


@pytest.mark.parametrize(
    ("kwargs", "payload_kwargs"),
    [
        ({"secret": "wrong-secret"}, {}),
        ({"secret": None}, {}),
    ],
)
def test_a_wrong_or_missing_secret_is_401_and_never_fetches(monkeypatch, kwargs, payload_kwargs):
    client, calls = _client(monkeypatch, row=_store_row())

    response = _post(client, _delivery(**payload_kwargs), **kwargs)

    assert response.status_code == 401
    assert calls["fetch"] == []
    assert calls["ingest"] == []


def test_an_unknown_store_is_401_and_never_fetches(monkeypatch):
    client, calls = _client(monkeypatch, row=None)

    response = _post(client, _delivery())

    assert response.status_code == 401
    assert calls["fetch"] == []


def test_a_store_without_a_provisioned_secret_is_401(monkeypatch):
    client, calls = _client(
        monkeypatch, row=_store_row(credentials={"webhook_secret": ""})
    )

    response = _post(client, _delivery())

    assert response.status_code == 401
    assert calls["fetch"] == []


def test_an_inactive_store_is_401_because_the_lookup_filters_status(monkeypatch):
    """The status filter lives in the SELECT, so an inactive store returns no row."""
    from routes import bigcommerce_webhooks as route

    source = (
        __import__("pathlib").Path(route.__file__).read_text(encoding="utf-8")
    )
    assert "lower(COALESCE(status, 'active')) IN ('active', 'connected')" in source
    assert "platform = 'bigcommerce'" in source

    client, calls = _client(monkeypatch, row=None)
    assert _post(client, _delivery()).status_code == 401
    assert calls["fetch"] == []


def test_a_producer_from_another_store_is_401_and_never_fetches(monkeypatch):
    client, calls = _client(monkeypatch, row=_store_row())

    response = _post(client, _delivery(producer="stores/someone-else"))

    assert response.status_code == 401
    assert calls["fetch"] == []
    assert calls["ingest"] == []


def test_a_missing_producer_is_401(monkeypatch):
    client, calls = _client(monkeypatch, row=_store_row())

    payload = _delivery()
    payload.pop("producer")
    assert _post(client, payload).status_code == 401
    assert calls["fetch"] == []


def test_an_unsupported_scope_is_ignored_before_any_fetch(monkeypatch):
    client, calls = _client(monkeypatch, row=_store_row())

    response = _post(client, _delivery(scope="store/product/updated"))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert calls["fetch"] == []
    assert calls["ingest"] == []


def test_a_fetch_failure_is_503_and_never_ingests(monkeypatch):
    from services.bigcommerce_order_fetch import BigCommerceOrderFetchError

    client, calls = _client(
        monkeypatch,
        row=_store_row(),
        fetch_error=BigCommerceOrderFetchError("BigCommerce order fetch failed with HTTP 502"),
    )

    response = _post(client, _delivery())

    assert response.status_code == 503
    assert calls["ingest"] == []


def test_a_delivery_without_an_order_id_is_422(monkeypatch):
    client, calls = _client(monkeypatch, row=_store_row())

    response = _post(client, _delivery(data={"type": "order"}))

    assert response.status_code == 422
    assert calls["fetch"] == []


def test_the_refund_scope_forwards_its_refunds_to_the_ledger(monkeypatch):
    client, calls = _client(
        monkeypatch,
        row=_store_row(),
        fetch=(
            _order(status_id=14, status="Partially Refunded", payment_status="partially refunded"),
            [_refund(901, "10.50"), _refund(902, "5.00")],
        ),
    )

    response = _post(client, _delivery(scope="store/order/refund/created"))

    assert response.status_code == 200
    events = calls["ingest"][0]["batch"].events
    assert sorted(
        event.refund_id for event in events if event.event_type == "refund.succeeded"
    ) == ["901", "902"]


def test_the_route_is_wrapped_and_charges_the_platform_tier():
    """The ingress ratchet asserts this too; pinning it here keeps the failure local."""
    import ast
    from pathlib import Path

    import routes.bigcommerce_webhooks as route

    tree = ast.parse(Path(route.__file__).read_text(encoding="utf-8"))
    decorators = [
        decorator
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and getattr(decorator.func, "id", None) == "telemetry_ingress_route"
    ]
    assert [decorator.args[0].value for decorator in decorators] == ["bigcommerce_webhook"]
    tiers = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "enforce_rate_limit"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert tiers == {"platform"}


def test_a_body_over_one_megabyte_is_refused(monkeypatch):
    client, calls = _client(monkeypatch, row=_store_row())

    payload = _delivery()
    payload["padding"] = "x" * 1_100_000
    response = _post(client, payload)

    assert response.status_code == 413
    assert calls["fetch"] == []


# ---- the fetcher ------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.responses[len(self.gets) - 1]


@pytest.mark.asyncio
async def test_fetcher_reads_the_order_and_skips_refunds_when_none_are_implied(monkeypatch):
    from services import bigcommerce_order_fetch as service

    fake = _FakeHttpClient([_FakeResponse(200, _order())])
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    context = await service.fetch_bigcommerce_order_context(
        store_hash=STORE_HASH,
        access_token="bc-access-token",
        client_id="bc-client",
        order_id="250",
        scope="store/order/updated",
    )

    assert context.order["id"] == ORDER_ID
    assert context.refunds == []
    assert len(fake.gets) == 1
    url, kwargs = fake.gets[0]
    assert url == "https://api.bigcommerce.com/stores/abcde/v2/orders/250"
    assert kwargs["headers"]["X-Auth-Token"] == "bc-access-token"
    assert kwargs["headers"]["X-Auth-Client"] == "bc-client"


@pytest.mark.asyncio
async def test_fetcher_reads_refunds_out_of_the_v3_data_wrapper(monkeypatch):
    from services import bigcommerce_order_fetch as service

    fake = _FakeHttpClient(
        [
            _FakeResponse(200, _order(payment_status="partially refunded")),
            _FakeResponse(200, {"data": [_refund(901, "10.50")], "meta": {}}),
        ]
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    context = await service.fetch_bigcommerce_order_context(
        store_hash=STORE_HASH,
        access_token="bc-access-token",
        client_id=None,
        order_id="250",
        scope="store/order/updated",
    )

    assert [row["id"] for row in context.refunds] == [901]
    assert fake.gets[1][0] == (
        "https://api.bigcommerce.com/stores/abcde/v3/orders/250/payment_actions/refunds"
    )


@pytest.mark.asyncio
async def test_fetcher_raises_on_a_non_2xx(monkeypatch):
    from services import bigcommerce_order_fetch as service

    fake = _FakeHttpClient([_FakeResponse(502, {})])
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    with pytest.raises(service.BigCommerceOrderFetchError):
        await service.fetch_bigcommerce_order_context(
            store_hash=STORE_HASH,
            access_token="t",
            client_id=None,
            order_id="250",
            scope="store/order/updated",
        )


@pytest.mark.asyncio
async def test_fetcher_raises_when_the_refund_read_fails(monkeypatch):
    from services import bigcommerce_order_fetch as service

    fake = _FakeHttpClient(
        [
            _FakeResponse(200, _order(payment_status="refunded")),
            _FakeResponse(500, {}),
        ]
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    with pytest.raises(service.BigCommerceOrderFetchError):
        await service.fetch_bigcommerce_order_context(
            store_hash=STORE_HASH,
            access_token="t",
            client_id=None,
            order_id="250",
            scope="store/order/updated",
        )


# ---- the ensure route: the secret the hooks carry is the secret the receiver holds ----


def _ensure_app(current_user):
    from fastapi import FastAPI

    from routes.merchant_store_connections import router
    from utils.auth import get_current_user

    app = FastAPI()
    app.include_router(router)

    async def fake_user():
        return current_user

    app.dependency_overrides[get_current_user] = fake_user
    return TestClient(app)


class _EnsureStoreDB:
    """One BigCommerce store row; records the credential UPDATE; can simulate a
    concurrent writer by answering the re-read with a different secret."""

    def __init__(self, credentials, *, reread_secret=None):
        self.credentials = dict(credentials)
        self.reread_secret = reread_secret
        self.updates = []

    async def fetch_one(self, query, values):
        if "SELECT api_key FROM merchant_stores" in str(query):
            creds = dict(self.credentials)
            if self.updates:
                creds = json.loads(self.updates[-1]["api_key"])
            if self.reread_secret is not None:
                creds["webhook_secret"] = self.reread_secret
            return {"api_key": json.dumps(creds)}
        return {
            "store_id": values["store_id"],
            "merchant_id": "merch_bc",
            "domain": "store-abc12.mybigcommerce.com",
            "api_key": json.dumps(self.credentials),
        }

    async def execute(self, query, values):
        self.updates.append(dict(values))
        return None


def _install_ensure_stubs(monkeypatch, db, installed):
    import routes.merchant_store_connections as route
    import services.bigcommerce_webhook_subscriptions as subs

    monkeypatch.setenv("BIGCOMMERCE_WEBHOOK_BASE_URL", "https://api.example.test")
    monkeypatch.setattr(route, "database", db)

    async def fake_ensure(**kwargs):
        installed.append(kwargs)
        return {"created_scopes": list(kwargs["scopes"]), "synchronized_scopes": [], "disabled_duplicates": []}

    monkeypatch.setattr(subs, "ensure_bigcommerce_subscriptions", fake_ensure)


def test_ensure_route_registers_hooks_with_the_secret_that_actually_persisted(monkeypatch):
    """A concurrent first-time call may win the UPDATE with a different secret;
    the hooks must carry THAT one, or every delivery is a 401 until re-ensured."""
    installed = []
    db = _EnsureStoreDB(
        {"store_hash": "abc12", "access_token": "tok", "client_id": "cid"},
        reread_secret="secret-from-the-other-writer",
    )
    _install_ensure_stubs(monkeypatch, db, installed)

    response = _ensure_app({"role": "merchant", "merchant_id": "merch_bc"}).post(
        "/integrations/bigcommerce/store_bc/webhooks/ensure"
    )

    assert response.status_code == 200, response.text
    assert len(db.updates) == 1, "a missing secret is minted and persisted exactly once"
    minted = json.loads(db.updates[0]["api_key"])["webhook_secret"]
    assert minted and minted != "secret-from-the-other-writer"
    assert installed[0]["secret"] == "secret-from-the-other-writer"
    assert installed[0]["callback_url"] == "https://api.example.test/webhooks/bigcommerce/store_bc"
    # Neither secret is ever returned to the caller.
    assert minted not in response.text
    assert "secret-from-the-other-writer" not in response.text


def test_ensure_route_reuses_an_existing_secret_without_rewriting_credentials(monkeypatch):
    installed = []
    db = _EnsureStoreDB(
        {"store_hash": "abc12", "access_token": "tok", "webhook_secret": "existing-secret"}
    )
    _install_ensure_stubs(monkeypatch, db, installed)

    response = _ensure_app({"role": "merchant", "merchant_id": "merch_bc"}).post(
        "/integrations/bigcommerce/store_bc/webhooks/ensure"
    )

    assert response.status_code == 200, response.text
    assert db.updates == []
    assert installed[0]["secret"] == "existing-secret"
    assert "existing-secret" not in response.text
    # A foreign merchant cannot trigger installation for this store.
    denied = _ensure_app({"role": "merchant", "merchant_id": "merch_other"}).post(
        "/integrations/bigcommerce/store_bc/webhooks/ensure"
    )
    assert denied.status_code == 403

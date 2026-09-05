"""The PrestaShop mapper, receiver and secret provisioning.

PrestaShop sends no webhooks of its own, so the sender under test here is the
module Pivota ships (integrations/prestashop-module/). What that module emits
is fixed by tests/test_prestashop_module_contract.py; these tests are about
what the receiver does with it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


STORE_ID = "store-ps"
MERCHANT_ID = "merch_ps"
SHOP_URL = "https://shop.example.test"
SECRET = "ps-hook-secret"
NOW = 2_000_000_000


def _order(**overrides):
    order = {
        "id": 1042,
        "reference": "XKBKNABJK",
        "id_cart": 55,
        "id_customer": 9,
        "currency": "EUR",
        "current_state": 2,
        "state_key": "payment",
        "state_flags": {"paid": True, "shipped": False, "delivery": False, "logable": True},
        "valid": True,
        "total_paid_tax_incl": "40.56",
        "total_paid_real": "40.56",
        "payment_module": "ps_checkout",
        "date_add": "2026-09-01 10:00:00",
        "date_upd": "2026-09-02 10:00:00",
    }
    order.update(overrides)
    return order


def _slip(**overrides):
    slip = {
        "id": 77,
        "amount": "8.00",
        "shipping_cost_amount": "0.00",
        "total_products_tax_incl": "10.50",
        "total_shipping_tax_incl": "0.00",
        "date_add": "2026-09-03 10:00:00",
    }
    slip.update(overrides)
    return slip


def _event(hook="actionValidateOrder", *, order=None, slip=None, **overrides):
    event = {
        "event_id": f"{hook}:1042:2",
        "hook": hook,
        "occurred_at": "2026-09-04T10:00:00+00:00",
        "order": order if order is not None else _order(),
        "order_slip": slip,
    }
    event.update(overrides)
    return event


def _map(event, **kwargs):
    from services.prestashop_event_adapter import map_prestashop_module_event

    return map_prestashop_module_event(event, store_id=STORE_ID, **kwargs)


# ---- the mapper ------------------------------------------------------------------


def test_validate_order_in_a_paid_state_is_created_and_paid():
    events = _map(_event(), delivery_id="delivery-1")

    assert [event.event_type for event in events] == ["order.created", "order.paid"]
    created, paid = events
    assert created.platform == "prestashop"
    assert created.source == "prestashop_module_outbox"
    assert created.store_id == STORE_ID
    assert created.order_id == "1042"
    # No writeback exists for PrestaShop, so an order always originated in the
    # shop and the namespace is always the platform.
    assert created.order_ref == "prestashop:1042"
    assert created.buyer_id == "9"
    assert created.cart_id == "55"
    assert created.currency == "EUR"
    assert created.amount_cents == 4056
    assert paid.amount_cents == 4056
    assert created.metadata["native_status"] == "payment"
    assert created.metadata["native_payment_method"] == "ps_checkout"
    assert created.metadata["webhook_delivery_id"] == "delivery-1"
    assert paid.metadata["native_amount_semantics"] == "total_paid_real"
    assert created.event_id != paid.event_id


def test_validate_order_in_an_unpaid_state_is_created_only():
    order = _order(
        state_key="other",
        current_state=1,
        state_flags={"paid": False, "shipped": False, "delivery": False, "logable": True},
        total_paid_real="0.00",
    )
    events = _map(_event(order=order))

    assert [event.event_type for event in events] == ["order.created"]
    assert events[0].amount_cents == 4056
    assert events[0].metadata["native_status"] == "other"


def test_status_update_to_a_paid_state_is_order_paid_and_prefers_what_was_captured():
    order = _order(total_paid_real="30.00", total_paid_tax_incl="40.56")
    events = _map(_event("actionOrderStatusPostUpdate", order=order))

    assert [event.event_type for event in events] == ["order.paid"]
    # `total_paid_real` is what actually arrived; the order total would
    # overstate a partial capture.
    assert events[0].amount_cents == 3000
    assert events[0].metadata["native_amount_semantics"] == "total_paid_real"


def test_a_paid_state_that_has_not_written_total_paid_real_falls_back_to_the_order_total():
    order = _order(total_paid_real="0.00")
    events = _map(_event("actionOrderStatusPostUpdate", order=order))

    assert events[0].amount_cents == 4056
    assert events[0].metadata["native_amount_semantics"] == "total_paid_tax_incl"


@pytest.mark.parametrize(
    ("state_key", "event_type"),
    [("canceled", "order.cancelled"), ("error", "payment.failed")],
)
def test_status_update_maps_the_money_states(state_key, event_type):
    order = _order(
        state_key=state_key,
        state_flags={"paid": False, "shipped": False, "delivery": False, "logable": True},
    )
    events = _map(_event("actionOrderStatusPostUpdate", order=order))

    assert [event.event_type for event in events] == [event_type]
    # Neither carries an amount: PrestaShop's state transition says nothing
    # about how much was cancelled or how much failed.
    assert events[0].amount_cents is None
    assert events[0].metadata["native_status"] == state_key


@pytest.mark.parametrize("state_key", ["refund", "shipped", "delivered", "other"])
def test_states_that_move_no_money_emit_nothing(state_key):
    """`refund` especially: the credit slip is the refund fact and carries the
    amount and its own id. Emitting on both would double-count every refund."""
    from services.prestashop_event_adapter import NoPrestaShopCanonicalEvents

    order = _order(
        state_key=state_key,
        state_flags={"paid": False, "shipped": False, "delivery": False, "logable": True},
    )
    with pytest.raises(NoPrestaShopCanonicalEvents):
        _map(_event("actionOrderStatusPostUpdate", order=order))


def test_a_credit_slip_is_one_refund_keyed_on_the_slip():
    events = _map(_event("actionOrderSlipAdd", slip=_slip(), order=_order()))

    assert [event.event_type for event in events] == ["refund.succeeded"]
    refund = events[0]
    assert refund.refund_id == "77"
    assert refund.order_id == "1042"
    assert refund.order_ref == "prestashop:1042"
    # products 10.50 + shipping 0.00, tax included — NOT the legacy `amount`
    # (8.00), which is products-only and tax-excluded on the default path.
    assert refund.amount_cents == 1050


def test_a_credit_slip_sums_products_and_shipping_tax_included():
    slip = _slip(total_products_tax_incl="10.50", total_shipping_tax_incl="4.99", amount="9.00")
    events = _map(_event("actionOrderSlipAdd", slip=slip, order=_order()))

    assert events[0].amount_cents == 1549


def test_a_partial_slip_with_only_the_legacy_columns_still_reports_a_refund():
    """`OrderSlip::createPartialOrderSlip()` writes only `amount` and
    `shipping_cost_amount` and leaves the four totals at 0. Core never calls it
    — a third-party module can — and reading the zeroed totals would report a
    refund of nothing."""
    slip = _slip(
        total_products_tax_incl="0.00",
        total_shipping_tax_incl="0.00",
        amount="12.00",
        shipping_cost_amount="3.00",
    )
    events = _map(_event("actionOrderSlipAdd", slip=slip, order=_order()))

    assert events[0].amount_cents == 1500


def test_two_slips_are_two_refunds_with_distinct_ids():
    first = _map(_event("actionOrderSlipAdd", slip=_slip(id=77), order=_order()))
    second = _map(_event("actionOrderSlipAdd", slip=_slip(id=78), order=_order()))

    assert first[0].event_id != second[0].event_id
    assert {first[0].refund_id, second[0].refund_id} == {"77", "78"}


def test_a_repeated_delivery_produces_identical_event_ids():
    """The module retries a batch it could not deliver, and the drain may
    re-send a row whose 2xx was lost. Every id must be a function of the
    entity, not of the delivery."""
    first = _map(_event(), delivery_id="delivery-1")
    replay = _map(_event(), delivery_id="delivery-9")

    assert [event.event_id for event in first] == [event.event_id for event in replay]
    slip_first = _map(_event("actionOrderSlipAdd", slip=_slip(), order=_order()), delivery_id="d1")
    slip_replay = _map(_event("actionOrderSlipAdd", slip=_slip(), order=_order()), delivery_id="d2")
    assert slip_first[0].event_id == slip_replay[0].event_id


def test_a_zero_decimal_currency_is_not_multiplied():
    order = _order(currency="JPY", total_paid_tax_incl="4056", total_paid_real="4056")
    events = _map(_event(order=order))

    assert [event.amount_cents for event in events] == [4056, 4056]
    slip = _slip(total_products_tax_incl="1050", total_shipping_tax_incl="0")
    refund = _map(_event("actionOrderSlipAdd", slip=slip, order=order))
    assert refund[0].amount_cents == 1050


def test_a_guest_order_without_a_customer_id_has_no_buyer():
    """`id_customer` is 0 on an order with no customer row. Zero is not an
    identity and must not become one."""
    events = _map(_event(order=_order(id_customer=0)))

    assert events[0].buyer_id is None


def test_an_unsupported_hook_is_ignored_not_rejected():
    from services.prestashop_event_adapter import UnsupportedPrestaShopEvent

    with pytest.raises(UnsupportedPrestaShopEvent):
        _map(_event("actionCartSave"))


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda event: event.pop("occurred_at"), "occurred_at"),
        (lambda event: event["order"].pop("id"), "order.id"),
        (lambda event: event["order"].pop("currency"), "currency"),
        (lambda event: event["order"].update({"state_key": "made_up"}), "state_key"),
    ],
)
def test_a_malformed_event_is_a_value_error(mutate, match):
    event = _event()
    mutate(event)
    with pytest.raises(ValueError, match=match):
        _map(event)


def test_a_slip_event_without_a_slip_id_is_a_value_error():
    with pytest.raises(ValueError, match="order_slip.id"):
        _map(_event("actionOrderSlipAdd", slip=_slip(id=0), order=_order()))


def test_the_mapper_carries_no_free_text_from_the_shop():
    """The module sends no personal data; the mapper additionally keeps the
    payload to the shared metadata allowlist."""
    from services.merchant_event_ingest_service import ALLOWED_MERCHANT_METADATA_KEYS

    order = _order(reference="XKBKNABJK")
    events = _map(_event(order=order), delivery_id="delivery-1")
    for event in events:
        assert set(event.metadata) <= set(ALLOWED_MERCHANT_METADATA_KEYS)
        serialized = event.model_dump_json()
        assert "XKBKNABJK" not in serialized


# ---- the receiver ----------------------------------------------------------------


class _FakeDB:
    def __init__(self, row):
        self.row = row

    async def fetch_one(self, *args, **kwargs):
        return self.row


def _store_row(**overrides):
    row = {
        "store_id": STORE_ID,
        "merchant_id": MERCHANT_ID,
        "domain": SHOP_URL,
        "api_key": json.dumps({"api_key": "ws-key", "webhook_secret": SECRET}),
    }
    row.update(overrides)
    return row


_DEFAULT_ROW = object()


def _client(monkeypatch, ingested, *, row=_DEFAULT_ROW):
    from routes import prestashop_webhooks as route

    async def fake_ingest(**kwargs):
        ingested.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(
        route, "database", _FakeDB(_store_row() if row is _DEFAULT_ROW else row)
    )
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(route.time, "time", lambda: NOW)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _post(client, events, *, secret=SECRET, timestamp=str(NOW), shop_url=SHOP_URL, body_shop_url=None):
    raw = json.dumps(
        {"events": events, "shop_url": SHOP_URL if body_shop_url is None else body_shop_url},
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + raw, hashlib.sha256
    ).hexdigest()
    return client.post(
        f"/webhooks/prestashop/{STORE_ID}",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Pivota-PrestaShop-Signature": f"sha256={digest}",
            "X-Pivota-PrestaShop-Timestamp": timestamp,
            "X-Pivota-PrestaShop-Delivery-Id": "delivery-1",
            "X-Pivota-PrestaShop-Shop-Url": shop_url,
        },
    )


def test_a_signed_batch_from_the_connected_shop_is_ingested(monkeypatch):
    ingested = []
    client = _client(monkeypatch, ingested)

    response = _post(client, [_event()])

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 2
    assert ingested[0]["merchant_id"] == MERCHANT_ID
    assert ingested[0]["agent_identity_confidence"] == "platform_asserted"
    assert ingested[0]["write_path"] == "prestashop_module"
    assert [event.event_type for event in ingested[0]["batch"].events] == [
        "order.created",
        "order.paid",
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"secret": "wrong-secret"},
        {"timestamp": str(NOW - 301)},
        {"timestamp": str(NOW + 301)},
        {"timestamp": "not-a-number"},
        {"shop_url": "https://other-shop.example.test"},
        {"body_shop_url": "https://other-shop.example.test"},
    ],
)
def test_a_delivery_that_fails_any_check_is_401_and_never_ingests(monkeypatch, kwargs):
    ingested = []
    client = _client(monkeypatch, ingested)

    response = _post(client, [_event()], **kwargs)

    assert response.status_code == 401, response.text
    assert ingested == []


@pytest.mark.parametrize(
    "row",
    [
        None,  # no such store / inactive: the SELECT filters both
        _store_row(api_key=json.dumps({"api_key": "ws-key"})),  # never provisioned
        _store_row(api_key="bare-webservice-key"),  # connected before telemetry existed
    ],
)
def test_an_unknown_or_unprovisioned_store_is_401(monkeypatch, row):
    ingested = []
    client = _client(monkeypatch, ingested, row=row)

    response = _post(client, [_event()])

    assert response.status_code == 401
    assert ingested == []


def test_a_missing_signature_header_is_401(monkeypatch):
    ingested = []
    client = _client(monkeypatch, ingested)
    raw = json.dumps({"events": [_event()], "shop_url": SHOP_URL}).encode()

    response = client.post(
        f"/webhooks/prestashop/{STORE_ID}",
        content=raw,
        headers={"X-Pivota-PrestaShop-Timestamp": str(NOW)},
    )

    assert response.status_code == 401
    assert ingested == []


def test_one_malformed_event_is_rejected_while_its_siblings_ingest(monkeypatch):
    ingested = []
    client = _client(monkeypatch, ingested)
    broken = _event()
    broken["order"] = dict(broken["order"])
    broken["order"].pop("currency")

    response = _post(client, [broken, _event("actionOrderSlipAdd", slip=_slip(), order=_order())])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rejected"] == 1
    assert body["ignored"] == 0
    assert [event.event_type for event in ingested[0]["batch"].events] == ["refund.succeeded"]


def test_an_unsupported_hook_is_counted_ignored(monkeypatch):
    ingested = []
    client = _client(monkeypatch, ingested)

    response = _post(client, [_event("actionCartSave"), _event()])

    assert response.status_code == 200
    body = response.json()
    assert body["ignored"] == 1
    assert body["rejected"] == 0
    assert body["accepted"] == 2


def test_a_batch_of_only_ignorable_events_records_nothing(monkeypatch):
    ingested = []
    client = _client(monkeypatch, ingested)

    response = _post(client, [_event("actionCartSave")])

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert ingested == []


def test_more_than_a_hundred_events_is_422(monkeypatch):
    ingested = []
    client = _client(monkeypatch, ingested)

    response = _post(client, [_event() for _ in range(101)])

    assert response.status_code == 422
    assert ingested == []


def test_an_empty_batch_is_422(monkeypatch):
    ingested = []
    client = _client(monkeypatch, ingested)

    assert _post(client, []).status_code == 422
    assert ingested == []


# ---- provisioning: the secret a human has to paste --------------------------------


class _EnsureStoreDB:
    """One PrestaShop store row that records credential UPDATEs."""

    def __init__(self, credentials, *, merchant_id=MERCHANT_ID):
        self.credentials = credentials
        self.merchant_id = merchant_id
        self.updates = []

    def _current(self):
        if self.updates:
            return self.updates[-1]["api_key"]
        return self.credentials

    async def fetch_one(self, query, values):
        if "SELECT api_key FROM merchant_stores" in str(query):
            return {"api_key": self._current()}
        return {
            "store_id": values["store_id"],
            "merchant_id": self.merchant_id,
            "domain": SHOP_URL,
            "api_key": self._current(),
        }

    async def execute(self, query, values):
        self.updates.append(dict(values))
        return None


def _ensure_client(current_user):
    from routes.merchant_store_connections import router
    from utils.auth import get_current_user

    app = FastAPI()
    app.include_router(router)

    async def fake_user():
        return current_user

    app.dependency_overrides[get_current_user] = fake_user
    return TestClient(app)


def _ensure(client, body=None):
    return client.post(
        f"/integrations/prestashop/{STORE_ID}/telemetry/ensure",
        json=body if body is not None else {},
    )


def test_the_first_ensure_returns_the_secret_once_and_persists_it(monkeypatch):
    import routes.merchant_store_connections as route

    db = _EnsureStoreDB(json.dumps({"api_key": "ws-key"}))
    monkeypatch.setattr(route, "database", db)
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://api.example.test")
    client = _ensure_client({"role": "merchant", "merchant_id": MERCHANT_ID})

    first = _ensure(client)

    assert first.status_code == 200, first.text
    payload = first.json()
    secret = payload["secret"]
    assert secret
    assert payload["endpoint"] == f"https://api.example.test/webhooks/prestashop/{STORE_ID}"
    assert payload["secret_provisioned"] is True
    assert payload["rotated"] is False
    # Persisted next to the Webservice key, which is NOT destroyed even though
    # merchant_connect_prestashop stores it as a bare string.
    persisted = json.loads(db.updates[0]["api_key"])
    assert persisted["webhook_secret"] == secret
    assert persisted["api_key"] == "ws-key"

    # ...and the second call does not hand it out again.
    second = _ensure(client)
    assert second.status_code == 200
    assert "secret" not in second.json()
    assert secret not in second.text
    assert second.json()["secret_provisioned"] is True
    assert len(db.updates) == 1, "an existing secret is never rewritten"


def test_a_bare_webservice_key_is_migrated_rather_than_overwritten(monkeypatch):
    import routes.merchant_store_connections as route

    db = _EnsureStoreDB("bare-webservice-key")
    monkeypatch.setattr(route, "database", db)
    client = _ensure_client({"role": "merchant", "merchant_id": MERCHANT_ID})

    assert _ensure(client).status_code == 200
    persisted = json.loads(db.updates[0]["api_key"])
    assert persisted["api_key"] == "bare-webservice-key"
    assert persisted["webhook_secret"]


def test_rotate_mints_a_new_secret_and_returns_it_once(monkeypatch):
    import routes.merchant_store_connections as route

    db = _EnsureStoreDB(json.dumps({"api_key": "ws-key", "webhook_secret": "old-secret"}))
    monkeypatch.setattr(route, "database", db)
    client = _ensure_client({"role": "merchant", "merchant_id": MERCHANT_ID})

    rotated = _ensure(client, {"rotate": True})

    assert rotated.status_code == 200, rotated.text
    payload = rotated.json()
    assert payload["secret"] and payload["secret"] != "old-secret"
    assert payload["rotated"] is True
    assert json.loads(db.updates[0]["api_key"])["webhook_secret"] == payload["secret"]
    # The old one is gone and the new one is not readable again.
    again = _ensure(client)
    assert "secret" not in again.json()
    assert payload["secret"] not in again.text


def test_the_secret_returned_is_the_one_that_actually_persisted(monkeypatch):
    """Two first-time calls race and the last UPDATE wins. The merchant must
    paste the secret the RECEIVER will hold, not the one this request minted."""
    import routes.merchant_store_connections as route

    class _RacingDB(_EnsureStoreDB):
        async def fetch_one(self, query, values):
            if "SELECT api_key FROM merchant_stores" in str(query):
                return {"api_key": json.dumps({"webhook_secret": "secret-from-the-other-writer"})}
            return await _EnsureStoreDB.fetch_one(self, query, values)

    db = _RacingDB(json.dumps({"api_key": "ws-key"}))
    monkeypatch.setattr(route, "database", db)
    client = _ensure_client({"role": "merchant", "merchant_id": MERCHANT_ID})

    response = _ensure(client)

    assert response.json()["secret"] == "secret-from-the-other-writer"
    assert json.loads(db.updates[0]["api_key"])["webhook_secret"] != "secret-from-the-other-writer"


def test_a_foreign_merchant_cannot_provision_this_store(monkeypatch):
    import routes.merchant_store_connections as route

    db = _EnsureStoreDB(json.dumps({"api_key": "ws-key"}))
    monkeypatch.setattr(route, "database", db)
    client = _ensure_client({"role": "merchant", "merchant_id": "merch_other"})

    response = _ensure(client)

    assert response.status_code == 403
    assert db.updates == []


def test_provisioning_never_writes_the_secret_to_a_log(monkeypatch, caplog):
    import routes.merchant_store_connections as route

    db = _EnsureStoreDB(json.dumps({"api_key": "ws-key"}))
    monkeypatch.setattr(route, "database", db)
    client = _ensure_client({"role": "merchant", "merchant_id": MERCHANT_ID})

    with caplog.at_level(logging.DEBUG):
        response = _ensure(client)

    secret = response.json()["secret"]
    assert secret
    assert secret not in caplog.text


# ---- the retired fail-open stub ---------------------------------------------------


def test_the_old_prestashop_validate_webhook_stub_refuses_instead_of_returning_true():
    """It used to `return True` for every input.

    PrestaShop sends no webhooks, so there was nothing for it to verify — but
    any caller wired to it would have had an authenticator that admits
    everything. It now raises, and points at the verifier that does the work.
    """
    from adapters.prestashop_adapter import PrestaShopAdapter

    adapter = PrestaShopAdapter({"store_url": SHOP_URL, "api_key": "ws-key"})
    with pytest.raises(NotImplementedError, match="prestashop_webhooks"):
        adapter.validate_webhook({"X-Anything": "1"}, b"{}")


def test_no_route_or_service_calls_the_retired_stub():
    """A raise is only a fix if nothing calls it. AST, not a regex: a ratchet
    that matches one syntactic form permits the others."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    callers = []
    imports_adapter = []
    for directory in ("routes", "services"):
        for path in sorted((root / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "validate_webhook":
                    callers.append(f"{path.relative_to(root)}:{node.lineno}")
                if isinstance(node, ast.ImportFrom) and node.module == "adapters.prestashop_adapter":
                    imports_adapter.append(str(path.relative_to(root)))
    assert callers == [], callers
    # ...and the grep is not vacuous: the connect route really does import the
    # adapter, so a reintroduced call would have been found.
    assert "routes/merchant_store_connections.py" in imports_adapter

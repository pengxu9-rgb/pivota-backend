"""The Webflow receiver, driven through the REAL route.

Everything below the store lookup and the Data API call is production code: the
URL-secret compare, the optional signature layer, the site binding, the delivery
dedupe, the thin-trigger fetch, the mapper, and `ingest_merchant_event_batch`.
Only `merchant_stores` and the outbound fetch are doubles, because neither is
what these tests are about.

The refusals are deliberately paired with POSITIVE cases that reach the ledger:
a receiver that 401'd everything would satisfy every refusal here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

URL_SECRET = "wf-url-secret-value"
CLIENT_SECRET = "wf-app-client-secret"
MERCHANT_ID = "merchant-wf"
STORE_ID = "store-wf"
SITE_ID = "5f1a0000000000000000aaaa"
ORDER_ID = "0000-0001"
PATH = f"/webhooks/webflow/{STORE_ID}/{URL_SECRET}"


@pytest.fixture(autouse=True)
def _layer_two_off(monkeypatch):
    """Layer 2 is ARMED BY AN ENV VAR, so its default state is ambient.

    Without this, a deployment env that happens to export
    `WEBFLOW_CLIENT_SECRET` would turn every unsigned test below into a 401 and
    the suite would be testing a different receiver from the one it describes.
    The tests that exercise layer 2 set it explicitly.
    """
    monkeypatch.delenv("WEBFLOW_CLIENT_SECRET", raising=False)


def _order(**overrides):
    order = {
        "orderId": ORDER_ID,
        "status": "unfulfilled",
        "acceptedOn": "2026-09-01T10:00:00.000Z",
        "customerPaid": {"unit": "USD", "value": 5898, "string": "$58.98"},
    }
    order.update(overrides)
    return order


def _delivery(*, trigger="ecomm_new_order", site_id=None, order=None):
    """The v2 envelope: `{triggerType, payload: <order>}`."""
    body = {"triggerType": trigger, "payload": order if order is not None else _order()}
    if site_id is not None:
        body["siteId"] = site_id
    return body


class _Recorder:
    """Stands in for `record_webflow_order`; records what it was handed."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result

    async def __call__(self, **kwargs):
        from services.webflow_ledger import WebflowIngestResult

        self.calls.append(kwargs)
        return self._result or WebflowIngestResult(
            status="recorded", accepted=2, duplicates=0
        )


def _app(monkeypatch, *, credentials=None, store=True, order=None, fetch_error=None, recorder=None):
    from routes import webflow_webhooks as route

    blob = credentials if credentials is not None else {
        "api_token": "wf-token",
        "site_id": SITE_ID,
        "url_secret": URL_SECRET,
    }

    class FakeStores:
        async def fetch_one(self, *args, **kwargs):
            if not store:
                return None
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "domain": "shop.webflow.io",
                "api_key": json.dumps(blob),
            }

    fetched = []

    async def fake_fetch(*, api_token, site_id, order_id, **kwargs):
        fetched.append({"api_token": api_token, "site_id": site_id, "order_id": order_id})
        if fetch_error is not None:
            raise fetch_error
        return order if order is not None else _order()

    monkeypatch.setattr(route, "database", FakeStores())
    monkeypatch.setattr(route, "fetch_webflow_order", fake_fetch)
    monkeypatch.setattr(route, "record_webflow_order", recorder or _Recorder())
    # The in-process delivery cache is module state; a leftover entry from a
    # sibling test would make a fresh delivery look like a redelivery.
    route._SEEN_DELIVERIES.clear()

    app = FastAPI()
    app.include_router(route.router)
    return app, fetched


def _post(client, payload, *, path=PATH, raw=None, headers=None):
    body = raw if raw is not None else json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        path,
        content=body,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _signed_headers(raw: bytes, *, secret=CLIENT_SECRET, timestamp=None):
    stamp = str(timestamp if timestamp is not None else int(time.time() * 1000))
    digest = hmac.new(
        secret.encode("utf-8"), f"{stamp}:".encode("utf-8") + raw, hashlib.sha256
    ).hexdigest()
    return {"x-webflow-timestamp": stamp, "x-webflow-signature": digest}


# ---- the positive case, first: the refusals below mean nothing without it ---


def test_a_thin_trigger_fetches_the_order_and_records_it(monkeypatch):
    recorder = _Recorder()
    app, fetched = _app(monkeypatch, recorder=recorder)

    response = _post(TestClient(app), _delivery())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "recorded"
    assert body["platform"] == "webflow"
    assert body["accepted"] == 2
    # The delivery carried the whole order inline and the receiver read it back
    # ANYWAY, scoped to the store's own site: for a site-token installation the
    # body is only as trustworthy as the URL secret that admitted it.
    assert fetched == [
        {"api_token": "wf-token", "site_id": SITE_ID, "order_id": ORDER_ID}
    ]
    assert recorder.calls[0]["merchant_id"] == MERCHANT_ID
    assert recorder.calls[0]["store_id"] == STORE_ID
    assert recorder.calls[0]["from_webhook"] is True
    assert recorder.calls[0]["trigger_type"] == "ecomm_new_order"
    # The FETCHED order, not the body's copy of it.
    assert recorder.calls[0]["order"] == _order()


def test_a_bare_order_body_without_the_v2_envelope_still_works(monkeypatch):
    """Whether v2 always wraps in `payload` is an assumed claim. Accepting both
    costs nothing; a wrong guess would 422 every delivery."""
    app, fetched = _app(monkeypatch)

    response = _post(
        TestClient(app), {"triggerType": "ecomm_order_changed", **_order()}
    )

    assert response.status_code == 200, response.text
    assert fetched[0]["order_id"] == ORDER_ID


def test_the_body_money_is_never_what_gets_recorded(monkeypatch):
    """A delivery claiming a different amount from the fetched order records the
    FETCHED one. Layer 1 proves the sender knows a secret; it does not make the
    sender's arithmetic Webflow's."""
    recorder = _Recorder()
    app, _ = _app(monkeypatch, order=_order(), recorder=recorder)
    lying = _order(customerPaid={"unit": "USD", "value": 99999999})

    assert _post(TestClient(app), _delivery(order=lying)).status_code == 200
    assert recorder.calls[0]["order"]["customerPaid"]["value"] == 5898


# ---- layer 1: the URL secret ------------------------------------------------


@pytest.mark.parametrize(
    "case, secret",
    [
        ("wrong", "not-the-secret"),
        ("empty-ish", "x"),
        ("prefix of the real one", URL_SECRET[:-1]),
        ("the real one plus a character", URL_SECRET + "a"),
        # A path segment carrying bytes outside ASCII. Starlette decodes path
        # segments as text, so this reaches the handler as a str with non-ASCII
        # code points, and `hmac.compare_digest` raises TypeError on one of
        # those. Without the bytes compare this is a 500, and a 500 from a
        # hostile URL is a denial-of-service handle rather than a refusal.
        ("non-ascii", "sécret-with-latin-1"),
        ("emoji", "secret-\U0001f600"),
    ],
)
def test_a_wrong_url_secret_is_401_and_never_500(monkeypatch, case, secret):
    app, fetched = _app(monkeypatch)

    response = _post(
        TestClient(app), _delivery(), path=f"/webhooks/webflow/{STORE_ID}/{secret}"
    )

    assert response.status_code == 401, f"{case}: {response.status_code} {response.text}"
    # And it never cost a Webflow API call.
    assert fetched == []


def test_a_missing_url_secret_segment_does_not_reach_the_route(monkeypatch):
    """The secret is a required PATH SEGMENT, so omitting it cannot be a
    delivery that merely lacks a header — there is no route to hit."""
    app, fetched = _app(monkeypatch)

    response = _post(TestClient(app), _delivery(), path=f"/webhooks/webflow/{STORE_ID}")

    assert response.status_code == 404
    assert fetched == []


def test_a_store_with_no_provisioned_secret_401s_every_delivery(monkeypatch):
    """An empty stored secret must never match an empty supplied one."""
    app, fetched = _app(
        monkeypatch, credentials={"api_token": "wf-token", "site_id": SITE_ID}
    )

    assert _post(TestClient(app), _delivery()).status_code == 401
    assert fetched == []


def test_an_unknown_store_is_the_same_401(monkeypatch):
    app, fetched = _app(monkeypatch, store=False)

    response = _post(TestClient(app), _delivery())

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid Webflow webhook credentials"
    assert fetched == []


# ---- layer 2: the signature, when an OAuth app is configured ----------------


def test_with_no_client_secret_configured_the_signature_layer_is_off(monkeypatch):
    """A site-token installation gets UNSIGNED deliveries. If the signature were
    required unconditionally, every one of them would 401."""
    monkeypatch.delenv("WEBFLOW_CLIENT_SECRET", raising=False)
    app, _ = _app(monkeypatch)

    assert _post(TestClient(app), _delivery()).status_code == 200


def test_with_a_client_secret_configured_a_valid_signature_is_accepted(monkeypatch):
    monkeypatch.setenv("WEBFLOW_CLIENT_SECRET", CLIENT_SECRET)
    app, _ = _app(monkeypatch)
    raw = json.dumps(_delivery(), separators=(",", ":")).encode()

    response = _post(TestClient(app), None, raw=raw, headers=_signed_headers(raw))

    assert response.status_code == 200, response.text


def test_with_a_client_secret_configured_an_unsigned_delivery_is_401(monkeypatch):
    """The whole point of arming layer 2: a correct URL secret is no longer
    enough on a deployment that runs an app."""
    monkeypatch.setenv("WEBFLOW_CLIENT_SECRET", CLIENT_SECRET)
    app, fetched = _app(monkeypatch)

    assert _post(TestClient(app), _delivery()).status_code == 401
    assert fetched == []


@pytest.mark.parametrize(
    "case, mutate",
    [
        ("wrong signature", lambda h: {**h, "x-webflow-signature": "0" * 64}),
        ("wrong key", None),  # handled below
        ("missing timestamp", lambda h: {"x-webflow-signature": h["x-webflow-signature"]}),
        ("missing signature", lambda h: {"x-webflow-timestamp": h["x-webflow-timestamp"]}),
        ("non-numeric timestamp", lambda h: {**h, "x-webflow-timestamp": "yesterday"}),
    ],
)
def test_a_bad_signature_is_401_and_never_500(monkeypatch, case, mutate):
    monkeypatch.setenv("WEBFLOW_CLIENT_SECRET", CLIENT_SECRET)
    app, fetched = _app(monkeypatch)
    raw = json.dumps(_delivery(), separators=(",", ":")).encode()
    headers = (
        _signed_headers(raw, secret="a-different-app-secret")
        if mutate is None
        else mutate(_signed_headers(raw))
    )

    response = _post(TestClient(app), None, raw=raw, headers=headers)

    assert response.status_code == 401, f"{case}: {response.status_code}"
    assert fetched == []


def test_a_non_ascii_signature_header_is_401_not_500(monkeypatch):
    """`hmac.compare_digest` raises TypeError on a str with non-ASCII code
    points, and Starlette decodes headers as latin-1."""
    monkeypatch.setenv("WEBFLOW_CLIENT_SECRET", CLIENT_SECRET)
    app, fetched = _app(monkeypatch)
    raw = json.dumps(_delivery(), separators=(",", ":")).encode()

    response = TestClient(app).post(
        PATH,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "x-webflow-timestamp": str(int(time.time() * 1000)),
            "x-webflow-signature": "sígnature-with-latin-1".encode("latin-1"),
        },
    )

    assert response.status_code == 401
    assert fetched == []


def test_a_stale_timestamp_is_refused_even_with_a_valid_digest(monkeypatch):
    """The replay window. The digest below is genuinely correct for its
    timestamp; only its age makes it unacceptable."""
    monkeypatch.setenv("WEBFLOW_CLIENT_SECRET", CLIENT_SECRET)
    app, fetched = _app(monkeypatch)
    raw = json.dumps(_delivery(), separators=(",", ":")).encode()
    stale = int((time.time() - 3600) * 1000)

    response = _post(
        TestClient(app), None, raw=raw, headers=_signed_headers(raw, timestamp=stale)
    )

    assert response.status_code == 401
    assert fetched == []


def test_the_timestamp_is_part_of_the_signed_input(monkeypatch):
    """A digest computed over the body ALONE must not be accepted.

    Without the timestamp in the input there is no replay resistance at all, and
    the check degrades to "this body was signed once, ever".
    """
    monkeypatch.setenv("WEBFLOW_CLIENT_SECRET", CLIENT_SECRET)
    app, fetched = _app(monkeypatch)
    raw = json.dumps(_delivery(), separators=(",", ":")).encode()
    body_only = hmac.new(CLIENT_SECRET.encode(), raw, hashlib.sha256).hexdigest()

    response = _post(
        TestClient(app),
        None,
        raw=raw,
        headers={
            "x-webflow-timestamp": str(int(time.time() * 1000)),
            "x-webflow-signature": body_only,
        },
    )

    assert response.status_code == 401
    assert fetched == []


# ---- the site binding -------------------------------------------------------


def test_a_delivery_naming_another_site_is_401(monkeypatch):
    app, fetched = _app(monkeypatch)

    response = _post(TestClient(app), _delivery(site_id="some-other-site"))

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid Webflow webhook source"
    assert fetched == []


def test_a_delivery_naming_this_site_is_accepted(monkeypatch):
    app, fetched = _app(monkeypatch)

    assert _post(TestClient(app), _delivery(site_id=SITE_ID)).status_code == 200
    assert fetched[0]["site_id"] == SITE_ID


def test_a_non_ascii_site_id_in_the_body_is_401_not_500(monkeypatch):
    app, _ = _app(monkeypatch)

    assert _post(TestClient(app), _delivery(site_id="sité-ïd")).status_code == 401


def test_a_delivery_with_no_site_id_still_reads_only_this_stores_site(monkeypatch):
    """The structural half of the binding. Webflow is not documented to put a
    `siteId` on an ecomm payload, so the check above can never be the only one:
    the fetch URL carries the store's OWN site id, and an order belonging to
    another site simply is not there."""
    app, fetched = _app(monkeypatch)

    assert _post(TestClient(app), _delivery()).status_code == 200
    assert fetched[0]["site_id"] == SITE_ID


def test_a_store_with_no_site_binding_cannot_fetch_and_says_so(monkeypatch):
    app, fetched = _app(
        monkeypatch,
        credentials={"api_token": "wf-token", "url_secret": URL_SECRET},
    )

    response = _post(TestClient(app), _delivery())

    assert response.status_code == 503
    assert "site_id" in response.json()["detail"]
    assert fetched == []


# ---- the fetch --------------------------------------------------------------


def test_a_failed_fetch_is_503_so_webflow_retries(monkeypatch):
    from services.webflow_order_fetch import WebflowOrderFetchError

    app, _ = _app(monkeypatch, fetch_error=WebflowOrderFetchError("upstream is unwell"))

    response = _post(TestClient(app), _delivery())

    assert response.status_code == 503
    assert "upstream is unwell" in response.json()["detail"]


def test_an_order_webflow_cannot_find_yet_is_503_not_200(monkeypatch):
    """Usually the read racing the delivery. A 200 here would drop the event with
    no second chance; a 503 is retried, and the sweep is the backstop if the
    order really is absent."""
    from services.webflow_order_fetch import WebflowOrderNotFoundError

    app, _ = _app(monkeypatch, fetch_error=WebflowOrderNotFoundError("HTTP 404"))

    assert _post(TestClient(app), _delivery()).status_code == 503


def test_a_refused_credential_is_503_not_401(monkeypatch):
    """The DELIVERY authenticated; it is OUR credential that Webflow refused.
    Answering 401 would tell Webflow to stop sending, which is the opposite of
    what a store with a stale token needs."""
    from services.webflow_order_fetch import WebflowOrderUnauthorizedError

    app, _ = _app(monkeypatch, fetch_error=WebflowOrderUnauthorizedError("HTTP 401"))

    assert _post(TestClient(app), _delivery()).status_code == 503


def test_a_store_with_no_token_cannot_fetch(monkeypatch):
    app, fetched = _app(
        monkeypatch, credentials={"site_id": SITE_ID, "url_secret": URL_SECRET}
    )

    assert _post(TestClient(app), _delivery()).status_code == 503
    assert fetched == []


# ---- triggers, ids, dedupe --------------------------------------------------


def test_an_unmapped_trigger_is_ignored_before_the_fetch(monkeypatch):
    """An unmapped trigger must not cost a Webflow API call, and must not be
    able to drive one."""
    app, fetched = _app(monkeypatch)

    response = _post(TestClient(app), _delivery(trigger="ecomm_inventory_changed"))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["accepted"] == 0
    assert fetched == []


def test_a_delivery_with_no_order_id_is_422(monkeypatch):
    app, fetched = _app(monkeypatch)

    response = _post(
        TestClient(app), {"triggerType": "ecomm_new_order", "payload": {"status": "paid"}}
    )

    assert response.status_code == 422
    assert fetched == []


def test_a_malformed_body_is_400(monkeypatch):
    app, _ = _app(monkeypatch)

    assert _post(TestClient(app), None, raw=b"not json").status_code == 400
    assert _post(TestClient(app), None, raw=b"[1,2,3]").status_code == 400


def test_an_oversized_body_is_413_before_it_is_buffered(monkeypatch):
    app, fetched = _app(monkeypatch)

    response = _post(TestClient(app), None, raw=b"x" * 1_000_001)

    assert response.status_code == 413
    assert fetched == []


def test_an_identical_redelivery_is_a_counted_duplicate(monkeypatch):
    """`duplicates: 1`, not 0. It IS a duplicate observation — the ledger would
    have counted it as one had the short-circuit not saved the API call."""
    app, fetched = _app(monkeypatch)
    client = TestClient(app)

    assert _post(client, _delivery()).status_code == 200
    second = _post(client, _delivery())

    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["duplicates"] == 1
    # The redelivery cost no Webflow call.
    assert len(fetched) == 1


def test_a_second_state_change_for_the_SAME_order_is_not_swallowed(monkeypatch):
    """The dedupe is keyed on the BODY, not the order.

    A Webflow order legitimately changes state several times, and each is a
    different `ecomm_order_changed` for the same order. Keying the cache on the
    order id would swallow the refund — the single most valuable event of the
    lot.
    """
    recorder = _Recorder()
    app, fetched = _app(monkeypatch, recorder=recorder)
    client = TestClient(app)

    first = _post(client, _delivery(trigger="ecomm_order_changed"))
    second = _post(
        client,
        _delivery(
            trigger="ecomm_order_changed",
            order=_order(status="refunded", refundedOn="2026-09-03T10:00:00.000Z"),
        ),
    )

    assert first.status_code == 200 and first.json()["status"] == "recorded"
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "recorded"
    assert len(fetched) == 2
    assert len(recorder.calls) == 2


def test_a_delivery_that_failed_to_fetch_is_not_remembered_as_seen(monkeypatch):
    """Ids are recorded only AFTER a successful ingest, so a 503'd delivery is
    retried rather than swallowed by the cache."""
    from services.webflow_order_fetch import WebflowOrderFetchError

    app, _ = _app(monkeypatch, fetch_error=WebflowOrderFetchError("nope"))
    client = TestClient(app)

    assert _post(client, _delivery()).status_code == 503
    # Same body again: still a real attempt, not a "duplicate".
    assert _post(client, _delivery()).status_code == 503


def test_a_malformed_order_from_the_fetch_is_422(monkeypatch):
    """A decimal `value` reaches the receiver as a 422 rather than as a silently
    skipped or 100x-wrong amount."""
    from routes import webflow_webhooks as route

    app, _ = _app(monkeypatch, order=_order(customerPaid={"unit": "USD", "value": "58.98"}))
    monkeypatch.setattr(
        route,
        "record_webflow_order",
        __import__(
            "services.webflow_ledger", fromlist=["record_webflow_order"]
        ).record_webflow_order,
    )

    assert _post(TestClient(app), _delivery()).status_code == 422

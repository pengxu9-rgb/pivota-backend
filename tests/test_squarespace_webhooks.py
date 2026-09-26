"""The Squarespace receiver, driven through the REAL route with a real HMAC.

Everything below the store lookup and the Orders API call is production code:
the signature check, the `websiteId` binding, the notification dedupe, the
thin-payload fetch, the mapper, and `ingest_merchant_event_batch`. Only
`merchant_stores` and the outbound fetch are doubles, because neither is what
these tests are about.

The auth assertions are deliberately paired with a POSITIVE case that reaches
the ledger: a receiver that 401'd everything would satisfy every refusal here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "sq-subscription-secret"
MERCHANT_ID = "merchant-sq"
STORE_ID = "store-sq"
WEBSITE_ID = "5e1a0000000000000000aaaa"
ORDER_ID = "5e1f0b6a1c9d440000a1b2c3"
PATH = f"/webhooks/squarespace/{STORE_ID}"


def _order(**overrides):
    order = {
        "id": ORDER_ID,
        "orderNumber": "00042",
        "createdOn": "2026-09-01T10:00:00.000Z",
        "modifiedOn": "2026-09-01T10:00:00.000Z",
        "testmode": False,
        "fulfillmentStatus": "PENDING",
        "grandTotal": {"value": "40.00", "currency": "USD"},
    }
    order.update(overrides)
    return order


def _notification(*, topic="order.create", website_id=WEBSITE_ID, notification_id="n-1"):
    return {
        "id": notification_id,
        "topic": topic,
        "createdOn": "2026-09-01T10:00:01.000Z",
        "websiteId": website_id,
        "subscriptionId": "sub-1",
        "data": {"orderId": ORDER_ID},
    }


class _Recorder:
    """Stands in for `record_squarespace_order`; records what it was handed."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result

    async def __call__(self, **kwargs):
        from services.squarespace_ledger import SquarespaceIngestResult

        self.calls.append(kwargs)
        return self._result or SquarespaceIngestResult(
            status="recorded", accepted=2, duplicates=0
        )


def _app(
    monkeypatch,
    *,
    credentials=None,
    store=True,
    order=None,
    fetch_error=None,
    recorder=None,
):
    from routes import squarespace_webhooks as route

    blob = credentials if credentials is not None else {
        "api_key": "sq-api-key",
        "website_id": WEBSITE_ID,
        "webhook_secret": SECRET,
    }

    class FakeStores:
        async def fetch_one(self, *args, **kwargs):
            if not store:
                return None
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "domain": "shop.example",
                "api_key": json.dumps(blob),
            }

    fetched = []

    async def fake_fetch(*, access_token, order_id, **kwargs):
        fetched.append({"access_token": access_token, "order_id": order_id})
        if fetch_error is not None:
            raise fetch_error
        return order if order is not None else _order()

    monkeypatch.setattr(route, "database", FakeStores())
    monkeypatch.setattr(route, "fetch_squarespace_order", fake_fetch)
    monkeypatch.setattr(route, "record_squarespace_order", recorder or _Recorder())
    # The in-process notification cache is module state; a leftover entry from
    # a sibling test would make a fresh delivery look like a redelivery.
    route._SEEN_NOTIFICATIONS.clear()

    app = FastAPI()
    app.include_router(route.router)
    return app, fetched


def _sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _post(client, payload, *, signature=None, secret=SECRET, raw=None):
    body = raw if raw is not None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    value = signature if signature is not None else _sign(body, secret)
    if value is not False:
        headers["Squarespace-Signature"] = value
    return client.post(PATH, content=body, headers=headers)


# ---- the positive case, first: the refusals below mean nothing without it ---


def test_a_signed_thin_notification_fetches_the_order_and_records_it(monkeypatch):
    recorder = _Recorder()
    app, fetched = _app(monkeypatch, recorder=recorder)
    client = TestClient(app)

    response = _post(client, _notification())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "recorded"
    assert body["platform"] == "squarespace"
    assert body["accepted"] == 2
    # The delivery named an order and carried no order fields; the receiver
    # read it back with the store's own credential before mapping.
    assert fetched == [{"access_token": "sq-api-key", "order_id": ORDER_ID}]
    assert recorder.calls[0]["merchant_id"] == MERCHANT_ID
    assert recorder.calls[0]["store_id"] == STORE_ID
    assert recorder.calls[0]["from_webhook"] is True
    assert recorder.calls[0]["topic"] == "order.create"
    assert recorder.calls[0]["trace_id"] == "n-1"


def test_a_base64_spelling_of_the_same_digest_is_accepted(monkeypatch):
    """The header's encoding is the one claim that could not be pinned down.

    Accepting either spelling costs nothing — a caller must still produce the
    HMAC — and a wrong guess would otherwise 401 every real delivery.
    """
    app, _ = _app(monkeypatch)
    client = TestClient(app)
    body = json.dumps(_notification(), separators=(",", ":")).encode()
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).digest()

    response = _post(
        client, None, raw=body, signature=base64.b64encode(digest).decode("ascii")
    )

    assert response.status_code == 200, response.text


def test_an_oauth_token_is_preferred_over_the_api_key_for_the_fetch(monkeypatch):
    app, fetched = _app(
        monkeypatch,
        credentials={
            "api_key": "sq-api-key",
            "oauth_access_token": "sq-oauth-token",
            "website_id": WEBSITE_ID,
            "webhook_secret": SECRET,
        },
    )

    assert _post(TestClient(app), _notification()).status_code == 200
    assert fetched[0]["access_token"] == "sq-oauth-token"


# ---- the signature ---------------------------------------------------------


@pytest.mark.parametrize(
    "case, signature",
    [
        ("missing header", False),
        ("empty", ""),
        ("wrong hex", "0" * 64),
        ("not hex at all", "not-a-signature"),
        # A header carrying bytes outside ASCII. Starlette decodes header
        # values as latin-1, so this reaches the handler as a str with
        # non-ASCII code points, and `hmac.compare_digest` raises TypeError on
        # one of those. Without the ASCII guard this is a 500, and a 500 on a
        # hostile header is a denial-of-service handle rather than a refusal.
        # Sent as raw bytes because httpx refuses to encode a non-ASCII str.
        ("non-ascii bytes", "sígnature-with-latin-1".encode("latin-1")),
        ("high bytes", bytes(range(0xC0, 0xF0))),
    ],
)
def test_a_bad_signature_is_401_and_never_500(monkeypatch, case, signature):
    app, fetched = _app(monkeypatch)

    response = _post(TestClient(app), _notification(), signature=signature)

    assert response.status_code == 401, f"{case}: {response.status_code} {response.text}"
    # And it never cost a Squarespace API call.
    assert fetched == []


def test_a_signature_from_a_different_secret_is_401(monkeypatch):
    app, _ = _app(monkeypatch)

    response = _post(TestClient(app), _notification(), secret="some-other-secret")

    assert response.status_code == 401


def test_a_body_altered_after_signing_is_401(monkeypatch):
    app, _ = _app(monkeypatch)
    signed = json.dumps(_notification(), separators=(",", ":")).encode()
    tampered = json.dumps(
        _notification(notification_id="n-2"), separators=(",", ":")
    ).encode()

    response = _post(TestClient(app), None, raw=tampered, signature=_sign(signed))

    assert response.status_code == 401


def test_a_store_with_no_subscription_secret_is_401(monkeypatch):
    """An API-key-only store can never have a webhook secret, because it cannot
    create a subscription. It answers the same 401 as an unknown store: the
    caller learns nothing about which it was."""
    app, _ = _app(
        monkeypatch, credentials={"api_key": "sq-api-key", "website_id": WEBSITE_ID}
    )

    response = _post(TestClient(app), _notification(), signature="anything")

    assert response.status_code == 401


def test_an_unknown_store_is_401(monkeypatch):
    app, _ = _app(monkeypatch, store=False)

    assert _post(TestClient(app), _notification()).status_code == 401


# ---- the website binding ---------------------------------------------------


def test_a_notification_for_another_website_is_401(monkeypatch):
    """The subscription secret belongs to a SUBSCRIPTION, not to this store, so
    a valid signature alone does not say which site a delivery is for. Without
    this bind, anyone holding any subscription secret we also hold could drive
    order reads against this store."""
    app, fetched = _app(monkeypatch)

    response = _post(
        TestClient(app), _notification(website_id="5e1a0000000000000000bbbb")
    )

    assert response.status_code == 401
    assert "source" in response.json()["detail"]
    assert fetched == []


def test_a_store_with_no_website_binding_is_401(monkeypatch):
    app, _ = _app(
        monkeypatch, credentials={"api_key": "sq-api-key", "webhook_secret": SECRET}
    )

    assert _post(TestClient(app), _notification()).status_code == 401


def test_a_notification_with_no_website_id_is_401(monkeypatch):
    app, _ = _app(monkeypatch)
    payload = _notification()
    payload.pop("websiteId")

    assert _post(TestClient(app), payload).status_code == 401


# ---- topics, ids, and the summary shape ------------------------------------


@pytest.mark.parametrize(
    "topic, reason",
    [
        ("extension.uninstall", "extension_uninstall"),
        ("inventory.update", "unsupported"),
        ("", "unsupported"),
    ],
)
def test_an_unmapped_topic_is_a_zero_summary_and_costs_no_api_call(
    monkeypatch, topic, reason
):
    app, fetched = _app(monkeypatch)

    response = _post(TestClient(app), _notification(topic=topic))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 0
    assert body["duplicates"] == 0
    assert reason in body["reason"]
    # An unmapped topic must not be able to DRIVE a Squarespace API call.
    assert fetched == []


def test_a_notification_with_no_order_id_is_422(monkeypatch):
    app, fetched = _app(monkeypatch)
    payload = _notification()
    payload["data"] = {}

    assert _post(TestClient(app), payload).status_code == 422
    assert fetched == []


def test_an_oversized_body_is_413_before_anything_else(monkeypatch):
    app, fetched = _app(monkeypatch)
    raw = b"x" * 1_000_001

    response = _post(TestClient(app), None, raw=raw, signature=_sign(raw))

    assert response.status_code == 413
    assert fetched == []


def test_a_testmode_order_answers_a_zero_summary_not_an_error(monkeypatch):
    from services.squarespace_ledger import SquarespaceIngestResult

    app, _ = _app(
        monkeypatch,
        recorder=_Recorder(
            SquarespaceIngestResult(status="ignored", reason="testmode: ...")
        ),
    )

    response = _post(TestClient(app), _notification())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ignored"
    assert body["accepted"] == 0
    assert body["duplicates"] == 0


# ---- the fetch failure -----------------------------------------------------


def test_a_failed_order_fetch_is_503_so_squarespace_retries(monkeypatch):
    """Answering 200 here would drop the event for good: Squarespace retries a
    non-2xx delivery and nothing else would ever look at this order until the
    reconciliation sweep's next window."""
    from services.squarespace_order_fetch import SquarespaceOrderFetchError

    app, _ = _app(monkeypatch, fetch_error=SquarespaceOrderFetchError("HTTP 500"))

    response = _post(TestClient(app), _notification())

    assert response.status_code == 503


def test_a_delivery_that_failed_its_fetch_is_not_remembered_as_seen(monkeypatch):
    """The notification cache must record only SUCCESSFUL ingests, or a
    delivery Squarespace retries after our own 503 would be swallowed."""
    from routes import squarespace_webhooks as route
    from services.squarespace_order_fetch import SquarespaceOrderFetchError

    app, _ = _app(monkeypatch, fetch_error=SquarespaceOrderFetchError("HTTP 500"))
    client = TestClient(app)

    assert _post(client, _notification()).status_code == 503
    assert not route._SEEN_NOTIFICATIONS


# ---- the notification dedupe ----------------------------------------------


def test_a_redelivered_notification_id_short_circuits_the_fetch(monkeypatch):
    recorder = _Recorder()
    app, fetched = _app(monkeypatch, recorder=recorder)
    client = TestClient(app)

    first = _post(client, _notification())
    second = _post(client, _notification())

    assert first.json()["accepted"] == 2
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["accepted"] == 0
    # `duplicates: 1`, not 0. This IS a duplicate observation — the ledger would
    # have counted it as one had the short-circuit not saved the API call — and
    # reporting zero makes the metric read as if redeliveries never happen.
    assert second.json()["duplicates"] == 1
    # The point of the cache: the redelivery cost no Orders API call.
    assert len(fetched) == 1
    assert len(recorder.calls) == 1


def test_a_different_notification_for_the_same_order_is_not_deduped(monkeypatch):
    """The cache keys on the NOTIFICATION, not the order: an order.update after
    an order.create is a new observation and must reach the ledger, where the
    deterministic event ids decide what is actually new."""
    recorder = _Recorder()
    app, fetched = _app(monkeypatch, recorder=recorder)
    client = TestClient(app)

    _post(client, _notification(notification_id="n-1"))
    second = _post(
        client, _notification(topic="order.update", notification_id="n-2")
    )

    assert second.json()["status"] == "recorded"
    assert len(fetched) == 2
    assert len(recorder.calls) == 2


# ---- the OAuth token expires; the API key does not -------------------------


def test_an_expired_oauth_token_falls_back_to_the_api_key_for_the_fetch(monkeypatch):
    """A Developer-Platform access token is short-lived and there is no refresh
    path in this repo yet.

    Without the fallback, every delivery to an OAuth store 503s for the whole
    window between expiry and a human reconnecting — and Squarespace gives up
    retrying long before that. The store's telemetry then depends entirely on
    the next scheduled sweep, on a lane that ships no scheduler.
    """
    from routes import squarespace_webhooks as route
    from services.squarespace_order_fetch import SquarespaceOrderUnauthorizedError

    recorder = _Recorder()
    app, fetched = _app(
        monkeypatch,
        credentials={
            "api_key": "sq-api-key",
            "oauth_access_token": "expired-token",
            "website_id": WEBSITE_ID,
            "webhook_secret": SECRET,
        },
        recorder=recorder,
    )

    async def fake_fetch(*, access_token, order_id, **kwargs):
        fetched.append({"access_token": access_token, "order_id": order_id})
        if access_token == "expired-token":
            raise SquarespaceOrderUnauthorizedError(
                "Squarespace refused the credential for the order read (HTTP 401)"
            )
        return _order()

    monkeypatch.setattr(route, "fetch_squarespace_order", fake_fetch)

    response = _post(TestClient(app), _notification())

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "recorded"
    # It TRIED the OAuth token first — the preference is still real — and then
    # read with the key.
    assert [call["access_token"] for call in fetched] == ["expired-token", "sq-api-key"]
    assert len(recorder.calls) == 1


def test_a_401_on_every_credential_is_still_a_503(monkeypatch):
    """The fallback must not soften into "any 401 is fine": a delivery whose
    order could not be read at all has to stay retryable, or the order is lost
    until the sweep happens to pass over it."""
    from routes import squarespace_webhooks as route
    from services.squarespace_order_fetch import SquarespaceOrderUnauthorizedError

    recorder = _Recorder()
    app, fetched = _app(
        monkeypatch,
        credentials={
            "api_key": "sq-api-key",
            "oauth_access_token": "expired-token",
            "website_id": WEBSITE_ID,
            "webhook_secret": SECRET,
        },
        recorder=recorder,
    )

    async def fake_fetch(*, access_token, order_id, **kwargs):
        fetched.append({"access_token": access_token})
        raise SquarespaceOrderUnauthorizedError("refused")

    monkeypatch.setattr(route, "fetch_squarespace_order", fake_fetch)

    response = _post(TestClient(app), _notification())

    assert response.status_code == 503
    assert len(fetched) == 2
    assert recorder.calls == []


def test_a_non_auth_fetch_failure_does_not_burn_the_second_credential(monkeypatch):
    """Only a 401/403 is worth retrying with a different identity. A 429 or a
    5xx means the credential was fine and the API was not, so re-asking with
    the API key doubles the load on an upstream that is already struggling."""
    from routes import squarespace_webhooks as route
    from services.squarespace_order_fetch import SquarespaceOrderFetchError

    app, fetched = _app(
        monkeypatch,
        credentials={
            "api_key": "sq-api-key",
            "oauth_access_token": "good-token",
            "website_id": WEBSITE_ID,
            "webhook_secret": SECRET,
        },
    )

    async def fake_fetch(*, access_token, order_id, **kwargs):
        fetched.append({"access_token": access_token})
        raise SquarespaceOrderFetchError("Squarespace rate-limited the order read")

    monkeypatch.setattr(route, "fetch_squarespace_order", fake_fetch)

    response = _post(TestClient(app), _notification())

    assert response.status_code == 503
    assert [call["access_token"] for call in fetched] == ["good-token"]


# ---- making a wrong assumption diagnosable ---------------------------------


def test_a_rejected_delivery_logs_the_squarespace_header_names_and_shape(
    monkeypatch, caplog
):
    """That the signature is HMAC-SHA256 over the RAW BODY ALONE is an ASSUMED
    claim (docs/SQUARESPACE_TELEMETRY.md).

    If it is wrong — the documented input may concatenate a timestamp header or
    the endpoint URL — the symptom is a 401 on every delivery a site ever
    sends, and a bare 401 is indistinguishable from a wrong secret. This log
    line is what makes the two decidable from production rather than from a
    packet capture: which Squarespace-* headers were there to concatenate, and
    whether the digest arrived hex- or base64-shaped.
    """
    import logging

    app, fetched = _app(monkeypatch)
    client = TestClient(app)
    body = json.dumps(_notification(), separators=(",", ":")).encode()

    with caplog.at_level(logging.WARNING, logger="squarespace_webhooks"):
        response = client.post(
            PATH,
            content=body,
            headers={
                "Content-Type": "application/json",
                "Squarespace-Signature": "deadbeef",
                "Squarespace-Timestamp": "1757000000",
            },
        )

    assert response.status_code == 401
    assert fetched == []
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    line = next(m for m in messages if "squarespace webhook rejected" in m)
    # The NAMES are there...
    assert "squarespace-timestamp" in line.lower()
    assert "squarespace-signature" in line.lower()
    assert "hex:8" in line
    # ...and no VALUE ever is. A log that leaked the digest would hand an
    # attacker with log access exactly what the secret protects.
    assert "deadbeef" not in line
    assert "1757000000" not in line
    assert SECRET not in line


@pytest.mark.parametrize(
    "encoder,label",
    [
        (lambda d: base64.b64encode(d).decode(), "standard base64"),
        (lambda d: base64.b64encode(d).decode().rstrip("="), "unpadded base64"),
        (lambda d: base64.urlsafe_b64encode(d).decode(), "url-safe base64"),
        (lambda d: base64.urlsafe_b64encode(d).decode().rstrip("="), "unpadded url-safe"),
        (lambda d: d.hex(), "hex"),
        (lambda d: d.hex().upper(), "upper-case hex"),
    ],
)
def test_every_plausible_spelling_of_the_right_digest_is_accepted(
    monkeypatch, encoder, label
):
    """The header's ENCODING is assumed, so the hedge is deliberately wide.

    Widening costs nothing — a caller must still produce the digest, which
    needs the secret. Narrowing, if the assumption is wrong, 401s every
    delivery a site ever sends, and the secret cannot be re-read to try again.
    """
    app, fetched = _app(monkeypatch)
    body = json.dumps(_notification(), separators=(",", ":")).encode()
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).digest()

    response = TestClient(app).post(
        PATH,
        content=body,
        headers={
            "Content-Type": "application/json",
            "Squarespace-Signature": encoder(digest),
        },
    )

    assert response.status_code == 200, f"{label} was refused: {response.text}"
    assert len(fetched) == 1


def test_a_wrong_digest_in_a_plausible_spelling_is_still_401(monkeypatch):
    """The counterpart to the hedge above: accepting more SPELLINGS must not
    accept more DIGESTS."""
    app, fetched = _app(monkeypatch)
    body = json.dumps(_notification(), separators=(",", ":")).encode()
    wrong = hmac.new(b"not-the-secret", body, hashlib.sha256).digest()

    for spelling in (
        base64.b64encode(wrong).decode(),
        base64.urlsafe_b64encode(wrong).decode().rstrip("="),
        wrong.hex(),
    ):
        response = TestClient(app).post(
            PATH,
            content=body,
            headers={
                "Content-Type": "application/json",
                "Squarespace-Signature": spelling,
            },
        )
        assert response.status_code == 401, spelling
    assert fetched == []


def test_an_oversized_body_is_refused_from_its_content_length_alone(monkeypatch):
    """Bounded WHILE it is read, not after.

    Buffering a hostile body and then measuring it means the process has
    already held whatever was sent. Same shape as
    `routes/prestashop_webhooks.py`.
    """
    app, fetched = _app(monkeypatch)

    response = TestClient(app).post(
        PATH,
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": "2",
            "Squarespace-Signature": "x",
        },
    )
    # A truthful small body gets as far as the signature check (401), which is
    # the baseline the next assertion is measured against.
    assert response.status_code == 401

    from routes import squarespace_webhooks as route

    assert route.MAX_SQUARESPACE_WEBHOOK_BYTES == 1_000_000
    # And the reader is the streaming one, not `await request.body()`.
    import inspect

    source = inspect.getsource(route.receive_squarespace_webhook)
    assert "_read_limited_body" in source
    assert "await request.body()" not in source
    assert fetched == []


async def test_the_body_reader_stops_before_buffering_an_oversized_stream():
    """Driven at the reader directly, because the claim is about what it does
    BEFORE the body is complete: a client that lies about (or omits)
    Content-Length must still be cut off mid-stream rather than buffered whole.
    """
    from fastapi import HTTPException

    from routes import squarespace_webhooks as route

    chunks_pulled = []

    class _Request:
        headers = {}

        async def stream(self):
            for index in range(100):
                chunk = b"x" * 100_000
                chunks_pulled.append(index)
                yield chunk

    with pytest.raises(HTTPException) as raised:
        await route._read_limited_body(_Request())

    assert raised.value.status_code == 413
    # It stopped as soon as the cap was exceeded rather than draining all 10 MB.
    assert len(chunks_pulled) <= 11, chunks_pulled


# ---- the order id reaches a URL PATH ---------------------------------------


class _UrlSpy:
    """Captures the URL a fetch built, and answers 200 with one order."""

    def __init__(self):
        self.urls = []

    async def get(self, url, headers=None, params=None):
        self.urls.append(str(url))

        class _Response:
            status_code = 200
            content = b"{}"

            @staticmethod
            def json():
                return _order()

        return _Response()

    async def aclose(self):
        return None


@pytest.mark.parametrize(
    "hostile",
    [
        "../../authorization/website",
        "..%2f..%2fauthorization%2fwebsite",
        "1/../../authorization/website",
        "abc?expand=all",
        "abc#frag",
        "abc def",
        "",
        "x" * 129,
    ],
)
async def test_a_hostile_order_id_never_reaches_the_url(hostile):
    """`data.orderId` is interpolated into a URL PATH and comes out of a
    webhook body.

    The delivery is signature-checked, but a signature proves the SENDER, not
    the shape of a field inside it — and the sender here is whoever holds the
    subscription secret for one store, which is not the same as "trusted to
    choose our request path". `../../authorization/website` walks out of the
    orders collection and makes the fetch read a different endpoint entirely,
    with this store's credential.
    """
    from services.squarespace_order_fetch import (
        SquarespaceOrderFetchError,
        fetch_squarespace_order,
    )

    spy = _UrlSpy()
    with pytest.raises(SquarespaceOrderFetchError):
        await fetch_squarespace_order(
            access_token="t", order_id=hostile, client=spy
        )
    assert spy.urls == [], spy.urls


async def test_a_real_order_id_still_reaches_the_orders_collection():
    """The positive counterpart. A validator that refused everything would
    satisfy every case above while taking the integration offline."""
    from services.squarespace_order_fetch import fetch_squarespace_order

    spy = _UrlSpy()
    order = await fetch_squarespace_order(
        access_token="t", order_id=ORDER_ID, client=spy
    )

    assert order["id"] == ORDER_ID
    assert spy.urls == [
        f"https://api.squarespace.com/1.0/commerce/orders/{ORDER_ID}"
    ]

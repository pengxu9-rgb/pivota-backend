"""Webflow Ecommerce notifications -> the canonical commerce ledger.

`POST /webhooks/webflow/{store_id}/{url_secret}`

TWO AUTH LAYERS, BECAUSE WEBFLOW SIGNS ONLY SOME DELIVERIES.

Webflow signs a webhook with `x-webflow-timestamp` + `x-webflow-signature`
(HMAC-SHA256 over `"{timestamp}:{raw body}"`) keyed with the **OAuth App's
client secret**, and it does so ONLY for webhooks a Data Client app created.
A webhook created with a Site API token — which is how a merchant connects a
single site without Pivota shipping a Webflow app — is delivered UNSIGNED. A
signature check alone would therefore either reject every site-token store or,
if made optional, authenticate nothing at all.

So:

* **Layer 1, always.** A 256-bit per-store secret is minted at provisioning and
  embedded in the webhook URL path. It is compared constant-time, as bytes,
  against the store's credential blob. A missing or wrong secret is 401 before
  anything else happens. This is the layer that authenticates a site-token
  delivery, and it is the reason the path has a second segment at all.
* **Layer 2, when `WEBFLOW_CLIENT_SECRET` is configured.** The signature is then
  additionally required and verified with a 5-minute skew window. A deployment
  that runs a Webflow OAuth app sets it and gets replay resistance; one that
  does not runs on Layer 1 alone, and docs/WEBFLOW_TELEMETRY.md says so rather
  than implying a signature that is not being checked.

WHERE THE URL SECRET IS WRITTEN DOWN. A secret in a path lands in request logs
the way a header never does, so all three logging channels this process owns
rewrite it through `middleware/structured_logging.py::redact_path`: the app's
structured access log, the rate limiter's anonymous-ceiling warning, and
`uvicorn.access` — the last via `UvicornAccessPathRedactionFilter`, installed in
main.py at import. uvicorn's is the one that matters most and was the last to be
closed: it writes the raw request line at INFO on every 200 AND every 401,
infra/gcp/Dockerfile starts it with neither `--no-access-log` nor a
`--log-config`, and no ASGITransport test can observe it because there is no
uvicorn in that loop.

What remains is OUTSIDE this process: the platform load balancer's
`httpRequest.requestUrl`, and any proxy or APM in front of it, record the full
path whatever the app does. That single residual is the argument for configuring
`WEBFLOW_CLIENT_SECRET` wherever an OAuth app exists — a URL read out of
somebody else's access log still cannot produce a fresh signature over the body.

Rotating the URL secret (`ensure` with `rotate=true`) changes the registered
URL. In-flight deliveries to the OLD url answer 401; Webflow retries them, and
anything that never lands is recovered by the reconciliation sweep.

THE DELIVERY IS A TRIGGER, NOT A FACT. Webflow puts the whole order in the body,
and this receiver reads exactly two things out of it — the trigger type and the
order id — then FETCHES the order from
`GET /v2/sites/{site_id}/orders/{order_id}` and maps that. Layer 1 proves the
sender knows a secret; it does not make the sender's arithmetic Webflow's. The
fetch is scoped to the store's own `site_id`, so an order id belonging to
another site cannot be read through this store's credential even if a delivery
names one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

from db.database import database
from services.webflow_connection import parse_webflow_credentials, webflow_read_tokens
from services.webflow_event_adapter import (
    is_supported_webflow_trigger,
    normalize_webflow_trigger,
    webflow_order_id,
)
from services.webflow_ledger import record_webflow_order
from services.webflow_order_fetch import (
    WebflowOrderFetchError,
    WebflowOrderNotFoundError,
    WebflowOrderUnauthorizedError,
    fetch_webflow_order,
)
from services.telemetry_ingress import current_ingress, telemetry_ingress_route


# `telemetry_ingress_route` already logs every non-2xx with the write path,
# principal, status and a bounded reason. This logger exists for what it cannot
# see: which auth layer refused a delivery, and which `x-webflow-*` headers a
# rejected one actually carried. Whether Webflow signs a site-token webhook, and
# what exactly it signs, are ASSUMED claims; a wrong assumption has to be
# diagnosable from one log line rather than from a packet capture.
logger = logging.getLogger("webflow_webhooks")

router = APIRouter(prefix="/webhooks/webflow", tags=["Webflow Webhooks"])

MAX_WEBFLOW_WEBHOOK_BYTES = 1_000_000
WEBFLOW_SIGNATURE_HEADER = "x-webflow-signature"
WEBFLOW_TIMESTAMP_HEADER = "x-webflow-timestamp"
# Webflow's documented replay window is 5 minutes.
WEBFLOW_SIGNATURE_MAX_SKEW_SECONDS = 300
_UNAUTHORIZED = "Invalid Webflow webhook credentials"

# Deliveries already ingested by THIS process, keyed on a digest of the raw
# body. A bounded, per-process optimisation and nothing more: it saves a
# redundant Data API call on a redelivery.
#
# Keyed on the BODY, not on the order id: a Webflow order legitimately changes
# state several times (`pending` -> `unfulfilled` -> `refunded`) and each of
# those is a different `ecomm_order_changed` delivery for the same order. Keying
# on the order would swallow the refund. A true redelivery repeats the body
# byte-for-byte; a state change does not.
#
# The correctness guarantee is not this cache — it is the ledger's deterministic
# event ids, which hold across processes, restarts, and the sweep. Entries are
# recorded only AFTER a successful ingest, so a delivery that 503'd on the fetch
# is retried rather than swallowed.
#
# PER STORE, not one global list. A single 4096-entry LRU is a shared resource
# with no fairness: one busy store's deliveries evict every other store's within
# a few minutes, so the quiet store's redelivery — the case the cache exists for
# — is the one that always misses. A per-store budget makes the saving a
# property of the store rather than of its neighbours, and the two caps together
# still bound the whole thing (stores x entries).
_SEEN_DELIVERIES: "OrderedDict[str, OrderedDict[str, None]]" = OrderedDict()
_SEEN_DELIVERY_CAP = 512
_SEEN_DELIVERY_STORE_CAP = 64


def _remember_delivery(store_id: str, digest: str) -> None:
    bucket = _SEEN_DELIVERIES.get(store_id)
    if bucket is None:
        bucket = OrderedDict()
        _SEEN_DELIVERIES[store_id] = bucket
    bucket[digest] = None
    bucket.move_to_end(digest)
    _SEEN_DELIVERIES.move_to_end(store_id)
    while len(bucket) > _SEEN_DELIVERY_CAP:
        bucket.popitem(last=False)
    while len(_SEEN_DELIVERIES) > _SEEN_DELIVERY_STORE_CAP:
        _SEEN_DELIVERIES.popitem(last=False)


def _delivery_seen(store_id: str, digest: str) -> bool:
    return digest in (_SEEN_DELIVERIES.get(store_id) or ())


def _delivery_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


async def _read_limited_body(request: Request) -> bytes:
    """The raw body, bounded WHILE it is read rather than after.

    Buffering a body and then measuring it means a hostile sender has already
    made this process hold whatever they sent. Same shape as
    `routes/prestashop_webhooks.py`.
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_WEBFLOW_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Webflow webhook exceeds 1 MB")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length header"
            ) from exc
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_WEBFLOW_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webflow webhook exceeds 1 MB")
        body.extend(chunk)
    return bytes(body)


def webflow_bytes_equal(expected: str, supplied: str) -> bool:
    """Constant-time compare of two strings AS BYTES.

    PUBLIC, and imported by `routes/merchant_store_connections.py`: the
    provisioning route compares the same `url_secret` against the same stored
    blob, and it was doing so with a bare `hmac.compare_digest` on `str`. One
    encoding rule for one secret, in one place, rather than two compares that
    can disagree about what a non-ASCII stored value means.

    `hmac.compare_digest` raises TypeError when handed a str carrying non-ASCII
    code points, and Starlette decodes both header values and path segments as
    text — so a hostile URL segment or header would otherwise be a 500, which is
    a denial-of-service handle rather than a refusal. Encoding to UTF-8 first
    makes every input comparable and the answer always a bool.

    `surrogatepass`, not plain UTF-8, and that error handler is load-bearing.
    Plain `.encode("utf-8")` RAISES `UnicodeEncodeError` on a lone surrogate,
    and a lone surrogate is not exotic: `json.loads` happily produces one from a
    body containing `"\\ud800"`, which is exactly how the delivered `siteId`
    reaches this compare. Without it, one escape sequence in a body turns a 401
    into a 500 — the same denial-of-service handle the bytes compare exists to
    close. `services/telemetry_ingress.py` encodes its client identity the same
    way for the same reason.
    """
    if not expected or not supplied:
        return False
    return hmac.compare_digest(
        expected.encode("utf-8", "surrogatepass"),
        supplied.encode("utf-8", "surrogatepass"),
    )


def _webflow_client_secret() -> str:
    """The OAuth App client secret, or "" when this deployment has no app.

    Read per request rather than captured at import: the env is what decides
    whether Layer 2 is armed, and a deployment that adds the secret must not
    have to be restarted to start verifying signatures.
    """
    return str(os.getenv("WEBFLOW_CLIENT_SECRET") or "").strip()


def _signature_shape(value: Optional[str]) -> str:
    """`hex:<len>`, `other:<len>`, `empty` or `non-ascii` — never the value."""
    supplied = str(value or "").strip()
    if not supplied:
        return "empty"
    try:
        supplied.encode("ascii")
    except UnicodeEncodeError:
        return "non-ascii"
    if all(char in "0123456789abcdefABCDEF" for char in supplied):
        return f"hex:{len(supplied)}"
    return f"other:{len(supplied)}"


def _valid_signature(
    *, raw: bytes, timestamp: Optional[str], signature: Optional[str], secret: str
) -> bool:
    """HMAC-SHA256 over `"{timestamp}:{raw body}"`, keyed with the app secret.

    The timestamp is part of the signed input AND is checked for freshness, so a
    captured delivery cannot be replayed after the window closes. Every
    malformed input — a missing header, a non-numeric timestamp, non-ASCII bytes
    — is False rather than an exception: a hostile header must cost a 401, not a
    500.
    """
    supplied = str(signature or "").strip()
    stamp = str(timestamp or "").strip()
    if not supplied or not stamp or not secret:
        return False
    try:
        supplied.encode("ascii")
        stamp.encode("ascii")
    except UnicodeEncodeError:
        return False
    try:
        # Webflow sends epoch MILLISECONDS. A value small enough to be seconds is
        # read as seconds too: guessing wrong in that direction would reject
        # every delivery, and the freshness check is the only thing the unit
        # affects.
        numeric = int(stamp)
    except ValueError:
        return False
    sent_at = numeric / 1000.0 if numeric > 10_000_000_000 else float(numeric)
    if abs(time.time() - sent_at) > WEBFLOW_SIGNATURE_MAX_SKEW_SECONDS:
        return False
    digest = hmac.new(
        secret.encode("utf-8"), f"{stamp}:".encode("utf-8") + raw, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, supplied.lower())


def _summary(*, status_value: str, reason: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    """The one response shape this receiver answers with.

    Always carries `accepted` and `duplicates`. A delivery that mapped to nothing
    is not a different KIND of answer — it is a summary whose counts are zero —
    so a caller (and a test) reads one shape whatever happened.
    """
    body: Dict[str, Any] = {
        "status": status_value,
        "platform": "webflow",
        "accepted": 0,
        "duplicates": 0,
    }
    if reason:
        body["reason"] = reason
    body.update(extra)
    return body


def _unwrap(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The order object out of a v2 `{triggerType, payload}` envelope.

    A body that is already the bare order (the v1 shape, and what a hand-rolled
    replay tends to send) is returned as-is. Whether v2 always wraps is an
    ASSUMED claim; accepting both costs nothing and a wrong guess would make
    every delivery 422 with "missing an order id".
    """
    inner = payload.get("payload")
    return dict(inner) if isinstance(inner, dict) else payload


@router.post("/{store_id}/{url_secret}")
# Spelled as a literal, not as a module constant: the ingress ratchet
# (tests/test_telemetry_ingress.py) reads this decorator with `ast`, and a name
# it has to resolve is a name it cannot check.
@telemetry_ingress_route("webflow_webhook")
async def receive_webflow_webhook(
    store_id: str,
    url_secret: str,
    request: Request,
    signature: Optional[str] = Header(default=None, alias=WEBFLOW_SIGNATURE_HEADER),
    timestamp: Optional[str] = Header(default=None, alias=WEBFLOW_TIMESTAMP_HEADER),
):
    raw = await _read_limited_body(request)

    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'webflow'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    credentials = parse_webflow_credentials(dict(store).get("api_key") if store else None)
    expected_secret = str(credentials.get("url_secret") or "").strip()
    client_secret = _webflow_client_secret()
    signature_ok = (
        True
        if not client_secret
        else _valid_signature(
            raw=raw, timestamp=timestamp, signature=signature, secret=client_secret
        )
    )
    # Unknown store, inactive store, an unprovisioned store, a wrong URL secret
    # and (when Layer 2 is armed) a bad signature all answer the same 401: the
    # caller learns nothing about which it was.
    if not store or not webflow_bytes_equal(expected_secret, url_secret) or not signature_ok:
        logger.warning(
            "webflow webhook rejected store_id=%s store_known=%s secret_provisioned=%s "
            "url_secret_ok=%s signature_layer=%s signature_ok=%s signature_shape=%s "
            "webflow_headers=%s",
            store_id,
            bool(store),
            bool(expected_secret),
            bool(store) and webflow_bytes_equal(expected_secret, url_secret),
            "armed" if client_secret else "off",
            signature_ok,
            _signature_shape(signature),
            sorted(
                name
                for name in request.headers.keys()
                if name.lower().startswith("x-webflow")
            ),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_UNAUTHORIZED)

    store = dict(store)
    merchant_id = str(store["merchant_id"])
    ingress = current_ingress(request)
    ingress.identify(merchant_id=merchant_id, store_id=store_id)
    await ingress.enforce_rate_limit("platform", store_id)

    try:
        envelope = json.loads(raw or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Webflow webhook JSON") from exc
    if not isinstance(envelope, dict):
        raise HTTPException(status_code=400, detail="Webflow webhook body must be an object")

    trigger = str(envelope.get("triggerType") or "").strip()
    body = _unwrap(envelope)
    if not is_supported_webflow_trigger(trigger):
        # Ignored BEFORE the fetch: an unmapped trigger must not cost a Webflow
        # API call, and must not be able to drive one.
        return _summary(
            status_value="ignored",
            reason=f"unsupported Webflow trigger type: {trigger or 'missing'}",
            trigger_type=trigger or None,
        )

    # The site binding, when the delivery carries one. The URL secret is
    # per-store, so a delivery that reached here already belongs to this store;
    # this catches the configuration fault where one site's webhook was pointed
    # at another store's URL, and it is checked as BYTES so a hostile id is a
    # 401 rather than a 500. The structural half of the binding is the fetch,
    # which is scoped to this store's own `site_id` regardless.
    expected_site_id = str(credentials.get("site_id") or "").strip()
    delivered_site_id = str(envelope.get("siteId") or body.get("siteId") or "").strip()
    if delivered_site_id and not webflow_bytes_equal(expected_site_id, delivered_site_id):
        raise HTTPException(status_code=401, detail="Invalid Webflow webhook source")

    delivery_digest = _delivery_digest(raw)
    if _delivery_seen(store_id, delivery_digest):
        # `duplicates: 1`, not 0. This IS a duplicate observation — the ledger
        # would have counted it as one had the short-circuit not saved the API
        # call — and reporting zero makes the metric read as if redeliveries
        # never happen.
        return _summary(
            status_value="duplicate",
            reason="delivery_already_processed",
            trigger_type=normalize_webflow_trigger(trigger),
            duplicates=1,
        )

    order_id = webflow_order_id(body)
    if not order_id:
        raise HTTPException(status_code=422, detail="Webflow webhook is missing an order id")
    if not expected_site_id:
        # Without a site binding there is no URL to fetch the order from. A
        # store in this state was never provisioned properly; say so rather than
        # building a request out of an empty path segment.
        raise HTTPException(
            status_code=503,
            detail="This Webflow store has no site_id binding; reconnect it",
        )

    tokens = webflow_read_tokens(credentials)
    if not tokens:
        raise HTTPException(
            status_code=503, detail="Webflow order read has no usable credential"
        )
    try:
        order = await fetch_webflow_order(
            api_token=tokens[0], site_id=expected_site_id, order_id=order_id
        )
    except WebflowOrderNotFoundError as exc:
        # 503, not 2xx. A 404 is usually the read racing the delivery (the order
        # is not queryable yet), which a retry fixes; a permanently absent order
        # exhausts Webflow's retries and is then recovered by the sweep. A 200
        # here would drop the event with no second chance.
        raise HTTPException(
            status_code=503,
            detail=f"Webflow order {order_id} is not readable on this site yet",
        ) from exc
    except WebflowOrderUnauthorizedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except WebflowOrderFetchError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        result = await record_webflow_order(
            merchant_id=merchant_id,
            store_id=store_id,
            order=order,
            from_webhook=True,
            trigger_type=normalize_webflow_trigger(trigger),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _remember_delivery(store_id, delivery_digest)
    return result.as_summary(trigger_type=normalize_webflow_trigger(trigger))

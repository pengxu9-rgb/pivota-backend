"""Signed Squarespace notifications -> the canonical commerce ledger.

`POST /webhooks/squarespace/{store_id}`

Three facts shape this receiver; docs/SQUARESPACE_TELEMETRY.md carries the
verified-vs-assumed table behind each.

1. **Only OAuth-connected sites can have webhooks at all.** The Webhook
   Subscriptions API is a Developer-Platform (OAuth) surface; a per-site API
   key cannot create a subscription. A store connected with an API key alone
   never provisions a `webhook_secret`, so it can never authenticate here, and
   its telemetry arrives through the reconciliation sweep instead
   (services/squarespace_order_sweep.py). That is why an unprovisioned secret
   is a plain 401 and not an error worth distinguishing.

2. **The delivery is thin.** A notification carries `{id, topic, websiteId,
   subscriptionId, data: {orderId}}` and no order fields, so the order is read
   back (services/squarespace_order_fetch.py) before anything is mapped. A
   fetch failure answers 503, never 200: Squarespace retries a failed delivery,
   and a 200 would drop the event for good.

3. **The subscription secret is per-subscription, not per-site**, so the
   signature alone cannot prove which store a delivery is for. The body's
   `websiteId` is therefore bound to the `website_id` this store recorded at
   connect time, exactly as the BigCommerce receiver binds `producer` to the
   store hash.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections import OrderedDict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

from db.database import database
from services.squarespace_connection import parse_squarespace_credentials
from services.squarespace_event_adapter import (
    SQUARESPACE_UNINSTALL_TOPIC,
    is_supported_squarespace_topic,
    normalize_squarespace_topic,
)
from services.squarespace_ledger import record_squarespace_order
from services.squarespace_order_fetch import (
    SquarespaceOrderFetchError,
    fetch_squarespace_order,
)
from services.telemetry_ingress import current_ingress, telemetry_ingress_route


# No module logger: every non-2xx this route raises is already logged by
# `telemetry_ingress_route` with the write path, principal, status and a bounded
# reason, and the two ignore reasons here (`testmode`, an unmapped topic) are
# expected traffic rather than something to page on.
router = APIRouter(prefix="/webhooks/squarespace", tags=["Squarespace Webhooks"])

MAX_SQUARESPACE_WEBHOOK_BYTES = 1_000_000
SQUARESPACE_SIGNATURE_HEADER = "Squarespace-Signature"
_UNAUTHORIZED = "Invalid Squarespace webhook credentials"

# Notification ids already ingested by THIS process. A bounded, per-process
# optimisation and nothing more: it saves a redundant Orders API call on a
# redelivery. The correctness guarantee is the ledger's deterministic event ids
# (first-write-wins), which hold across processes, restarts, and the sweep.
# Ids are recorded only AFTER a successful ingest, so a delivery that 503'd on
# the fetch is retried rather than swallowed.
_SEEN_NOTIFICATIONS: "OrderedDict[str, None]" = OrderedDict()
_SEEN_NOTIFICATION_CAP = 4096


def _remember_notification(key: str) -> None:
    _SEEN_NOTIFICATIONS[key] = None
    _SEEN_NOTIFICATIONS.move_to_end(key)
    while len(_SEEN_NOTIFICATIONS) > _SEEN_NOTIFICATION_CAP:
        _SEEN_NOTIFICATIONS.popitem(last=False)


def _valid_signature(raw: bytes, signature: Optional[str], secret: str) -> bool:
    """Constant-time HMAC-SHA256 over the RAW body with the subscription secret.

    Both hex and base64 spellings of the same digest are accepted. The header's
    encoding is the one claim in this integration that could not be pinned down
    (see the assumed table in docs/SQUARESPACE_TELEMETRY.md); accepting either
    costs nothing, because a caller must still produce the digest itself.

    A malformed header — empty, or carrying bytes that are not ASCII — is False,
    never an exception: `hmac.compare_digest` raises TypeError on a str with
    non-ASCII code points, which would turn a hostile header into a 500.
    """
    supplied = str(signature or "").strip()
    if not supplied or not secret:
        return False
    try:
        supplied.encode("ascii")
    except UnicodeEncodeError:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if hmac.compare_digest(digest.hex(), supplied.lower()):
        return True
    return hmac.compare_digest(
        base64.b64encode(digest).decode("ascii"), supplied
    )


def _summary(
    *,
    status_value: str,
    reason: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """The one response shape this receiver answers with.

    Always carries `accepted` and `duplicates`. A delivery that mapped to
    nothing is not a different KIND of answer — it is a summary whose counts
    are zero — so a caller (and a test) reads one shape whatever happened.
    """
    body: Dict[str, Any] = {
        "status": status_value,
        "platform": "squarespace",
        "accepted": 0,
        "duplicates": 0,
    }
    if reason:
        body["reason"] = reason
    body.update(extra)
    return body


@router.post("/{store_id}")
# Spelled as a literal, not as the module constant: the ingress ratchet
# (tests/test_telemetry_ingress.py) reads this decorator with `ast` and a name
# it has to resolve is a name it cannot check.
@telemetry_ingress_route("squarespace_webhook")
async def receive_squarespace_webhook(
    store_id: str,
    request: Request,
    signature: Optional[str] = Header(default=None, alias=SQUARESPACE_SIGNATURE_HEADER),
):
    raw = await request.body()
    if len(raw) > MAX_SQUARESPACE_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Squarespace webhook exceeds 1 MB")

    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'squarespace'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    credentials = parse_squarespace_credentials(dict(store).get("api_key") if store else None)
    webhook_secret = str(credentials.get("webhook_secret") or "").strip()
    # Unknown store, inactive store, an API-key-only store with no subscription
    # secret, and a wrong signature all answer the same 401: the caller learns
    # nothing about which it was.
    if not store or not _valid_signature(raw, signature, webhook_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_UNAUTHORIZED)

    store = dict(store)
    merchant_id = str(store["merchant_id"])
    ingress = current_ingress(request)
    ingress.identify(merchant_id=merchant_id, store_id=store_id)
    await ingress.enforce_rate_limit("platform", store_id)

    try:
        payload = json.loads(raw or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Squarespace webhook JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="Squarespace webhook body must be an object"
        )

    # The subscription secret belongs to a SUBSCRIPTION, not to this store, so
    # a valid signature alone does not say which site the notification is for.
    # The site binding recorded at connect time is what makes it say so.
    expected_website_id = str(credentials.get("website_id") or "").strip()
    delivered_website_id = str(payload.get("websiteId") or "").strip()
    if (
        not expected_website_id
        or not delivered_website_id
        # Compared as BYTES: `compare_digest` raises TypeError on a str with
        # non-ASCII code points, and a 500 from a hostile id is a worse answer
        # than a 401.
        or not hmac.compare_digest(
            expected_website_id.encode("utf-8"), delivered_website_id.encode("utf-8")
        )
    ):
        raise HTTPException(status_code=401, detail="Invalid Squarespace webhook source")

    topic = str(payload.get("topic") or "").strip()
    notification_id = str(payload.get("id") or "").strip() or None
    if not is_supported_squarespace_topic(topic):
        # Ignored BEFORE the fetch: an unmapped topic must not cost a
        # Squarespace API call, and must not be able to drive one.
        # `extension.uninstall` is subscribed so the event is observable in the
        # platform's own subscription list; it names no order and disconnecting
        # the store is a merchant-facing decision, not this receiver's.
        return _summary(
            status_value="ignored",
            reason=(
                "extension_uninstall"
                if topic.lower() == SQUARESPACE_UNINSTALL_TOPIC
                else f"unsupported Squarespace webhook topic: {topic or 'missing'}"
            ),
            topic=topic or None,
        )

    seen_key = f"{store_id}:{notification_id}" if notification_id else None
    if seen_key and seen_key in _SEEN_NOTIFICATIONS:
        return _summary(
            status_value="duplicate",
            reason="notification_already_processed",
            topic=normalize_squarespace_topic(topic),
        )

    data = payload.get("data")
    order_id = str((data or {}).get("orderId") or "").strip() if isinstance(data, dict) else ""
    if not order_id:
        raise HTTPException(
            status_code=422, detail="Squarespace webhook is missing an order id"
        )

    access_token = (
        str(credentials.get("oauth_access_token") or "").strip()
        or str(credentials.get("api_key") or "").strip()
    )
    try:
        order = await fetch_squarespace_order(access_token=access_token, order_id=order_id)
    except SquarespaceOrderFetchError as exc:
        # Retryable: Squarespace redelivers a non-2xx.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        result = await record_squarespace_order(
            merchant_id=merchant_id,
            store_id=store_id,
            order=order,
            from_webhook=True,
            topic=normalize_squarespace_topic(topic),
            trace_id=notification_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if seen_key:
        _remember_notification(seen_key)
    return result.as_summary(topic=normalize_squarespace_topic(topic))

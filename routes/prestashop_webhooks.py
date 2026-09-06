"""Signed PrestaShop module deliveries -> the canonical commerce ledger.

PrestaShop has **no outbound webhooks**: no subscription API, no signed
delivery, no callback registry. Its only extension point is a hook that runs
inside the shop's own PHP process. So this receiver's counterpart is a sender
Pivota ships and the merchant installs —
``integrations/prestashop-module/pivotatelemetry/`` — exactly like the
Salesforce B2C cartridge in ``routes/sfcc_events.py``.

Two consequences the sibling receivers do not have:

* **The secret is pasted by a human.** There is no OAuth handshake to mint it
  through, so ``POST /integrations/prestashop/{store_id}/telemetry/ensure``
  returns it once, at mint time, and the merchant types it into the module's
  configuration page. See ``routes/merchant_store_connections.py``.
* **The shop names itself.** The delivery carries the shop's own base URL, in
  a header AND inside the signed body, and the receiver refuses it unless both
  hosts equal the host of the store row's ``domain`` (which
  ``merchant_connect_prestashop`` sets from ``store_url``). A module copied to
  a second shop keeps signing correctly but now says a host the store row does
  not have, and stops writing into the first shop's ledger. Same precedent as
  ``X-WC-Webhook-Source`` on WooCommerce.

Auth chain, in order: 1 MB cap -> active ``prestashop`` store -> per-store
``webhook_secret`` -> timestamp within +/-300 s -> constant-time HMAC over
``timestamp + "." + body`` -> JSON parses -> shop-url host (header and signed
body) equals the store's domain host -> identify + platform rate limit -> 1..100
events -> map -> ingest. The credential failures answer with ONE message, so a
caller never learns which of them it hit; the host mismatch has its own,
because it is a configuration error on a delivery that already proved it holds
the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request, status

from db.database import database
from services.merchant_event_ingest_service import (
    MerchantEventBatch,
    ingest_merchant_event_batch,
)
from services.prestashop_event_adapter import (
    UnsupportedPrestaShopEvent,
    map_prestashop_module_event,
)
from services.telemetry_ingress import current_ingress, telemetry_ingress_route


router = APIRouter(prefix="/webhooks/prestashop", tags=["PrestaShop Events"])

MAX_PRESTASHOP_WEBHOOK_BYTES = 1_000_000
MAX_PRESTASHOP_EVENTS_PER_BATCH = 100
MAX_PRESTASHOP_SIGNATURE_AGE_SECONDS = 300

_UNAUTHORIZED = "Invalid PrestaShop event credentials"
_WRONG_SHOP = "Invalid PrestaShop event shop"


def _credentials(raw: Any) -> Dict[str, Any]:
    """The credential JSON in ``merchant_stores.api_key``.

    ``merchant_connect_prestashop`` persists the bare Webservice key today, so
    a store connected before this PR has a plain string here and simply has no
    telemetry secret yet — which fails closed.
    """
    if isinstance(raw, dict):
        return dict(raw)
    value = str(raw or "").strip()
    if not value.startswith("{"):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _host(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    return (urlparse(raw).hostname or "").lower()


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_PRESTASHOP_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="PrestaShop event batch exceeds 1 MB")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_PRESTASHOP_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="PrestaShop event batch exceeds 1 MB")
        body.extend(chunk)
    return bytes(body)


def _verify_signature(
    raw: bytes,
    *,
    secret: str,
    signature: Optional[str],
    timestamp: Optional[str],
) -> None:
    try:
        timestamp_int = int(str(timestamp or ""))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED) from exc
    if abs(int(time.time()) - timestamp_int) > MAX_PRESTASHOP_SIGNATURE_AGE_SECONDS:
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
    supplied = str(signature or "").strip().lower()
    if not supplied.startswith("sha256="):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
    expected = hmac.new(
        secret.encode("utf-8"),
        str(timestamp_int).encode("ascii") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    # BYTES, not str. `hmac.compare_digest` raises TypeError when either str
    # holds a non-ASCII code point, and Starlette decodes header bytes as
    # latin-1 — so a header of `sha256=\xe9...` reached this line as a str the
    # comparison could not accept and became an UNAUTHENTICATED 500. Encoding
    # here turns every malformed value back into the one 401.
    try:
        candidate = supplied[7:].encode("ascii")
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED) from exc
    if not hmac.compare_digest(expected.encode("ascii"), candidate):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)


def _events(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="PrestaShop event batch must be an object")
    values = payload.get("events")
    if (
        not isinstance(values, list)
        or not values
        or len(values) > MAX_PRESTASHOP_EVENTS_PER_BATCH
    ):
        raise HTTPException(status_code=422, detail="PrestaShop batch must contain 1 to 100 events")
    if not all(isinstance(event, dict) for event in values):
        raise HTTPException(status_code=400, detail="PrestaShop events must be JSON objects")
    return [dict(event) for event in values]


@router.post("/{store_id}")
@telemetry_ingress_route("prestashop_module")
async def receive_prestashop_events(
    store_id: str,
    request: Request,
    signature: Optional[str] = Header(default=None, alias="X-Pivota-PrestaShop-Signature"),
    timestamp: Optional[str] = Header(default=None, alias="X-Pivota-PrestaShop-Timestamp"),
    delivery_id: Optional[str] = Header(default=None, alias="X-Pivota-PrestaShop-Delivery-Id"),
    shop_url: Optional[str] = Header(default=None, alias="X-Pivota-PrestaShop-Shop-Url"),
):
    raw = await _read_limited_body(request)
    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'prestashop'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    credentials = _credentials(dict(store).get("api_key") if store else None)
    secret = str(credentials.get("webhook_secret") or "").strip()
    if not store or not secret:
        # Unknown store, inactive store and unprovisioned store are one answer.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_UNAUTHORIZED)
    _verify_signature(raw, secret=secret, signature=signature, timestamp=timestamp)

    store = dict(store)
    try:
        payload = json.loads(raw or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid PrestaShop event JSON") from exc

    # The shop must be the shop this store row was connected as. The header is
    # NOT covered by the signature, so it is checked against the SIGNED body's
    # own `shop_url` as well as against the store's `domain`: all three hosts
    # must agree. That makes the binding a fact the shop stated inside the
    # signed material, not a header anyone in the path could rewrite. Same
    # precedent as `X-WC-Webhook-Source` on WooCommerce.
    expected_host = _host(store.get("domain"))
    header_host = _host(shop_url)
    body_host = _host(payload.get("shop_url") if isinstance(payload, dict) else None)
    if (
        not expected_host
        or not header_host
        or header_host != expected_host
        or body_host != expected_host
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_WRONG_SHOP)

    ingress = current_ingress(request)
    ingress.identify(merchant_id=store["merchant_id"], store_id=store_id)
    await ingress.enforce_rate_limit("platform", store_id)

    events = _events(payload)

    mapped = []
    ignored = 0
    rejected = 0
    for event in events:
        try:
            mapped.extend(
                map_prestashop_module_event(
                    event,
                    store_id=store_id,
                    delivery_id=delivery_id,
                )
            )
        except UnsupportedPrestaShopEvent:
            ignored += 1
        except ValueError:
            # A permanent schema error in ONE signed event must not poison its
            # valid siblings. The 2xx lets the module's outbox delete the
            # batch; the count stays observable without echoing the payload.
            rejected += 1
    if not mapped:
        if rejected:
            # NOT `status: "ignored"`. `TelemetryIngress.record_result`
            # short-circuits on that status and records exactly one `ignored`
            # event, so a delivery whose every event was REJECTED counted as
            # ignored and the rejection never reached the metrics. Returning
            # the normal summary shape (with `accepted = 0`) makes the ingress
            # walk its accepted/duplicate/ignored/rejected fields instead.
            return {
                "status": "rejected",
                "platform": "prestashop",
                "accepted": 0,
                "duplicates": 0,
                "events": [],
                "ignored": ignored,
                "rejected": rejected,
            }
        return {
            "status": "ignored",
            "platform": "prestashop",
            "ignored": ignored,
            "rejected": rejected,
        }
    result = await ingest_merchant_event_batch(
        merchant_id=str(store["merchant_id"]),
        batch=MerchantEventBatch(events=mapped),
        agent_identity_confidence="platform_asserted",
        write_path="prestashop_module",
    )
    return {
        "status": "recorded",
        "platform": "prestashop",
        "ignored": ignored,
        "rejected": rejected,
        **result,
    }

from __future__ import annotations

import hmac
import json
import time
from collections import deque
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from db.database import database
from services.adobe_commerce_event_adapter import (
    UnsupportedAdobeCommerceEvent,
    map_adobe_commerce_io_event,
)
from services.adobe_io_webhook_auth import (
    AdobeIOPublicKeyUnavailable,
    is_adobe_io_key_cached,
    verify_adobe_io_signature,
)
from services.merchant_event_ingest_service import MerchantEventBatch, ingest_merchant_event_batch
from services.telemetry_ingress import current_ingress, telemetry_ingress_route


router = APIRouter(prefix="/webhooks/adobe-commerce", tags=["Adobe Commerce Events"])
MAX_ADOBE_IO_WEBHOOK_BYTES = 2_000_000
MAX_ADOBE_IO_EVENTS_PER_BATCH = 100
MAX_ADOBE_IO_KEY_MISSES_PER_STORE_MINUTE = 12
_key_miss_windows: Dict[str, deque[float]] = {}


def _credentials(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    value = str(raw or "").strip()
    if not value or not value.startswith("{"):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


async def _store(store_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT store_id, merchant_id, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'magento'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    return dict(row) if row else None


def _events(payload: Any) -> List[Dict[str, Any]]:
    values = payload if isinstance(payload, list) else [payload]
    if not values or len(values) > MAX_ADOBE_IO_EVENTS_PER_BATCH:
        raise HTTPException(status_code=422, detail="Adobe I/O batch must contain 1 to 100 events")
    if not all(isinstance(event, dict) for event in values):
        raise HTTPException(status_code=400, detail="Adobe I/O events must be JSON objects")
    return [dict(event) for event in values]


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_ADOBE_IO_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Adobe I/O webhook exceeds 2 MB")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_ADOBE_IO_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Adobe I/O webhook exceeds 2 MB")
        body.extend(chunk)
    return bytes(body)


def _require_key_fetch_budget(store_id: str, paths: List[Optional[str]]) -> None:
    if all(not path or is_adobe_io_key_cached(path) for path in paths):
        return
    now = time.monotonic()
    window = _key_miss_windows.setdefault(store_id, deque())
    while window and window[0] <= now - 60:
        window.popleft()
    if len(window) >= MAX_ADOBE_IO_KEY_MISSES_PER_STORE_MINUTE:
        raise HTTPException(status_code=429, detail="Adobe I/O key verification rate exceeded")
    window.append(now)
    if len(_key_miss_windows) > 4096:
        for key in list(_key_miss_windows):
            candidate = _key_miss_windows[key]
            if not candidate or candidate[-1] <= now - 60:
                _key_miss_windows.pop(key, None)


@router.get("/{store_id}", response_class=PlainTextResponse)
async def validate_adobe_io_webhook(
    store_id: str,
    challenge: str = Query(min_length=1, max_length=256),
):
    store = await _store(store_id)
    credentials = _credentials((store or {}).get("api_key"))
    if (
        not store
        or not str(credentials.get("adobe_io_client_id") or "").strip()
        or not str(credentials.get("adobe_io_provider_source") or "").strip()
    ):
        raise HTTPException(status_code=404, detail="Adobe I/O eventing is not configured")
    return challenge


@router.post("/{store_id}")
@telemetry_ingress_route("adobe_io_events")
async def receive_adobe_commerce_event(
    store_id: str,
    request: Request,
    signature_1: Optional[str] = Header(default=None, alias="X-Adobe-Digital-Signature-1"),
    signature_2: Optional[str] = Header(default=None, alias="X-Adobe-Digital-Signature-2"),
    public_key_path_1: Optional[str] = Header(default=None, alias="X-Adobe-Public-Key1-Path"),
    public_key_path_2: Optional[str] = Header(default=None, alias="X-Adobe-Public-Key2-Path"),
    delivery_id: Optional[str] = Header(default=None, alias="X-Adobe-Delivery-Id"),
):
    raw = await _read_limited_body(request)
    store = await _store(store_id)
    credentials = _credentials((store or {}).get("api_key"))
    expected_client_id = str(credentials.get("adobe_io_client_id") or "").strip()
    expected_provider_source = str(
        credentials.get("adobe_io_provider_source") or ""
    ).strip().lower()
    if not store or not expected_client_id or not expected_provider_source:
        raise HTTPException(status_code=401, detail="Invalid Adobe I/O webhook credentials")
    try:
        payload = json.loads(raw or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Adobe I/O webhook JSON") from exc

    # Adobe's asynchronous challenge contains no commerce event and is never
    # fetched automatically here; GET challenge validation is the supported,
    # SSRF-safe activation path.
    if isinstance(payload, dict) and set(payload) == {"validationUrl"}:
        return {"status": "validation_required", "platform": "magento"}

    envelopes = _events(payload)
    if any(
        not hmac.compare_digest(
            str(envelope.get("recipientclientid") or "").strip(), expected_client_id
        )
        for envelope in envelopes
    ):
        raise HTTPException(status_code=401, detail="Invalid Adobe I/O event recipient")
    if any(
        not hmac.compare_digest(
            str(envelope.get("source") or "").strip().lower(), expected_provider_source
        )
        for envelope in envelopes
    ):
        raise HTTPException(status_code=401, detail="Invalid Adobe I/O event provider")
    _require_key_fetch_budget(store_id, [public_key_path_1, public_key_path_2])
    try:
        valid_signature = await verify_adobe_io_signature(
            raw,
            signature_1=signature_1,
            signature_2=signature_2,
            public_key_path_1=public_key_path_1,
            public_key_path_2=public_key_path_2,
        )
    except AdobeIOPublicKeyUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Adobe I/O public key service is temporarily unavailable",
        ) from exc
    if not valid_signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Adobe I/O event signature",
        )
    ingress = current_ingress(request)
    ingress.identify(merchant_id=store["merchant_id"], store_id=store_id)
    await ingress.enforce_rate_limit("platform", store_id)

    mapped = []
    ignored = 0
    for envelope in envelopes:
        try:
            mapped.extend(
                map_adobe_commerce_io_event(
                    envelope,
                    store_id=store_id,
                    delivery_id=delivery_id,
                ).events
            )
        except UnsupportedAdobeCommerceEvent:
            ignored += 1
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not mapped:
        return {"status": "ignored", "platform": "magento", "ignored": ignored}
    aggregate = {"accepted": 0, "duplicates": 0, "events": []}
    for offset in range(0, len(mapped), MAX_ADOBE_IO_EVENTS_PER_BATCH):
        result = await ingest_merchant_event_batch(
            merchant_id=str(store["merchant_id"]),
            batch=MerchantEventBatch(
                events=mapped[offset: offset + MAX_ADOBE_IO_EVENTS_PER_BATCH]
            ),
            agent_identity_confidence="platform_asserted",
            write_path="adobe_io_events",
        )
        aggregate["accepted"] += int(result.get("accepted") or 0)
        aggregate["duplicates"] += int(result.get("duplicates") or 0)
        aggregate["events"].extend(result.get("events") or [])
    return {
        "status": "recorded",
        "platform": "magento",
        "ignored": ignored,
        **aggregate,
    }

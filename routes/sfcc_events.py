from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

from db.database import database
from services.merchant_event_ingest_service import MerchantEventBatch, ingest_merchant_event_batch
from services.telemetry_ingress import current_ingress, telemetry_ingress_route
from services.sfcc_event_adapter import (
    UnsupportedSFCCEvent,
    map_sfcc_integration_event,
)


router = APIRouter(
    prefix="/webhooks/salesforce-commerce-cloud",
    tags=["Salesforce Commerce Cloud Events"],
)
MAX_SFCC_WEBHOOK_BYTES = 1_000_000
MAX_SFCC_EVENTS_PER_BATCH = 100
MAX_SFCC_SIGNATURE_AGE_SECONDS = 300


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


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_SFCC_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="SFCC event batch exceeds 1 MB")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_SFCC_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="SFCC event batch exceeds 1 MB")
        body.extend(chunk)
    return bytes(body)


def _events(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="SFCC event batch must be an object")
    values = payload.get("events")
    if not isinstance(values, list) or not values or len(values) > MAX_SFCC_EVENTS_PER_BATCH:
        raise HTTPException(status_code=422, detail="SFCC batch must contain 1 to 100 events")
    if not all(isinstance(event, dict) for event in values):
        raise HTTPException(status_code=400, detail="SFCC events must be JSON objects")
    return [dict(event) for event in values]


def _verify_event_sites(events: List[Dict[str, Any]], expected_site_id: str) -> None:
    for event in events:
        event_site_id = str(event.get("site_id") or "").strip()
        if not event_site_id or not hmac.compare_digest(expected_site_id, event_site_id):
            raise HTTPException(status_code=401, detail="Invalid SFCC event site")


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
        raise HTTPException(status_code=401, detail="Invalid SFCC event timestamp") from exc
    if abs(int(time.time()) - timestamp_int) > MAX_SFCC_SIGNATURE_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Expired SFCC event signature")
    supplied = str(signature or "").strip().lower()
    if not supplied.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Invalid SFCC event signature")
    expected = hmac.new(
        secret.encode("utf-8"),
        str(timestamp_int).encode("ascii") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, supplied[7:]):
        raise HTTPException(status_code=401, detail="Invalid SFCC event signature")


@router.post("/{store_id}")
@telemetry_ingress_route("sfcc_cartridge")
async def receive_sfcc_events(
    store_id: str,
    request: Request,
    signature: Optional[str] = Header(default=None, alias="X-Pivota-SFCC-Signature"),
    timestamp: Optional[str] = Header(default=None, alias="X-Pivota-SFCC-Timestamp"),
    delivery_id: Optional[str] = Header(default=None, alias="X-Pivota-SFCC-Delivery-Id"),
    site_id: Optional[str] = Header(default=None, alias="X-Pivota-SFCC-Site-Id"),
):
    raw = await _read_limited_body(request)
    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'salesforce_commerce_cloud'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    credentials = _credentials((dict(store).get("api_key") if store else None))
    secret = str(credentials.get("telemetry_signing_secret") or "").strip()
    expected_site_id = str(credentials.get("site_id") or "").strip()
    if not store or not secret or not expected_site_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid SFCC event credentials",
        )
    if not site_id or not hmac.compare_digest(expected_site_id, str(site_id).strip()):
        raise HTTPException(status_code=401, detail="Invalid SFCC event site")
    _verify_signature(raw, secret=secret, signature=signature, timestamp=timestamp)
    ingress = current_ingress(request)
    ingress.identify(merchant_id=dict(store)["merchant_id"], store_id=store_id)
    await ingress.enforce_rate_limit("platform", store_id)
    try:
        payload = json.loads(raw or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid SFCC event JSON") from exc
    events = _events(payload)
    _verify_event_sites(events, expected_site_id)
    mapped = []
    ignored = 0
    rejected = 0
    for event in events:
        try:
            mapped.append(
                map_sfcc_integration_event(
                    event,
                    store_id=store_id,
                    delivery_id=delivery_id,
                )
            )
        except UnsupportedSFCCEvent:
            ignored += 1
        except ValueError:
            # A permanent schema error in one signed event must not poison valid
            # siblings. The 2xx acknowledgement lets the outbox delete the batch;
            # rejected counts remain observable without echoing payload details.
            rejected += 1
    if not mapped:
        return {
            "status": "ignored",
            "platform": "salesforce_commerce_cloud",
            "ignored": ignored,
            "rejected": rejected,
        }
    result = await ingest_merchant_event_batch(
        merchant_id=str(store["merchant_id"]),
        batch=MerchantEventBatch(events=mapped),
        agent_identity_confidence="platform_asserted",
        write_path="sfcc_cartridge",
    )
    return {
        "status": "recorded",
        "platform": "salesforce_commerce_cloud",
        "ignored": ignored,
        "rejected": rejected,
        **result,
    }

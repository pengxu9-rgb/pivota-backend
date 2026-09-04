from __future__ import annotations

import hmac
import json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

from services.cafe24_event_adapter import (
    UnsupportedCafe24Event,
    extract_cafe24_mall_id,
    map_cafe24_webhook,
)
from services.cafe24_integration_service import find_cafe24_store
from services.merchant_event_ingest_service import ingest_merchant_event_batch


router = APIRouter(prefix="/webhooks/cafe24", tags=["Cafe24 Webhooks"])
MAX_CAFE24_WEBHOOK_BYTES = 1_000_000


@router.post("")
async def receive_cafe24_webhook(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_trace_id: Optional[str] = Header(default=None, alias="X-Trace-ID"),
):
    raw = await request.body()
    if len(raw) > MAX_CAFE24_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Cafe24 webhook exceeds 1 MB")
    try:
        payload = json.loads(raw or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Cafe24 webhook JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Cafe24 webhook body must be an object")

    mall_id = extract_cafe24_mall_id(payload)
    store = await find_cafe24_store(mall_id)
    expected_key = str(((store or {}).get("credentials") or {}).get("webhook_api_key") or "")
    if not store or not x_api_key or not expected_key or not hmac.compare_digest(x_api_key, expected_key):
        # Unknown store and invalid key deliberately share one response.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Cafe24 webhook credentials")

    try:
        batch = map_cafe24_webhook(
            payload,
            trace_id=x_trace_id,
            store_id=str(store["store_id"]),
        )
    except UnsupportedCafe24Event as exc:
        # Acknowledge unrelated Cafe24 app events so Cafe24 does not retry them;
        # this endpoint records only the canonical commerce subset.
        return {
            "status": "ignored",
            "platform": "cafe24",
            "reason": str(exc),
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = await ingest_merchant_event_batch(
        merchant_id=str(store["merchant_id"]),
        batch=batch,
        agent_identity_confidence="platform_asserted",
    )
    return {
        "status": "recorded",
        "platform": "cafe24",
        "mall_id": mall_id,
        **result,
    }

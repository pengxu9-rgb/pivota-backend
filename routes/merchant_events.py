from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import ValidationError

from services.merchant_event_ingest_service import MerchantEventBatch, ingest_merchant_event_batch
from services.merchant_hmac_auth import MerchantHMACAuthError, authenticate_hmac_merchant


router = APIRouter(prefix="/merchant-events/v1", tags=["Merchant Events"])

MAX_REQUEST_BYTES = 1_000_000


@router.post("/batch")
async def ingest_event_batch(
    request: Request,
    x_pivota_merchant_id: Optional[str] = Header(default=None, alias="X-Pivota-Merchant-Id"),
    x_pivota_signature: Optional[str] = Header(default=None, alias="X-Pivota-Signature"),
):
    """Ingest up to 100 canonical commerce events from any store adapter.

    The signature is HMAC-SHA256 over the exact raw body using the merchant API
    key. Each event_id is the upstream idempotency key, making whole-batch retries
    safe after a partial transport or database failure.
    """
    raw_body = await request.body()
    if len(raw_body) > MAX_REQUEST_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Request body exceeds 1 MB",
        )

    try:
        merchant = await authenticate_hmac_merchant(
            raw_body=raw_body,
            merchant_id=x_pivota_merchant_id,
            signature=x_pivota_signature,
        )
    except MerchantHMACAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    try:
        payload = json.loads(raw_body or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object")

    try:
        batch = MerchantEventBatch.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(include_context=False),
        ) from exc

    result = await ingest_merchant_event_batch(
        merchant_id=str(merchant["merchant_id"]),
        batch=batch,
    )
    return {"status": "recorded", **result}

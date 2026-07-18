"""
Conversion-report / receipt-ingest API — ADR-017 workstream (#1482).

The **primary non-custodial attribution closure**: a merchant (or their platform /
settlement rail) reports a settled order referencing the Pivota `click_id`, and
Pivota RECORDS the attribution + stamps the attributed GMV. Pivota never holds,
routes, or sees the funds (ADR-016) — it sees the *order report*, not the money.

Auth: HMAC-SHA256 of the RAW request body (hex) in `X-Pivota-Signature`, keyed by
the reporting merchant's API key; `X-Pivota-Merchant-Id` names the merchant. The
key is per-merchant and is never transmitted. Binding is idempotent on
(merchant_id, external_order_id): a replay never double-counts GMV.
"""
import hashlib
import hmac
import json
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attribution/conversions", tags=["Attribution"])


@router.post("/report")
async def report_conversion(
    request: Request,
    x_pivota_merchant_id: Optional[str] = Header(default=None, alias="X-Pivota-Merchant-Id"),
    x_pivota_signature: Optional[str] = Header(default=None, alias="X-Pivota-Signature"),
):
    """Report a settled merchant conversion for Pivota attribution — NON-CUSTODIAL.

    Body (JSON): `external_order_id` (required, the dedup key), `click_id` (the
    Pivota pvt_click_id the redirect carried — the join key), `gross_amount_cents`,
    `currency`, `converting_shop_domain` (optional).

    401 on missing/invalid signature or unknown merchant (same status — no oracle
    for which merchant ids exist). 400 on a bad body / missing `external_order_id`.
    """
    from db.merchant_onboarding import get_merchant_onboarding
    from services.commerce_attribution_service import close_external_order_conversion

    if not x_pivota_merchant_id or not x_pivota_signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Pivota-Merchant-Id or X-Pivota-Signature",
        )

    # Verify the HMAC over the RAW body BEFORE parsing/trusting anything. Read the
    # raw bytes (Starlette caches them) so the signature is over exactly what the
    # merchant signed. Unknown merchant / no api key fails the same way as a bad
    # signature — no oracle. Constant-time compare.
    raw_body = await request.body()
    merchant = await get_merchant_onboarding(x_pivota_merchant_id)
    api_key = (merchant or {}).get("api_key")
    expected = (
        hmac.new(api_key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if api_key
        else None
    )
    if not expected or not hmac.compare_digest(str(x_pivota_signature), expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    merchant_id = merchant["merchant_id"]

    try:
        payload = json.loads(raw_body or b"{}")
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object")

    external_order_id = str(payload.get("external_order_id") or "").strip()
    if not external_order_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing external_order_id")

    reported_cents = payload.get("gross_amount_cents")
    reported_cents = int(reported_cents) if reported_cents is not None else None

    # Bind the reported order to its Pivota click → attribution edge. Idempotent
    # (ON CONFLICT on (merchant_id, external_order_id)); records even an unknown
    # click (the order carries a Pivota-minted click id, so the GMV is genuinely
    # Pivota-referred). NO funds are touched here — this is pure attribution.
    result = await close_external_order_conversion(
        merchant_id=merchant_id,
        click_id=payload.get("click_id"),
        external_order_id=external_order_id,
        gross_amount_cents=reported_cents,
        currency=payload.get("currency"),
        converting_shop_domain=payload.get("converting_shop_domain"),
        note_attrs_or_payload={"source": "merchant_report"},
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing external_order_id")

    # Reconciliation (AC3): if this order was already closed with a DIFFERENT gross,
    # the merchant is reporting a value that disagrees with what Pivota recorded.
    # The first close wins (idempotent — we never overwrite); surface the mismatch
    # so under-/over-reporting is observable rather than silent.
    recorded_cents = result.get("gross_attributed_gmv_cents")
    discrepancy = bool(
        result.get("replayed")
        and reported_cents is not None
        and recorded_cents is not None
        and int(reported_cents) != int(recorded_cents)
    )
    if discrepancy:
        logger.warning(
            "conversion_report_gmv_discrepancy merchant=%s order=%s reported=%s recorded=%s",
            merchant_id, external_order_id, reported_cents, recorded_cents,
        )

    return {
        "status": "recorded",
        "edge_id": result.get("edge_id"),
        "replayed": bool(result.get("replayed")),
        "click_matched": bool(result.get("click_matched")),
        "gross_attributed_gmv_cents": recorded_cents,
        "gmv_discrepancy": discrepancy,
    }

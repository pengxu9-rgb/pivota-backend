from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from db.database import database
from utils.auth import require_admin


router = APIRouter(tags=["Admin - Partners"])


@router.get("/admin/partners/{channel_partner_id}/subsidies", response_model=None)
async def get_partner_subsidies(
    channel_partner_id: int,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    partner_row = await database.fetch_one(
        """
        SELECT id, per_brand_subsidy_cap_cents
        FROM channel_partners
        WHERE id = :channel_partner_id
        LIMIT 1
        """,
        {"channel_partner_id": channel_partner_id},
    )
    if not partner_row:
        return JSONResponse(status_code=404, content={"error": "partner_not_found"})

    cap_cents = _row_get(partner_row, "per_brand_subsidy_cap_cents")
    utilization_rows = await database.fetch_all(
        """
        SELECT
          merchant_id,
          SUM(amount_cents) AS total_issued_cents
        FROM partner_subsidy_ledger
        WHERE channel_partner_id = :channel_partner_id
        GROUP BY merchant_id
        ORDER BY total_issued_cents DESC
        """,
        {"channel_partner_id": channel_partner_id},
    )
    ledger_rows = await database.fetch_all(
        """
        SELECT
          id AS ledger_id,
          merchant_id,
          kind,
          amount_cents,
          reference_id,
          notes,
          issued_by,
          issued_at
        FROM partner_subsidy_ledger
        WHERE channel_partner_id = :channel_partner_id
        ORDER BY issued_at DESC
        """,
        {"channel_partner_id": channel_partner_id},
    )

    merchant_ids = sorted(
        {
            str(_row_get(row, "merchant_id") or "").strip()
            for row in list(utilization_rows or []) + list(ledger_rows or [])
            if str(_row_get(row, "merchant_id") or "").strip()
        }
    )
    merchant_names = await _merchant_business_names(merchant_ids)

    brand_cap_utilization = []
    for row in utilization_rows or []:
        merchant_id = str(_row_get(row, "merchant_id") or "").strip()
        total_issued_cents = _as_int(_row_get(row, "total_issued_cents"))
        brand_cap_utilization.append(
            {
                "merchant_id": merchant_id,
                "business_name": merchant_names.get(merchant_id) or merchant_id,
                "total_issued_cents": total_issued_cents,
                "cap_remaining_cents": (
                    None
                    if cap_cents is None
                    else max(_as_int(cap_cents) - total_issued_cents, 0)
                ),
            }
        )

    ledger = []
    for row in ledger_rows or []:
        merchant_id = str(_row_get(row, "merchant_id") or "").strip()
        ledger.append(
            {
                "ledger_id": int(_row_get(row, "ledger_id")),
                "merchant_id": merchant_id,
                "business_name": merchant_names.get(merchant_id) or merchant_id,
                "kind": _row_get(row, "kind"),
                "amount_cents": _as_int(_row_get(row, "amount_cents")),
                "reference_id": _row_get(row, "reference_id"),
                "notes": _row_get(row, "notes"),
                "issued_by": _row_get(row, "issued_by"),
                "issued_at": _row_get(row, "issued_at"),
            }
        )

    return {
        "per_brand_subsidy_cap_cents": cap_cents,
        "brand_cap_utilization": brand_cap_utilization,
        "ledger": ledger,
    }


async def _merchant_business_names(merchant_ids: list[str]) -> dict[str, str]:
    if not merchant_ids:
        return {}
    try:
        rows = await database.fetch_all(
            """
            SELECT merchant_id, business_name
            FROM merchants
            WHERE merchant_id = ANY(:ids)
            """,
            {"ids": merchant_ids},
        )
    except Exception:
        rows = await database.fetch_all(
            """
            SELECT merchant_id, business_name
            FROM merchant_onboarding
            WHERE merchant_id = ANY(:ids)
            """,
            {"ids": merchant_ids},
        )
    return {
        str(_row_get(row, "merchant_id")): str(_row_get(row, "business_name") or "")
        for row in rows or []
        if _row_get(row, "merchant_id")
    }


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)

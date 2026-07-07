from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db.database import database
from services import cohort_target_evaluator, settlement_file_service, subsidy_service
from utils.auth import require_admin


router = APIRouter(tags=["Admin - Partners"])


class PartnerSubsidyIssueRequest(BaseModel):
    merchant_id: str
    kind: str
    amount_cents: int
    reference_id: str | None = None
    notes: str | None = None


class StripeConnectUpsertRequest(BaseModel):
    stripe_connect_account_id: str | None = None


@router.get("/admin/partners")
async def list_admin_partners(
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """List partners with operator dashboard aggregates."""

    rows = await database.fetch_all(
        """
        SELECT
          cp.id,
          cp.legal_name,
          cp.archetype,
          cp.status,
          cp.term_start_date,
          cp.stripe_connect_account_id,
          COALESCE(abc.active_brand_count, 0) AS active_brand_count,
          COALESCE(ytd.ytd_gmv_cents, 0) AS ytd_gmv_cents
        FROM channel_partners cp
        LEFT JOIN (
          SELECT
            pa.channel_partner_id,
            COUNT(DISTINCT pa.merchant_id) AS active_brand_count
          FROM partner_attribution pa
          WHERE pa.status IN ('signed', 'active')
            AND pa.activated_at IS NOT NULL
          GROUP BY pa.channel_partner_id
        ) abc ON abc.channel_partner_id = cp.id
        LEFT JOIN (
          SELECT
            pa.channel_partner_id,
            COALESCE(SUM(mbs.gmv_usd_cents), 0) AS ytd_gmv_cents
          FROM partner_attribution pa
          JOIN monthly_brand_statements mbs ON mbs.merchant_id = pa.merchant_id
          WHERE mbs.calendar_month >= CAST(DATE_TRUNC('year', CURRENT_DATE) AS date)
            AND mbs.status IN ('frozen', 'invoiced')
          GROUP BY pa.channel_partner_id
        ) ytd ON ytd.channel_partner_id = cp.id
        ORDER BY cp.id ASC
        """
    )

    partners = []
    for row in rows or []:
        partner_id = int(_row_get(row, "id"))
        cohort_progress = await _first_open_cohort_progress(partner_id)
        ytd_gmv_raw_cents = int(_row_get(row, "ytd_gmv_cents") or 0)
        partners.append(
            {
                "id": partner_id,
                "legal_name": _row_get(row, "legal_name"),
                "archetype": _row_get(row, "archetype"),
                "status": _row_get(row, "status"),
                "term_start_date": _row_get(row, "term_start_date"),
                "active_brand_count": int(_row_get(row, "active_brand_count") or 0),
                "ytd_gmv_cents": ytd_gmv_raw_cents,
                "stripe_connect_account_id": _row_get(
                    row, "stripe_connect_account_id"
                ),
                "cohort_progress": cohort_progress,
            }
        )
    return {"partners": partners}


@router.get("/admin/partners/{channel_partner_id}", response_model=None)
async def get_admin_partner(
    channel_partner_id: int,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Fetch a single partner with dashboard aggregates and contract fields."""

    row = await database.fetch_one(
        """
        SELECT
          cp.id,
          cp.legal_name,
          cp.archetype,
          cp.status,
          cp.term_start_date,
          cp.term_months,
          cp.term_auto_renew AS auto_renew,
          cp.per_brand_tail_months,
          cp.churn_clawback_days,
          cp.nonpayment_clawback_days,
          cp.per_brand_subsidy_cap_cents,
          cp.gmv_take_rate_bp,
          cp.gmv_take_definition,
          cp.stripe_connect_account_id,
          COALESCE(abc.active_brand_count, 0) AS active_brand_count,
          COALESCE(ytd.ytd_gmv_cents, 0) AS ytd_gmv_cents
        FROM channel_partners cp
        LEFT JOIN (
          SELECT
            pa.channel_partner_id,
            COUNT(DISTINCT pa.merchant_id) AS active_brand_count
          FROM partner_attribution pa
          WHERE pa.status IN ('signed', 'active')
            AND pa.activated_at IS NOT NULL
          GROUP BY pa.channel_partner_id
        ) abc ON abc.channel_partner_id = cp.id
        LEFT JOIN (
          SELECT
            pa.channel_partner_id,
            COALESCE(SUM(mbs.gmv_usd_cents), 0) AS ytd_gmv_cents
          FROM partner_attribution pa
          JOIN monthly_brand_statements mbs ON mbs.merchant_id = pa.merchant_id
          WHERE mbs.calendar_month >= CAST(DATE_TRUNC('year', CURRENT_DATE) AS date)
            AND mbs.status IN ('frozen', 'invoiced')
          GROUP BY pa.channel_partner_id
        ) ytd ON ytd.channel_partner_id = cp.id
        WHERE cp.id = :channel_partner_id
        LIMIT 1
        """,
        {"channel_partner_id": channel_partner_id},
    )
    if not row:
        return JSONResponse(status_code=404, content={"error": "partner_not_found"})

    partner_id = int(_row_get(row, "id"))
    cohort_progress = await _first_open_cohort_progress(partner_id)
    return {
        "id": partner_id,
        "legal_name": _row_get(row, "legal_name"),
        "archetype": _row_get(row, "archetype"),
        "status": _row_get(row, "status"),
        "term_start_date": _row_get(row, "term_start_date"),
        "term_months": _row_get(row, "term_months"),
        "auto_renew": _row_get(row, "auto_renew"),
        "per_brand_tail_months": _row_get(row, "per_brand_tail_months"),
        "churn_clawback_days": _row_get(row, "churn_clawback_days"),
        "nonpayment_clawback_days": _row_get(row, "nonpayment_clawback_days"),
        "per_brand_subsidy_cap_cents": _row_get(
            row, "per_brand_subsidy_cap_cents"
        ),
        "gmv_take_rate_bp": _row_get(row, "gmv_take_rate_bp"),
        "gmv_take_definition": _row_get(row, "gmv_take_definition"),
        "stripe_connect_account_id": _row_get(row, "stripe_connect_account_id"),
        "active_brand_count": int(_row_get(row, "active_brand_count") or 0),
        "ytd_gmv_cents": int(_row_get(row, "ytd_gmv_cents") or 0),
        "cohort_progress": cohort_progress,
    }


@router.put(
    "/admin/partners/{channel_partner_id}/stripe-connect",
    response_model=None,
)
async def upsert_partner_stripe_connect(
    channel_partner_id: int,
    body: StripeConnectUpsertRequest,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Set or clear the partner's Stripe Connect account id.

    Pass `stripe_connect_account_id: null` to clear. Otherwise the value must
    look like a Stripe account id (`acct_...`).
    """

    raw = body.stripe_connect_account_id
    normalized: str | None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        normalized = None
    else:
        normalized = raw.strip()
        if not normalized.startswith("acct_") or len(normalized) < 8:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_stripe_account_id"},
            )

    existing = await database.fetch_one(
        "SELECT id FROM channel_partners WHERE id = :id LIMIT 1",
        {"id": channel_partner_id},
    )
    if not existing:
        return JSONResponse(status_code=404, content={"error": "partner_not_found"})

    if normalized is not None:
        conflict = await database.fetch_one(
            """
            SELECT id, legal_name
            FROM channel_partners
            WHERE stripe_connect_account_id = :acct
              AND id <> :id
            LIMIT 1
            """,
            {"acct": normalized, "id": channel_partner_id},
        )
        if conflict:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "stripe_account_id_already_used",
                    "conflicting_partner_id": int(_row_get(conflict, "id")),
                    "conflicting_partner_name": _row_get(conflict, "legal_name"),
                },
            )

    await database.execute(
        """
        UPDATE channel_partners
        SET stripe_connect_account_id = :acct
        WHERE id = :id
        """,
        {"acct": normalized, "id": channel_partner_id},
    )

    return await get_admin_partner(channel_partner_id, current_admin)


@router.post(
    "/admin/partners/{channel_partner_id}/subsidies",
    status_code=201,
    response_model=None,
)
async def issue_partner_subsidy(
    channel_partner_id: int,
    body: PartnerSubsidyIssueRequest,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Issue a partner subsidy through the cap-enforcing service."""

    requested_raw_cents = int(body.amount_cents)
    try:
        ledger_id = await subsidy_service.issue(
            channel_partner_id=channel_partner_id,
            merchant_id=body.merchant_id,
            kind=body.kind,
            amount_cents=requested_raw_cents,
            reference_id=body.reference_id,
            notes=body.notes,
            issued_by=str(current_admin.get("email") or ""),
        )
    except subsidy_service.SubsidyKindInvalid as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_subsidy_kind",
                "message": str(exc),
                "allowed_values": list(subsidy_service.ALLOWED_KINDS),
            },
        )
    except subsidy_service.SubsidyCapExceeded as exc:
        available_raw_cents = max(
            int(exc.cap_cents) - int(exc.already_issued_cents),
            0,
        )
        return JSONResponse(
            status_code=409,
            content={
                "error": "subsidy_cap_exceeded",
                "cap_cents": int(exc.cap_cents),
                "already_issued_cents": int(exc.already_issued_cents),
                "requested_cents": int(exc.requested_cents),
                "available_cents": available_raw_cents,
            },
        )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        return JSONResponse(
            status_code=status_code,
            content={"error": "invalid_subsidy_request", "message": str(exc)},
        )

    issued_at = await _subsidy_issued_at(ledger_id)
    return {
        "ledger_id": ledger_id,
        "issued_at": issued_at or datetime.now(timezone.utc),
    }


@router.post(
    "/admin/partners/{channel_partner_id}/settlements/{file_id}/retry",
    response_model=None,
)
async def retry_settlement_transfer(
    channel_partner_id: int,
    file_id: int,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Reset a failed settlement file to pending and retry transfer now."""

    async with database.transaction():
        file_row = await database.fetch_one(
            """
            SELECT id, channel_partner_id, transfer_status
            FROM settlement_files
            WHERE id = :file_id
              AND channel_partner_id = :channel_partner_id
            FOR UPDATE
            """,
            {"file_id": file_id, "channel_partner_id": channel_partner_id},
        )
        if not file_row:
            raise HTTPException(status_code=404, detail="Settlement file not found")

        transfer_status = str(_row_get(file_row, "transfer_status") or "")
        if transfer_status != "failed":
            return JSONResponse(
                status_code=409,
                content={
                    "error": "settlement_transfer_not_retryable",
                    "file_id": file_id,
                    "transfer_status": transfer_status,
                },
            )

        await database.execute(
            """
            UPDATE settlement_files
            SET transfer_status = 'pending',
                stripe_transfer_error = NULL
            WHERE id = :file_id
              AND transfer_status = 'failed'
            """,
            {"file_id": file_id},
        )

    await settlement_file_service.transfer(settlement_file_id=file_id)
    updated = await _settlement_file_retry_result(
        channel_partner_id=channel_partner_id,
        file_id=file_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Settlement file not found")
    return updated


@router.get(
    "/admin/partners/{channel_partner_id}/attributions",
    response_model=None,
)
async def list_partner_attributions(
    channel_partner_id: int,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """List the merchant attributions for a partner, newest registration first.

    Staff uses this to see which referred brands are still `registered` and
    awaiting a contract-signed mark, versus `signed`/`active`.
    """

    partner = await database.fetch_one(
        "SELECT id FROM channel_partners WHERE id = :id LIMIT 1",
        {"id": channel_partner_id},
    )
    if not partner:
        return JSONResponse(status_code=404, content={"error": "partner_not_found"})

    rows = await database.fetch_all(
        """
        SELECT id, merchant_id, channel_partner_id, status,
               registered_at, signed_at, activated_at, attribution_window_until
        FROM partner_attribution
        WHERE channel_partner_id = :channel_partner_id
        ORDER BY registered_at DESC, id DESC
        """,
        {"channel_partner_id": channel_partner_id},
    )
    return {"attributions": [_attribution_row(row) for row in rows or []]}


@router.post(
    "/admin/partners/{channel_partner_id}/attributions/{merchant_id}/sign",
    response_model=None,
)
async def sign_partner_attribution(
    channel_partner_id: int,
    merchant_id: str,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Mark a merchant's attribution to this partner as contract-signed.

    Transitions status `registered` -> `signed` and stamps `signed_at`.
    Idempotent: an attribution that is already `signed` or `active` returns 200
    unchanged. A `revoked`/`expired` attribution is not signable (409).

    If the brand already activated (activated_at set), signing is moot — the
    brand is already accruing — and stamping signed_at=NOW() would set it after
    activated_at, violating the migration-111 `activated_at >= signed_at` CHECK.
    So we leave signed_at untouched in that case and return the row as-is.
    """

    async with database.transaction():
        row = await database.fetch_one(
            """
            SELECT id, status, signed_at, activated_at
            FROM partner_attribution
            WHERE channel_partner_id = :channel_partner_id
              AND merchant_id = :merchant_id
            LIMIT 1
            FOR UPDATE
            """,
            {
                "channel_partner_id": channel_partner_id,
                "merchant_id": merchant_id,
            },
        )
        if not row:
            return JSONResponse(
                status_code=404,
                content={"error": "attribution_not_found"},
            )

        status = str(_row_get(row, "status") or "")
        if status in ("revoked", "expired"):
            return JSONResponse(
                status_code=409,
                content={"error": "attribution_not_signable", "status": status},
            )

        # Only stamp signed_at from a clean `registered` row that has not yet
        # activated. Everything else (already signed/active) is a no-op so the
        # call stays idempotent and constraint-safe.
        if (
            status == "registered"
            and _row_get(row, "signed_at") is None
            and _row_get(row, "activated_at") is None
        ):
            await database.execute(
                """
                UPDATE partner_attribution
                SET status = 'signed',
                    signed_at = NOW()
                WHERE id = :id
                  AND status = 'registered'
                  AND signed_at IS NULL
                  AND activated_at IS NULL
                """,
                {"id": _row_get(row, "id")},
            )

    return await _attribution_result(channel_partner_id, merchant_id)


async def _attribution_result(
    channel_partner_id: int,
    merchant_id: str,
) -> dict[str, Any] | JSONResponse:
    row = await database.fetch_one(
        """
        SELECT id, merchant_id, channel_partner_id, status,
               registered_at, signed_at, activated_at, attribution_window_until
        FROM partner_attribution
        WHERE channel_partner_id = :channel_partner_id
          AND merchant_id = :merchant_id
        LIMIT 1
        """,
        {"channel_partner_id": channel_partner_id, "merchant_id": merchant_id},
    )
    if not row:
        return JSONResponse(
            status_code=404,
            content={"error": "attribution_not_found"},
        )
    return _attribution_row(row)


def _attribution_row(row: Any) -> dict[str, Any]:
    return {
        "id": int(_row_get(row, "id")),
        "merchant_id": _row_get(row, "merchant_id"),
        "channel_partner_id": int(_row_get(row, "channel_partner_id")),
        "status": _row_get(row, "status"),
        "registered_at": _row_get(row, "registered_at"),
        "signed_at": _row_get(row, "signed_at"),
        "activated_at": _row_get(row, "activated_at"),
        "attribution_window_until": _row_get(row, "attribution_window_until"),
    }


async def _first_open_cohort_progress(
    channel_partner_id: int,
) -> dict[str, Any] | None:
    targets = await cohort_target_evaluator.get_partner_target_progress(
        channel_partner_id
    )
    for target in targets:
        if str(target.get("status") or "") == "open":
            return {
                "target_id": int(target.get("id") or target.get("target_id")),
                "target_brand_count": int(target.get("target_brand_count") or 0),
                "current_count": int(target.get("current_count") or 0),
            }
    return None


async def _subsidy_issued_at(ledger_id: int) -> Any:
    row = await database.fetch_one(
        """
        SELECT issued_at
        FROM partner_subsidy_ledger
        WHERE id = :ledger_id
        LIMIT 1
        """,
        {"ledger_id": ledger_id},
    )
    return _row_get(row, "issued_at")


async def _settlement_file_retry_result(
    *,
    channel_partner_id: int,
    file_id: int,
) -> dict[str, Any] | None:
    row = await database.fetch_one(
        """
        SELECT
          id,
          transfer_status,
          stripe_transfer_id,
          stripe_transfer_error
        FROM settlement_files
        WHERE id = :file_id
          AND channel_partner_id = :channel_partner_id
        LIMIT 1
        """,
        {"file_id": file_id, "channel_partner_id": channel_partner_id},
    )
    if not row:
        return None
    return {
        "file_id": int(_row_get(row, "id")),
        "transfer_status": _row_get(row, "transfer_status"),
        "stripe_transfer_id": _row_get(row, "stripe_transfer_id"),
        "stripe_transfer_error": _row_get(row, "stripe_transfer_error"),
    }


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)

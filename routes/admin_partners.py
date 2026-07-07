from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db.database import database
from services import cohort_target_evaluator, settlement_file_service, subsidy_service
from utils.auth import require_admin


router = APIRouter(tags=["Admin - Partners"])

# Mirrors the channel_partners / partner_rate_schedules CHECK constraints
# (migrations 108 + 125). Validated in-handler so a bad payload returns a clean
# 400 instead of surfacing a raw DB constraint violation as a 500.
_PARTNER_ARCHETYPES = frozenset(
    {
        "curated_marketplace",
        "agency",
        "affiliate",
        "platform",
        "protocol_partner",
        "other",
    }
)
_PARTNER_STATUSES = frozenset({"pending", "active", "inactive", "suspended"})
_RATE_SCOPES = frozenset({"A", "B", "C"})
_GMV_TAKE_DEFINITIONS = frozenset({"gross", "net", "channel_tiered"})

# Build-brief §6.4 default Scope-B rate table (basis points) by brand year.
# Seeded on partner creation so the rev-share engine resolves non-zero rates
# from day one — a partner with no rate rows silently earns $0 (the resolver
# returns 0 on a schedule miss).
_DEFAULT_RATE_BP: dict[str, dict[int, int]] = {
    "subscription": {1: 2700, 2: 1700, 3: 700},
    "credit_overage": {1: 1700, 2: 1200, 3: 700},
    "gmv": {1: 3000, 2: 2200, 3: 1200},
}


class PartnerSubsidyIssueRequest(BaseModel):
    merchant_id: str
    kind: str
    amount_cents: int
    reference_id: str | None = None
    notes: str | None = None


class StripeConnectUpsertRequest(BaseModel):
    stripe_connect_account_id: str | None = None


class PartnerCreateRequest(BaseModel):
    """Create a channel partner with structured contract terms.

    Only legal_name and archetype are required; every contract term defaults to
    the build-brief value (migration 125). By default the standard Scope-rate
    schedule is seeded so the partner is immediately earning-capable.
    """

    legal_name: str
    archetype: str
    contact_email: str | None = None
    status: str = "pending"
    term_start_date: date | None = None
    term_months: int = 12
    term_auto_renew: bool = True
    per_brand_tail_months: int = 36
    churn_clawback_days: int = 90
    nonpayment_clawback_days: int = 60
    per_brand_subsidy_cap_cents: int | None = 500000
    gmv_take_rate_bp: int = 1000
    active_rate_scope: str = "B"
    gmv_take_definition: str = "net"
    prepaid_credits_supported: bool = True
    monthly_overage_supported: bool = True
    seed_default_rate_schedule: bool = True


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


@router.post("/admin/partners", status_code=201, response_model=None)
async def create_admin_partner(
    body: PartnerCreateRequest,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Create a channel partner and seed its rate schedule.

    Validates the enum/range fields up front (clean 400s), guards against an
    accidental duplicate legal_name, inserts the partner, and — unless
    `seed_default_rate_schedule` is false — seeds the standard rate table under
    the partner's active scope so rev-share is non-zero from day one. All in one
    transaction: a rate-seed failure rolls back the partner insert.
    """

    legal_name = (body.legal_name or "").strip()
    error = _validate_partner_create(body, legal_name)
    if error is not None:
        return error

    # Guard against double-submit / duplicate onboarding. inactive partners are
    # ignored so a name can be reused after a partner is retired.
    duplicate = await database.fetch_one(
        """
        SELECT id
        FROM channel_partners
        WHERE lower(legal_name) = lower(:legal_name)
          AND status <> 'inactive'
        LIMIT 1
        """,
        {"legal_name": legal_name},
    )
    if duplicate:
        return JSONResponse(
            status_code=409,
            content={
                "error": "partner_already_exists",
                "existing_partner_id": int(_row_get(duplicate, "id")),
            },
        )

    async with database.transaction():
        created = await database.fetch_one(
            """
            INSERT INTO channel_partners (
              legal_name, contact_email, archetype, status,
              term_start_date, term_months, term_auto_renew,
              per_brand_tail_months, churn_clawback_days, nonpayment_clawback_days,
              per_brand_subsidy_cap_cents, gmv_take_rate_bp, active_rate_scope,
              gmv_take_definition, prepaid_credits_supported, monthly_overage_supported
            ) VALUES (
              :legal_name, :contact_email, :archetype, :status,
              COALESCE(:term_start_date, CURRENT_DATE), :term_months, :term_auto_renew,
              :per_brand_tail_months, :churn_clawback_days, :nonpayment_clawback_days,
              :per_brand_subsidy_cap_cents, :gmv_take_rate_bp, :active_rate_scope,
              :gmv_take_definition, :prepaid_credits_supported, :monthly_overage_supported
            )
            RETURNING id, term_start_date
            """,
            {
                "legal_name": legal_name,
                "contact_email": (body.contact_email or None),
                "archetype": body.archetype,
                "status": body.status,
                "term_start_date": body.term_start_date,
                "term_months": body.term_months,
                "term_auto_renew": body.term_auto_renew,
                "per_brand_tail_months": body.per_brand_tail_months,
                "churn_clawback_days": body.churn_clawback_days,
                "nonpayment_clawback_days": body.nonpayment_clawback_days,
                "per_brand_subsidy_cap_cents": body.per_brand_subsidy_cap_cents,
                "gmv_take_rate_bp": body.gmv_take_rate_bp,
                "active_rate_scope": body.active_rate_scope,
                "gmv_take_definition": body.gmv_take_definition,
                "prepaid_credits_supported": body.prepaid_credits_supported,
                "monthly_overage_supported": body.monthly_overage_supported,
            },
        )
        new_partner_id = int(_row_get(created, "id"))
        effective_from = _row_get(created, "term_start_date")

        seeded_rate_count = 0
        if body.seed_default_rate_schedule:
            for stream, brand_year, rate_bp in _default_rate_rows(
                body.gmv_take_definition
            ):
                # RETURNING + fetch_one so the reported count reflects rows
                # actually inserted, not attempted — ON CONFLICT DO NOTHING
                # yields no row when a schedule already exists.
                inserted = await database.fetch_one(
                    """
                    INSERT INTO partner_rate_schedules (
                      channel_partner_id, scope, stream, brand_year,
                      rate_bp, effective_from, notes
                    ) VALUES (
                      :channel_partner_id, :scope, :stream, :brand_year,
                      :rate_bp, :effective_from,
                      'Seeded on partner creation (build brief §6.4 defaults)'
                    )
                    ON CONFLICT
                      (channel_partner_id, scope, stream, brand_year, effective_from)
                    DO NOTHING
                    RETURNING id
                    """,
                    {
                        "channel_partner_id": new_partner_id,
                        "scope": body.active_rate_scope,
                        "stream": stream,
                        "brand_year": brand_year,
                        "rate_bp": rate_bp,
                        "effective_from": effective_from,
                    },
                )
                if inserted is not None:
                    seeded_rate_count += 1

    partner = await get_admin_partner(new_partner_id, current_admin)
    if isinstance(partner, JSONResponse):
        return partner
    partner["seeded_rate_schedule_count"] = seeded_rate_count
    return partner


def _validate_partner_create(
    body: PartnerCreateRequest,
    legal_name: str,
) -> JSONResponse | None:
    if not legal_name:
        return _bad_request("legal_name_required", "legal_name must be non-empty")
    if body.archetype not in _PARTNER_ARCHETYPES:
        return _bad_request(
            "invalid_archetype",
            "archetype must be one of the allowed values",
            allowed=sorted(_PARTNER_ARCHETYPES),
        )
    if body.status not in _PARTNER_STATUSES:
        return _bad_request(
            "invalid_status",
            "status must be one of the allowed values",
            allowed=sorted(_PARTNER_STATUSES),
        )
    if body.active_rate_scope not in _RATE_SCOPES:
        return _bad_request(
            "invalid_active_rate_scope",
            "active_rate_scope must be A, B, or C",
            allowed=sorted(_RATE_SCOPES),
        )
    if body.gmv_take_definition not in _GMV_TAKE_DEFINITIONS:
        return _bad_request(
            "invalid_gmv_take_definition",
            "gmv_take_definition must be one of the allowed values",
            allowed=sorted(_GMV_TAKE_DEFINITIONS),
        )
    if not (0 <= body.gmv_take_rate_bp <= 10000):
        return _bad_request(
            "invalid_gmv_take_rate_bp",
            "gmv_take_rate_bp must be between 0 and 10000",
        )
    if body.term_months <= 0:
        return _bad_request("invalid_term_months", "term_months must be positive")
    if body.per_brand_tail_months <= 0:
        return _bad_request(
            "invalid_per_brand_tail_months",
            "per_brand_tail_months must be positive",
        )
    if body.churn_clawback_days <= 0 or body.nonpayment_clawback_days <= 0:
        return _bad_request(
            "invalid_clawback_days",
            "churn_clawback_days and nonpayment_clawback_days must be positive",
        )
    if (
        body.per_brand_subsidy_cap_cents is not None
        and body.per_brand_subsidy_cap_cents < 0
    ):
        return _bad_request(
            "invalid_subsidy_cap",
            "per_brand_subsidy_cap_cents must be null or non-negative",
        )
    if not (body.prepaid_credits_supported or body.monthly_overage_supported):
        return _bad_request(
            "invalid_billing_mode",
            "at least one of prepaid_credits_supported / "
            "monthly_overage_supported must be true",
        )
    return None


def _bad_request(
    error: str,
    message: str,
    *,
    allowed: list[str] | None = None,
) -> JSONResponse:
    content: dict[str, Any] = {"error": error, "message": message}
    if allowed is not None:
        content["allowed_values"] = allowed
    return JSONResponse(status_code=400, content=content)


def _default_rate_rows(gmv_take_definition: str) -> list[tuple[str, int, int]]:
    """Return (stream, brand_year, rate_bp) rows for the default seed.

    The GMV stream depends on gmv_take_definition: gross/net resolve `gmv_take`,
    channel_tiered resolves `gmv_take_personal` + `gmv_take_third_party`. Seeding
    the wrong stream would leave the GMV share at 0.
    """

    rows: list[tuple[str, int, int]] = []
    for stream in ("subscription", "credit_overage"):
        for brand_year, rate_bp in _DEFAULT_RATE_BP[stream].items():
            rows.append((stream, brand_year, rate_bp))

    if gmv_take_definition == "channel_tiered":
        gmv_streams = ("gmv_take_personal", "gmv_take_third_party")
    else:
        gmv_streams = ("gmv_take",)
    for stream in gmv_streams:
        for brand_year, rate_bp in _DEFAULT_RATE_BP["gmv"].items():
            rows.append((stream, brand_year, rate_bp))
    return rows


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

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config.settings import settings
from db.database import database
from utils.auth import require_admin
from utils.email_sender import mask_email, send_email


router = APIRouter(tags=["Admin - Partners"])

_VALID_TEMPLATES = {
    "settlement_monthly",
    "settlement_skipped",
    "settlement_failed_notice",
}


# ── Pydantic models ────────────────────────────────────────────────────────────

class ContactUpsertRequest(BaseModel):
    contact_name: str | None = None
    contact_role: str | None = None
    contact_email: str | None = None
    cc_emails: list[str] | None = None
    internal_notes: str | None = None


class SendSettlementRequest(BaseModel):
    to: str
    cc: list[str] = []
    subject: str | None = None
    body: str | None = None
    template_id: str = "settlement_monthly"
    schedule: str = "now"


# ── Contact endpoints ──────────────────────────────────────────────────────────

@router.get("/admin/partners/{channel_partner_id}/contact")
async def get_partner_contact(
    channel_partner_id: int,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Return the contact record; if none exists return empty defaults."""

    row = await database.fetch_one(
        """
        SELECT
          contact_name,
          contact_role,
          contact_email,
          cc_emails,
          internal_notes,
          internal_notes_updated_by,
          internal_notes_updated_at
        FROM partner_contacts
        WHERE channel_partner_id = :channel_partner_id
        LIMIT 1
        """,
        {"channel_partner_id": channel_partner_id},
    )
    if not row:
        return {
            "channel_partner_id": channel_partner_id,
            "contact_name": None,
            "contact_role": None,
            "contact_email": None,
            "cc_emails": [],
            "internal_notes": None,
            "internal_notes_updated_by": None,
            "internal_notes_updated_at": None,
        }
    return {
        "channel_partner_id": channel_partner_id,
        "contact_name": _row_get(row, "contact_name"),
        "contact_role": _row_get(row, "contact_role"),
        "contact_email": _row_get(row, "contact_email"),
        "cc_emails": _coerce_json_list(_row_get(row, "cc_emails")),
        "internal_notes": _row_get(row, "internal_notes"),
        "internal_notes_updated_by": _row_get(row, "internal_notes_updated_by"),
        "internal_notes_updated_at": _row_get(row, "internal_notes_updated_at"),
    }


@router.put("/admin/partners/{channel_partner_id}/contact", response_model=None)
async def upsert_partner_contact(
    channel_partner_id: int,
    body: ContactUpsertRequest,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Upsert the partner contact record. Only supplied fields are updated."""

    # Validate emails
    if body.contact_email is not None and "@" not in body.contact_email:
        return JSONResponse(status_code=400, content={"error": "invalid_email"})
    if body.cc_emails is not None:
        if len(body.cc_emails) > 5:
            return JSONResponse(status_code=400, content={"error": "too_many_cc_emails"})
        for addr in body.cc_emails:
            if "@" not in addr:
                return JSONResponse(status_code=400, content={"error": "invalid_email"})

    admin_email = str(current_admin.get("email") or "")
    now = datetime.now(timezone.utc)

    # Fetch existing row to detect internal_notes change
    existing = await database.fetch_one(
        "SELECT internal_notes FROM partner_contacts WHERE channel_partner_id = :id LIMIT 1",
        {"id": channel_partner_id},
    )
    existing_notes = _row_get(existing, "internal_notes") if existing else None
    notes_changed = (
        body.internal_notes is not None and body.internal_notes != existing_notes
    )

    cc_json = json.dumps(body.cc_emails) if body.cc_emails is not None else None

    if existing is None:
        # INSERT
        await database.execute(
            """
            INSERT INTO partner_contacts (
              channel_partner_id,
              contact_name,
              contact_role,
              contact_email,
              cc_emails,
              internal_notes,
              internal_notes_updated_by,
              internal_notes_updated_at
            ) VALUES (
              :channel_partner_id,
              :contact_name,
              :contact_role,
              :contact_email,
              COALESCE(CAST(:cc_emails AS jsonb), '[]'::jsonb),
              :internal_notes,
              :notes_updated_by,
              :notes_updated_at
            )
            """,
            {
                "channel_partner_id": channel_partner_id,
                "contact_name": body.contact_name,
                "contact_role": body.contact_role,
                "contact_email": body.contact_email,
                "cc_emails": cc_json,
                "internal_notes": body.internal_notes,
                "notes_updated_by": admin_email if notes_changed else None,
                "notes_updated_at": now if notes_changed else None,
            },
        )
    else:
        # UPDATE — build SET clauses only for supplied fields
        set_clauses: list[str] = []
        params: dict[str, Any] = {"channel_partner_id": channel_partner_id}

        if body.contact_name is not None:
            set_clauses.append("contact_name = :contact_name")
            params["contact_name"] = body.contact_name
        if body.contact_role is not None:
            set_clauses.append("contact_role = :contact_role")
            params["contact_role"] = body.contact_role
        if body.contact_email is not None:
            set_clauses.append("contact_email = :contact_email")
            params["contact_email"] = body.contact_email
        if body.cc_emails is not None:
            set_clauses.append("cc_emails = CAST(:cc_emails AS jsonb)")
            params["cc_emails"] = cc_json
        if body.internal_notes is not None:
            set_clauses.append("internal_notes = :internal_notes")
            params["internal_notes"] = body.internal_notes
            if notes_changed:
                set_clauses.append("internal_notes_updated_by = :notes_updated_by")
                set_clauses.append("internal_notes_updated_at = :notes_updated_at")
                params["notes_updated_by"] = admin_email
                params["notes_updated_at"] = now

        if set_clauses:
            await database.execute(
                f"""
                UPDATE partner_contacts
                SET {", ".join(set_clauses)}
                WHERE channel_partner_id = :channel_partner_id
                """,
                params,
            )

    return await get_partner_contact(channel_partner_id, current_admin)


# ── Send log endpoint ──────────────────────────────────────────────────────────

@router.get("/admin/partners/{channel_partner_id}/sends")
async def list_partner_sends(
    channel_partner_id: int,
    limit: int = Query(10, ge=1, le=50),
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """List outbound settlement emails sent to this partner (emails masked)."""

    rows = await database.fetch_all(
        """
        SELECT
          id,
          settlement_file_id,
          template_id,
          to_email,
          cc_emails,
          subject,
          sent_by,
          sent_at,
          send_status,
          send_error,
          provider_message_id
        FROM partner_send_log
        WHERE channel_partner_id = :channel_partner_id
        ORDER BY sent_at DESC
        LIMIT :limit
        """,
        {"channel_partner_id": channel_partner_id, "limit": limit},
    )
    sends = []
    for row in rows or []:
        raw_to = str(_row_get(row, "to_email") or "")
        raw_cc = _coerce_json_list(_row_get(row, "cc_emails"))
        sends.append(
            {
                "id": int(_row_get(row, "id")),
                "settlement_file_id": _row_get(row, "settlement_file_id"),
                "template_id": _row_get(row, "template_id"),
                "to_email": mask_email(raw_to) if raw_to else "",
                "cc_emails": [mask_email(e) for e in raw_cc],
                "subject": _row_get(row, "subject"),
                "sent_by": _row_get(row, "sent_by"),
                "sent_at": _row_get(row, "sent_at"),
                "send_status": _row_get(row, "send_status"),
                "send_error": _row_get(row, "send_error"),
                "provider_message_id": _row_get(row, "provider_message_id"),
            }
        )
    return {"sends": sends}


# ── Send settlement endpoint ───────────────────────────────────────────────────

@router.post(
    "/admin/partners/{channel_partner_id}/settlements/{file_id}/send",
    response_model=None,
)
async def send_settlement_email(
    channel_partner_id: int,
    file_id: int,
    body: SendSettlementRequest,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Email a settlement statement to the partner and log the send."""

    if body.schedule != "now":
        return JSONResponse(
            status_code=400,
            content={"error": "schedule_cron_not_supported",
                     "message": "Only schedule='now' is supported in v1."},
        )

    if "@" not in body.to:
        return JSONResponse(status_code=400, content={"error": "invalid_to_email"})
    for addr in body.cc:
        if "@" not in addr:
            return JSONResponse(status_code=400, content={"error": "invalid_cc_email"})

    if body.template_id not in _VALID_TEMPLATES:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_template_id",
                "allowed": sorted(_VALID_TEMPLATES),
            },
        )

    # Fetch settlement file (scoped to partner)
    file_row = await database.fetch_one(
        """
        SELECT
          id,
          calendar_month,
          transfer_amount_cents,
          net_before_carryover_cents,
          transfer_status
        FROM settlement_files
        WHERE id = :file_id
          AND channel_partner_id = :channel_partner_id
        LIMIT 1
        """,
        {"file_id": file_id, "channel_partner_id": channel_partner_id},
    )
    if not file_row:
        return JSONResponse(
            status_code=404,
            content={"error": "settlement_file_not_found"},
        )

    subject = body.subject or _build_subject(body.template_id, file_row)
    text_body = body.body or _build_body(body.template_id, file_row)

    result = send_email(
        to_email=body.to,
        subject=subject,
        text_body=text_body,
        from_email=str(getattr(settings, "from_email", "") or ""),
        from_name="Pivota",
    )

    send_status = "sent" if result.ok else "failed"
    sent_at = datetime.now(timezone.utc)
    admin_email = str(current_admin.get("email") or "")

    log_row = await database.fetch_one(
        """
        INSERT INTO partner_send_log (
          channel_partner_id,
          settlement_file_id,
          template_id,
          to_email,
          cc_emails,
          subject,
          body_text,
          sent_by,
          sent_at,
          provider_message_id,
          send_status,
          send_error
        ) VALUES (
          :channel_partner_id,
          :settlement_file_id,
          :template_id,
          :to_email,
          CAST(:cc_emails AS jsonb),
          :subject,
          :body_text,
          :sent_by,
          :sent_at,
          :provider_message_id,
          :send_status,
          :send_error
        )
        RETURNING id
        """,
        {
            "channel_partner_id": channel_partner_id,
            "settlement_file_id": file_id,
            "template_id": body.template_id,
            "to_email": body.to,
            "cc_emails": json.dumps(body.cc),
            "subject": subject,
            "body_text": text_body,
            "sent_by": admin_email,
            "sent_at": sent_at,
            "provider_message_id": result.message_id,
            "send_status": send_status,
            "send_error": result.error if not result.ok else None,
        },
    )

    return {
        "send_id": int(_row_get(log_row, "id")),
        "send_status": send_status,
        "provider_message_id": result.message_id,
        "sent_at": sent_at,
    }


# ── Template helpers ───────────────────────────────────────────────────────────

def _build_subject(template_id: str, file_row: Any) -> str:
    month_label = _month_label(file_row)
    transfer_cents = int(_row_get(file_row, "transfer_amount_cents") or 0)
    amount_str = f"${transfer_cents / 100:,.2f}"
    if template_id == "settlement_monthly":
        return f"Your {month_label} Pivota settlement — {amount_str} transferred"
    if template_id == "settlement_skipped":
        return f"Your {month_label} Pivota settlement — no transfer this month"
    return f"Your {month_label} Pivota settlement — transfer pending"


def _build_body(template_id: str, file_row: Any) -> str:
    month_label = _month_label(file_row)
    transfer_cents = int(_row_get(file_row, "transfer_amount_cents") or 0)
    net_cents = int(_row_get(file_row, "net_before_carryover_cents") or 0)

    if template_id == "settlement_monthly":
        return (
            f"Hi,\n\n"
            f"Your {month_label} Pivota settlement has been processed.\n\n"
            f"  Transfer amount: ${transfer_cents / 100:,.2f}\n\n"
            f"Questions? Reply to this email or contact your Pivota partner manager.\n\n"
            f"— Pivota"
        )
    if template_id == "settlement_skipped":
        shortfall = abs(net_cents)
        return (
            f"Hi,\n\n"
            f"No funds were transferred for {month_label}.\n\n"
            f"Your net settlement for this period was negative "
            f"(${shortfall / 100:,.2f} shortfall). "
            f"This amount has been carried forward and will be applied "
            f"against next month's settlement.\n\n"
            f"— Pivota"
        )
    return (
        f"Hi,\n\n"
        f"Your {month_label} Pivota settlement encountered an issue during transfer. "
        f"Our team is looking into it and will follow up shortly.\n\n"
        f"— Pivota"
    )


def _month_label(file_row: Any) -> str:
    value = _row_get(file_row, "calendar_month")
    if isinstance(value, (datetime,)):
        return value.strftime("%B %Y")
    if hasattr(value, "strftime"):
        return value.strftime("%B %Y")
    if isinstance(value, str) and len(value) >= 7:
        try:
            from datetime import date
            d = date.fromisoformat(value[:10])
            return d.strftime("%B %Y")
        except ValueError:
            return str(value)
    return str(value or "")


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _coerce_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
            if isinstance(loaded, list):
                return [str(v) for v in loaded]
        except json.JSONDecodeError:
            pass
    return []


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)

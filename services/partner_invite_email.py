"""Send a channel partner their invite link + how-it-works blurb by email.

Called best-effort from the invite-token issue route so generating a link also
emails it to the partner's contact address. Never raises to the caller — the
route surfaces the outcome (sent / no-contact / error) so the operator knows
whether they still need to send the link manually.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any

from db.database import database
from utils.email_sender import mask_email, send_email


def _from_email() -> str:
    # send_email has no from-address default (returns FROM_EMAIL_MISSING when
    # unset), so pass one explicitly — same env + fallback the other Pivota
    # email paths use (order confirmations, settlement statements).
    return (os.getenv("FROM_EMAIL") or "noreply@pivota.ai").strip()


logger = logging.getLogger(__name__)


async def send_invite_email(
    *,
    channel_partner_id: int,
    signup_url: str,
    expires_at: Any,
    issued_by: str = "",
) -> dict[str, Any]:
    """Email the partner's contact their invite link. Best-effort.

    Returns {email_sent, recipient?, reason?} — reason is set when we couldn't
    send (e.g. no contact email on file, or a provider error).
    """

    try:
        partner = await database.fetch_one(
            "SELECT legal_name FROM channel_partners WHERE id = :id LIMIT 1",
            {"id": int(channel_partner_id)},
        )
        if not partner:
            return {"email_sent": False, "reason": "partner_not_found"}
        partner_name = str(_row_get(partner, "legal_name") or "there")

        contact = await database.fetch_one(
            """
            SELECT contact_email, contact_name
            FROM partner_contacts
            WHERE channel_partner_id = :id
            LIMIT 1
            """,
            {"id": int(channel_partner_id)},
        )
        contact_email = str(
            (_row_get(contact, "contact_email") if contact else "") or ""
        ).strip()
        if not contact_email:
            return {"email_sent": False, "reason": "no_contact_email"}
        contact_name = str(
            (_row_get(contact, "contact_name") if contact else "") or ""
        ).strip()

        greeting_name = contact_name or partner_name
        subject = "Your Pivota partner invite link"
        text_body = _compose_text(
            greeting_name=greeting_name,
            partner_name=partner_name,
            signup_url=signup_url,
            expires_on=_fmt_date(expires_at),
        )

        result = send_email(
            to_email=contact_email,
            subject=subject,
            text_body=text_body,
            from_email=_from_email(),
            from_name="Pivota Partnerships",
            tags={"kind": "partner_invite", "partner_id": str(channel_partner_id)},
        )
        ok = bool(getattr(result, "ok", False))
        # Record in partner_send_log so the invite shows in "Recent sends".
        await _record_send_log(
            channel_partner_id=channel_partner_id,
            to_email=contact_email,
            subject=subject,
            body_text=text_body,
            sent_by=issued_by,
            ok=ok,
            provider_message_id=getattr(result, "message_id", None),
            error=getattr(result, "error", None),
        )
        if ok:
            logger.info(
                "partner invite email sent partner_id=%s to=%s",
                channel_partner_id,
                mask_email(contact_email),
            )
            return {"email_sent": True, "recipient": contact_email}
        return {
            "email_sent": False,
            "recipient": contact_email,
            "reason": getattr(result, "error", None) or "send_failed",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "partner invite email failed partner_id=%s: %s",
            channel_partner_id,
            exc,
            exc_info=True,
        )
        return {"email_sent": False, "reason": "exception"}


async def _record_send_log(
    *,
    channel_partner_id: int,
    to_email: str,
    subject: str,
    body_text: str,
    sent_by: str,
    ok: bool,
    provider_message_id: str | None,
    error: str | None,
) -> None:
    """Best-effort partner_send_log row so the invite appears in Recent sends.

    Never raises to the caller — a log-write failure must not affect the email
    result. cc_emails defaults to '[]' (no bind), avoiding the param-then-
    double-colon jsonb cast footgun (use CAST(... AS JSONB) if a bind is ever
    needed).
    """

    try:
        await database.execute(
            """
            INSERT INTO partner_send_log (
              channel_partner_id, template_id, to_email, subject,
              body_text, sent_by, send_status, provider_message_id, send_error
            ) VALUES (
              :channel_partner_id, 'partner_invite', :to_email, :subject,
              :body_text, :sent_by, :send_status, :provider_message_id, :send_error
            )
            """,
            {
                "channel_partner_id": int(channel_partner_id),
                "to_email": to_email,
                "subject": subject,
                "body_text": body_text,
                "sent_by": (sent_by or "").strip() or "system",
                "send_status": "sent" if ok else "failed",
                "provider_message_id": provider_message_id,
                "send_error": None if ok else error,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "partner invite send-log write failed partner_id=%s",
            channel_partner_id,
            exc_info=True,
        )


def _compose_text(
    *,
    greeting_name: str,
    partner_name: str,
    signup_url: str,
    expires_on: str | None,
) -> str:
    valid_line = (
        f"This link is valid until {expires_on}.\n"
        if expires_on
        else ""
    )
    return (
        f"Hi {greeting_name},\n\n"
        f"Here is your Pivota invite link for onboarding the brands "
        f"{partner_name} is bringing on:\n\n"
        f"{signup_url}\n\n"
        "How it works:\n"
        "- Share this link with the brands you're onboarding. When a brand "
        "completes signup through it, they're automatically credited to you — "
        "that attribution is what drives your rev-share, so it matters that "
        "they use this exact link rather than signing up directly.\n"
        "- The same link works for every brand you bring on — you don't need a "
        "new one per brand.\n"
        f"{valid_line}"
        "\nOnce a brand signs up, they'll appear on your partner account and "
        "we'll take it from there. Any questions, just reply.\n\n"
        "Best,\n"
        "The Pivota Partnerships team\n"
    )


def _fmt_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] or None


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)

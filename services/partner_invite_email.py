"""Send a channel partner their invite link + how-it-works blurb by email.

Called best-effort from the invite-token issue route so generating a link also
emails it to the partner's contact address. Never raises to the caller — the
route surfaces the outcome (sent / no-contact / error) so the operator knows
whether they still need to send the link manually.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from db.database import database
from utils.email_sender import mask_email, send_email


logger = logging.getLogger(__name__)


async def send_invite_email(
    *,
    channel_partner_id: int,
    signup_url: str,
    expires_at: Any,
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
            from_name="Pivota Partnerships",
            tags={"kind": "partner_invite", "partner_id": str(channel_partner_id)},
        )
        if getattr(result, "ok", False):
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

"""
Operator notification for self-serve agent registration.

Registration at POST /agent/account/register is instant and unreviewed. That is the intended
onboarding (no approval gate), but ops still needs to SEE each new agent — to upgrade a launch
partner's tier, catch abuse, or reach out. This posts one JSON message per registration to
AGENT_REGISTRATION_NOTIFY_WEBHOOK_URL (Slack incoming-webhook compatible: `text` + structured
fields). Unset = silent. Never raises and never delays the caller beyond the short timeout:
registration must not fail because a chat webhook is down.

Never includes the API key. Only identity/attribution fields.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 4.0


def _webhook_url() -> Optional[str]:
    raw = (os.getenv("AGENT_REGISTRATION_NOTIFY_WEBHOOK_URL") or "").strip()
    return raw or None


def build_registration_notice(
    *,
    agent_id: str,
    agent_name: str,
    email: str,
    company: Optional[str],
    client_ip: str,
    key_sync_source: str,
) -> dict:
    company_text = (company or "").strip() or "—"
    text = (
        f"New agent registered: {agent_name} ({agent_id})\n"
        f"email: {email} · company: {company_text} · ip: {client_ip} · key path: {key_sync_source}"
    )
    return {
        "text": text,
        "event": "agent.registered",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "email": email,
        "company": company_text,
        "client_ip": client_ip,
        "key_sync_source": key_sync_source,
    }


async def notify_agent_registered(
    *,
    agent_id: str,
    agent_name: str,
    email: str,
    company: Optional[str],
    client_ip: str,
    key_sync_source: str,
) -> bool:
    """Post the notice. Returns True only when the webhook accepted it; False otherwise."""
    url = _webhook_url()
    if not url:
        return False
    payload = build_registration_notice(
        agent_id=agent_id,
        agent_name=agent_name,
        email=email,
        company=company,
        client_ip=client_ip,
        key_sync_source=key_sync_source,
    )
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
        if 200 <= resp.status_code < 300:
            return True
        logger.warning("agent registration notify rejected: status=%s", resp.status_code)
        return False
    except Exception as exc:  # noqa: BLE001 — a notification must never break registration
        logger.warning("agent registration notify failed: %s", exc)
        return False

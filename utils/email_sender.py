"""
Email sending utilities.

Primary provider: Amazon SES (via boto3).

Design goals:
- Avoid logging PII (email addresses, reset links, OTP codes).
- Return structured results so callers can choose fail-open vs fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
import os
from typing import Any, Dict, Optional


logger = logging.getLogger("email_sender")


@dataclass(frozen=True)
class EmailSendResult:
    ok: bool
    provider: str
    message_id: Optional[str] = None
    error: Optional[str] = None


def mask_email(email: str) -> str:
    """
    Mask an email address for logs/metrics (avoid PII).

    Example: "alice@example.com" -> "a***e@example.com"
    """
    try:
        local, domain = (email or "").split("@", 1)
    except ValueError:
        return "***"
    if not local:
        return f"***@{domain}" if domain else "***"
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


def _email_provider() -> str:
    """
    Resolve which email provider to use.

    Precedence:
    - EMAIL_PROVIDER env var ("ses" | "sendgrid")
    - Default: "ses"
    """
    raw = (os.getenv("EMAIL_PROVIDER") or "").strip().lower()
    if raw:
        return raw
    return "ses"


def _aws_region() -> Optional[str]:
    return (
        (os.getenv("AWS_SES_REGION") or "").strip()
        or (os.getenv("AWS_REGION") or "").strip()
        or (os.getenv("AWS_DEFAULT_REGION") or "").strip()
        or None
    )


def _aws_endpoint_url() -> Optional[str]:
    # Optional override for local testing (e.g. LocalStack).
    return (
        (os.getenv("AWS_SES_ENDPOINT_URL") or "").strip()
        or (os.getenv("AWS_ENDPOINT_URL") or "").strip()
        or None
    )


@lru_cache(maxsize=4)
def _sesv2_client(region: Optional[str], endpoint_url: Optional[str]) -> Any:
    import boto3

    return boto3.client("sesv2", region_name=region, endpoint_url=endpoint_url)


def _send_via_ses(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: Optional[str],
    from_email: str,
    from_name: Optional[str],
    reply_to: Optional[str],
    tags: Optional[Dict[str, str]],
) -> EmailSendResult:
    try:
        region = _aws_region()
        endpoint_url = _aws_endpoint_url()
        client = _sesv2_client(region, endpoint_url)
    except Exception as exc:
        logger.warning("email.ses.client_unavailable error=%s", type(exc).__name__)
        return EmailSendResult(ok=False, provider="ses", error="SES_CLIENT_UNAVAILABLE")

    from_addr = (from_email or "").strip()
    if not from_addr:
        return EmailSendResult(ok=False, provider="ses", error="FROM_EMAIL_MISSING")

    display = (from_name or "").strip()
    from_header = f"{display} <{from_addr}>" if display else from_addr

    body: Dict[str, Any] = {"Text": {"Data": text_body or ""}}
    if html_body:
        body["Html"] = {"Data": html_body}

    params: Dict[str, Any] = {
        "FromEmailAddress": from_header,
        "Destination": {"ToAddresses": [to_email]},
        "Content": {"Simple": {"Subject": {"Data": subject or ""}, "Body": body}},
    }
    if reply_to:
        params["ReplyToAddresses"] = [reply_to]
    if tags:
        # SES v2 tags are a list of {Name, Value}
        params["EmailTags"] = [{"Name": k, "Value": v} for k, v in tags.items() if k and v]

    try:
        resp = client.send_email(**params)
        msg_id = str(resp.get("MessageId") or "").strip() or None
        logger.info("email.sent provider=ses to=%s", mask_email(to_email))
        return EmailSendResult(ok=True, provider="ses", message_id=msg_id)
    except Exception as exc:
        # Do not log the exception message (may include details we don't want).
        logger.warning("email.send_failed provider=ses error=%s", type(exc).__name__)
        return EmailSendResult(ok=False, provider="ses", error="SES_SEND_FAILED")


def _send_via_sendgrid(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: Optional[str],
    from_email: str,
    from_name: Optional[str],
    reply_to: Optional[str],
) -> EmailSendResult:
    api_key = (os.getenv("SENDGRID_API_KEY") or "").strip()
    if not api_key:
        return EmailSendResult(ok=False, provider="sendgrid", error="SENDGRID_API_KEY_MISSING")

    try:
        import requests
    except Exception:
        return EmailSendResult(ok=False, provider="sendgrid", error="REQUESTS_MISSING")

    payload: Dict[str, Any] = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": from_name or "Pivota"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": text_body}],
    }
    if html_body:
        payload["content"].append({"type": "text/html", "value": html_body})
    if reply_to:
        payload["reply_to"] = {"email": reply_to}

    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
    except Exception as exc:
        logger.warning("email.send_failed provider=sendgrid error=%s", type(exc).__name__)
        return EmailSendResult(ok=False, provider="sendgrid", error="SENDGRID_UNAVAILABLE")

    if resp.status_code not in {200, 202}:
        logger.warning("email.send_failed provider=sendgrid status=%s", resp.status_code)
        return EmailSendResult(ok=False, provider="sendgrid", error="SENDGRID_UNAVAILABLE")

    logger.info("email.sent provider=sendgrid to=%s", mask_email(to_email))
    msg_id = (
        resp.headers.get("x-message-id")
        or resp.headers.get("X-Message-Id")
        or resp.headers.get("X-Message-ID")
        or ""
    ).strip()
    return EmailSendResult(ok=True, provider="sendgrid", message_id=msg_id or None)


def send_email(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    tags: Optional[Dict[str, str]] = None,
) -> EmailSendResult:
    """
    Send an email with the configured provider.

    Returns EmailSendResult; callers decide whether failures should be fatal.
    """
    provider = _email_provider()
    if provider == "ses":
        return _send_via_ses(
            to_email=to_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            from_email=(from_email or "").strip(),
            from_name=from_name,
            reply_to=reply_to,
            tags=tags,
        )
    if provider == "sendgrid":
        return _send_via_sendgrid(
            to_email=to_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            from_email=(from_email or "").strip(),
            from_name=from_name,
            reply_to=reply_to,
        )
    return EmailSendResult(ok=False, provider=provider, error="EMAIL_PROVIDER_UNCONFIGURED")

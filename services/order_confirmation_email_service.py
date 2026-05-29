from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from db.orders import get_order, update_order
from services.merchant_store_service import get_primary_store
from utils.email_sender import EmailSendResult, send_email
from utils.order_track_token import mint_order_track_token


logger = logging.getLogger("order_confirmation_email")
_SEND_LOCKS: Dict[str, asyncio.Lock] = {}


def order_confirmation_email_enabled() -> bool:
    raw = (os.getenv("ORDER_CONFIRMATION_EMAIL_ENABLED") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _from_email_default() -> str:
    return (os.getenv("FROM_EMAIL") or "noreply@pivota.ai").strip()


def _from_name_default() -> str:
    return (os.getenv("REVIEWS_INVITATION_FROM_NAME") or "Pivota").strip()


def _reply_to_support_email_enabled() -> bool:
    raw = (os.getenv("REVIEWS_INVITATION_REPLY_TO_SUPPORT_EMAIL") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _include_store_url_in_email() -> bool:
    raw = (os.getenv("REVIEWS_INVITATION_INCLUDE_STORE_URL") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _store_name_default(store: Optional[Dict[str, Any]]) -> str:
    if isinstance(store, dict):
        name = str(store.get("name") or "").strip()
        if name:
            return name
    return (os.getenv("REVIEWS_INVITATION_STORE_NAME") or "Pivota").strip()


def _store_url_default(store: Optional[Dict[str, Any]]) -> str:
    if not _include_store_url_in_email():
        return ""
    if isinstance(store, dict):
        domain = str(store.get("domain") or "").strip()
        if domain:
            if domain.startswith("http://") or domain.startswith("https://"):
                return domain
            return f"https://{domain}"
    return (os.getenv("REVIEWS_INVITATION_STORE_URL") or "").strip()


def _support_email_default(*, store: Optional[Dict[str, Any]], from_email: str) -> str:
    if isinstance(store, dict):
        email = str(store.get("support_email") or "").strip()
        if email:
            return email
    if "REVIEWS_INVITATION_SUPPORT_EMAIL" in os.environ:
        return (os.environ.get("REVIEWS_INVITATION_SUPPORT_EMAIL") or "").strip()

    raw = (os.getenv("REVIEWS_INVITATION_SUPPORT_EMAIL_FALLBACK_TO_FROM_EMAIL") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return (from_email or "").strip()

    return ""


def _order_track_url(token: str) -> str:
    explicit = (os.getenv("ORDER_TRACK_BASE_URL") or "").strip()
    if explicit:
        base = explicit.rstrip("/")
        if base.endswith("/order/track"):
            return f"{base}?token={quote(token)}"
        return f"{base}/order/track?token={quote(token)}"

    ui_base = (
        (os.getenv("CHECKOUT_UI_BASE_URL") or "").strip()
        or (os.getenv("ORDER_TRACK_UI_BASE_URL") or "").strip()
        or "https://agent.pivota.cc"
    ).rstrip("/")
    return f"{ui_base}/order/track?token={quote(token)}"


def _first_name_from_order(order: Dict[str, Any]) -> str:
    raw = str(order.get("customer_name") or "").strip()
    if not raw:
        shipping = order.get("shipping_address")
        if isinstance(shipping, dict):
            raw = str(
                shipping.get("name")
                or shipping.get("full_name")
                or shipping.get("recipient_name")
                or ""
            ).strip()
    return raw.split()[0].strip() if raw else ""


def _item_title(item: Dict[str, Any]) -> str:
    return str(
        item.get("product_title")
        or item.get("title")
        or item.get("name")
        or item.get("sku")
        or "Item"
    ).strip()


def _item_quantity(item: Dict[str, Any]) -> int:
    try:
        qty = int(item.get("quantity") or 1)
    except Exception:
        qty = 1
    return max(qty, 1)


def _build_items_summary(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "Your order"
    typed_items = [item for item in items if isinstance(item, dict)]
    if not typed_items:
        return "Your order"

    first = typed_items[0]
    first_summary = f"{_item_title(first)} x{_item_quantity(first)}"
    if len(typed_items) == 1:
        return first_summary
    return f"{first_summary} (+{len(typed_items) - 1} more)"


def _format_money(value: Any, currency: str) -> str:
    currency_code = (currency or "USD").strip().upper() or "USD"
    try:
        amount = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        amount = Decimal("0")
    prefix = "$" if currency_code == "USD" else ""
    return f"{prefix}{amount.quantize(Decimal('0.01'))} {currency_code}"


def _compose_order_confirmation_email(
    order: Dict[str, Any],
    track_url: str,
) -> Tuple[str, str, str]:
    first_name = _first_name_from_order(order)
    store_name = str(order.get("_email_store_name") or "Pivota").strip() or "Pivota"
    support_email = str(order.get("_email_support_email") or "").strip()
    store_url = str(order.get("_email_store_url") or "").strip()
    order_id = str(order.get("order_id") or "").strip()
    currency = str(order.get("currency") or "USD").strip().upper() or "USD"
    summary = _build_items_summary(order.get("items"))
    total = _format_money(order.get("total"), currency)

    greeting = f"Hi {first_name}," if first_name else "Hi,"
    closing_parts: List[str] = []
    if support_email:
        closing_parts.append(support_email)
    if store_url:
        closing_parts.append(store_url)
    closing_line = " | ".join(closing_parts)

    subject = "Your Pivota order is confirmed"
    text_lines = [
        greeting,
        "",
        f"Your order from {store_name} is confirmed.",
        "",
        f"Order ID: {order_id}",
        f"Summary: {summary}",
        f"Total: {total}",
        "",
        f"Track your order: {track_url}",
        "",
        "We will keep this link updated as your order moves forward.",
        "",
        "Thanks,",
        "Pivota",
    ]
    if closing_line:
        text_lines.append(closing_line)

    safe_track_url = html.escape(track_url, quote=True)
    safe_greeting = html.escape(greeting, quote=True)
    safe_store = html.escape(store_name, quote=True)
    safe_order_id = html.escape(order_id, quote=True)
    safe_summary = html.escape(summary, quote=True)
    safe_total = html.escape(total, quote=True)

    html_lines = [
        f"<p>{safe_greeting}</p>",
        f"<p>Your order from {safe_store} is confirmed.</p>",
        "<p>"
        f"<strong>Order ID:</strong> {safe_order_id}<br>"
        f"<strong>Summary:</strong> {safe_summary}<br>"
        f"<strong>Total:</strong> {safe_total}"
        "</p>",
        (
            f'<p><a href="{safe_track_url}" '
            'style="display:inline-block;padding:12px 18px;background:#111827;'
            'color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;">'
            "Track your order</a></p>"
        ),
        "<p>We will keep this link updated as your order moves forward.</p>",
        "<p>Thanks,<br>Pivota</p>",
    ]
    if support_email:
        html_lines.append(f"<p>{html.escape(support_email, quote=True)}</p>")
    if store_url:
        safe_store_url = html.escape(store_url, quote=True)
        html_lines.append(f'<p><a href="{safe_store_url}">{safe_store_url}</a></p>')

    return subject, "\n".join(text_lines).strip() + "\n", "\n".join(html_lines).strip() + "\n"


def _failure(error: str, *, provider: str = "order_confirmation") -> EmailSendResult:
    return EmailSendResult(ok=False, provider=provider, error=error)


def _report_exception(exc: Exception, *, order_id: Optional[str], operation: str) -> None:
    try:
        from config.sentry_config import capture_exception

        capture_exception(
            exc,
            {
                "component": "order_confirmation_email",
                "operation": operation,
                "order_id": order_id,
            },
        )
    except Exception:
        logger.debug("order confirmation email reporting failed", exc_info=True)


async def send_order_confirmation_email(order: Dict[str, Any]) -> EmailSendResult:
    try:
        if not isinstance(order, dict):
            return _failure("ORDER_MISSING")

        order_id = str(order.get("order_id") or "").strip()
        to_email = str(order.get("customer_email") or "").strip()
        if not order_id:
            return _failure("ORDER_ID_MISSING")
        if not to_email:
            return _failure("ORDER_EMAIL_MISSING")

        store: Optional[Dict[str, Any]] = None
        merchant_id = str(order.get("merchant_id") or "").strip()
        if merchant_id:
            try:
                store = await get_primary_store(merchant_id)
            except Exception as exc:
                logger.warning("order_confirmation_email.store_lookup_failed order_id=%s error=%s", order_id, type(exc).__name__)
                _report_exception(exc, order_id=order_id, operation="store_lookup")

        from_email = _from_email_default()
        from_name = _from_name_default()
        support_email = _support_email_default(store=store, from_email=from_email)
        reply_to = support_email if support_email and _reply_to_support_email_enabled() else None

        token = mint_order_track_token(order_id)
        track_url = _order_track_url(token)
        email_order = dict(order)
        email_order["_email_store_name"] = _store_name_default(store)
        email_order["_email_support_email"] = support_email
        email_order["_email_store_url"] = _store_url_default(store)
        subject, text_body, html_body = _compose_order_confirmation_email(email_order, track_url)

        return await asyncio.to_thread(
            send_email,
            to_email=to_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            from_email=from_email,
            from_name=from_name or None,
            reply_to=reply_to,
            tags={"type": "order_confirmation"},
        )
    except Exception as exc:
        order_id = str(order.get("order_id") or "").strip() if isinstance(order, dict) else None
        logger.warning("order_confirmation_email.send_failed order_id=%s error=%s", order_id, type(exc).__name__)
        _report_exception(exc, order_id=order_id, operation="send")
        return _failure(type(exc).__name__)


async def send_order_confirmation_email_once(order_id: str) -> EmailSendResult:
    raw_order_id = str(order_id or "").strip()
    if not order_confirmation_email_enabled():
        return _failure("DISABLED")
    if not raw_order_id:
        return _failure("ORDER_ID_MISSING")

    lock = _SEND_LOCKS.setdefault(raw_order_id, asyncio.Lock())
    async with lock:
        return await _send_order_confirmation_email_once_locked(raw_order_id)


async def _send_order_confirmation_email_once_locked(order_id: str) -> EmailSendResult:
    try:
        order = await get_order(order_id)
        if not order:
            return _failure("ORDER_NOT_FOUND")

        metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
        if str(metadata.get("order_confirmation_email_sent_at") or "").strip():
            return _failure("ALREADY_SENT")
        if not str(order.get("customer_email") or "").strip():
            return _failure("ORDER_EMAIL_MISSING")

        result = await send_order_confirmation_email(order)
        if not getattr(result, "ok", False):
            return result

        try:
            new_meta = dict(metadata)
            new_meta["order_confirmation_email_sent_at"] = datetime.now(timezone.utc).isoformat()
            if getattr(result, "provider", None):
                new_meta["order_confirmation_email_provider"] = str(result.provider)
            if getattr(result, "message_id", None):
                new_meta["order_confirmation_email_message_id"] = str(result.message_id)
            await update_order(order_id, {"metadata": new_meta})
        except Exception as exc:
            logger.warning("order_confirmation_email.metadata_update_failed order_id=%s error=%s", order_id, type(exc).__name__)
            _report_exception(exc, order_id=order_id, operation="metadata_update")

        return result
    except Exception as exc:
        logger.warning("order_confirmation_email.once_failed order_id=%s error=%s", order_id, type(exc).__name__)
        _report_exception(exc, order_id=order_id, operation="send_once")
        return _failure(type(exc).__name__)

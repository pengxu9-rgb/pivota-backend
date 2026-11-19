"""The owner-facing view of an A2A-rail purchase: what `detail` holds in the unified purchase body.

Built from the owner-scoped row (db/a2a_rail_ledger.get_purchase_for_owner) by an ALLOWLIST of
columns. Never in it: the buyer's email and address, the rail member id, the rail's payment method,
token, quote and order ids, the claim and the pending write. A URL is only passed on after it is
re-vetted at read time, because both came from the rail and both are meant to be opened by a human:

  hosted_url                 the rail's buyer page (card binding / approval): https, a host on the
                             rail's allowlist (by SHA-256, as the transport does), the kind matching
                             the state, and not past its deadline. Otherwise absent: nowhere to send
                             the buyer, which is exactly what an absent key says.
  totals.ceiling_source      present when `quoted_total_minor` is an approval ceiling Pivota built (the
                             checkout path has no quote): how it was built, e.g. `allowance_v1`.
  merchant_order_status_url  the merchant's own order page: https, on the merchant's domain or one of
                             its subdomains. A platform host (any store can mint one) is not
                             evidence it is THIS merchant's page, so it is not shown.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit

import db.a2a_rail_ledger as ledger
from services.payment_orchestration import a2a_rail, purchase_consent

#: Extra SHA-256 digests (comma-separated, lower-case hex) of hosts the rail serves buyer pages from,
#: when they are not the API hosts themselves. Configured, not named, like the API host allowlist.
HOSTED_HOSTS_ENV = "A2A_RAIL_HOSTED_HOST_SHA256"

#: The state each buyer-page kind belongs to. A page from another state is stale: not shown.
#: `paying` carries the merchant's challenge (3DS) while the order is being placed.
_KIND_FOR_STATE = {
    "needs_payment_method": "card_binding",
    "awaiting_authorization": "approval",
    "paying": "authenticate",
}

#: How soon an agent polling should ask again, by state. None once terminal.
_POLL_AFTER = {"opened": 5, "quoting": 5, "paying": 5, "needs_payment_method": 15, "awaiting_authorization": 15}

_CEILING_SOURCE_RE = re.compile(r"^[a-z0-9_]{1,32}\Z")

_PUBLIC_COLUMNS = (
    "id", "state", "merchant_domain", "product_key", "product_name", "variant_title", "quantity",
    "currency", "refusal_reason", "last_error_code", "consent_version", "consented_at",
    "created_at", "updated_at", "terminal_at",
)


def _host_digests() -> frozenset:
    extra = {d.strip().lower() for d in (os.getenv(HOSTED_HOSTS_ENV) or "").split(",") if d.strip()}
    return frozenset(a2a_rail.ALLOWED_HOST_SHA256) | frozenset(extra)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _default_port(parts: Any) -> bool:
    try:
        return parts.port in (None, 443)
    except ValueError:
        return False


def hosted_url_is_allowed(url: Any) -> bool:
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" or "@" in parts.netloc or not parts.hostname or not _default_port(parts):
        return False
    host = parts.hostname.rstrip(".").lower()
    return hashlib.sha256(host.encode("utf-8")).hexdigest() in _host_digests()


def merchant_order_url_is_allowed(url: Any, merchant_domain: str) -> bool:
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" or "@" in parts.netloc or not parts.hostname or not _default_port(parts):
        return False
    host = parts.hostname.rstrip(".").lower()
    domain = str(merchant_domain or "").lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def build_view(row: Mapping[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The `detail` body for one owner-scoped row. Pure."""
    now = now or datetime.now(timezone.utc)
    view: Dict[str, Any] = {key: row.get(key) for key in _PUBLIC_COLUMNS}
    state = str(row.get("state") or "")
    view["totals"] = {
        "currency": row.get("currency"),
        "our_price_minor": row.get("our_price_minor"),
        "quoted_total_minor": row.get("quoted_total_minor"),
        "final_total_minor": row.get("final_total_minor"),
        "shipping_minor": row.get("shipping_minor"),
        "tax_minor": row.get("tax_minor"),
    }
    # The checkout path has no quote: `quoted_total_minor` is then an approval CEILING Pivota built (items
    # plus tax and shipping allowances), and this says how. Present only for such a ceiling, so a reader
    # can say "you approve up to this; the final total includes shipping and tax".
    source = row.get("ceiling_source")
    if row.get("quoted_total_minor") is not None and isinstance(source, str) and _CEILING_SOURCE_RE.match(source):
        view["totals"]["ceiling_source"] = source
    expires = _aware(row.get("buyer_action_expires_at"))
    if (
        _KIND_FOR_STATE.get(state) is not None
        and row.get("buyer_action_kind") == _KIND_FOR_STATE[state]
        and hosted_url_is_allowed(row.get("buyer_action_url"))
        and (expires is None or expires > now)
    ):
        view["hosted_url"] = row["buyer_action_url"]
        view["hosted_url_expires_at"] = row.get("buyer_action_expires_at")
    if state == "completed" and row.get("merchant_order_id"):
        view["order_reference"] = row["merchant_order_id"]
        if merchant_order_url_is_allowed(row.get("merchant_order_status_url"), str(row.get("merchant_domain") or "")):
            view["merchant_order_status_url"] = row["merchant_order_status_url"]
    view["poll_after_seconds"] = None if state in ledger.TERMINAL_STATES else _POLL_AFTER.get(state, 10)
    return view


async def owner_view(
    *, purchase_id: str, agent_id: str, agent_user_ref_hash: str, consents: Any = purchase_consent.LOOKUP
) -> Optional[Dict[str, Any]]:
    """None when the purchase is not this owner's or does not exist (the same answer).

    Every surface takes this rail's hosted URL from here, so the AgenPas consent gate is applied here:
    with AGENPAS_CONSENT_GATE on and no consent for the page's step, no `hosted_url` and a `buyer_step`
    marker instead (services/payment_orchestration/purchase_consent.py; `authenticate` is never gated).
    `consents` is a list page's one-read snapshot."""
    row = await ledger.get_purchase_for_owner(purchase_id, agent_id, agent_user_ref_hash)
    if row is None:
        return None
    return await purchase_consent.gate(
        build_view(row), rail="a2a", rail_purchase_id=str(row.get("id") or purchase_id),
        rail_state=row.get("state"), agent_id=agent_id, consents=consents,
    )

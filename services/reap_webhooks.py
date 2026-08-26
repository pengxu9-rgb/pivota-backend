"""Reap webhook processing — the reconcile half of the card rail.

⚠️ WIRE FORMAT NOT YET VERIFIED AGAINST REAP, same status as reap_issuer.py: the event field
names in `parse_event` are the adapter's best-understood shape, confined to that one function.
The signature scheme (HMAC-SHA256 of the raw body, hex, optional "sha256=" prefix, header name
configurable) is the industry-common default and equally awaits verification.

Three rules that hold regardless of how the wire format moves:

1. THE SIGNATURE COVERS THE EXACT BYTES RECEIVED. verify_signature takes the raw body; nothing
   re-serializes JSON first (re-serialization reorders keys and breaks valid signatures — and
   accepting a re-serialized match would mean accepting bodies we cannot re-verify later).

2. PARSE BY ALLOWLIST, DROP THE REST, LOG NONE OF IT. An issuer event can embed card data.
   `parse_event` extracts the handful of fields the handlers need; the raw body is never
   stored, never logged, never attached to an exception.

3. THE ISSUER'S REPORT CANNOT MOVE OUR MONEY MODEL. Handlers update card state and record
   outcomes; nothing here mints, revokes-into-reissue, or changes a cap. A hostile or confused
   webhook can at worst mark its own card exhausted.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional

from utils.logger import logger

_ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "KMF", "XOF", "XAF"})


def webhook_secret() -> str:
    return str(os.getenv("REAP_WEBHOOK_SECRET") or "").strip()


def signature_header_name() -> str:
    return str(os.getenv("REAP_WEBHOOK_SIG_HEADER") or "x-reap-signature").strip().lower()


def verify_signature(raw_body: bytes, provided: Optional[str], secret: str) -> bool:
    """Constant-time HMAC check over the exact received bytes. No secret => never valid — the
    caller turns that into a 503, not an open door."""
    if not secret or not provided:
        return False
    candidate = provided.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate[7:]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(candidate.lower(), expected.lower())


@dataclass(frozen=True)
class ReapEvent:
    event_id: str
    event_type: str            # normalized: auth_approved | auth_declined | settlement | other
    issuer_card_ref: str
    pivota_card_id: Optional[str]   # our id, echoed from mint-time metadata
    amount_minor: Optional[int]
    currency: Optional[str]
    decline_reason: Optional[str]


_TYPE_MAP = {
    "authorization.approved": "auth_approved",
    "authorization.declined": "auth_declined",
    "transaction.settled": "settlement",
    "settlement.completed": "settlement",
}


def _as_minor(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    try:
        d = Decimal(str(value).strip())
    except Exception:
        return None
    return int(d) if d >= 0 and d == d.to_integral_value() else None


def parse_event(body: Dict[str, Any]) -> Optional[ReapEvent]:
    """Allowlist extraction. Returns None when the body has no usable identity — the route
    answers 200 for those (a malformed event is Reap's bug to notice, not a retry we invite)."""
    if not isinstance(body, dict):
        return None
    event_id = str(body.get("id") or body.get("event_id") or "").strip()
    raw_type = str(body.get("type") or body.get("event_type") or "").strip().lower()
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    card = data.get("card") if isinstance(data.get("card"), dict) else None
    if card is not None:
        ref = str(card.get("card_id") or card.get("id") or "").strip()
    else:
        # Flat shape: `id` at this level is the EVENT id, so only an explicit card_id counts.
        # Letting `id` double as the card ref here made every flat event claim its own event id
        # was a card — the test that pinned this had to exist before the bug was visible.
        card = data
        ref = str(data.get("card_id") or "").strip()
    if not event_id or not raw_type or not ref:
        return None
    meta = card.get("metadata") if isinstance(card.get("metadata"), dict) else {}
    amount_obj = data.get("amount") if isinstance(data.get("amount"), dict) else {}
    amount_minor = _as_minor(amount_obj.get("amount") if amount_obj else data.get("amount_minor"))
    currency = amount_obj.get("currency") or data.get("currency")
    return ReapEvent(
        event_id=event_id[:128],
        event_type=_TYPE_MAP.get(raw_type, "other"),
        issuer_card_ref=ref[:128],
        pivota_card_id=(str(meta.get("pivota_card_id")).strip()[:64] or None)
        if meta.get("pivota_card_id") is not None
        else None,
        amount_minor=amount_minor,
        currency=str(currency).strip().upper()[:8] if currency else None,
        decline_reason=(str(data.get("decline_reason") or "").strip()[:48] or None),
    )


def minor_to_major(amount_minor: int, currency: str) -> Decimal:
    exponent = 0 if currency.upper() in _ZERO_DECIMAL_CURRENCIES else 2
    return Decimal(amount_minor) / (Decimal(10) ** exponent)


def check_card_consistency(event: ReapEvent, card: Dict[str, Any]) -> Optional[str]:
    """Cross-checks that should never fail — each one that does is an alarm, not an update.

    Returns an alarm code, or None when the event is consistent with the card we minted.
    """
    if event.pivota_card_id and event.pivota_card_id != card["card_id"]:
        # The issuer's card ref resolved to one of our rows, but the metadata echo names a
        # DIFFERENT row: either their store crossed wires or someone replayed metadata.
        return "CARD_REF_METADATA_MISMATCH"
    if event.currency and event.currency != card["currency"]:
        return "CARD_CURRENCY_MISMATCH"
    if (
        event.event_type == "auth_approved"
        and event.amount_minor is not None
        and event.amount_minor > card["amount_cap_minor"]
    ):
        # The single most important alarm on this rail: the issuer approved MORE than the cap
        # we minted. The constraint model failed at the issuer — stop trusting it quietly.
        return "CARD_CAP_BREACH"
    return None


def alarm(code: str, card_id: str, event: ReapEvent) -> None:
    # ERROR level on purpose: these are "the safety model did not hold" signals, and log-based
    # alerting keys on severity. Amounts and refs only — never event bodies.
    logger.error(
        f"card-rail webhook alarm code={code} card_id={card_id} event_id={event.event_id} "
        f"type={event.event_type} amount_minor={event.amount_minor} currency={event.currency}"
    )

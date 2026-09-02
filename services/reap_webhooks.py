"""Reap webhook processing — the reconcile half of the card rail.

SIGNATURE SCHEME: VERIFIED against Reap's docs (docs.reap.global/webhooks/signature-verification,
/webhooks/overview, read 2026-09-01). Header `X-Reap-Webhook-Signature`, value shaped
`t=<unix seconds>,v1=<hex hmac>`; the signed payload is the string `"{t}.{raw_body}"`; the MAC is
HMAC-SHA256 over that, hex, compared constant-time; deliveries outside a 300 s tolerance window
are rejected as replays. Reap also sends `X-Reap-Webhook-Id` (mirrors body `id`) and
`X-Reap-Webhook-Timestamp`, but the value we sign over is the `t=` INSIDE the signature header —
that is the one Reap actually MAC'd, so the separate header cannot be trusted for the computation.

⚠️ EVENT FIELD NAMES still not verified against Reap, same status as reap_issuer.py: the shape
`parse_event` reads is the adapter's best-understood guess, confined to that one function.

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
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional

from utils.logger import logger

_ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "KMF", "XOF", "XAF"})

# Module-level reference so tests patch services.reap_webhooks._now and never the stdlib module
# object (patching time.time globally breaks every other clock in the process).
_now = time.time

# Reap's documented replay window. Applied SYMMETRICALLY: the docs say to reject when the
# timestamp "differs from the current time by more than 300 seconds", which reads on both sides,
# and a future-dated t is either a clock we cannot trust or a signature farmed to outlive the
# window. Rejecting it costs a redelivery; accepting it extends every stolen signature's life.
SIGNATURE_TOLERANCE_SECONDS = 300

# ASCII digits only. `int()` happily parses unicode digits ("١٢٣" -> 123), which would then fail
# to .encode("ascii") when we rebuild the signed payload; refuse the shape up front instead.
_TIMESTAMP_RE = re.compile(r"[0-9]{1,20}")


def webhook_secret() -> str:
    return str(os.getenv("REAP_WEBHOOK_SECRET") or "").strip()


def signature_header_name() -> str:
    return str(
        os.getenv("REAP_WEBHOOK_SIG_HEADER") or "x-reap-webhook-signature"
    ).strip().lower()


def verify_signature(raw_body: bytes, provided: Optional[str], secret: str) -> bool:
    """Reap's scheme: header `t=<unix seconds>,v1=<hex hmac>`, MAC = HMAC-SHA256 over the string
    `"{t}.{raw_body}"`, compared constant-time, within a 300 s tolerance window.

    The timestamp is part of the SIGNED input, so it cannot be edited to slide a captured
    delivery forward — changing `t` invalidates `v1`. That is why the window is enforced against
    the `t` inside this header and not against `X-Reap-Webhook-Timestamp`, which Reap sends
    separately and never MAC'd.

    No secret => never valid — the caller turns that into a 503, not an open door.
    """
    if not secret or not provided:
        return False

    timestamp: Optional[str] = None
    candidates: list[str] = []
    for part in provided.strip().split(","):
        key, sep, value = part.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "t":
            if timestamp is None:
                timestamp = value
        elif key == "v1":
            # Accept-if-ANY-matches. The docs describe one v1 and say rotation invalidates the
            # old secret immediately, so this is normally a one-element list; each entry still
            # has to be a real HMAC under our secret, so tolerating several costs nothing.
            candidates.append(value)
    # A header missing either half is not "partially signed", it is unsigned.
    if not timestamp or not candidates:
        return False
    if not _TIMESTAMP_RE.fullmatch(timestamp):
        return False

    # Replay window, symmetric. Enforced BEFORE the MAC so a stale delivery is refused even when
    # its signature is perfectly valid — which is the entire point of the timestamp.
    if abs(_now() - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
        return False

    # `{t}.{raw_body}` — built from the EXACT received bytes, never a re-serialized body.
    signed_payload = timestamp.encode("ascii") + b"." + raw_body
    expected = (
        hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest().encode("ascii")
    )
    for candidate in candidates:
        # Compare as BYTES: hmac.compare_digest raises TypeError on non-ASCII str, and Starlette
        # decodes headers latin-1, so any byte >= 0x80 in the header was an unauthenticated 500.
        try:
            candidate_bytes = candidate.lower().encode("utf-8")
        except Exception:
            continue
        if hmac.compare_digest(candidate_bytes, expected):
            return True
    return False


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
    """Minor units are INTEGERS. A decimal string like "23.00" is a MAJOR-unit amount that would
    be recorded 100x wrong if coerced (23 minor = $0.23) — review proved this path live. So:
    ints and digit-only strings only; anything with a '.' is refused, and the handler treats a
    refused amount as absent, which skips the write. A skipped amount is a gap we can see; a
    silently wrong one poisons reconciliation."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return int(s)
        if s and "." in s:
            logger.warning("reap webhook: decimal-shaped amount refused (expected minor units)")
    return None


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
        event.event_type in ("auth_approved", "settlement")
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

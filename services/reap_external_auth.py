"""Reap EXTERNAL AUTHORIZATION — the live decision on the card rail.

With the sandbox project configured Program-Funded + External authorization, Reap stops at the
network on EVERY card authorization, POSTs us a signed CARD_AUTHORIZATION_REQUEST, and waits
1.6 seconds for {"decision":"APPROVE"} or {"decision":"DECLINE","reason":...}. Anything else —
timeout, non-2xx, unreachable, unparseable — is a DECLINE at Reap's end. The rail fails closed
without our help, which is the property that lets every ambiguity here resolve toward "decline".

WHAT THIS IS NOT. It is not the record. The authoritative record is the CARD_TRANSACTION_CREATED
webhook that follows (routes/reap_webhooks.receive_reap_webhook), and it carries our eventId
back as triggerEventId. So the decision NEVER touches agent_issued_cards: apply_auth_approved is
guarded on status='issued' and alarms AUTH_ON_NON_ISSUED_CARD otherwise, so a decision that
exhausted the card would make its own record alarm falsely, on every single approval. The
decision's state lives in agent_card_auth_decisions (migration 207) instead, and that ledger is
what makes single-use atomic across the gap: rule (d) reserves the card by scanning for a prior
APPROVE row, under a per-card advisory lock that serializes concurrent authorizations.

WHERE OUR GUARDRAILS SIT RELATIVE TO REAP'S. Reap runs card state and spend policy BEFORE
calling us, so everything below is a SECOND opinion, not the only one. That matters for how the
rules are ordered: cheap identity checks first, the reservation before the amount work, and the
merchant check — the one that writes — last.

THE MERCHANT REGISTRY IS LEARNED, AND THAT IS A DELIBERATE WEAKENING.
The card network gives us a descriptor ("ACME STORE", "Berlin", "DE"), never a domain, and no
descriptor-to-domain mapping exists for us to consult. So: a merchant_domain with NO pinned
descriptors approves its first authorization on the strength of the other constraints and PINS
what it saw; every later authorization for that domain must match a pin or be declined. The
row records merchant_verified=false for that first one, so the weaker decisions are queryable
rather than indistinguishable.

Why that is acceptable HERE and would not be on a general-purpose card: the instrument is
single-use, capped at the merchant's own quoted total, and expires. The worst case for an
unpinned domain is exactly ONE authorization, at or below a cap we set, on a card that dies
after it — not a standing credential at an attacker-chosen merchant.

FOLLOW-UP (not in this change): CARD_TRANSACTION_CREATED also carries a merchant object. Pinning
from the webhook as well would let a domain be taught by a settled, reconciled transaction
rather than only by a live decision under a 1.6s budget — source='webhook' exists in the schema
for it.

LOGGING. Never the body. The alarms below carry ids and the reason code only: no merchant name,
city, postcode, amount, accountId or digitalWallet ever reaches a log line, and
tests/test_reap_external_auth.py pins that against caplog.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from db.agent_card_auth_decisions import (
    find_decision,
    has_approval,
    list_descriptors,
    pin_descriptor,
    record_decision,
    touch_descriptor,
)
from db.agent_issued_cards import find_by_issuer_ref
from db.database import database
from services.reap_webhooks import major_to_minor
from utils.logger import logger

REQUEST_TYPE = "CARD_AUTHORIZATION_REQUEST"

# Reap's decline vocabulary. Exactly two values exist; anything else is rejected by their API.
REASON_INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
REASON_TRANSACTION_NOT_ALLOWED = "TRANSACTION_NOT_ALLOWED"

# The only channel an agent checkout can legitimately produce. A card minted for a web checkout
# being presented at an ATM or a point-of-sale terminal is not a near-miss, it is evidence the
# credential left the flow it was minted for.
ALLOWED_CHANNELS = frozenset({"ECOMMERCE"})

# Above this, the 1.6s budget is close enough to warn. Chosen with headroom for Reap's own
# network leg in both directions, which our clock never sees.
LATENCY_WARN_MS = 800


# ── configuration ────────────────────────────────────────────────────────────────────────────


def external_auth_enabled() -> bool:
    """Default OFF. Until Reap's project is actually switched to EXTERNAL authorization, the
    endpoint must not exist — an enabled receiver that nobody calls is only an attack surface."""
    return str(os.getenv("REAP_EXTERNAL_AUTH_ENABLED") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def auth_webhook_secret() -> str:
    """The REQUEST-mode endpoint's OWN signing secret, returned once by Reap's
    create-webhook-endpoint call. Deliberately NOT falling back to REAP_WEBHOOK_SECRET: they are
    different endpoints with different secrets, and a fallback would mean a misconfiguration
    silently authenticates live spending decisions with the notification receiver's key."""
    return str(os.getenv("REAP_AUTH_WEBHOOK_SECRET") or "").strip()


def auth_signature_header_name() -> str:
    return str(
        os.getenv("REAP_AUTH_WEBHOOK_SIG_HEADER") or "x-reap-webhook-signature"
    ).strip().lower()


# ── the request, by allowlist ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuthorizationRequest:
    """Exactly the fields a decision needs. Everything else Reap sends — accountId, fees,
    digitalWallet, postCode, mccCategory, occurredAt — is dropped at the door: not stored, not
    logged, not attached to an exception."""

    event_id: str
    card_ref: str
    channel: str
    currency: Optional[str]
    amount: Optional[Decimal]
    original_currency: Optional[str]
    original_amount: Optional[Decimal]
    merchant_name: Optional[str]
    merchant_city: Optional[str]
    merchant_country: Optional[str]
    mcc: Optional[str]


def _currency(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()[:8]
    return text or None


def _amount(value: Any) -> Optional[Decimal]:
    """Reap sends decimal MAJOR-unit numbers. The route parses with parse_float=Decimal, so a
    JSON number arrives as Decimal or int; a float here would mean that guard was removed, and
    it is refused rather than silently absorbed."""
    if isinstance(value, bool) or value is None or isinstance(value, float):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_authorization_request(body: Any) -> Optional[AuthorizationRequest]:
    """None when the body carries no usable identity. The route turns that into a 400 — we
    cannot record a decision without an eventId, and an unrecorded decision is worse than a
    decline, because Reap's fail-closed default already declines for us."""
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    event_id = str(data.get("eventId") or "").strip()
    card_ref = str(data.get("cardId") or "").strip()
    if not event_id or not card_ref:
        return None
    merchant = data.get("merchant") if isinstance(data.get("merchant"), dict) else {}
    return AuthorizationRequest(
        event_id=event_id[:128],
        card_ref=card_ref[:128],
        channel=str(data.get("channel") or "").strip().upper()[:16],
        currency=_currency(data.get("currency")),
        amount=_amount(data.get("amount")),
        original_currency=_currency(data.get("originalCurrency")),
        original_amount=_amount(data.get("originalAmount")),
        merchant_name=(str(merchant.get("name") or "").strip()[:255] or None),
        merchant_city=(str(merchant.get("city") or "").strip()[:255] or None),
        merchant_country=(str(merchant.get("country") or "").strip().upper()[:2] or None),
        mcc=(str(merchant.get("mccCode") or "").strip()[:4] or None),
    )


# ── the decision ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Decision:
    decision: str                      # APPROVE | DECLINE
    reason: Optional[str]              # Reap's code; None on APPROVE
    reason_code: str                   # ours — the rule that fired
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    merchant_verified: bool = False
    replayed: bool = False             # answered from the ledger, not re-evaluated

    def body(self) -> Dict[str, str]:
        """EXACTLY what goes on the wire — nothing else. An extra key is not decoration here:
        Reap parses this under a 1.6s budget and an unparseable body is a decline."""
        if self.decision == "APPROVE":
            return {"decision": "APPROVE"}
        return {"decision": "DECLINE", "reason": self.reason or REASON_TRANSACTION_NOT_ALLOWED}


def _approve(**over: Any) -> Decision:
    return Decision(decision="APPROVE", reason=None, reason_code="approved", **over)


def _decline(reason: str, reason_code: str, **over: Any) -> Decision:
    return Decision(decision="DECLINE", reason=reason, reason_code=reason_code, **over)


def alarm(code: str, *, event_id: str, reason_code: str, card_id: Optional[str] = None,
          issuer_card_ref: Optional[str] = None) -> None:
    """ERROR level: these are "the safety model did not hold" signals and log alerting keys on
    severity. IDS AND THE RULE ONLY — never a merchant descriptor, an amount, or a body."""
    logger.error(
        f"card-rail auth alarm code={code} event_id={event_id} card_id={card_id} "
        f"issuer_card_ref={issuer_card_ref} reason_code={reason_code}"
    )


def normalize_descriptor(value: Optional[str]) -> str:
    """The ONE descriptor normalizer. A second implementation would silently un-pin every
    merchant, because the registry stores what this function produced.

    casefold -> cut at the first '*' -> non-alphanumerics to spaces -> collapse whitespace.

      "ACME Store, Inc.*1234"  ->  "acme store inc"
      "acme-store inc"         ->  "acme store inc"

    Punctuation becomes a SPACE rather than being deleted, so "ACME-STORE" and "ACME STORE"
    normalize together instead of to "acmestore" and "acme store".

    KNOWN LIMITATION, stated because it will bite someone: the '*' cut is a SUFFIX rule, and
    several acquirers put their tag in front ("SQ *ACME STORE", "PAYPAL *ACME"). Those normalize
    to "sq" and "paypal" — which does not mis-approve anything (a wrong descriptor still has to
    match a pin) but does pin a useless value for the first authorization at such a merchant.
    Splitting on which side of the '*' carries the merchant needs the acquirer-prefix list we do
    not have yet; the webhook-side pinning follow-up is where to add it.
    """
    text = (value or "").casefold().strip()
    text = text.split("*", 1)[0]
    text = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in text)
    return " ".join(text.split())


def _is_expired(expires_at: Any, now: datetime) -> bool:
    """A missing expiry counts as EXPIRED. migration 201 makes the column NOT NULL for the
    reason it states — "an unexpiring cap is not a cap" — so a None here is a broken row, and
    the fail-closed reading of a broken row is to decline."""
    if expires_at is None:
        return True
    if not isinstance(expires_at, datetime):
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


async def _match_or_pin(request: AuthorizationRequest, merchant_domain: str) -> Optional[bool]:
    """Rule (g). Returns True (matched a pin), False (domain had no pins — learned this one),
    or None (the domain has pins and this descriptor is not one of them => decline)."""
    name_norm = normalize_descriptor(request.merchant_name)
    country = (request.merchant_country or "").strip().upper()
    pins: List[Dict[str, Any]] = await list_descriptors(merchant_domain)
    if not pins:
        await pin_descriptor(
            merchant_domain=merchant_domain,
            name_norm=name_norm,
            country=country,
            city_norm=normalize_descriptor(request.merchant_city) or None,
            source="authorization",
        )
        return False
    for pin in pins:
        if pin["name_norm"] == name_norm and (pin["country"] or "") == country:
            await touch_descriptor(int(pin["id"]))
            return True
    return None


async def _evaluate(request: AuthorizationRequest, card: Optional[Dict[str, Any]],
                    now: datetime) -> Decision:
    """First failing rule wins. Order is deliberate: identity, then liveness, then the
    single-use reservation, then the channel, then the money, then the merchant — the merchant
    check writes to the registry, so it must not run for a decision already lost."""
    # (b) an authorization on our program for a card we never minted
    if card is None:
        alarm(
            "CARD_AUTH_UNKNOWN_CARD", event_id=request.event_id,
            issuer_card_ref=request.card_ref, reason_code="unknown_card",
        )
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "unknown_card")

    card_id = card["card_id"]

    # (c) liveness
    if card["status"] != "issued":
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "card_not_live")
    if _is_expired(card["expires_at"], now):
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "card_expired")

    # (d) the single-use reservation. Before the amount work on purpose: a card already spent
    # must decline identically whatever this authorization is for.
    if card["single_use"] and await has_approval(card_id):
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "already_authorized")

    # (e) channel
    if request.channel not in ALLOWED_CHANNELS:
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "channel_not_allowed")

    # (f) the amount, IN THE CARD'S CURRENCY. Reap sends the billing pair (currency/amount) and
    # the merchant's presentment pair (originalCurrency/originalAmount). The cap is in the
    # card's currency, so we compare against whichever pair is denominated in it — the
    # presentment first, because that is the number the merchant actually charged.
    card_currency = str(card["currency"] or "").strip().upper()
    if request.original_currency and request.original_currency == card_currency:
        raw_amount: Optional[Decimal] = request.original_amount
    elif request.currency and request.currency == card_currency:
        raw_amount = request.amount
    else:
        # Neither leg is in the card's currency: an FX conversion we did not authorize stands
        # between this charge and our cap, so the cap is not enforceable on it.
        alarm(
            "CARD_AUTH_CURRENCY_MISMATCH", event_id=request.event_id, card_id=card_id,
            issuer_card_ref=request.card_ref, reason_code="currency_mismatch",
        )
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "currency_mismatch")

    amount_minor = major_to_minor(raw_amount, card_currency) if raw_amount is not None else None
    if amount_minor is None:
        return _decline(
            REASON_TRANSACTION_NOT_ALLOWED, "amount_unparseable", currency=card_currency
        )

    # over_cap is the ONE rule that answers INSUFFICIENT_BALANCE: it is the only decline here
    # that means "this instrument does not carry that much", which is what the cardholder-facing
    # message says. Every other rule is our own control and maps to TRANSACTION_NOT_ALLOWED.
    if amount_minor > int(card["amount_cap_minor"]):
        return _decline(
            REASON_INSUFFICIENT_BALANCE, "over_cap",
            amount_minor=amount_minor, currency=card_currency,
        )

    # (g) merchant
    verified = await _match_or_pin(request, card["merchant_domain"])
    if verified is None:
        alarm(
            "CARD_AUTH_MERCHANT_MISMATCH", event_id=request.event_id, card_id=card_id,
            issuer_card_ref=request.card_ref, reason_code="merchant_mismatch",
        )
        return _decline(
            REASON_TRANSACTION_NOT_ALLOWED, "merchant_mismatch",
            amount_minor=amount_minor, currency=card_currency,
        )

    # (h)
    return _approve(
        amount_minor=amount_minor, currency=card_currency, merchant_verified=verified
    )


def _decision_values(request: AuthorizationRequest, card: Optional[Dict[str, Any]],
                     outcome: Decision, latency_ms: int) -> Dict[str, Any]:
    return {
        "event_id": request.event_id,
        "card_id": card["card_id"] if card else None,
        "issuer_card_ref": request.card_ref,
        "decision": outcome.decision,
        "reason": outcome.reason,
        "reason_code": outcome.reason_code,
        "amount_minor": outcome.amount_minor,
        "currency": outcome.currency,
        "channel": request.channel or None,
        "merchant_name": request.merchant_name,
        "merchant_city": request.merchant_city,
        "merchant_country": request.merchant_country,
        "mcc": request.mcc,
        # Both NOT NULL in migration 207, and an explicit None bind DEFEATS a column default —
        # the failure mode that made 22 faked-DB tests green over a dead write on this rail
        # once already (routes/reap_webhooks._outcome_values carries the same note).
        "merchant_verified": bool(outcome.merchant_verified),
        "latency_ms": int(latency_ms),
    }


async def decide(request: AuthorizationRequest, started_at: float) -> Decision:
    """Answer one CARD_AUTHORIZATION_REQUEST. Every path writes exactly one decision row.

    ONE transaction, opened with a per-CARD advisory lock, because rule (d) is a
    read-then-write reservation: at READ COMMITTED two concurrent authorizations for one
    single-use card both see no prior APPROVE and both approve. The lock serializes them on the
    Reap card id (different cards stay concurrent) and releases at commit. The precedent and the
    dialect guard are services/shopify_webhook_ingest.py's per-merchant lock: sqlite has no
    advisory locks, so an unavailable lock is logged at debug and the decision proceeds
    unserialized rather than failing — on Postgres, where it matters, it is always available,
    and tests/test_reap_external_auth_postgres.py proves the race is closed there.

    Nothing in this span does network IO, and it is sequential in one request task — the safe
    shape for databases==0.7.0's shared connection.
    """
    async with database.transaction():
        try:
            await database.execute(
                "SELECT pg_advisory_xact_lock(CAST(hashtext(CAST(:lock_key AS text)) AS bigint))",
                {"lock_key": f"reap_auth:{request.card_ref}"},
            )
        except Exception:
            logger.debug("pg_advisory_xact_lock unavailable; reap authorization not serialized")

        # (a) idempotency. A retried request gets the verdict we already gave — re-evaluating
        # would run rule (d) against the reservation our own earlier APPROVE created and
        # decline the very authorization we approved.
        stored = await find_decision(request.event_id)
        if stored is not None:
            return Decision(
                decision=str(stored["decision"]),
                reason=stored["reason"],
                reason_code=str(stored["reason_code"]),
                amount_minor=stored["amount_minor"],
                currency=stored["currency"],
                merchant_verified=bool(stored["merchant_verified"]),
                replayed=True,
            )

        card = await find_by_issuer_ref(request.card_ref)
        outcome = await _evaluate(request, card, datetime.now(timezone.utc))

        latency_ms = max(0, int((time.monotonic() - started_at) * 1000))
        claimed = await record_decision(
            _decision_values(request, card, outcome, latency_ms)
        )
        if not claimed:
            # Lost a race for this event_id without the advisory lock holding. The stored row is
            # the answer; ours is discarded rather than being a second verdict for one
            # authorization.
            existing = await find_decision(request.event_id)
            if existing is not None:
                return Decision(
                    decision=str(existing["decision"]),
                    reason=existing["reason"],
                    reason_code=str(existing["reason_code"]),
                    amount_minor=existing["amount_minor"],
                    currency=existing["currency"],
                    merchant_verified=bool(existing["merchant_verified"]),
                    replayed=True,
                )

    if latency_ms > LATENCY_WARN_MS:
        # Reap's budget is 1.6s end to end and this clock does not include either network leg.
        logger.warning(
            f"reap authorization slow: latency_ms={latency_ms} event_id={request.event_id} "
            f"reason_code={outcome.reason_code}"
        )
    return replace(outcome, replayed=False)

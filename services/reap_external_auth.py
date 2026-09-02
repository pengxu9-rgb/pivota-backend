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

A WRONG PIN IS NOT HARMLESS, AND AN EARLIER VERSION OF THIS COMMENT CLAIMED IT WAS.
That claim said a bad descriptor "mis-approves nothing, because a wrong descriptor still has to
match a pin". It was false, and the mechanism is the pinning itself: when the FIRST
authorization for a domain normalizes to a token that identifies an ACQUIRER rather than a
merchant — "SQ *HONEST SHOP" reduced to "sq" under the old prefix-keeping rule — that token
becomes the pin. The next authorization from a DIFFERENT merchant behind the same acquirer
normalizes to the same token, matches, and is approved merchant_verified=TRUE. The wrong pin
does not merely fail to protect; it manufactures positive evidence for an unrelated merchant.

Two guards, because the first is a heuristic: normalize_descriptor keeps the LONGER side of a
'*' split (acquirer tags are short, merchant names are not), and is_pinnable REFUSES to learn a
descriptor carrying fewer than MIN_PINNABLE_ALNUM alphanumerics. A domain that never learns is
strictly safer than one that learns a token — it simply keeps approving merchant_verified=false
under the cap, the single use and the expiry.

Nothing un-pins on its own, so db/agent_card_auth_decisions exposes unpin_descriptor and
pin_descriptor_manual and the runbook carries the recipe: a domain alarming merchant_mismatch is
an operator action, not a wait.

FOLLOW-UP (not in this change): CARD_TRANSACTION_CREATED also carries a merchant object. Pinning
from the webhook as well would let a domain be taught by a settled, reconciled transaction
rather than only by a live decision under a 1.6s budget — source='webhook' exists in the schema
for it.

TIME IS A CORRECTNESS PROPERTY HERE, NOT A PERFORMANCE ONE. Reap declines at 1.6s, so an answer
computed after that is an answer nobody acted on — and an APPROVE recorded then would reserve a
single-use card against a purchase that was already refused, killing the buyer's retry with
`already_authorized`. Hence bounded lock/statement timeouts on the transaction AND a deadline
downgrade before the row is written (_arm_deadlines, decide()).

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
from typing import Any, Dict, List, Optional, Tuple

from db.agent_card_auth_decisions import (
    approved_total_minor,
    find_decision,
    has_approval,
    list_descriptors,
    pin_descriptor,
    record_decision,
    touch_descriptor,
)
from db.agent_issued_cards import count_issued_by_issuer_ref, find_by_issuer_ref
from db.database import IS_POSTGRES, database
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

# Transaction-scoped ceilings, armed on Postgres only (see _arm_deadlines). lock_timeout is the
# tighter of the two because contention on ONE card is the expected case and losing that race
# should cost the loser its whole budget, not the shopper theirs; statement_timeout bounds
# everything else inside Reap's 1.6s.
LOCK_TIMEOUT_MS = 400
STATEMENT_TIMEOUT_MS = 1200

# Past this, an APPROVE is downgraded to a DECLINE (see decide()). Default matches
# STATEMENT_TIMEOUT_MS: the database ceiling and the answer-is-worthless line are the same
# moment, and letting them drift apart just makes one of them unreachable.
DEFAULT_DEADLINE_MS = 1200


# Module-level reference so tests move OUR clock by patching
# services.reap_external_auth._now_monotonic, never the stdlib module object — patching
# time.monotonic globally breaks the harness that is doing the patching.
_now_monotonic = time.monotonic


def deadline_ms() -> int:
    """REAP_EXTERNAL_AUTH_DEADLINE_MS, floored at 1ms. A misconfigured 0 would downgrade every
    approval on the rail to deadline_exceeded — the dial must not be able to silently mean
    "approve nothing"."""
    raw = str(os.getenv("REAP_EXTERNAL_AUTH_DEADLINE_MS") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_DEADLINE_MS
    except ValueError:
        value = DEFAULT_DEADLINE_MS
    return max(1, value)


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


def _approve(reason_code: str = "approved", **over: Any) -> Decision:
    return Decision(decision="APPROVE", reason=None, reason_code=reason_code, **over)


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



# The shortest descriptor we will LEARN. Below this a normalized descriptor carries no merchant
# identity — see normalize_descriptor's acquirer-prefix note — and pinning it would lock a domain
# to a token that matches unrelated merchants.
MIN_PINNABLE_ALNUM = 3


def _scrub(text: str) -> str:
    """Non-alphanumerics to spaces, whitespace collapsed. Punctuation becomes a SPACE rather
    than being deleted, so "ACME-STORE" and "ACME STORE" normalize together instead of to
    "acmestore" and "acme store"."""
    return " ".join(
        "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in text).split()
    )


def normalize_descriptor(value: Optional[str]) -> str:
    """The ONE descriptor normalizer. A second implementation would silently un-pin every
    merchant, because the registry stores what this function produced.

    casefold -> split on the first '*' and keep the LONGER side -> non-alphanumerics to
    spaces -> collapse whitespace.

      "ACME Store, Inc.*1234"  ->  "acme store inc"     (suffix tag dropped)
      "SQ *HONEST SHOP"        ->  "honest shop"        (acquirer PREFIX dropped)
      "acme-store inc"         ->  "acme store inc"

    WHY THE LONGER SIDE, AND WHAT THAT HEURISTIC IS WORTH. Acquirers put their tag on either
    side of the '*': "ACME STORE*1234" (suffix) and "SQ *ACME STORE", "PAYPAL *ACME",
    "TST* ACME" (prefix) are all in the wild. An earlier cut of this function always kept the
    PREFIX, which mapped every Square merchant to "sq" — and because the first authorization for
    a domain PINS what it saw, that pinned "sq" and then approved any other Square merchant
    against it with merchant_verified=true. The tag is short and the merchant name is not, so
    the longer side is the better guess; it is a heuristic and not a rule, which is exactly why
    MIN_PINNABLE_ALNUM exists as the second line of defence: a side that wins and still carries
    no identity is refused as a pin rather than trusted as one.
    """
    raw = (value or "").casefold().strip()
    if "*" in raw:
        left, _, right = raw.partition("*")
        left_norm, right_norm = _scrub(left), _scrub(right)
        return left_norm if len(left_norm) >= len(right_norm) else right_norm
    return _scrub(raw)


def is_pinnable(name_norm: str) -> bool:
    """Whether a normalized descriptor is specific enough to LEARN.

    "", "sq", "tst" and anything that scrubs to nothing ("***", "!!!") are not. The failure this
    prevents is the sharp one: once a useless token IS the pin for a domain, the next
    authorization carrying the same token — from a DIFFERENT merchant behind the same acquirer —
    matches it and is approved merchant_verified=true. A domain that never learns is strictly
    safer, because the cap, the single use and the expiry still bound it, and every one of its
    authorizations is queryable as merchant_verified=false.
    """
    return sum(1 for ch in name_norm if ch.isalnum()) >= MIN_PINNABLE_ALNUM


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
    """Rule (g). Returns True (matched a pin), False (not verified — the domain had no pins), or
    None (the domain has pins and this descriptor is not one of them => decline).

    False covers TWO cases that must not be conflated in code even though they answer alike:
    the domain had no pins and we LEARNED this descriptor, and the domain had no pins and the
    descriptor was too weak to learn, so the domain stays unlearned. Both approve
    merchant_verified=false; only the first writes.
    """
    name_norm = normalize_descriptor(request.merchant_name)
    country = (request.merchant_country or "").strip().upper()
    pins: List[Dict[str, Any]] = await list_descriptors(merchant_domain)
    if not pins:
        if is_pinnable(name_norm):
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


def _select_leg(request: AuthorizationRequest,
                card_currency: str) -> Tuple[bool, Optional[Decimal]]:
    """(is either leg in the card's currency, the amount the cap is enforced against).

    The two halves are returned TOGETHER rather than left for the caller to re-derive, because
    they are the same decision: `currency_mismatch` and `amount_unparseable` are different
    declines, and telling them apart from a bare `None` needs the currency test repeated at the
    call site — a second copy of this function's matching rule, free to drift from it.

    THE FIELD SEMANTICS, verified against docs.reap.global/transactions/amounts on 2026-09-02
    (same convention as services/reap_webhooks.py, which states where its wire assumptions come
    from): `currency`/`amount` is the BILLING leg — the cardholder's billing currency and the
    value that actually debits the account — while `originalCurrency`/`originalAmount` is the
    MERCHANT PRESENTMENT, "the currency the merchant charged in". Reap converts presentment to
    billing with a conversionRate, and the docs are explicit that for a domestic transaction
    "both currency pairs are identical and the conversion rate is 1.0".

    That last sentence is why both-legs-in-the-card's-currency takes the MAX rather than the
    presentment. When both legs are the card's currency the transaction is domestic and the two
    numbers are supposed to be EQUAL; a request where they differ is anomalous, and a
    presentment-first rule would let `originalAmount: 0.01` with `amount: 999999.00` pass a cap
    check for one minor unit while the account is debited a million. Taking the larger costs
    nothing when they agree (max of equals) and refuses to be steered when they do not.

    Presentment-first survives only where it is actually informative: a FOREIGN billing leg,
    where the merchant's own number is the one our cap was quoted in.
    """
    original_is_card = bool(request.original_currency) and request.original_currency == card_currency
    billing_is_card = bool(request.currency) and request.currency == card_currency
    if original_is_card and billing_is_card:
        legs = [a for a in (request.original_amount, request.amount) if a is not None]
        return True, (max(legs) if legs else None)
    if original_is_card:
        return True, request.original_amount
    if billing_is_card:
        return True, request.amount
    return False, None


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

    # (b2) TWO live cards claiming one issuer reference. issuer_card_ref carries no unique
    # constraint, so this is representable, and it is not a tie we may break: the two rows can
    # carry different caps and different merchant_domains, and find_by_issuer_ref's ORDER BY
    # picks one of them deterministically but not correctly. Declining is the only answer that
    # does not silently enforce the wrong instrument's limits.
    if await count_issued_by_issuer_ref(request.card_ref) > 1:
        alarm(
            "CARD_AUTH_AMBIGUOUS_CARD", event_id=request.event_id, card_id=card_id,
            issuer_card_ref=request.card_ref, reason_code="ambiguous_card",
        )
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "ambiguous_card")

    # (c) liveness
    if card["status"] != "issued":
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "card_not_live")
    if _is_expired(card["expires_at"], now):
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "card_expired")

    # (d) the single-use reservation. Before the amount work on purpose: a card already spent
    # must decline identically whatever this authorization is for. has_approval counts only
    # approvals that MOVED MONEY (amount_minor > 0), so a $0.00 verification does not burn the
    # card it was checking.
    if card["single_use"] and await has_approval(card_id):
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "already_authorized")

    # (e) channel
    if request.channel not in ALLOWED_CHANNELS:
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "channel_not_allowed")

    # (f) the amount, IN THE CARD'S CURRENCY. See _select_leg for which leg and why.
    card_currency = str(card["currency"] or "").strip().upper()
    currency_matched, raw_amount = _select_leg(request, card_currency)
    if not currency_matched:
        # Neither leg is in the card's currency: an FX conversion we did not authorize stands
        # between this charge and our cap, so the cap is not enforceable on it.
        alarm(
            "CARD_AUTH_CURRENCY_MISMATCH", event_id=request.event_id, card_id=card_id,
            issuer_card_ref=request.card_ref, reason_code="currency_mismatch",
        )
        return _decline(REASON_TRANSACTION_NOT_ALLOWED, "currency_mismatch")

    # (f2) A ZERO-AMOUNT VERIFICATION AUTHORIZATION. Merchants routinely run a $0.00 auth to
    # check a card is live before charging it; declining those declines the purchase that
    # follows. It is approved, recorded with amount_minor 0 — which keeps it out of the rule (d)
    # reservation and out of nothing else — and it does NOT pin a descriptor: a verification
    # tells us the card works, not that this merchant is the one the card was minted for, and
    # learning from it would let a zero-cost probe teach the registry.
    if raw_amount is not None and raw_amount == 0:
        return _approve(
            reason_code="zero_amount_verification", amount_minor=0, currency=card_currency
        )

    amount_minor = major_to_minor(raw_amount, card_currency) if raw_amount is not None else None
    if amount_minor is None:
        return _decline(
            REASON_TRANSACTION_NOT_ALLOWED, "amount_unparseable", currency=card_currency
        )

    # (f3) the cap, CUMULATIVELY. For a single-use card the sum is provably 0 (rule (d) above
    # declined anything with a prior spend approval), so this is the same comparison it always
    # was. For a MULTI-USE card it is the difference between amount_cap_minor bounding each
    # authorization and bounding the card: without the sum, ten authorizations at the cap spend
    # ten times the cap, which is not a cap.
    #
    # over_cap is the ONE rule that answers INSUFFICIENT_BALANCE: it is the only decline here
    # that means "this instrument does not carry that much", which is what the cardholder-facing
    # message says. Every other rule is our own control and maps to TRANSACTION_NOT_ALLOWED.
    committed_minor = await approved_total_minor(card_id)
    if committed_minor + amount_minor > int(card["amount_cap_minor"]):
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


def _stored_decision(row: Dict[str, Any]) -> Decision:
    return Decision(
        decision=str(row["decision"]),
        reason=row["reason"],
        reason_code=str(row["reason_code"]),
        amount_minor=row["amount_minor"],
        currency=row["currency"],
        merchant_verified=bool(row["merchant_verified"]),
        replayed=True,
    )


async def _arm_deadlines() -> None:
    """SET LOCAL lock_timeout / statement_timeout for this transaction, on Postgres only.

    WHY THIS EXISTS. pg_advisory_xact_lock blocks INDEFINITELY, and DB_STATEMENT_TIMEOUT_SECONDS
    defaults to 0 — no ceiling (db/database.py). So two authorizations on one card, or any slow
    statement, produce the worst outcome this rail has: Reap gives up at 1.6s and declines the
    purchase, our transaction eventually commits an APPROVE anyway, and that APPROVE reserves a
    single-use card against a charge that never happened. The buyer's real retry then declines
    `already_authorized` and the card is dead. A phantom approval is worse than no answer.

    Bounded, the same situation aborts the statement, rolls the transaction back, and returns a
    500 — which Reap also declines, but with NO row and nothing reserved. Same visible outcome
    for the shopper, no wreckage.

    NOT wrapped in try/except, unlike the shopify per-merchant lock this pattern comes from
    (services/shopify_webhook_ingest.py). There an unavailable lock costs a forked hash chain
    and proceeding is the better trade. Here a swallowed exception is precisely the bug: a
    lock_timeout caught and logged at debug would proceed UNSERIALIZED and re-open the
    double-approve race — the failure would look like the fix. Dialect is decided by IS_POSTGRES
    rather than by catching, so sqlite skips these statements without a catch that could also
    swallow a real timeout.
    """
    if not IS_POSTGRES:
        return
    await database.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
    await database.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")


async def _take_card_lock(card_ref: str) -> None:
    """Serialize decisions for ONE Reap card. Different cards stay concurrent.

    Rule (d) is a read-then-write reservation, and at READ COMMITTED two concurrent
    authorizations both see no prior APPROVE and both approve. This is what closes that, and it
    must be taken BEFORE the first read of the decisions table — a lock acquired after the read
    it is meant to protect serializes nothing. tests/test_reap_external_auth.py asserts the
    ORDER, not merely the presence.
    """
    if not IS_POSTGRES:
        return
    await database.execute(
        "SELECT pg_advisory_xact_lock(CAST(hashtext(CAST(:lock_key AS text)) AS bigint))",
        {"lock_key": f"reap_auth:{card_ref}"},
    )


async def decide(request: AuthorizationRequest, started_at: float) -> Decision:
    """Answer one CARD_AUTHORIZATION_REQUEST. Every path writes exactly one decision row.

    ONE transaction, opened with bounded timeouts and a per-CARD advisory lock. Nothing in this
    span does network IO, and it is sequential in one request task — the safe shape for
    databases==0.7.0's shared connection.
    """
    async with database.transaction():
        await _arm_deadlines()
        await _take_card_lock(request.card_ref)

        # (a) idempotency. A retried request gets the verdict we already gave — re-evaluating
        # would run rule (d) against the reservation our own earlier APPROVE created and
        # decline the very authorization we approved.
        stored = await find_decision(request.event_id)
        if stored is not None:
            return _stored_decision(stored)

        card = await find_by_issuer_ref(request.card_ref)
        outcome = await _evaluate(request, card, datetime.now(timezone.utc))

        latency_ms = max(0, int((_now_monotonic() - started_at) * 1000))

        # THE DEADLINE DOWNGRADE. Reap waits 1.6s and then declines on its own; past that point
        # an APPROVE we record is a decision NOBODY ACTED ON, and on a single-use card it
        # reserves the instrument against a purchase that was already refused. So a late
        # approval is recorded as the decline it effectively became. Declines are NOT
        # downgraded: a late decline agrees with what Reap did, and rewriting its reason_code
        # would destroy the evidence of which rule actually fired.
        if outcome.decision == "APPROVE" and latency_ms > deadline_ms():
            logger.warning(
                f"reap authorization missed its deadline: latency_ms={latency_ms} "
                f"event_id={request.event_id} downgraded_from={outcome.reason_code}"
            )
            outcome = _decline(
                REASON_TRANSACTION_NOT_ALLOWED, "deadline_exceeded",
                amount_minor=outcome.amount_minor, currency=outcome.currency,
                merchant_verified=outcome.merchant_verified,
            )

        claimed = await record_decision(
            _decision_values(request, card, outcome, latency_ms)
        )
        if not claimed:
            # Lost a race for this event_id. The stored row is the answer; ours is discarded
            # rather than being a second verdict for one authorization.
            existing = await find_decision(request.event_id)
            if existing is None:
                # The insert was refused AND no row is visible. We cannot explain that, and the
                # one thing we must not do is answer APPROVE with nothing recorded — which is
                # what falling through to `outcome` would do. Raising ends as a 500, which Reap
                # declines. Event id only: never the body.
                raise RuntimeError(
                    f"reap authorization decision was neither recorded nor found: "
                    f"event_id={request.event_id}"
                )
            return _stored_decision(existing)

    if latency_ms > LATENCY_WARN_MS:
        # Reap's budget is 1.6s end to end and this clock does not include either network leg.
        logger.warning(
            f"reap authorization slow: latency_ms={latency_ms} event_id={request.event_id} "
            f"reason_code={outcome.reason_code}"
        )
    return replace(outcome, replayed=False)

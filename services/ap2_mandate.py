"""
AP2 verifiable-credential mandates (ADR-012, authority layer — first slice).

Mandates are the *authority* half of AP2: instead of an opaque scope list, the
user's delegation to the agent travels as signed, verifiable objects — an
**Intent** mandate (user authorizes an agent to act within constraints), a
**Cart** mandate (a specific cart is approved), and a **Payment** mandate (the
payment itself is authorized). Each is signed by its **issuer's DID** and
verified against the key resolved from that DID — reusing the same DID
resolution as the signed rail's identity (services/ap2_identity.py).

This module ships the verification *primitive* and constraint enforcement. It is
deliberately not yet wired into the grant/transaction flow, and the full
Intent→Cart→Payment chain-linkage + trusted-issuer *registry* are later slices
(see ADR-012). Everything here **fails closed**: any structural, signature,
temporal, binding, or constraint problem raises ``MandateError``.

SECURITY — the primitive's *defaults are permissive by design* and the money-
gating wiring layer MUST tighten them, never rely on them:
- ``verify_mandate(trusted_issuers=None)`` **skips** the issuer-trust check. The
  wiring layer MUST pass the trusted-issuer set — otherwise a well-formed mandate
  self-signed by ANY attacker-generated DID verifies.
- ``check_mandate_constraints`` treats an **absent** constraint dimension as
  *unconstrained* (permit). At the authority boundary the caller MUST require a
  ``constraints`` block and treat "no applicable constraint" as **deny**, not
  allow. (A malformed constraint value is already rejected here.)
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple

from services.ap2_identity import is_did, resolve_agent_identity
from services.crypto_service import crypto_service

INTENT_MANDATE = "IntentMandate"
CART_MANDATE = "CartMandate"
PAYMENT_MANDATE = "PaymentMandate"
_MANDATE_TYPES = frozenset({INTENT_MANDATE, CART_MANDATE, PAYMENT_MANDATE})

_REQUIRED_FIELDS = ("type", "issuer", "subject", "issued_at", "expires_at")

# A resolver maps a DID -> (public_key_pem, algorithm). Injectable for tests.
Resolver = Callable[[str], Awaitable[Tuple[str, str]]]


class MandateError(ValueError):
    """A mandate failed structural, signature, temporal, or binding validation."""


def _parse_ts(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise MandateError(f"{field} must be an ISO-8601 string")
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise MandateError(f"{field} is not a valid ISO-8601 timestamp")
    # Naive timestamps are treated as UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def verify_mandate(
    mandate: Dict[str, Any],
    signature: str,
    *,
    now: Optional[datetime] = None,
    expected_subject: Optional[str] = None,
    trusted_issuers: Optional[Iterable[str]] = None,
    leeway_seconds: int = 60,
    resolver: Resolver = resolve_agent_identity,
) -> Dict[str, Any]:
    """
    Verify a single mandate and return it on success.

    ``signature`` is the detached base64 signature over the canonical JSON of the
    ``mandate`` object exactly as passed (do not embed the signature in it).

    Checks, all fail-closed:
    - structure: required fields present, known ``type``, issuer & subject are DIDs
    - trust: if ``trusted_issuers`` is given, ``issuer`` must be in it
    - binding: if ``expected_subject`` is given, ``subject`` must equal it
    - temporal: ``issued_at`` not in the future (± leeway), ``expires_at`` in the future
    - signature: verifies against the key resolved from the issuer DID
    """
    if not isinstance(mandate, dict):
        raise MandateError("mandate must be an object")

    missing = [f for f in _REQUIRED_FIELDS if f not in mandate]
    if missing:
        raise MandateError(f"mandate missing fields: {', '.join(missing)}")

    # isinstance guard first: `mandate["type"]` is attacker-controlled and an
    # unhashable value ([]/{}) would raise TypeError on the frozenset membership
    # test — a non-MandateError escape at the pre-signature boundary.
    if not isinstance(mandate["type"], str) or mandate["type"] not in _MANDATE_TYPES:
        raise MandateError(f"unknown mandate type: {mandate['type']!r}")

    issuer = mandate["issuer"]
    subject = mandate["subject"]
    if not is_did(issuer):
        raise MandateError("mandate issuer must be a DID")
    if not is_did(subject):
        raise MandateError("mandate subject must be a DID")

    if trusted_issuers is not None and issuer not in set(trusted_issuers):
        raise MandateError(f"issuer not trusted: {issuer}")

    if expected_subject is not None and subject != expected_subject:
        raise MandateError("mandate subject does not match the presenting agent")

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_ts = now.timestamp()
    issued_at = _parse_ts(mandate["issued_at"], "issued_at")
    expires_at = _parse_ts(mandate["expires_at"], "expires_at")
    if issued_at.timestamp() > expires_at.timestamp():
        raise MandateError("mandate issued_at is after expires_at")
    if expires_at.timestamp() <= now_ts:
        raise MandateError("mandate has expired")
    if issued_at.timestamp() > now_ts + leeway_seconds:
        raise MandateError("mandate is not yet valid (issued_at in the future)")

    # Resolve the issuer's key from its DID and verify the detached signature over
    # the canonical mandate. The DID is authoritative about the algorithm.
    # Catch broadly: a did:web issuer resolves over the network, so the resolver
    # can raise non-ValueError transport errors (httpx). At this authority
    # boundary every resolver failure must fail CLOSED as MandateError, never
    # escape as an uncaught 500.
    try:
        issuer_pem, algorithm = await resolver(issuer)
    except Exception as exc:
        raise MandateError(f"unresolvable issuer DID: {exc}")

    ok = crypto_service.verify_agent_signature(
        public_key=issuer_pem,
        signature=signature,
        payload=mandate,
        algorithm=algorithm,
    )
    if not ok:
        raise MandateError("mandate signature is invalid")

    return mandate


def check_mandate_constraints(
    mandate: Dict[str, Any],
    *,
    action: Optional[str] = None,
    amount: Optional[Decimal] = None,
    currency: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> None:
    """
    Enforce a (already-verified) mandate's ``constraints`` against a concrete
    action. Raises MandateError on any violation; returns None if all satisfied.

    Recognised constraints (all optional):
    - ``actions`` / ``scope``: allowed action list      → ``action`` must be in it
    - ``max_amount``:          numeric ceiling           → ``amount`` must be ≤ it
    - ``currency``:            required currency          → ``currency`` must match
    - ``merchants``:           merchant allowlist         → ``merchant_id`` must be in it

    A malformed constraint (wrong type) raises MandateError — it never degrades
    to a looser check. In particular an allowlist MUST be a list/tuple/set: a
    bare string would otherwise make ``in`` a *substring* test and permit actions
    that were never granted (widening — the dangerous direction).
    """
    if not isinstance(mandate, dict):
        raise MandateError("mandate must be an object")

    constraints = mandate.get("constraints") or {}
    if not isinstance(constraints, dict):
        raise MandateError("mandate constraints must be an object")

    allowed = constraints.get("actions", constraints.get("scope"))
    if allowed is not None and action is not None:
        if not isinstance(allowed, (list, tuple, set)):
            raise MandateError("mandate constraints.actions must be a list")
        if action not in allowed:
            raise MandateError(f"action '{action}' not permitted by mandate")

    if "max_amount" in constraints and amount is not None:
        try:
            ceiling = Decimal(str(constraints["max_amount"]))
        except (InvalidOperation, TypeError):
            raise MandateError("mandate max_amount is not a number")
        if amount > ceiling:
            raise MandateError(
                f"amount {amount} exceeds mandate max_amount {ceiling}"
            )

    want_currency = constraints.get("currency")
    if want_currency is not None and currency is not None and currency != want_currency:
        raise MandateError(
            f"currency {currency} not permitted (mandate requires {want_currency})"
        )

    merchants = constraints.get("merchants")
    if merchants is not None and merchant_id is not None:
        if not isinstance(merchants, (list, tuple, set)):
            raise MandateError("mandate constraints.merchants must be a list")
        if merchant_id not in merchants:
            raise MandateError(f"merchant {merchant_id} not in mandate allowlist")


def _require_str(mandate: Dict[str, Any], field: str, label: str) -> str:
    value = mandate.get(field)
    if not isinstance(value, str) or not value:
        raise MandateError(f"{label} mandate missing string '{field}'")
    return value


def _as_decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise MandateError(f"{field} is not a valid amount")


async def verify_mandate_chain(
    intent: Dict[str, Any],
    intent_signature: str,
    cart: Dict[str, Any],
    cart_signature: str,
    payment: Dict[str, Any],
    payment_signature: str,
    *,
    agent_did: str,
    trusted_issuers: Iterable[str],
    action: str = "create_payment",
    now: Optional[datetime] = None,
    leeway_seconds: int = 60,
    resolver: Resolver = resolve_agent_identity,
) -> Dict[str, Dict[str, Any]]:
    """
    Verify a full AP2 Intent→Cart→Payment mandate chain and return the validated
    ``{"intent", "cart", "payment"}`` on success. Raises MandateError on any
    failure. This is the money-gating authority check, so it is **deny by
    default**: it REQUIRES ``trusted_issuers`` and REQUIRES the Intent to carry a
    non-empty ``constraints`` block.

    Each mandate is first verified individually (signature over the issuer-DID
    key, temporal window, subject bound to ``agent_did``, issuer in
    ``trusted_issuers``, correct type). Then the chain is linked and bounded:

    - **linkage:** ``cart.intent_ref == intent.id`` and ``payment.cart_ref ==
      cart.id`` (ids are inside the signed envelopes, so tamper-proof);
    - **amount:** ``payment.amount == cart.total`` and ``cart.total`` within the
      Intent's ``max_amount`` (via ``check_mandate_constraints``);
    - **currency:** ``payment.currency == cart.currency`` (and the Intent's
      required currency, if any);
    - **merchant + action:** ``cart.merchant_id`` and ``action`` satisfy the
      Intent's ``merchants`` / ``actions`` constraints.

    ``now``/``resolver`` are injectable for tests.
    """
    if trusted_issuers is None:
        raise MandateError(
            "verify_mandate_chain requires trusted_issuers (deny by default)"
        )

    # 1. Verify each mandate individually, bound to the presenting agent.
    verified: Dict[str, Dict[str, Any]] = {}
    for mandate, signature, expected_type, label in (
        (intent, intent_signature, INTENT_MANDATE, "intent"),
        (cart, cart_signature, CART_MANDATE, "cart"),
        (payment, payment_signature, PAYMENT_MANDATE, "payment"),
    ):
        vm = await verify_mandate(
            mandate,
            signature,
            now=now,
            expected_subject=agent_did,
            trusted_issuers=trusted_issuers,
            leeway_seconds=leeway_seconds,
            resolver=resolver,
        )
        if vm.get("type") != expected_type:
            raise MandateError(
                f"{label} mandate has wrong type {vm.get('type')!r} "
                f"(expected {expected_type})"
            )
        verified[label] = vm

    intent, cart, payment = verified["intent"], verified["cart"], verified["payment"]

    # 2. Linkage — ids and refs (all inside the signed envelopes).
    intent_id = _require_str(intent, "id", "intent")
    cart_id = _require_str(cart, "id", "cart")
    if _require_str(cart, "intent_ref", "cart") != intent_id:
        raise MandateError("cart.intent_ref does not match intent.id")
    if _require_str(payment, "cart_ref", "payment") != cart_id:
        raise MandateError("payment.cart_ref does not match cart.id")

    # 3. Amount + currency consistency across cart and payment.
    cart_total = _as_decimal(cart.get("total"), "cart.total")
    payment_amount = _as_decimal(payment.get("amount"), "payment.amount")
    if payment_amount != cart_total:
        raise MandateError(
            f"payment.amount {payment_amount} != cart.total {cart_total}"
        )
    cart_currency = _require_str(cart, "currency", "cart")
    if _require_str(payment, "currency", "payment") != cart_currency:
        raise MandateError("payment.currency does not match cart.currency")
    cart_merchant = _require_str(cart, "merchant_id", "cart")

    # 4. Deny by default: the Intent MUST carry constraints, and the concrete
    # cart must satisfy them (max_amount, currency, merchants, actions).
    constraints = intent.get("constraints")
    if not isinstance(constraints, dict) or not constraints:
        raise MandateError("intent mandate must carry a non-empty constraints block")
    check_mandate_constraints(
        intent,
        action=action,
        amount=cart_total,
        currency=cart_currency,
        merchant_id=cart_merchant,
    )

    return {"intent": intent, "cart": cart, "payment": payment}

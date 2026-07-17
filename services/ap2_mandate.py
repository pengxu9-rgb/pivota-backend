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

    if mandate["type"] not in _MANDATE_TYPES:
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
    if expires_at.timestamp() <= now_ts:
        raise MandateError("mandate has expired")
    if issued_at.timestamp() > now_ts + leeway_seconds:
        raise MandateError("mandate is not yet valid (issued_at in the future)")

    # Resolve the issuer's key from its DID and verify the detached signature over
    # the canonical mandate. The DID is authoritative about the algorithm.
    try:
        issuer_pem, algorithm = await resolver(issuer)
    except ValueError as exc:
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
    """
    constraints = mandate.get("constraints") or {}
    if not isinstance(constraints, dict):
        raise MandateError("mandate constraints must be an object")

    allowed = constraints.get("actions", constraints.get("scope"))
    if allowed is not None and action is not None and action not in allowed:
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
    if merchants is not None and merchant_id is not None and merchant_id not in merchants:
        raise MandateError(f"merchant {merchant_id} not in mandate allowlist")

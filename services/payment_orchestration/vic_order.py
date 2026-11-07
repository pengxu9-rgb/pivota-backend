"""The A2A rail's VIC order step: the client for the rail provider's Visa Intelligent Commerce order API.

The provider is a VIC Agent Enabler (TR-TSP): it holds the TRID, tokenizes with VTS, hosts the card and
passkey pages, makes the Visa calls and, by the 10-12 agreement, FILLS THE MERCHANT CHECKOUT ITSELF
(executor `rail_managed`). Pivota sends a confirmed order, shows the buyer the provider's hosted page,
and then only READS STATUS until the provider reports the merchant's order number. That is all.

  submit_order(order)   -> the provider's order id and its hosted page (the buyer approves there with a
                           Visa passkey). A maybe-write: an answer nobody saw is read back by
                           `order_status`, never resent.
  order_status(ref)     -> authorization (pending / approved / declined / expired / abandoned), then the
                           provider's checkout progress (not_started / in_progress / completed / failed),
                           the merchant's order number and the total charged. A read.

THE RULES THIS MODULE ENFORCES

1. NO PAYMENT CREDENTIAL CROSSES THIS MODULE. Pivota never receives, holds, forwards, logs or types a
   DPAN, cryptogram or dCVV. Every answer goes through `a2a_rail._strip_credentials` (the same rules as the
   A2A transport, unchanged) BEFORE anything reads it, and the removals are counted and logged as a count,
   never as values. Then only an ALLOWLIST of fields is read out of what is left (`VicAnswer`): a field
   the contract does not name never leaves this module, whatever it is called. Nothing here asks for a
   credential: there is no call that could return one by design.
2. STRUCTURED, ALLOWLISTED STATUSES. A status outside the contract's words is `unrecognised`, never
   mapped by guesswork.
3. NEVER RESEND A MAYBE-WRITE. A timeout, transport error, 5xx, 408 or unparseable answer to a submit is
   `outcome_unknown`; only a 4xx (other than 408/429) or an error object that says not retryable is a
   definite `refused`. The worker reads the order back by our reference (`client_reference`).
4. No body, payload, URL, email, address or member id is logged.

Dark: `from_env()` returns None unless A2A_RAIL_VIC_ORDER_STEP_ENABLED is on AND a transport is
configured. The provider has not published this API yet, so no production transport exists: today the
worker runs this step only against the in-repo fake (tests/evonet_vic_fake.py). The contract it is
written to is the one we are asking the provider for (reports/, local).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Tuple

from services.payment_orchestration import a2a_rail

logger = logging.getLogger(__name__)

A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV = "A2A_RAIL_VIC_ORDER_STEP_ENABLED"
_TRUTHY = frozenset({"1", "true", "on", "yes"})

#: The purchase type sent: human in the loop (the buyer approves this one purchase with a passkey).
PURCHASE_TYPE_HITL = "HITL"
#: The currencies an order may be sent in. The same set as the token step: only USD has been measured.
ORDER_CURRENCIES = a2a_rail.TOKEN_CURRENCIES

AUTHORIZATION_STATUSES = frozenset({"pending", "approved", "declined", "expired", "abandoned"})
CHECKOUT_STATUSES = frozenset({"not_started", "in_progress", "completed", "failed"})

_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_CODE_RE = re.compile(r"^[a-z0-9_]{1,48}\Z")
#: 4xx answers that say nothing about the order itself: our credentials, a timeout, throttling.
_RETRYABLE_4XX = frozenset({401, 403, 408, 429})


def is_enabled() -> bool:
    return (os.getenv(A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV) or "").strip().lower() in _TRUTHY


class VicOrderTransport(Protocol):
    """The provider's wire. Each call returns (http status, parsed JSON body or None) or raises on a
    transport failure (a timeout is `asyncio.TimeoutError` / `TimeoutError`)."""

    async def submit_order(self, body: Mapping[str, Any]) -> Tuple[int, Any]: ...

    async def get_order(self, client_reference: str) -> Tuple[int, Any]: ...


@dataclass
class VicAnswer:
    """One classified answer. Only these fields ever leave the module.

    kind:
      ok               a well-formed order answer (`approval_status` is one of AUTHORIZATION_STATUSES)
      refused          the provider definitely did not take the submit (4xx, a non-retryable error)
      not_found        the provider has no order under our reference (a read's 404)
      outcome_unknown  a submit with no definite answer: read it back, never resend
      retry            a read that failed (timeout, transport, 5xx, throttle): ask again later
      unrecognised     a 200 whose body is not the contract's shape
    """

    kind: str
    http_status: Optional[int] = None
    order_id: Optional[str] = None
    client_reference: Optional[str] = None
    approval_status: Optional[str] = None
    hosted_url: Optional[str] = None
    hosted_url_expires_in: Optional[int] = None
    checkout: Optional[str] = None
    merchant_order_number: Optional[str] = None
    merchant_order_status_url: Optional[str] = None
    total_minor: Optional[int] = None
    currency: Optional[str] = None
    error_code: Optional[str] = None
    credential_fields_stripped: int = 0

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def _ref(value: Any) -> Optional[str]:
    if isinstance(value, str) and _REF_RE.match(value):
        return value
    return None


def _https(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.startswith("https://") and len(value) <= 2048:
        return value
    return None


def _minor(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _word(value: Any, allowed: frozenset) -> Optional[str]:
    text = str(value or "").strip().lower() if isinstance(value, str) else ""
    return text if text in allowed else None


def parse_order(body: Any, stripped: int = 0) -> VicAnswer:
    """The allowlisted read of an ALREADY-STRIPPED order body. Pure."""
    if not isinstance(body, Mapping):
        return VicAnswer(kind="unrecognised", credential_fields_stripped=stripped)
    auth = body.get("authorization") if isinstance(body.get("authorization"), Mapping) else {}
    checkout = body.get("checkout") if isinstance(body.get("checkout"), Mapping) else {}
    expires_in = auth.get("expires_in")
    answer = VicAnswer(
        kind="ok",
        order_id=_ref(body.get("order_id")),
        client_reference=_ref(body.get("client_reference")),
        approval_status=_word(auth.get("status"), AUTHORIZATION_STATUSES),
        hosted_url=_https(auth.get("hosted_url")),
        hosted_url_expires_in=expires_in if isinstance(expires_in, int) and not isinstance(expires_in, bool) else None,
        checkout=_word(checkout.get("status"), CHECKOUT_STATUSES) if checkout else None,
        merchant_order_number=_ref(checkout.get("order_number")),
        merchant_order_status_url=_https(checkout.get("order_status_url")),
        total_minor=_minor(checkout.get("total_minor")),
        currency=checkout.get("currency") if isinstance(checkout.get("currency"), str)
        and re.fullmatch(r"[A-Z]{3}", checkout["currency"]) else None,
        credential_fields_stripped=stripped,
    )
    if answer.order_id is None or answer.approval_status is None or (checkout and answer.checkout is None):
        answer.kind = "unrecognised"
    return answer


def _error_code(body: Any) -> Tuple[Optional[str], Optional[bool]]:
    error = body.get("error") if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping) else None
    if error is None:
        return None, None
    code = str(error.get("code") or "").strip().lower()
    retryable = error.get("retryable")
    return (code if _CODE_RE.match(code) else "unknown"), (retryable if isinstance(retryable, bool) else None)


class VicOrderClient:
    """Classifies the provider's answers; strips every credential before anything reads one."""

    def __init__(self, transport: VicOrderTransport, *, timeout_s: float = 30.0) -> None:
        self._transport = transport
        self._timeout_s = float(timeout_s)

    async def submit_order(self, order: Mapping[str, Any]) -> VicAnswer:
        """Send ONE confirmed order. A maybe-write (rule 3)."""
        try:
            status, raw = await asyncio.wait_for(self._transport.submit_order(dict(order)), self._timeout_s)
        except Exception as exc:  # noqa: BLE001 - a timeout or transport failure: the outcome is unseen
            return self._done("submit", VicAnswer(kind="outcome_unknown"), exc)
        body, stripped = a2a_rail._strip_credentials(raw)
        if status in (200, 201):
            answer = parse_order(body, stripped)
            if answer.kind == "unrecognised":
                # A 2xx we cannot read may still have created an order: unseen, never resent.
                answer.kind = "outcome_unknown"
            answer.http_status = status
            return self._done("submit", answer)
        code, retryable = _error_code(body)
        refused = 400 <= status < 500 and status not in _RETRYABLE_4XX and retryable is not True
        answer = VicAnswer(kind="refused" if refused else "outcome_unknown", http_status=status,
                           error_code=code, credential_fields_stripped=stripped)
        return self._done("submit", answer)

    async def order_status(self, client_reference: str) -> VicAnswer:
        """Read the order back by OUR reference. A read: safe to repeat."""
        if not _REF_RE.match(str(client_reference or "")):
            raise ValueError("client_reference must be 1..128 safe characters")
        try:
            status, raw = await asyncio.wait_for(self._transport.get_order(client_reference), self._timeout_s)
        except Exception as exc:  # noqa: BLE001 - a read that failed: ask again later
            return self._done("status", VicAnswer(kind="retry"), exc)
        body, stripped = a2a_rail._strip_credentials(raw)
        if status == 200:
            answer = parse_order(body, stripped)
            answer.http_status = status
            return self._done("status", answer)
        code, _ = _error_code(body)
        kind = "not_found" if status == 404 else "retry"
        return self._done("status", VicAnswer(kind=kind, http_status=status, error_code=code,
                                              credential_fields_stripped=stripped))

    def _done(self, op: str, answer: VicAnswer, exc: Optional[BaseException] = None) -> VicAnswer:
        logger.info(
            "vic_order: op=%s kind=%s http_status=%s approval=%s checkout=%s stripped=%s error_type=%s",
            op, answer.kind, answer.http_status, answer.approval_status, answer.checkout,
            answer.credential_fields_stripped, type(exc).__name__ if exc is not None else None,
        )
        if answer.credential_fields_stripped:
            # The contract forbids credentials in any answer: a count, never a value.
            logger.warning(
                "vic_order: removed %s credential field(s) from a %s answer (the provider must not send them)",
                answer.credential_fields_stripped, op,
            )
        return answer


def order_amount(row: Mapping[str, Any]) -> Optional[int]:
    """The total the buyer confirmed, in minor units: the locked quote when there is one, else the unit
    price they consented to times the quantity. None when the row has neither."""
    quoted = row.get("quoted_total_minor")
    if isinstance(quoted, int) and not isinstance(quoted, bool) and quoted > 0:
        return quoted
    unit, quantity = row.get("our_price_minor"), row.get("quantity") or 1
    if isinstance(unit, bool) or not isinstance(unit, int) or not isinstance(quantity, int) or unit <= 0 or quantity <= 0:
        return None
    return unit * quantity


def build_order(row: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The order body for one purchase row, or None when the row cannot make one (the step fails it).

    The provider's hosted page shows the buyer the amount they approve (`order_amount`); the provider
    must not charge more (the contract)."""
    amount = order_amount(row)
    unit, quantity = row.get("our_price_minor"), row.get("quantity") or 1
    if amount is None:
        return None
    return {
        "client_reference": str(row["id"]),
        "member_id": str(row["member_id"]),
        "payment_method_id": str(row["payment_method_id"]),
        "purchase_type": PURCHASE_TYPE_HITL,
        "merchant": {"domain": row["merchant_domain"], "checkout_url": row["checkout_url"]},
        "amount_minor": amount,
        "currency": str(row["currency"]).upper(),
        "items": [{
            "product_key": row.get("product_key"),
            "name": row.get("product_name"),
            "variant": row.get("variant_title"),
            "quantity": quantity,
            "unit_amount_minor": unit,
        }],
        "buyer": {"email": row.get("buyer_email"), "shipping_address": row.get("shipping_address")},
    }


def from_env(transport: Optional[VicOrderTransport] = None) -> Optional[VicOrderClient]:
    """The client, or None while the dial is off or no transport exists. The provider has not published
    this API, so the job passes no transport: an armed dial with nothing to call is still None."""
    if not is_enabled() or transport is None:
        return None
    return VicOrderClient(transport)

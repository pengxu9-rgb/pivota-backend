"""The owner check of the keyed payment routes. Behind AGENT_PAYMENT_ORDER_OWNER_CHECK, default off.

Four routes compared the caller's merchant scope with the order's MERCHANT and never the caller
with the order's OWNER (orders.agent_id, the agent that created it):

  * POST /agent/v1/payments                           (route label `payments_create`)
  * POST /agent/v1/orders/payment-offer-evidence      (`payment_offer_evidence`)
  * GET  /agent/v1/payments/{payment_id}              (`payment_status`)
  * POST /agent/v1/orders/{order_id}/confirm-payment  (`payment_confirm`)

So any key in the merchant's scope that knew an order id could start or reuse a payment on
another agent's order (and receive its payment client secret), rewrite its metadata, read its
payment, or have its payment confirmed and captured and the order marked paid. The order read (GET /agent/v1/orders/{id}) and the /agent/v2 routes already refuse such
a caller with 403 "Not authorized for this order".

The rule is the one those routes use, routes.agent_forwarded_owner.order_access_allowed, and not
a second one: the caller's agent is the order's agent. Two things about that helper, stated here
because this module relies on them:

  * with PARTNER_IDENTITY_FORWARDING_ENABLED on it also serves the service that vouched for the
    order's owner, on an order stamped as created through it. That is unchanged here: it is the
    same order the service held before that flag, and the stamp is written by this service only;
  * it compares two strings, so it would call an order with NO owner a match for a caller with
    no agent id. No caller has an empty agent id today; this module does not depend on that and
    refuses an order with no owner outright.

Nobody is exempt. An internal trusted key is a caller like any other: it is served on the orders
its own agent created (every order made with that key) and refused on the rest. A checkout token
is already bound to one order of its own agent by the token binding
(routes.agent_auth.checkout_token_may_use_order), which runs first and is not changed; such a
caller is the order's agent by that binding and so passes this check.

One difference between the two, kept on purpose: the token binding strips whitespace from both
ids before it compares them, and the owner rule compares them exactly. So a token bound to an
order whose stored agent_id carries stray whitespace passes the binding and is NOT the owner
here: observed it logs `caller_kind=checkout_token`, enforced it is refused. The owner rule is
not loosened to match: trimming would make two different stored ids one owner, which can only
widen who is served, and the shared rule the other routes use does not trim either.

Modes (read per call; unset or anything else is `off`):
  off      the four routes run exactly as they did before this module existed;
  observe  nothing is refused and no answer changes; each call from a caller that is not the
           owner, at the point where `enforce` would refuse it, logs ONE warning line;
  enforce  such a call is refused with 403 "Not authorized for this order", with one warning
           line, before the route calls a PSP, writes to the order or a payment, or answers with
           anything about the order.

What still happens on a refusal, as on every early exit of these routes: POST /agent/v1/payments
emits its own checkout-failed telemetry event (the order id and the refusal text) from its
failure handler. Nothing else is recorded and nothing is written to the order.

Where each route asks. POST /agent/v1/payments asks right after its not-found answer, for EVERY
caller: enforced, a caller outside the merchant's scope that is not the owner gets this refusal
instead of the route's earlier answers about the order (already paid, canceled, total or currency
mismatch) and its merchant refusal. Its line says `scope=out`; observed, such a caller is logged
and answered exactly as before. The other three ask after their own merchant refusal, so their
lines always say `scope=in`.

The line carries closed labels and the CALLER's own agent id. It never carries the order's id,
the owner's id, an API key, a token, a client secret or a payment intent id.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from fastapi import HTTPException

import routes.agent_auth as agent_auth
from routes.agent_forwarded_owner import order_access_allowed
from services.quote_service import INTERNAL_TRUSTED_AGENT_PREFIX
from utils.logger import logger

ORDER_OWNER_CHECK_ENV = "AGENT_PAYMENT_ORDER_OWNER_CHECK"
MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"
_ACTIVE_MODES = frozenset({MODE_OBSERVE, MODE_ENFORCE})

#: The answer the routes that already check the owner give (routes.agent_v2, routes.agent_api).
ORDER_OWNER_DENIED_STATUS = 403
ORDER_OWNER_DENIED_DETAIL = "Not authorized for this order"

ROUTE_PAYMENTS_CREATE = "payments_create"
ROUTE_PAYMENT_OFFER_EVIDENCE = "payment_offer_evidence"
ROUTE_PAYMENT_STATUS = "payment_status"
ROUTE_PAYMENT_CONFIRM = "payment_confirm"
_ROUTES = frozenset(
    {ROUTE_PAYMENTS_CREATE, ROUTE_PAYMENT_OFFER_EVIDENCE, ROUTE_PAYMENT_STATUS, ROUTE_PAYMENT_CONFIRM}
)

#: (outcome, route, scope, caller_kind, owner_kind, quote) counts for this process. outcome is
#: `would_refuse` (observe) or `refused` (enforce).
ORDER_OWNER_CHECK_OUTCOMES: Counter = Counter()


def order_owner_check_mode() -> str:
    """`observe` or `enforce` when the variable says exactly that; `off` for anything else."""
    raw = str(os.getenv(ORDER_OWNER_CHECK_ENV) or "").strip().lower()
    return raw if raw in _ACTIVE_MODES else MODE_OFF


def _metadata(order: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = order.get("metadata")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _service_agent_ids() -> frozenset:
    try:
        return agent_auth.issuing_excluded_agent_ids()
    except Exception:  # noqa: BLE001 -- a log label, never a reason to fail
        return frozenset()


def _agent_kind(agent_id: str) -> str:
    if not agent_id:
        return "none"
    if agent_id.startswith(INTERNAL_TRUSTED_AGENT_PREFIX):
        return "internal_trusted"
    if agent_id in _service_agent_ids():
        return "service_agent"
    return "agent"


def _caller_kind(context: Any) -> str:
    if getattr(context, "checkout_token_payload", None) is not None:
        return "checkout_token"
    return _agent_kind(str(getattr(context, "agent_id", "") or "").strip())


def _quote_label(order: Mapping[str, Any]) -> str:
    """Is the order still inside its quote (`live`), past it (`expired`), or unstamped (`none`)?

    POST /agent/v1/payments refuses an order past metadata.pricing_quote.expires_at, so `live`
    marks a would-be refusal on an order that can still be paid. A label for the log line only:
    it never decides who is served."""
    quote = _metadata(order).get("pricing_quote")
    raw = quote.get("expires_at") if isinstance(quote, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return "none"
    try:
        expires_at = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return "none"
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return "live" if expires_at > datetime.now(timezone.utc) else "expired"


def caller_owns_order(order: Mapping[str, Any], context: Any) -> bool:
    """The existing owner rule (order_access_allowed), and an order with no owner is nobody's."""
    if not str(order.get("agent_id") or "").strip():
        return False
    if not str(getattr(context, "agent_id", "") or "").strip():
        return False
    return bool(order_access_allowed(order, context))


def check_order_owner(
    order: Mapping[str, Any], context: Any, *, route: str, mode: str, in_scope: bool = True
) -> None:
    """Refuse (enforce) or note (observe) a caller that does not own `order`.

    `mode` is the caller's reading of order_owner_check_mode(): the routes read it once and do not
    come here at all when it is `off`. Anything that is not `enforce` refuses nothing.
    `in_scope` only labels the line (`scope=in|out`): whether the caller passed, or would pass, the
    route's own merchant check. It never decides who is refused."""
    if route not in _ROUTES:
        raise ValueError(f"unknown owner-check route label: {route!r}")
    if mode not in _ACTIVE_MODES:
        return
    if caller_owns_order(order, context):
        return
    outcome = "refused" if mode == MODE_ENFORCE else "would_refuse"
    caller_kind = _caller_kind(context)
    owner_kind = _agent_kind(str(order.get("agent_id") or "").strip())
    quote = _quote_label(order)
    scope = "in" if in_scope is True else "out"
    ORDER_OWNER_CHECK_OUTCOMES[(outcome, route, scope, caller_kind, owner_kind, quote)] += 1
    # utils.logger at WARNING: plain module loggers lose INFO in a deployed service.
    logger.warning(
        "order_owner_check outcome=%s mode=%s route=%s scope=%s caller_kind=%s owner_kind=%s quote=%s caller_agent_id=%s",
        outcome, mode, route, scope, caller_kind, owner_kind, quote, getattr(context, "agent_id", None),
    )
    if mode == MODE_ENFORCE:
        raise HTTPException(status_code=ORDER_OWNER_DENIED_STATUS, detail=ORDER_OWNER_DENIED_DETAIL)


async def check_payment_order_owner(order_id: Any, context: Any, *, mode: str) -> None:
    """check_order_owner for the payment status route, which holds a payment row and not its order:
    reads the order's owner (and its metadata, for the vouching stamp and the quote label) first.

    A read that fails is not an answer about the owner. Observing, it is logged and the caller is
    served as before, because `observe` refuses nothing. Enforcing, it is raised: the route then
    answers 500 "Failed to get payment status" (its handler for any failure; not a 503, also when
    the database was only busy) and serves nothing."""
    if mode not in _ACTIVE_MODES:
        return
    from db.database import database

    try:
        row = await database.fetch_one(
            "SELECT agent_id, metadata FROM orders WHERE order_id = :order_id",
            {"order_id": order_id},
        )
    except Exception as exc:  # noqa: BLE001 -- see the docstring: served only while observing
        logger.warning(
            "order_owner_check outcome=owner_read_failed mode=%s route=%s error=%s",
            mode, ROUTE_PAYMENT_STATUS, type(exc).__name__,
        )
        if mode == MODE_ENFORCE:
            raise
        return
    check_order_owner(dict(row) if row else {}, context, route=ROUTE_PAYMENT_STATUS, mode=mode)

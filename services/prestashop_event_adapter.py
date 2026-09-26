"""Pure mapper: one event from the Pivota PrestaShop module -> canonical events.

PrestaShop ships **no outbound webhooks**. There is no "create webhook" REST
call, no signed delivery, nothing to subscribe to: the platform's extension
point is a *hook* that runs inside the shop's own PHP process. So, exactly like
the Salesforce B2C cartridge, Pivota ships the sender —
``integrations/prestashop-module/pivotatelemetry/`` — and this module maps what
that sender emits. The wire contract is fixed in BOTH places and pinned by
``tests/test_prestashop_module_contract.py``; changing a key here without
changing the PHP breaks that test.

One event as the module emits it::

    {"event_id": "actionValidateOrder:1042:2",
     "hook": "actionValidateOrder",
     "occurred_at": "2026-09-05T10:00:00+00:00",
     "order": {"id": 1042, "reference": "XKBKNABJK", "id_cart": 55,
               "id_customer": 9, "currency": "EUR", "current_state": 2,
               "state_key": "payment",
               "state_flags": {"paid": true, "shipped": false,
                               "delivery": false, "logable": true},
               "valid": true, "total_paid_tax_incl": "40.56",
               "total_paid_real": "40.56", "payment_module": "ps_checkout",
               "date_add": "...", "date_upd": "..."},
     "order_slip": null}

Why ``state_key`` and not ``current_state``: order-state ids are rows in the
shop's own ``order_state`` table and differ per install (and per language
pack). The module resolves the id against ``Configuration::get('PS_OS_*')``
inside the shop, so this receiver never keys money on a shop-specific integer.
``current_state`` is still carried, for diagnostics only.

Why a ``refund`` STATE alone emits nothing: PrestaShop's refund state
(``PS_OS_REFUND``) records that someone marked the order refunded; it carries
no amount, no per-slip identity, and can be set and unset. The *credit slip*
(``OrderSlip``, hook ``actionOrderSlipAdd``) is the fact that carries money and
its own id. Emitting ``refund.succeeded`` on both would double-count every
refund of every order that also gets flipped to the refund state, so the state
transition is ignored and only the slip is mapped.

Verification status of the PrestaShop facts relied on here is recorded in
``docs/PRESTASHOP_TELEMETRY.md``; anything the docs did not state is marked
UNVERIFIED in a comment at the point it is used.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from services.commerce_order_ref import build_order_ref
from services.merchant_event_ingest_service import MerchantCommerceEvent


PLATFORM = "prestashop"
SOURCE = "prestashop_module_outbox"

# The three hooks the shipped module registers. Anything else is a sender we
# did not write (or a newer module than this receiver), and is IGNORED rather
# than rejected.
SUPPORTED_PRESTASHOP_HOOKS = frozenset(
    {
        "actionvalidateorder",
        # The shipped module registers PostUpdate, not Update: OrderHistory
        # fires `actionOrderStatusUpdate` BEFORE the new state is written
        # (classes/order/OrderHistory.php L110), so `total_paid_real` and
        # `current_state` are still the old ones there. Both spellings are
        # accepted so a shop running an older build of the module is not
        # silently dropped.
        "actionorderstatusupdate",
        "actionorderstatuspostupdate",
        "actionorderslipadd",
    }
)

# The vocabulary the module resolves in PHP from Configuration::get('PS_OS_*').
# `error` comes from PS_OS_ERROR alone: **PS_OS_PAYMENT_ERROR does not exist**
# in PrestaShop (verified against install-dev/data/xml/configuration.xml on
# 8.2.x — a repo-wide search finds no such key). Reading it would return false
# and silently match order state 0, so the module must never look it up.
STATE_KEYS = frozenset(
    {"payment", "canceled", "refund", "error", "shipped", "delivered", "other"}
)

# ---- the wire contract with integrations/prestashop-module/ -------------------
#
# Declared here, in the receiver, because this is the side that is tested.
# tests/test_prestashop_module_contract.py asserts BOTH directions against the
# shipped PHP: the module emits exactly these keys, and every key this mapper
# reads is one of them. A key the PHP renames therefore fails a test rather
# than silently arriving as None.
#
# Not every declared key is read: `reference`, `current_state`, `valid`,
# `date_add`, `date_upd` and the non-`paid` state flags are diagnostics the
# module sends so a support question can be answered from the payload. They are
# deliberately in the contract and deliberately unread.
MODULE_ENVELOPE_KEYS = frozenset({"events", "shop_url"})
MODULE_EVENT_KEYS = frozenset({"event_id", "hook", "occurred_at", "order", "order_slip"})
MODULE_ORDER_KEYS = frozenset(
    {
        "id",
        "reference",
        "id_cart",
        "id_customer",
        "currency",
        "current_state",
        "state_key",
        "state_flags",
        "valid",
        "total_paid_tax_incl",
        "total_paid_real",
        "payment_module",
        "date_add",
        "date_upd",
    }
)
# OrderState booleans. `template` is NOT one — it is a multilang STRING on
# OrderState (classes/order/OrderState.php), so it is not carried here.
MODULE_STATE_FLAG_KEYS = frozenset({"paid", "shipped", "delivery", "logable"})
MODULE_SLIP_KEYS = frozenset(
    {
        "id",
        "amount",
        "shipping_cost_amount",
        "total_products_tax_incl",
        "total_shipping_tax_incl",
        "date_add",
    }
)

ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)


class UnsupportedPrestaShopEvent(ValueError):
    """A hook this bridge does not map. The receiver counts it `ignored`."""


class NoPrestaShopCanonicalEvents(UnsupportedPrestaShopEvent):
    """Understood, but says nothing canonical — a shipped/delivered/refund
    state transition. Ignored with a 200 so the module's outbox deletes it."""


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value if value is not None else "").strip()
    return normalized or None


def _obj(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _occurred_at(value: Any) -> datetime:
    raw = _text(value)
    if not raw:
        raise ValueError("PrestaShop event is missing occurred_at")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PrestaShop event occurred_at is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _amount_cents(value: Any, currency: Optional[str]) -> Optional[int]:
    """A PrestaShop decimal string -> minor units, ROUND_HALF_UP."""
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("PrestaShop amount is invalid") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("PrestaShop amount is invalid")
    multiplier = (
        Decimal("1")
        if str(currency or "").upper() in ZERO_DECIMAL_CURRENCIES
        else Decimal("100")
    )
    return int((amount * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _entity_event_id(store_id: str, event_type: str, entity_id: str) -> str:
    material = json.dumps(
        [PLATFORM, store_id, event_type, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{PLATFORM}:{event_type}:{digest}"


def _int(value: Any) -> Optional[int]:
    raw = _text(value)
    if raw is None:
        return None
    try:
        return int(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None


def _slip_amount_cents(slip: Dict[str, Any], currency: str) -> int:
    """The refund total of one credit slip, in minor units.

    ``total_products_tax_incl`` + ``total_shipping_tax_incl`` is authoritative,
    and ``amount`` must NOT be used while they are available. Verified against
    ``src/Adapter/Order/Refund/OrderSlipCreator.php`` (8.2.x), which writes::

        $orderSlip->amount = $add_tax ? ...total_products_tax_EXCL
                                      : ...total_products_tax_incl;
        $orderSlip->shipping_cost_amount = $orderSlip->total_shipping_tax_incl;

    so ``amount`` is (a) products only — shipping never rides in it — and
    (b) tax-EXCLUDED on the default path, with no field on the row saying which
    basis was used. It is also overwritten wholesale by an operator-typed
    figure. Reading it as "the refund" would under-report every taxed refund.

    The one row shape where the totals are absent is
    ``OrderSlip::createPartialOrderSlip()``, which sets only ``amount`` and
    ``shipping_cost_amount`` and leaves the four totals at 0. Core never calls
    it (no callers anywhere in the 8.2.x tree) but a third-party module can, so
    a slip whose totals are missing OR sum to zero while ``amount`` is non-zero
    falls back to ``amount + shipping_cost_amount`` rather than reporting a
    refund of nothing. The module sends all four fields so this choice is made
    HERE, where it is tested, and not in unlinted PHP.

    A slip on which EVERY basis is zero is refused outright, as a
    ``ValueError`` the receiver counts as ``rejected``. The ledger dedupes
    first-write-wins on the event key, and that key is the slip id — so a
    ``refund.succeeded`` carrying ``amount_cents = 0`` is not a harmless
    under-report, it is a permanent shadow: the real amount for the same slip
    can never be written afterwards. Money is emitted only on a positive
    settled amount.
    """
    products = _text(slip.get("total_products_tax_incl"))
    shipping = _text(slip.get("total_shipping_tax_incl"))
    if products is not None:
        modern = _amount_cents(products, currency) or 0
        if shipping is not None:
            modern += _amount_cents(shipping, currency) or 0
        if modern > 0:
            return modern
    legacy = 0
    legacy_raw = _text(slip.get("amount"))
    if legacy_raw is not None:
        legacy = _amount_cents(legacy_raw, currency) or 0
        shipping_cost = _text(slip.get("shipping_cost_amount"))
        if shipping_cost is not None:
            legacy += _amount_cents(shipping_cost, currency) or 0
    if legacy > 0:
        return legacy
    raise ValueError("PrestaShop credit slip has no positive refund amount")


def _order_facts(event: Dict[str, Any]) -> Dict[str, Any]:
    order = _obj(event.get("order"))
    order_id = _int(order.get("id"))
    if not order_id:
        raise ValueError("PrestaShop event is missing order.id")
    currency = str(order.get("currency") or "").strip().upper()
    if len(currency) != 3:
        raise ValueError("PrestaShop event is missing a 3-letter order.currency")
    state_key = str(order.get("state_key") or "").strip().lower()
    if state_key not in STATE_KEYS:
        # The module resolves this from Configuration; an unknown token means a
        # sender we do not understand, not a shop-specific state id.
        raise ValueError(f"PrestaShop event has an unknown state_key: {state_key or 'missing'}")
    flags = _obj(order.get("state_flags"))
    buyer_id = _int(order.get("id_customer"))
    cart_id = _int(order.get("id_cart"))
    return {
        "order": order,
        "order_id": str(order_id),
        "currency": currency,
        "state_key": state_key,
        "paid": bool(flags.get("paid")) or state_key == "payment",
        # `id_customer` is 0 for a guest-less/back-office order; 0 is not an
        # identity, so it must not become a buyer id.
        "buyer_id": str(buyer_id) if buyer_id else None,
        "cart_id": str(cart_id) if cart_id else None,
    }


def _paid_amount_cents(order: Dict[str, Any], currency: str) -> tuple[Optional[int], str]:
    """What was actually captured, and which field said so.

    ``total_paid_real`` is the sum actually received; ``total_paid_tax_incl``
    is what the order is worth. A payment module that has not yet written
    ``total_paid_real`` leaves it at 0, so fall back rather than record a paid
    order worth nothing.
    """
    real = _amount_cents(order.get("total_paid_real"), currency)
    if real:
        return real, "total_paid_real"
    return _amount_cents(order.get("total_paid_tax_incl"), currency), "total_paid_tax_incl"


def map_prestashop_module_event(
    event: Dict[str, Any],
    *,
    store_id: str,
    delivery_id: Optional[str] = None,
) -> List[MerchantCommerceEvent]:
    """One module event -> zero or more canonical events.

    Raises ``UnsupportedPrestaShopEvent`` (which the receiver counts as
    ``ignored``) for a hook we do not map or a transition that moves no money,
    and ``ValueError`` (counted ``rejected``) for a malformed event.
    """
    if not isinstance(event, dict):
        raise ValueError("PrestaShop event must be an object")
    hook = str(event.get("hook") or "").strip()
    if hook.lower() not in SUPPORTED_PRESTASHOP_HOOKS:
        raise UnsupportedPrestaShopEvent(
            f"unsupported PrestaShop hook: {hook or 'missing'}"
        )
    occurred_at = _occurred_at(event.get("occurred_at"))
    facts = _order_facts(event)
    order = facts["order"]
    order_id = facts["order_id"]
    currency = facts["currency"]

    metadata = {
        "native_status": facts["state_key"],
        "native_payment_method": _text(order.get("payment_module")),
        "webhook_delivery_id": _text(delivery_id),
    }

    def _event(
        event_type: str,
        *,
        entity_id: str,
        amount_cents: Optional[int],
        refund_id: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> MerchantCommerceEvent:
        payload_metadata = {**metadata, **(extra_metadata or {})}
        return MerchantCommerceEvent(
            event_id=_entity_event_id(store_id, event_type, entity_id),
            event_type=event_type,
            occurred_at=occurred_at,
            platform=PLATFORM,
            source=SOURCE,
            store_id=store_id,
            buyer_id=facts["buyer_id"],
            cart_id=facts["cart_id"],
            order_id=order_id,
            # PrestaShop has no order writeback in this repo
            # (`create_prestashop_order` does not exist), so every order here
            # originated in the shop and the namespace is always the platform.
            order_ref=build_order_ref(PLATFORM, order_id),
            refund_id=refund_id,
            trace_id=_text(delivery_id) or _text(event.get("event_id")),
            amount_cents=amount_cents,
            currency=currency,
            metadata={
                key: value
                for key, value in payload_metadata.items()
                if value not in (None, "", [], {})
            },
        )

    hook_key = hook.lower()

    if hook_key == "actionorderslipadd":
        slip = _obj(event.get("order_slip"))
        slip_id = _int(slip.get("id"))
        if not slip_id:
            raise ValueError("PrestaShop credit slip event is missing order_slip.id")
        return [
            _event(
                "refund.succeeded",
                # Keyed on the SLIP, never the order: two partial refunds of
                # one order are two slips and must stay two events.
                entity_id=str(slip_id),
                amount_cents=_slip_amount_cents(slip, currency),
                refund_id=str(slip_id),
            )
        ]

    paid_cents, amount_semantics = _paid_amount_cents(order, currency)

    if hook_key == "actionvalidateorder":
        events = [
            _event(
                "order.created",
                entity_id=order_id,
                amount_cents=_amount_cents(order.get("total_paid_tax_incl"), currency),
            )
        ]
        if facts["paid"]:
            # An order validated straight into a paid state (the common case
            # for a synchronous payment module) is created AND paid.
            events.append(
                _event(
                    "order.paid",
                    entity_id=order_id,
                    amount_cents=paid_cents,
                    extra_metadata={"native_amount_semantics": amount_semantics},
                )
            )
        return events

    # actionOrderStatusUpdate
    state_key = facts["state_key"]
    if facts["paid"]:
        return [
            _event(
                "order.paid",
                entity_id=order_id,
                amount_cents=paid_cents,
                extra_metadata={"native_amount_semantics": amount_semantics},
            )
        ]
    if state_key == "canceled":
        return [_event("order.cancelled", entity_id=order_id, amount_cents=None)]
    if state_key == "error":
        return [_event("payment.failed", entity_id=order_id, amount_cents=None)]
    # `refund`, `shipped`, `delivered`, `other`: understood, no money fact.
    # See the module docstring for why `refund` is deliberately in this list.
    raise NoPrestaShopCanonicalEvents(
        f"PrestaShop state transition carries no canonical event: {state_key}"
    )

"""The canonical identity of ONE purchase, across every authority that reports it.

The same order reaches the ledger from several writers, and each of them knows
the order by its own id:

* the Stripe PSP bridge holds the Pivota ``orders`` row, so it says ``ord_1``;
* the Shopify webhook says ``6600123``, Shopify's numeric order id;
* the agent checkout route says ``ord_1`` again (checkout_id == order id);
* WooCommerce/Cafe24/SHOPLINE/Adobe/SFCC each say their own native id.

``order_id`` therefore lives in several unrelated namespaces at once, and the
funnel — which keys paid amounts on ``(platform, store_id, order_id)`` — cannot
tell that two of those rows are the same purchase. One Pivota-originated
Shopify order paid through Stripe counted its GMV twice.

``order_ref`` is the fix: ``<namespace>:<id in that namespace's system of
record>``. Two systems can never collide because the namespace is part of the
key.

* An order that ORIGINATED in Pivota is ``pivota:<orders.order_id>``, whichever
  authority reports it. The Stripe bridge always knows this (it holds the order
  row); the agent checkout always knows it; the Shopify adapter learns it from
  the ``pivota_order_id`` marker the order writeback stamps, or — for orders
  written back before that marker existed — from the ``orders.shopify_order_id``
  lookup the Shopify ingest performs.
* An order that originated on the store platform is ``<platform>:<native id>``,
  e.g. ``shopify:6600123`` or ``woocommerce:44``.

``order_id`` is unchanged and still stored: it remains the diagnostic answer to
"what did this authority call it", and legacy rows with a NULL ``order_ref``
keep aggregating on the old key.
"""

from __future__ import annotations

import re
from typing import Any, Optional


# The ledger column and the model field are both this wide.
ORDER_REF_MAX_LENGTH = 160

# The namespace reserved for orders that originated in Pivota. A merchant
# collector may never claim it: doing so would merge its own events into an
# interaction it does not own.
PIVOTA_ORDER_REF_NAMESPACE = "pivota"

# A namespace is a lowercase token; the native id is anything without
# whitespace, because platform order ids are not ours to constrain.
ORDER_REF_PATTERN = re.compile(r"^[a-z0-9_]+:[^\s]+$")

_NAMESPACE_UNSAFE = re.compile(r"[^a-z0-9_]+")


def _text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value or "").strip()


def normalize_order_ref_namespace(value: Any) -> str:
    """Return the namespace token for a platform name (``custom`` stays ``custom``)."""
    return _NAMESPACE_UNSAFE.sub("_", _text(value).lower()).strip("_")


def build_order_ref(namespace: Any, native_order_id: Any) -> Optional[str]:
    """``<namespace>:<native id>``, or None when either half is unusable.

    Returning None rather than raising is deliberate: every caller is an
    adapter on a best-effort telemetry path, and a ref it cannot build must
    degrade to the legacy ``order_id`` keying, never drop the event.
    """
    prefix = normalize_order_ref_namespace(namespace)
    native = _text(native_order_id)
    if not prefix or not native or any(char.isspace() for char in native):
        return None
    ref = f"{prefix}:{native}"
    if len(ref) > ORDER_REF_MAX_LENGTH:
        return None
    return ref


def pivota_order_ref(order_id: Any) -> Optional[str]:
    """The canonical ref for an order that originated in Pivota."""
    return build_order_ref(PIVOTA_ORDER_REF_NAMESPACE, order_id)


def order_ref_namespace(value: Any) -> Optional[str]:
    """The namespace half of a well-formed ref, else None."""
    ref = _text(value)
    if not is_valid_order_ref(ref):
        return None
    return ref.split(":", 1)[0]


def is_valid_order_ref(value: Any) -> bool:
    ref = _text(value)
    return bool(ref) and len(ref) <= ORDER_REF_MAX_LENGTH and bool(ORDER_REF_PATTERN.match(ref))

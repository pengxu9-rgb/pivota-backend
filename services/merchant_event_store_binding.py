"""Bind a merchant-signed event batch to the merchant's connected stores.

The HMAC collector proves possession of the merchant API key and nothing
else. Until this existed every event's ``store_id`` and ``platform`` were
taken from the body as-is, which had two consequences:

* trust — a merchant collector could label its events with any platform,
  so a ``platform="shopify"`` row need not come from a Shopify store;
* correctness — interaction ids and every stitch lookup are scoped by
  ``(merchant_id, store_id)`` (commerce_interaction_service). An event under
  a store id the native webhook does not use fragments the same purchase into
  two interactions that can never merge.

The rule: every event must resolve to one of the merchant's active connected
stores (the same set the Stripe PSP bridge resolves against, so the two
authorities land in the same scope). An omitted ``store_id`` is filled only
when the merchant has exactly one such store; a merchant with several must
say which. An omitted platform (the model default ``custom``) is filled from
the store; an explicit platform that disagrees with the store is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from services.merchant_event_ingest_service import MerchantEventBatch
from services.merchant_store_service import get_merchant_active_stores

# The batch model's platform default. A collector that sends nothing gets
# the store's platform; a collector that sends this literal is treated the
# same way, because the two are indistinguishable after validation.
_UNSET_PLATFORM = "custom"

# A surface a merchant collector may never claim: the funnel's legacy
# refund-authority inference reads it as the settlement authority for rows
# written before the authority column existed, and no merchant is a PSP.
FORBIDDEN_MERCHANT_SURFACES = frozenset({"psp"})


@dataclass(frozen=True)
class MerchantEventBindingError(Exception):
    status_code: int
    detail: str


def _text(value: Any) -> str:
    return str(value or "").strip()


async def connected_store_index(merchant_id: str) -> Dict[str, str]:
    """``{store_id: platform}`` for the merchant's active connected stores."""
    stores: List[Dict[str, Any]] = await get_merchant_active_stores(merchant_id)
    index: Dict[str, str] = {}
    for store in stores:
        store_id = _text(store.get("store_id"))
        platform = _text(store.get("platform")).lower()
        if store_id and platform:
            index[store_id] = platform
    return index


def bind_batch_to_stores(
    batch: MerchantEventBatch,
    *,
    stores: Dict[str, str],
) -> MerchantEventBatch:
    """Resolve every event to a connected store, mutating the validated batch.

    Pure so the rule is testable without a database; the route supplies
    ``stores`` from :func:`connected_store_index`.
    """
    if not stores:
        raise MerchantEventBindingError(
            422, "Merchant has no active connected store to bind events to"
        )
    sole_store: Optional[str] = next(iter(stores)) if len(stores) == 1 else None

    for index, event in enumerate(batch.events):
        if _text(event.surface).lower() in FORBIDDEN_MERCHANT_SURFACES:
            raise MerchantEventBindingError(
                422, f"events[{index}].surface may not claim {event.surface!r}"
            )
        store_id = _text(event.store_id)
        if not store_id:
            if sole_store is None:
                raise MerchantEventBindingError(
                    422,
                    f"events[{index}].store_id is required when the merchant has "
                    "more than one connected store",
                )
            store_id = sole_store
        store_platform = stores.get(store_id)
        if store_platform is None:
            # Unknown and inactive stores share one message: the collector
            # learns nothing about which store ids exist for another state.
            raise MerchantEventBindingError(
                422, f"events[{index}].store_id is not an active connected store"
            )
        event_platform = _text(event.platform).lower()
        if event_platform and event_platform != _UNSET_PLATFORM and event_platform != store_platform:
            raise MerchantEventBindingError(
                422,
                f"events[{index}].platform {event_platform!r} does not match the "
                f"connected store's platform",
            )
        event.store_id = store_id
        event.platform = store_platform
    return batch

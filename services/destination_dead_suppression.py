"""Withdraw a product whose destination page is confirmed dead — reversibly first, finally later.

WHY A PENDING STEP. A destination-liveness lane retires a row only after two CORROBORATED
confirmed-dead observations at least 24h apart (services/external_seed_destination_liveness). That
rule is right for a withdrawal nothing undoes, and it means the row is served, in stock, for at
least a day after the first corroborated 404 — with the nightly cadence, two or three. Prod
2026-10-09: 286 served mirror rows carried a confirmed-dead verdict.

So the FIRST corroborated dead observation hides the product with a PENDING reason, and the next
observation that reaches the origin and finds the page alive lifts exactly that reason. The second
corroborated observation converts it into the lane's final reason. Pending is reversible by
construction; final is not (see the retirement docstring for why an automatic un-retire is a
resurrection primitive).

WHAT CAN LIFT IT. Only a lane's own "the page is alive" answer, which needs the origin to have
answered: a bot challenge, a 429/5xx, a timeout or a robots refusal is `unverifiable` and moves
nothing. A WAF that serves a 200 interstitial on the product's own handle could lift a pending
row — that is the cost of reversibility, and the row then waits for the next corroborated look.

OURS ONLY. Every statement is scoped by the caller's product keys AND by the pending reason it was
handed: a lift never clears another lane's suppression, and a finalize converts only rows that are
either live or pending under this reason. Offers are cascaded and reverted through
`services/catalog_offer_suppression` with the SAME pending reason, so the revert's reason + lane
stamp scope matches exactly what was cascaded; a finalize relabels those offers to the cascade's
ordinary `product_suppressed` label, which is what every other withdrawal leaves behind.

A KNOWN GAP, shared with every suppressing lane: while a row is pending, another lane's
`WHERE suppressed_at IS NULL` writer skips it, so a decision that lane would have recorded is not
recorded. Lifting then serves the row again. Rows are pending for a day or two.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, List

from db.database import database
from services.catalog_offer_suppression import (
    CASCADE_LANE,
    CASCADE_LANE_KEY,
    PRODUCT_SUPPRESSED_REASON,
    cascade_offer_suppression,
    revert_offer_suppression,
)

logger = logging.getLogger(__name__)

#: Hide live rows under the pending reason. Only rows nobody has suppressed.
PENDING_SUPPRESS_PRODUCTS_SQL = """
    UPDATE catalog_products
       SET suppressed_at = :stamp,
           suppression_reason = CAST(:reason AS text),
           updated_at = NOW()
     WHERE product_key = ANY(:product_keys)
       AND suppressed_at IS NULL
    RETURNING product_key
"""

#: Lift ONLY our pending reason.
LIFT_PENDING_PRODUCTS_SQL = """
    UPDATE catalog_products
       SET suppressed_at = NULL,
           suppression_reason = NULL,
           updated_at = NOW()
     WHERE product_key = ANY(:product_keys)
       AND suppressed_at IS NOT NULL
       AND suppression_reason = CAST(:reason AS text)
    RETURNING product_key
"""

#: The final step: a live row, or one pending under OUR reason, takes the final reason.
FINALIZE_PRODUCTS_SQL = """
    UPDATE catalog_products
       SET suppressed_at = :stamp,
           suppression_reason = CAST(:final_reason AS text),
           updated_at = NOW()
     WHERE product_key = ANY(:product_keys)
       AND (suppressed_at IS NULL OR suppression_reason = CAST(:pending_reason AS text))
    RETURNING product_key
"""

#: Offers we cascaded as pending become ordinary cascaded offers. Reason AND lane stamp, as the
#: cascade module's own revert scopes them.
RELABEL_PENDING_OFFERS_SQL = """
    UPDATE catalog_offers
       SET suppression_reason = CAST(:final_reason AS text),
           updated_at = NOW()
     WHERE product_key = ANY(:product_keys)
       AND suppressed_at IS NOT NULL
       AND suppression_reason = CAST(:pending_reason AS text)
       AND suppression_metadata->>CAST(:lane_key AS text) = CAST(:lane AS text)
    RETURNING offer_id
"""


def _keys(product_keys: Iterable[str]) -> List[str]:
    return sorted({str(k or "").strip() for k in (product_keys or [])} - {""})


async def suppress_pending(
    product_keys: Iterable[str], *, reason: str, stamp: datetime, db: Any = None
) -> dict:
    """Hide these products and their live offers under `reason`. Idempotent."""
    keys = _keys(product_keys)
    if not keys:
        return {"products": [], "offers": []}
    write_db = db or database
    rows = await write_db.fetch_all(
        PENDING_SUPPRESS_PRODUCTS_SQL, {"product_keys": keys, "reason": reason, "stamp": stamp}
    )
    gated = [str(r["product_key"]) for r in (rows or [])]
    # Cascade only to the products THIS statement gated, as retirement does.
    offers = await cascade_offer_suppression(gated, reason=reason, db=write_db)
    return {"products": gated, "offers": offers}


async def lift_pending(product_keys: Iterable[str], *, reason: str, db: Any = None) -> dict:
    """Undo `suppress_pending` for these products. Touches nothing suppressed for another reason."""
    keys = _keys(product_keys)
    if not keys:
        return {"products": [], "offers": []}
    write_db = db or database
    rows = await write_db.fetch_all(LIFT_PENDING_PRODUCTS_SQL, {"product_keys": keys, "reason": reason})
    lifted = [str(r["product_key"]) for r in (rows or [])]
    # Offers are reverted for the lifted products only: an offer we cascaded under a product that
    # is somehow no longer ours to lift stays withdrawn (fail closed).
    offers = await revert_offer_suppression(lifted, reason=reason, db=write_db)
    return {"products": lifted, "offers": offers}


async def finalize_dead(
    product_keys: Iterable[str],
    *,
    final_reason: str,
    pending_reason: str,
    stamp: datetime,
    db: Any = None,
) -> dict:
    """Withdraw these products under `final_reason`, converting our pending rows. Returns what moved."""
    keys = _keys(product_keys)
    if not keys:
        return {"products": [], "offers": []}
    write_db = db or database
    rows = await write_db.fetch_all(
        FINALIZE_PRODUCTS_SQL,
        {"product_keys": keys, "final_reason": final_reason, "pending_reason": pending_reason, "stamp": stamp},
    )
    gated = [str(r["product_key"]) for r in (rows or [])]
    relabeled = await relabel_pending_offers(gated, pending_reason=pending_reason, db=write_db)
    cascaded = await cascade_offer_suppression(gated, db=write_db)
    return {"products": gated, "offers": relabeled + cascaded}


async def relabel_pending_offers(
    product_keys: Iterable[str], *, pending_reason: str, db: Any = None
) -> List[str]:
    keys = _keys(product_keys)
    if not keys:
        return []
    write_db = db or database
    rows = await write_db.fetch_all(
        RELABEL_PENDING_OFFERS_SQL,
        {"product_keys": keys, "final_reason": PRODUCT_SUPPRESSED_REASON, "pending_reason": pending_reason,
         "lane_key": CASCADE_LANE_KEY, "lane": CASCADE_LANE},
    )
    return [str(r["offer_id"]) for r in (rows or [])]


__all__ = (
    "FINALIZE_PRODUCTS_SQL",
    "LIFT_PENDING_PRODUCTS_SQL",
    "PENDING_SUPPRESS_PRODUCTS_SQL",
    "RELABEL_PENDING_OFFERS_SQL",
    "finalize_dead",
    "lift_pending",
    "relabel_pending_offers",
    "suppress_pending",
)

"""One attribution edge per cart-link click: a first-writer-wins claim (migration 228).

THE PROBLEM. A Reap cart-link purchase is ONE sale that TWO channels can close:

  * Reap:     services/reap_agentic_purchase._close_attribution closes under
              (merchant_domain, Reap orderId);
  * merchant: the Shopify order carries our `pivota_click_id` cart attribute (the permalink put
              it there), so the `orders/paid` webhook (routes/webhook_routes.py) and the
              read_orders poller (services/external_conversion_poller.py) close it under
              (tenant merchant_id, Shopify order id).

`close_external_order_conversion` dedupes only on (merchant_id, external_order_id), and those two
keys never collide. Without this module the result is two edges and double GMV for one sale.

THE CLAIM. `claim_click` is one statement,
`INSERT … ON CONFLICT (click_id) DO NOTHING RETURNING click_id`. The PRIMARY KEY makes it atomic
on both engines with NO TRANSACTION. That matters: on this app's shared `databases` connection,
transaction statements bypass the query lock and silently lose writes. The first channel to
insert owns the edge. A later insert gets no row back, and the claimant then skips, unless the
existing claim is ITS OWN for the SAME order, in which case it proceeds. That second case is a
retry, and the edge close is itself idempotent on (merchant_id, external_order_id).

FIRST WRITER WINS, AND THAT IS ACCEPTED. A Reap edge can pre-empt a seller-verified merchant
edge for the same sale, and the merchant edge is the better-evidenced one: the store reported it
directly. One edge with the weaker provenance is still correct GMV. Two edges are not.

SCOPE, ON BOTH SIDES, AND ONLY THERE:
  * Reap side: every cart_link completion claims (`claim_click`, called from `_complete`). It
    FAILS CLOSED: any error skips the edge and records `attribution_claim_unavailable`.
  * Merchant side: `close_merchant_conversion_with_claim` claims ONLY when a
    `reap_agentic_purchases` row with that click id and `item_source = 'cart_link'` exists.
    Every other click is passed straight to the close exactly as before and never touches the
    claims table. It FAILS OPEN: any error on the claim path logs a WARNING and closes as before
    228, so a new table can never block merchant-webhook attribution.

Nothing here logs a click id's buyer, an order body or an address. The log lines carry ids and
exception TYPES only.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from db.database import database

logger = logging.getLogger(__name__)

__all__ = [
    "ATTRIBUTION_CLAIM_UNAVAILABLE",
    "CLOSED_BY_OTHER_CHANNEL",
    "MERCHANT_CLAIMANT",
    "REAP_CLAIMANT",
    "claim_click",
    "close_merchant_conversion_with_claim",
    "is_reap_cart_link_click",
    "release_click_claim",
]

#: The two claimants. The CHECK on `conversion_click_claims.claimed_by` holds the same pair.
REAP_CLAIMANT = "reap_agentic"
#: The webhook and the poller close the SAME Shopify order under the SAME key, so they are ONE
#: claimant. Two merchant-side names would let them pre-empt each other for no reason.
MERCHANT_CLAIMANT = "merchant_order"

#: `last_error_code` on a completed cart-link purchase whose edge another channel wrote.
CLOSED_BY_OTHER_CHANNEL = "attribution_closed_by_other_channel"
#: `last_error_code` when the claim itself could not be taken (Reap side fails closed).
ATTRIBUTION_CLAIM_UNAVAILABLE = "attribution_claim_unavailable"

# Module-level constants, one statement each, identical on both engines (SQLite 3.35+ has
# ON CONFLICT … DO NOTHING RETURNING), so tests/test_repo_sql_prepare_postgres.py can see and
# PREPARE every one of them.
_IS_REAP_CART_LINK_CLICK_SQL = """
    SELECT 1 AS hit
      FROM reap_agentic_purchases
     WHERE click_id = :click_id
       AND item_source = 'cart_link'
     LIMIT 1
"""

_CLAIM_CLICK_SQL = """
    INSERT INTO conversion_click_claims (click_id, claimed_by, external_order_id)
    VALUES (:click_id, :claimed_by, :external_order_id)
    ON CONFLICT (click_id) DO NOTHING
    RETURNING click_id
"""

_SELECT_CLAIM_SQL = """
    SELECT click_id, claimed_by, external_order_id
      FROM conversion_click_claims
     WHERE click_id = :click_id
"""

# Only the claimant that holds it, for the order it holds it for, can release it.
_RELEASE_CLAIM_SQL = """
    DELETE FROM conversion_click_claims
     WHERE click_id = :click_id
       AND claimed_by = :claimed_by
       AND external_order_id = :external_order_id
"""


def _require(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


async def is_reap_cart_link_click(click_id: Any) -> bool:
    """Does this click belong to a cart-link Reap purchase? Uses
    `idx_reap_agentic_purchases_click_id` (mig 228). Raises on a database error, and the caller
    decides which way to fail."""
    text = str(click_id or "").strip()
    if not text:
        return False
    row = await database.fetch_one(_IS_REAP_CART_LINK_CLICK_SQL, {"click_id": text})
    return row is not None


async def claim_click(click_id: Any, *, claimed_by: str, external_order_id: Any) -> bool:
    """True when THIS (claimant, order) owns the click's edge. Raises on a database error.

    The INSERT is the decision. The SELECT after a conflict does not decide anything; it only
    tells a retry by the owner that it IS the owner, so a close that failed after its claim can
    be re-run by the same channel. Anyone else gets False.
    """
    if claimed_by not in (REAP_CLAIMANT, MERCHANT_CLAIMANT):
        raise ValueError(f"unknown claimant {claimed_by!r}")
    params = {
        "click_id": _require(click_id, "click_id"),
        "claimed_by": claimed_by,
        "external_order_id": _require(external_order_id, "external_order_id"),
    }
    won = await database.fetch_one(_CLAIM_CLICK_SQL, params)
    if won is not None:
        return True
    existing = await database.fetch_one(_SELECT_CLAIM_SQL, {"click_id": params["click_id"]})
    if existing is None:
        return False
    existing = dict(existing)
    return (
        str(existing.get("claimed_by") or "") == claimed_by
        and str(existing.get("external_order_id") or "") == params["external_order_id"]
    )


async def release_click_claim(click_id: Any, *, claimed_by: str, external_order_id: Any) -> None:
    """Give back a claim whose edge was NOT written, so the other channel can still close the sale.
    Best-effort: a failure is logged (type only) and swallowed. The worst case is a held claim
    with no edge, and a same-claimant retry can still fill it."""
    try:
        await database.execute(
            _RELEASE_CLAIM_SQL,
            {
                "click_id": _require(click_id, "click_id"),
                "claimed_by": claimed_by,
                "external_order_id": _require(external_order_id, "external_order_id"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "conversion_click_claims: release failed click=%s claimant=%s error_type=%s",
            click_id, claimed_by, type(exc).__name__,
        )


async def close_merchant_conversion_with_claim(
    close: Callable[..., Awaitable[Optional[dict]]],
    *,
    click_id: Optional[str],
    external_order_id: str,
    **close_kwargs: Any,
) -> Optional[dict]:
    """THE ONE MERCHANT-SIDE HELPER, called from the `orders/paid` webhook and the read_orders
    poller in place of a bare `close(...)`.

    `close` is passed IN, not imported, so each caller keeps calling its OWN module-level
    `close_external_order_conversion`: the same object, with the same keyword arguments, as
    before 228.

      * not a cart-link Reap click (the common case)  → `close(...)` exactly as before; the
        claims table is never read or written;
      * a cart-link Reap click, claim won (or already ours for this order) → `close(...)`;
      * a cart-link Reap click, claimed by Reap       → NO close; returns None and logs INFO;
      * ANY error while deciding                      → FAIL OPEN: WARNING, then `close(...)`.

    If `close` raises after a claim was won, the claim is released before re-raising, so Reap is
    not locked out of a sale that has no edge.
    """
    claimed = False
    try:
        if click_id and await is_reap_cart_link_click(click_id):
            claimed = await claim_click(
                click_id, claimed_by=MERCHANT_CLAIMANT, external_order_id=external_order_id
            )
            if not claimed:
                logger.info(
                    "conversion_click_claims: merchant close skipped click=%s order=%s (%s)",
                    click_id, external_order_id, CLOSED_BY_OTHER_CHANNEL,
                )
                return None
    except Exception as exc:  # noqa: BLE001 — fail OPEN on the merchant side, by design
        claimed = False
        logger.warning(
            "conversion_click_claims: claim path failed, closing as before click=%s order=%s "
            "error_type=%s",
            click_id, external_order_id, type(exc).__name__,
        )
    try:
        return await close(click_id=click_id, external_order_id=external_order_id, **close_kwargs)
    except Exception:
        if claimed:
            await release_click_claim(
                click_id, claimed_by=MERCHANT_CLAIMANT, external_order_id=external_order_id
            )
        raise

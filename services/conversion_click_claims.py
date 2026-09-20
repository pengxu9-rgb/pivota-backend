"""One attribution edge per cart-link click: a first-writer-wins claim (migration 230).

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

A CLAIM IS NEVER RELEASED. A MISSED EDGE BEATS A DOUBLE EDGE. An earlier revision gave a claim
back whenever its edge was not written (a lost fence, or a close that raised). The #2214 review
showed every such release can re-open the double edge. The owner retry means two workers of ONE
claimant (two Reap pods on one row, or the webhook and the poller on one order) both hold "the"
claim. If one of them writes the edge and the other then releases, the DELETE removes the claim
the writer is standing on, and the other channel wins a fresh one: two edges. A per-attempt token
does not help, because the releaser was the INSERT winner. So the release path is gone.

WHAT THAT COSTS, EXACTLY: a claim can be held with NO edge.
  * Merchant side: a close that raised after its claim leaves the claim held. The NEXT merchant
    close of that order (a webhook redelivery, or the read_orders poller's next pass, which
    re-reads paid orders by watermark) is a same-claimant, same-order retry. It proceeds, and
    fills the edge. So a merchant-side miss heals on the poller's own cadence.
  * Reap side: a Reap completion that claimed and then did not write its edge (the close raised,
    the process died between the completing write and the close, or its fence was lost) does
    NOT heal. A 'completed' purchase row is never advanced again, so nothing retries it. That
    sale then has NO edge, and the merchant side skips it because Reap holds the claim.
  Both are made VISIBLE rather than silent. A WARNING (click id and claimant, ids only) is logged
  whenever a claim is won and its close then fails, and `list_claims_without_edge` is the
  read-only SELECT an operator reconciles from.

FIRST WRITER WINS, AND THAT IS ACCEPTED. A Reap edge can pre-empt a seller-verified merchant
edge for the same sale, and the merchant edge is the better-evidenced one: the store reported it
directly. One edge with the weaker provenance is still correct GMV. Two edges are not.

SCOPE, ON BOTH SIDES, AND ONLY THERE:
  * Reap side: every cart_link completion claims (`claim_click`, called from `_complete`). It
    FAILS CLOSED: any error skips the edge and records `attribution_claim_unavailable`.
  * Merchant side: `close_merchant_conversion_with_claim` claims ONLY when a
    `reap_agentic_purchases` row with that click id and `item_source = 'cart_link'` exists.
    Every other click is passed straight to the close exactly as before and never touches the
    claims table. A claim-path error DEFERS closure: we cannot prove that this click is outside
    the cart-link scope, and closing without a claim can double-count the Reap sale. The Shopify
    order itself is unaffected; the read_orders poller holds its watermark and retries closure.

Nothing here logs a click id's buyer, an order body or an address. The log lines carry ids and
exception TYPES only.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

__all__ = [
    "ATTRIBUTION_CLAIM_UNAVAILABLE",
    "CLOSED_BY_OTHER_CHANNEL",
    "ClickClaimUnavailable",
    "MERCHANT_CLAIMANT",
    "REAP_CLAIMANT",
    "claim_click",
    "close_merchant_conversion_with_claim",
    "is_reap_cart_link_click",
    "is_skipped_claimed",
    "list_claims_without_edge",
    "warn_claim_without_edge",
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

_SKIPPED_KEY = "skipped_claimed"


class ClickClaimUnavailable(RuntimeError):
    """Attribution closure must retry when the cart-link claim cannot be checked."""


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

# RECONCILIATION, READ-ONLY. A claim whose owner never wrote its edge: no edge carries the
# claim's click id AND the claim's order id. Keyed on (click_id, external_order_id), not on
# merchant_id, because the two claimants write their edges under different merchant ids.
_CLAIMS_WITHOUT_EDGE_SQL = """
    SELECT c.click_id, c.claimed_by, c.external_order_id, c.claimed_at
      FROM conversion_click_claims c
     WHERE NOT EXISTS (
            SELECT 1
              FROM commerce_attribution_edges e
             WHERE e.click_id = c.click_id
               AND e.external_order_id = c.external_order_id
           )
     ORDER BY c.claimed_at, c.click_id
     LIMIT :limit
"""


def _require(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


async def is_reap_cart_link_click(click_id: Any) -> bool:
    """Does this click belong to a cart-link Reap purchase? Uses
    the partial unique index `uq_reap_agentic_purchases_cart_link_click`, whose predicate is this
    query's. Raises on a database error, and the caller
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
    be re-run by the same claimant for the same order (on the merchant side, the poller's next
    pass). Anyone else gets False. Nothing ever deletes a claim; see the module docstring.
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


def warn_claim_without_edge(click_id: Any, *, claimed_by: str, external_order_id: Any) -> None:
    """The one log line for "we own this click and wrote no edge". Ids only, no bodies."""
    logger.warning(
        "conversion_click_claims: claim held WITHOUT an edge click=%s claimant=%s order=%s; "
        "see list_claims_without_edge",
        click_id, claimed_by, external_order_id,
    )


async def list_claims_without_edge(limit: int = 100) -> List[Dict[str, Any]]:
    """Claims whose owner has no edge for (click_id, order), oldest first. READ-ONLY, for ops
    reconciliation. A merchant-side row here usually heals on the poller's next pass; a
    `reap_agentic` row never heals on its own (a completed purchase is not revisited), and its
    `reap_checkout_id` on the purchase row is what a human re-closes it from."""
    bounded = max(1, min(1000, int(limit)))
    rows = await database.fetch_all(_CLAIMS_WITHOUT_EDGE_SQL, {"limit": bounded})
    return [dict(row) for row in rows]


def is_skipped_claimed(result: Any) -> bool:
    """Did `close_merchant_conversion_with_claim` skip because another channel owns the click?"""
    return isinstance(result, dict) and result.get(_SKIPPED_KEY) is True


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
    before 230.

      * not a cart-link Reap click (the common case)  → `close(...)` exactly as before; the
        claims table is never read or written;
      * a cart-link Reap click, claim won (or already ours for this order) → `close(...)`;
      * a cart-link Reap click, claimed by Reap       → NO close; returns
        `{"skipped_claimed": True, ...}` (see `is_skipped_claimed`) and logs INFO;
      * ANY error while deciding                      → raise ClickClaimUnavailable; do not close.

    If `close` raises after a claim was won, the claim is KEPT (see the module docstring for why
    a release re-opens the double edge), a WARNING names it, and the exception propagates as
    before. The next close of the same order by this claimant fills it.
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
                return {_SKIPPED_KEY: True, "reason": CLOSED_BY_OTHER_CHANNEL}
    except Exception as exc:  # noqa: BLE001 — an uncertain claim must never double-count
        logger.warning(
            "conversion_click_claims: claim path failed, deferring close click=%s order=%s "
            "error_type=%s",
            click_id, external_order_id, type(exc).__name__,
        )
        raise ClickClaimUnavailable("click claim unavailable; retry attribution close") from None
    try:
        return await close(click_id=click_id, external_order_id=external_order_id, **close_kwargs)
    except Exception:
        if claimed:
            warn_claim_without_edge(
                click_id, claimed_by=MERCHANT_CLAIMANT, external_order_id=external_order_id
            )
        raise

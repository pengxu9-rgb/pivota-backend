"""The referral for a store cart the gateway's checkout door builds. Behind
UCP_STOREFRONT_CART_REFERRAL_ENABLED, default OFF.

WHY. The gateway's checkout door can build a cart on a seller's own UCP endpoint and hand the buyer
that cart's link. The door reads the product with its own service key, so the link it holds is the
seller's direct page, not a stamped `/r` hop: the cart link carried no tracking member and no click
was recorded, and a purchase from such a cart could not be joined to us or to the partner.

WHAT THIS DOES. Just before the door builds the cart, it asks here for a referral: ONE
surface_click_events row with a fresh click id (`issue_click`, the writer every issued link uses, so
the test-traffic stamp is the same), and the tracking members a `/r` referral destination carries,
built by the SAME two functions the `/r` issue paths use (`apply_utm` with the default template, then
`append_referral_click_param`). The door carries those members on the cart link and, when it sends
one, in the cart's `attribution`. Nothing else is returned and nothing about the buyer is taken.

THE ROW. Written under the hand-off surface (services.storefront_handoff_record.HANDOFF_SURFACE),
with `context.kind = "ucp_storefront_cart_referral"`:

  click_id     a new click id (`clk_...`), the value of `pvt_click_id` and `utm_content`;
  agent_id     the PARTNER the gateway vouched for (a verified request-bound assertion adopted by
               routes.agent_forwarded_owner), or NULL when the request names no partner (partner
               identity forwarding off, or a caller that is nobody's partner): the row is then
               ours and no partner's, like a link minted for no caller in particular;
  merchant_id  the observed seller's id, when the row named one;
  dest_domain  the seller host the cart is built on;
  context      {kind, v, tool, market, join_mode, via_agent_id}.

NOT A LINK ANYBODY FOLLOWS. No `/r` token carries this id, so click_count stays 0 for ever. It is on
the hand-off surface for that reason: every reader that counts issued links must leave that surface
out (see the note in services/storefront_handoff_record.py), and one filter then covers both the
hand-off record and this row. The destination URL is not stored: the cart does not exist yet, and a
cart link is the seller's bearer handle.

NO AFFILIATE SUB-ID. A store cart is on the seller's own host, never an affiliate network link, so
nothing here stamps a network sub-id and no network template is read.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlparse

from services.commerce_attribution_service import IssuedClick, issue_click, new_click_id
from services.outbound_links_service import DEFAULT_UTM_TEMPLATE, append_referral_click_param, apply_utm
from services.storefront_handoff_record import HANDOFF_SURFACE

STOREFRONT_CART_REFERRAL_ENV = "UCP_STOREFRONT_CART_REFERRAL_ENABLED"

#: The gateway operation name its issuing-agent assertion is signed for.
REFERRAL_OP = "ucp_storefront_cart.referral"
#: `context.kind` of a referral row: what tells it from a hand-off record on the same surface.
REFERRAL_KIND = "ucp_storefront_cart_referral"
#: The `{{tool}}` of the UTM template: the operation that builds the cart, whichever door it came in by.
REFERRAL_TOOL = "create_checkout"
#: The `{{market}}` when the door named none, as every `/r` issue path defaults it.
DEFAULT_MARKET = "US"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_TRACKING_PREFIXES = ("utm_", "pvt_")


def storefront_cart_referral_enabled() -> bool:
    return str(os.getenv(STOREFRONT_CART_REFERRAL_ENV) or "").strip().lower() in _TRUTHY


def referral_members(*, seller_host: str, market: Optional[str], click_id: str) -> Dict[str, str]:
    """The tracking members a `/r` referral destination on `seller_host` carries for this click, in
    the order they appear there. Built by the `/r` issue path's own builders on a bare destination,
    then read back from its query, so the names and values cannot differ from a normal referral."""
    destination = apply_utm(
        f"https://{seller_host}/",
        DEFAULT_UTM_TEMPLATE,
        {"market": str(market or DEFAULT_MARKET), "tool": REFERRAL_TOOL},
    )
    destination = append_referral_click_param(destination, click_id)
    members: Dict[str, str] = {}
    for key, value in parse_qsl(urlparse(destination).query, keep_blank_values=False):
        if key.startswith(_TRACKING_PREFIXES) and key not in members:
            members[key] = value
    return members


def _context(*, market: Optional[str], via_agent_id: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"kind": REFERRAL_KIND, "v": 1, "tool": REFERRAL_TOOL, "join_mode": "referral_only"}
    if market:
        out["market"] = market
    if via_agent_id:
        out["via_agent_id"] = via_agent_id
    return out


async def mint_referral(
    *,
    seller_host: str,
    market: Optional[str] = None,
    merchant_id: Optional[str] = None,
    partner_agent_id: Optional[str] = None,
    via_agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the referral row and return `{click_id, surface, attribution}`. Raises on a database
    error: the caller answers that the referral was not minted, and the door builds its cart without."""
    click_id = new_click_id()
    await issue_click(IssuedClick(
        click_id=click_id,
        surface=HANDOFF_SURFACE,
        commerce_surface=HANDOFF_SURFACE,
        agent_id=partner_agent_id,
        merchant_id=merchant_id,
        dest_domain=seller_host,
        context=_context(market=market, via_agent_id=via_agent_id),
    ))
    return {
        "click_id": click_id,
        "surface": HANDOFF_SURFACE,
        "attribution": referral_members(seller_host=seller_host, market=market, click_id=click_id),
    }

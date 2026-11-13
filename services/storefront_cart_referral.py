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

THE MARKET IS THE CALLER'S, NEVER A DEFAULT. It becomes `utm_campaign` on the seller's cart link, so
it is a claim about the cart: the door sends the market the cart was built for (the one it sent the
seller, an assumed one included, which the door itself labels as assumed). A call that names none,
or one that is not two capital letters, mints nothing (`ReferralMarketRequired`), and neither does
one the market registry does not list (`ReferralMarketUnknown`): no value is filled in here.

KNOWN MEANS LISTED, NOT SERVED. The one source is services/market_registry.py: its table is loaded
from config/market_registry.json ONCE, when that module is imported, and each call here looks the
market up in that loaded table (a change to the file takes effect at the next start, as for every
reader of the registry). A market counts whatever its rollout status
there (off / acquisition / shadow / served): this records a referral for a cart the door already
decided to build for that market. It does not decide serving, and must not refuse a cart the door
built. Case is not folded: the door sends the code in capitals, the row and `utm_campaign` carry
exactly what was sent, and a lower-case code is refused as malformed before the registry is asked.

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

from services import market_registry
from services.commerce_attribution_service import IssuedClick, issue_click, new_click_id
from services.outbound_links_service import DEFAULT_UTM_TEMPLATE, append_referral_click_param, apply_utm
from services.storefront_handoff_record import HANDOFF_SURFACE, MARKET_RE

STOREFRONT_CART_REFERRAL_ENV = "UCP_STOREFRONT_CART_REFERRAL_ENABLED"

#: The gateway operation name its issuing-agent assertion is signed for.
REFERRAL_OP = "ucp_storefront_cart.referral"
#: `context.kind` of a referral row: what tells it from a hand-off record on the same surface.
REFERRAL_KIND = "ucp_storefront_cart_referral"
#: The `{{tool}}` of the UTM template: the operation that builds the cart, whichever door it came in by.
REFERRAL_TOOL = "create_checkout"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_TRACKING_PREFIXES = ("utm_", "pvt_")


def storefront_cart_referral_enabled() -> bool:
    return str(os.getenv(STOREFRONT_CART_REFERRAL_ENV) or "").strip().lower() in _TRUTHY


class ReferralMarketRequired(ValueError):
    """The referral was asked for with no market, or with one that is not two capital letters."""


class ReferralMarketUnknown(ValueError):
    """The referral names a well-formed market the market registry does not list."""


def market_is_known(market: str) -> bool:
    """Listed in the market registry, whatever its rollout status (see the module note). The
    registry's own lookup, per call, on the table it loaded at import: no second list or copy of
    that table is kept here."""
    return market_registry.market_or_none(market) is not None


def named_market(market: Any) -> str:
    """The market exactly as the caller named it: required (ReferralMarketRequired), two capital
    letters (the same), then listed in the registry (ReferralMarketUnknown). Never a default."""
    if not isinstance(market, str) or not MARKET_RE.fullmatch(market):
        raise ReferralMarketRequired("a store-cart referral names the market its cart is built for")
    if not market_is_known(market):
        raise ReferralMarketUnknown("a store-cart referral names a market the registry lists")
    return market


def referral_members(*, seller_host: str, market: str, click_id: str) -> Dict[str, str]:
    """The tracking members a `/r` referral destination on `seller_host` carries for this click, in
    the order they appear there. Built by the `/r` issue path's own builders on a bare destination,
    then read back from its query, so the names and values cannot differ from a normal referral."""
    destination = apply_utm(
        f"https://{seller_host}/",
        DEFAULT_UTM_TEMPLATE,
        {"market": named_market(market), "tool": REFERRAL_TOOL},
    )
    destination = append_referral_click_param(destination, click_id)
    members: Dict[str, str] = {}
    for key, value in parse_qsl(urlparse(destination).query, keep_blank_values=False):
        if key.startswith(_TRACKING_PREFIXES) and key not in members:
            members[key] = value
    return members


def _context(*, market: str, via_agent_id: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "kind": REFERRAL_KIND, "v": 1, "tool": REFERRAL_TOOL, "join_mode": "referral_only", "market": market,
    }
    if via_agent_id:
        out["via_agent_id"] = via_agent_id
    return out


async def mint_referral(
    *,
    seller_host: str,
    market: str,
    merchant_id: Optional[str] = None,
    partner_agent_id: Optional[str] = None,
    via_agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the referral row and return `{click_id, surface, attribution}`. Raises
    ReferralMarketRequired or ReferralMarketUnknown, before anything is written, when `market` is
    not a named, listed market; raises
    on a database error: the caller answers that the referral was not minted, and the door builds
    its cart without."""
    market = named_market(market)
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

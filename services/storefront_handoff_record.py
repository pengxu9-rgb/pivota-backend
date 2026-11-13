"""The UCP storefront hand-off record (FIX_PLAN O-02). Behind UCP_STOREFRONT_HANDOFF_RECORD_ENABLED,
default OFF.

WHY. The gateway's UCP door answers a checkout for a seller Pivota does not transact with a
`requires_escalation` checkout whose `continue_url` is the seller's own storefront (or a cart the
seller's own UCP door built). Its id (`esc_...`) was unsigned and stateless: nothing recorded that
the hand-off happened, which partner it was for, what total was quoted or where that total came
from, and `get_order` on the id answered "not found".

WHAT IS STORED. One surface_click_events row per hand-off, under its own surface
(`ucp_storefront_handoff`), so no new table and no migration. Its columns:

  click_id     the hand-off record id (`esh_` + 22 base64url chars, 128 random bits the gateway
               minted and carries inside the esc id it hands out);
  agent_id     the PARTNER the gateway vouched for (a verified X-Pivota-Issuing-Agent, adopted by
               routes.agent_forwarded_owner); never a request body value;
  merchant_id  the observed seller's id, when the row named one;
  dest_domain  the seller host the buyer is sent to;
  issued_at    when the hand-off was issued (and created_at, as for every row);
  context      {kind, v, binding, referral_click_id, buyer_market, quoted_total_minor, currency,
               quote_source, price_as_of, via_agent_id}.

`binding` is the sha256 of the full esc id as handed out. A read must present the same esc id: a
record id lifted into a forged esc id (other items, another cart) does not match, and neither does
a guessed one. The esc id itself is not stored: a seller-priced id carries the seller's cart id,
which is that seller's bearer handle.

ROWS ARE NOT CLICKS. click_count stays 0 and `/r` never sees this id (no token carries it). Every
reader that counts ISSUED links must leave this surface out, or each hand-off reads as one more
issued link nobody followed. The two that count issued rows both do: scripts/agent_attribution_funnel.py
(O-02 review B-2) and services/click_reporting.py (the staff issued-vs-clicked report). The operator
smoke's per-merchant row count (scripts/smoke_commerce_channels_signoff.py) leaves it out too. The
inferred-click lookup and the merchant funnels only read rows /r touched (click or impression
count > 0). The conversion pollers' candidate queries (services/external_conversion_poller.py,
services/woocommerce_conversion_poller.py) select merchants from ANY recent row, so a hand-off row
with a merchant_id puts its merchant in the rotation; they then join a connected Shopify or
WooCommerce store, which an escalated seller is not, so nothing is polled for it (O-02 review B-4).

LATER STATE. What the hand-off became is read from records this service already keeps, keyed on
the REFERRAL click the catalog link carried (pvt_click_id), never on anything a caller sends, and
ONLY when that referral click was issued to THIS partner (its surface_click_events row names the
hand-off's owner as agent_id; O-02 review B-1) and is NOT stamped test traffic
(services.test_traffic; a stamped referral click reads as no click at all):
  buyer_followed_link    `/r` counted a click on that referral id at or after the hand-off;
  seller_order_observed  a converted commerce_attribution_edges row joins that referral id, at or
                         after the hand-off, credited to this partner or to no agent, and NOT
                         stamped test traffic (services.test_traffic: a rig merchant's or a test
                         order's edge is no seller order to report to a partner).
A referral id with no click row, an agent-less one (a link minted for no caller in particular: the
product-detail mint, the shared search cache) or another agent's may be shared by every caller
served that link, so it says nothing about THIS partner's buyer: such a hand-off answers
`handed_off`, and nothing of another buyer's follow or order, nor its timing, reaches the partner.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from db.database import database
from services.commerce_attribution_service import IssuedClick, issue_click
from services.test_traffic import not_test_traffic_sql

logger = logging.getLogger(__name__)

STOREFRONT_HANDOFF_RECORD_ENV = "UCP_STOREFRONT_HANDOFF_RECORD_ENABLED"

#: The surface_click_events surface (and commerce_surface) a hand-off row is written under.
HANDOFF_SURFACE = "ucp_storefront_handoff"
#: The gateway operation names its issuing-agent assertion is signed for (PIVOTA-Agent
#: src/attribution/issuingAgentAssertion.js STOREFRONT_HANDOFF_OPS).
RECORD_OP = "ucp_storefront_handoff.record"
READ_OP = "ucp_storefront_handoff.read"

RECORD_ID_RE = re.compile(r"esh_[A-Za-z0-9_-]{22}")
BINDING_RE = re.compile(r"[0-9a-f]{64}")
REFERRAL_CLICK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: A lower-case host name (length is bounded separately: pydantic's engine has no look-ahead).
SELLER_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
MARKET_RE = re.compile(r"[A-Z]{2}")
CURRENCY_RE = re.compile(r"[A-Z]{3}")
QUOTE_SOURCES = frozenset({"seller_cart", "catalogue_fallback"})

STATUS_HANDED_OFF = "handed_off"
STATUS_LINK_FOLLOWED = "buyer_followed_link"
STATUS_ORDER_OBSERVED = "seller_order_observed"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def storefront_handoff_record_enabled() -> bool:
    return str(os.getenv(STOREFRONT_HANDOFF_RECORD_ENV) or "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class HandoffRecord:
    record_id: str
    binding: str
    partner_agent_id: str
    via_agent_id: Optional[str]
    seller_host: str
    currency: str
    quoted_total_minor: int
    quote_source: str
    buyer_market: Optional[str] = None
    referral_click_id: Optional[str] = None
    price_as_of: Optional[str] = None
    merchant_id: Optional[str] = None


def _context(record: HandoffRecord) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "kind": HANDOFF_SURFACE,
        "v": 1,
        "binding": record.binding,
        "quoted_total_minor": int(record.quoted_total_minor),
        "currency": record.currency,
        "quote_source": record.quote_source,
    }
    for key in ("referral_click_id", "buyer_market", "price_as_of", "via_agent_id"):
        value = getattr(record, key)
        if value:
            out[key] = value
    return out


def _as_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def _row(record_id: str) -> Optional[Mapping[str, Any]]:
    row = await database.fetch_one(
        "SELECT click_id, agent_id, merchant_id, dest_domain, context, issued_at, created_at "
        "FROM surface_click_events WHERE click_id = :click_id AND surface = :surface",
        {"click_id": record_id, "surface": HANDOFF_SURFACE},
    )
    return dict(row) if row is not None else None


async def record_handoff(record: HandoffRecord) -> str:
    """Write the hand-off row. Returns "recorded", or "conflict" when the id is already taken by a
    row that is not this partner's hand-off with this binding (a replay of the SAME write is
    "recorded": issuing is idempotent). Raises on a database error."""
    await issue_click(IssuedClick(
        click_id=record.record_id,
        surface=HANDOFF_SURFACE,
        commerce_surface=HANDOFF_SURFACE,
        agent_id=record.partner_agent_id,
        merchant_id=record.merchant_id,
        dest_domain=record.seller_host,
        context=_context(record),
    ))
    row = await _row(record.record_id)
    if not row:
        return "conflict"
    context = _as_dict(row.get("context"))
    if str(row.get("agent_id") or "") != record.partner_agent_id or context.get("binding") != record.binding:
        return "conflict"
    return "recorded"


async def _later_state(row: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    """What the hand-off became, from the referral click's own records. Never raises: a read that
    fails says nothing more than handed_off."""
    out: Dict[str, Any] = {"status": STATUS_HANDED_OFF}
    referral = context.get("referral_click_id")
    if not isinstance(referral, str) or not REFERRAL_CLICK_ID_RE.fullmatch(referral):
        return out
    since = _aware(row.get("issued_at")) or _aware(row.get("created_at"))
    owner = str(row.get("agent_id") or "")
    if not owner:
        return out
    try:
        click = await database.fetch_one(
            "SELECT agent_id, click_count, last_click_at FROM surface_click_events WHERE click_id = :click_id "
            # A referral click stamped test traffic at write (a synthetic caller's, a rig seller's) is
            # nobody's buyer: it is not found here, so neither a follow nor an order is read from it.
            "AND " + not_test_traffic_sql("context"),
            {"click_id": referral},
        )
        click = dict(click) if click is not None else None
        # BOUND TO THIS PARTNER, or not read at all: no row, no agent, another agent's, or a test click.
        if not click or str(click.get("agent_id") or "") != owner:
            return out
        last_click = _aware(click.get("last_click_at"))
        if int(click.get("click_count") or 0) > 0 and last_click and (since is None or last_click >= since):
            out["status"] = STATUS_LINK_FOLLOWED
            out["link_followed_at"] = _iso(last_click)
        # An order credited to ANOTHER agent is that agent's buyer's, even on this partner's referral id.
        edge = await database.fetch_one(
            "SELECT state, converted_at, created_at FROM commerce_attribution_edges "
            "WHERE click_id = :click_id AND state = 'converted' "
            "AND (agent_id IS NULL OR agent_id = '' OR agent_id = :owner) "
            # A test order (stamped at write) is not a seller order: the same exclusion the money reads apply.
            "AND " + not_test_traffic_sql("metadata") + " "
            "ORDER BY created_at DESC LIMIT 1",
            {"click_id": referral, "owner": owner},
        )
        edge = dict(edge) if edge is not None else None
        if edge:
            observed = _aware(edge.get("converted_at")) or _aware(edge.get("created_at"))
            if observed and (since is None or observed >= since):
                out["status"] = STATUS_ORDER_OBSERVED
                out["seller_order_observed_at"] = _iso(observed)
    except Exception as exc:  # noqa: BLE001 -- later state is a bonus; the hand-off itself stands
        logger.warning("storefront_handoff.later_state_unreadable error_type=%s", type(exc).__name__)
    return out


async def read_handoff(record_id: str, binding: str, partner_agent_id: str) -> Dict[str, Any]:
    """{"outcome": "accepted", "handoff": {...}} | {"outcome": "not_found"} | {"outcome": "not_owner"}.
    `not_found` covers an unknown id AND a binding that does not match (a forged esc id). Raises on
    a database error."""
    row = await _row(record_id)
    if not row:
        return {"outcome": "not_found"}
    context = _as_dict(row.get("context"))
    stored = context.get("binding")
    if not isinstance(stored, str) or not hmac.compare_digest(stored, binding):
        return {"outcome": "not_found"}
    if str(row.get("agent_id") or "") != partner_agent_id:
        return {"outcome": "not_owner"}
    state = await _later_state(row, context)
    handoff: Dict[str, Any] = {
        "record_id": record_id,
        **state,
        "handed_off_at": _iso(row.get("issued_at") or row.get("created_at")),
        "seller_host": row.get("dest_domain"),
        "quote_source": context.get("quote_source"),
        "quoted_total": {"amount": context.get("quoted_total_minor"), "currency": context.get("currency")},
    }
    for key in ("buyer_market", "price_as_of"):
        if context.get(key):
            handoff[key] = context[key]
    return {"outcome": "accepted", "handoff": handoff}

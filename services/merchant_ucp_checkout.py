"""Buyer-side UCP client: build a checkout on the MERCHANT'S own door and read its landed total.

This is the hop the card rail was missing. `services/agent_card_issuance.py` can cap a card from
a merchant-quoted checkout, but nothing in this backend could CREATE that checkout — the only
UCP write client lives in the gateway (`src/services/ucpWarmHandoff.js`) and is hard-bounded to
cart-build + continue_url. This module is the backend's first merchant-side checkout caller.

THE WIRE SHAPE IS PROBED, NOT EXTRAPOLATED. Every argument name and nesting level below comes
from a live `tools/list` against cosrx's own door on 2026-08-31, saved beside the test that pins
it. The probe corrected two things that a plausible-looking guess got wrong, both of which were
already shipped in `resolve_merchant_quote` and both of which made every call fail:

  * `meta` is REQUIRED on every tool, and `meta["ucp-agent"]["profile"]` must be a REACHABLE
    profile URI — the merchant fetches it. Omitting it does not degrade to an anonymous call; it
    fails the whole request with `-32001 invalid_profile_url` ("Missing profile uri").
  * `get_checkout` takes `id`, not `checkout_id`.

MERCHANT VERSION DRIFT IS REAL: cosrx served UCP `2026-04-08` in August and serves `2026-08-25`
now. Pin nothing to a version string; re-probe when a shape stops working.

SCOPE — this module builds and prices a checkout. It never completes one: `complete_checkout` is
the money hop and stays out until the credential question (can a Reap reveal handle produce the
token `dev.shopify.card` wants?) is settled against a sandbox.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import httpx

# The SSRF guard is IMPORTED, never re-implemented. It is the reviewed one from the card rail
# (resolve-and-check every getaddrinfo answer, refuse unless all are public), and a second copy
# here would be a second thing to keep correct — the exact drift these modules' comments keep
# warning about.
from services.agent_card_issuance import (
    resolves_only_public,
    validate_merchant_domain,
)
from services.outbound_links_service import REFERRAL_CLICK_PARAM
from utils.logger import logger

_TIMEOUT_SECONDS = 12.0

# Mutating ops only. `get_checkout` is a read and the card rail already depends on it, so gating
# it here would break a merged path rather than protect one.
_WRITE_FLAG = "MERCHANT_UCP_CHECKOUT_ENABLED"

# What we tell the merchant referred the buyer. Not the API host: this is the surface a human
# would have seen, which is what an attribution field is for.
_DEFAULT_REFERRING_DOMAIN = "agent.pivota.cc"


class MerchantUcpError(ValueError):
    """A merchant-side UCP call could not be completed.

    `caller_fault` splits 4xx from 502 at the route, mirroring `MerchantQuoteError` so the two
    clients present one failure vocabulary to their callers. Never string-match the message.
    """

    def __init__(self, message: str, *, caller_fault: bool = False, rpc_code: Optional[int] = None):
        super().__init__(message)
        self.caller_fault = caller_fault
        self.rpc_code = rpc_code


def write_ops_enabled() -> bool:
    return str(os.getenv(_WRITE_FLAG) or "").strip().lower() in ("1", "true", "on", "yes")


def agent_profile_url() -> Optional[str]:
    """Our UCP agent profile URI, which the merchant will FETCH.

    A dead pointer is strictly worse than a missing one: absent → the merchant answers
    un-negotiated, dead → the whole call 422s with a discovery error that reads like an auth
    problem. So an unset var refuses here rather than sending a call we know will fail.
    Serving hosts today: ucp.pivota.cc, mcp.pivota.cc.
    """
    raw = str(os.getenv("UCP_AGENT_PROFILE_URL") or "").strip()
    return raw or None


def build_meta() -> Dict[str, Any]:
    profile = agent_profile_url()
    if not profile:
        raise MerchantUcpError(
            "UCP_AGENT_PROFILE_URL is not configured; the merchant cannot negotiate this call"
        )
    # The hyphen in `ucp-agent` is the merchant's spelling, not ours. Probed 2026-08-31.
    return {"ucp-agent": {"profile": profile}}


def build_attribution(
    click_id: Optional[str],
    *,
    referring_domain: Optional[str] = None,
    campaign: Optional[str] = None,
) -> Dict[str, Any]:
    """The Option-C stamp: our origination evidence, carried INSIDE the protocol.

    A card-paid checkout fires no confirmation-page pixel, so the affiliate network's conversion
    event never happens and a cookie cannot carry us. UCP's `attribution` member is the
    replacement channel — the merchant's order keeps it as a read-only snapshot, which is what
    makes an agentic order legible as Pivota-originated in the merchant's own admin.

    Field names are the merchant's, probed 2026-08-31. `click_id_tag` is the PARAMETER NAME and
    `click_id_value` its value — the same pair we already put on referral URLs, so one click id
    reconciles across the referral lane and this one.
    """
    attribution: Dict[str, Any] = {
        "referring_domain": referring_domain or _DEFAULT_REFERRING_DOMAIN,
        "utm_source": "pivota",
        "utm_medium": "agent",
    }
    if click_id:
        attribution["click_id_tag"] = REFERRAL_CLICK_PARAM
        attribution["click_id_value"] = str(click_id)
    if campaign:
        attribution["utm_campaign"] = str(campaign)
    return attribution


def build_line_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize to the merchant's shape: `{"item": {"id": <variant id>}, "quantity": n}`.

    `item.id` is the PRODUCT VARIANT ID per the merchant's own schema description — not our
    catalog sig, not a product id. A row whose storefront variant identity we could not justify
    has no business reaching this call: the execution spec publishes `variant_id: None` for
    exactly those, and they belong on the referral rail.
    """
    out: List[Dict[str, Any]] = []
    for raw in items or []:
        variant_id = str((raw or {}).get("variant_id") or "").strip()
        if not variant_id:
            raise MerchantUcpError(
                "each line item needs a storefront variant_id", caller_fault=True
            )
        # ABSENT defaults to 1; PRESENT-BUT-ZERO is refused. `or 1` collapsed those two, turning
        # an explicit quantity of 0 into a line the buyer never asked for — and quantity is one
        # of the two numbers a cap is derived from.
        raw_qty = (raw or {}).get("quantity")
        if raw_qty is None:
            raw_qty = 1
        try:
            quantity = int(raw_qty)
        except (TypeError, ValueError):
            raise MerchantUcpError("line item quantity must be an integer", caller_fault=True)
        if quantity < 1:
            raise MerchantUcpError("line item quantity must be at least 1", caller_fault=True)
        out.append({"item": {"id": variant_id}, "quantity": quantity})
    if not out:
        raise MerchantUcpError("at least one line item is required", caller_fault=True)
    return out


def build_destination(address: Dict[str, Any]) -> Dict[str, Any]:
    """Map an address onto the merchant's `fulfillment.methods[].destinations[]` member.

    Only keys the merchant declares are emitted — an invented field is how a double starts
    describing a shape the real endpoint never had.
    """
    a = address or {}
    mapping = (
        ("first_name", "first_name"),
        ("last_name", "last_name"),
        ("phone_number", "phone_number"),
        ("street_address", "street_address"),
        ("extended_address", "extended_address"),
        ("address_locality", "address_locality"),
        ("address_region", "address_region"),
        ("postal_code", "postal_code"),
        ("address_country", "address_country"),
    )
    dest = {out_key: str(a[in_key]).strip() for out_key, in_key in mapping if a.get(in_key)}
    if not dest.get("address_country"):
        raise MerchantUcpError(
            "a destination needs address_country to be priced", caller_fault=True
        )
    return dest


def _unwrap(rpc: Any) -> Dict[str, Any]:
    """Pull the checkout payload out of an MCP tools/call response.

    ERRORS COME BACK 200. The merchant answers a JSON-RPC error with HTTP 200 and an `error`
    member, so a status-code check alone sees success. The card rail's original transport read
    only `result`, so a discovery refusal surfaced as the misleading "carried no checkout
    payload" — a wrong diagnosis of a wrong request. Read `error` FIRST.
    """
    if not isinstance(rpc, dict):
        raise MerchantUcpError("merchant response was not a JSON-RPC object")

    err = rpc.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        detail = str(data.get("code") or err.get("message") or "unknown error")
        # A profile/discovery refusal is OUR misconfiguration, not the merchant being down —
        # and it is the failure this module exists to stop shipping, so it gets named.
        caller_fault = str(data.get("code") or "") in ("invalid_profile_url", "profile_unreachable")
        raise MerchantUcpError(
            f"merchant refused the call: {detail}", caller_fault=caller_fault, rpc_code=code
        )

    result = rpc.get("result")
    if not isinstance(result, dict):
        raise MerchantUcpError("merchant response carried no result")

    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list):
        for chunk in content:
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                try:
                    parsed = json.loads(chunk.get("text") or "")
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    raise MerchantUcpError("merchant response carried no checkout payload")


async def call_tool(
    merchant_domain: str, tool: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """ONE transport for every merchant-side UCP call.

    Host is caller input and we fetch it, so both guards run here and the scheme and path are
    pinned — the caller controls the host label and nothing else. Redirects are not followed:
    a 30x to somewhere else is not a merchant door.

    NOTE ON THE ENDPOINT. We pin `https://{domain}/api/ucp/mcp` rather than reading the endpoint
    out of the merchant's `/.well-known/ucp`. Verified working on the apex 2026-08-31 (cosrx.com
    answers directly, though its profile names cosrx-renewal.myshopify.com). Discovery is the
    more correct hop and is deliberately NOT done yet: the endpoint URL would then be
    merchant-controlled input that we fetch, which reopens the SSRF surface the guards above
    close. Doing it properly means validating the discovered host with the same two guards.
    """
    domain = validate_merchant_domain(merchant_domain)
    if not domain:
        raise MerchantUcpError(
            "merchant_domain is not a fetchable public hostname", caller_fault=True
        )
    if not resolves_only_public(domain):
        raise MerchantUcpError(
            "merchant_domain does not resolve to a public address", caller_fault=True
        )

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    url = f"https://{domain}/api/ucp/mcp"
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            resp = await client.post(url, json=body)
    except httpx.HTTPError as err:
        logger.warning(
            "merchant-ucp %s failed domain=%s: %s", tool, domain, type(err).__name__
        )
        raise MerchantUcpError("merchant endpoint unreachable") from err
    if resp.status_code >= 300:
        raise MerchantUcpError(f"merchant endpoint returned HTTP {resp.status_code}")
    try:
        rpc = resp.json()
    except Exception as err:
        raise MerchantUcpError("merchant response was not JSON") from err
    return _unwrap(rpc)


async def create_checkout(
    merchant_domain: str,
    *,
    line_items: List[Dict[str, Any]],
    click_id: Optional[str] = None,
    referring_domain: Optional[str] = None,
    campaign: Optional[str] = None,
    buyer: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a checkout on the merchant's door, stamped with our attribution.

    Attribution is stamped UNCONDITIONALLY and at CREATE, not bolted on later: `order.attribution`
    is a read-only snapshot of the checkout's, so a stamp added after the fact may never reach
    the order. This is the whole monetization bet — it is not an optional enrichment.

    `payment` is never sent, on create or update, even though the merchant's schema permits it.
    That narrowing is deliberate and matches our own seller door.
    """
    if not write_ops_enabled():
        raise MerchantUcpError(f"{_WRITE_FLAG} is not enabled")

    checkout: Dict[str, Any] = {
        "line_items": build_line_items(line_items),
        "attribution": build_attribution(
            click_id, referring_domain=referring_domain, campaign=campaign
        ),
    }
    if buyer:
        checkout["buyer"] = buyer
    return await call_tool(
        merchant_domain, "create_checkout", {"meta": build_meta(), "checkout": checkout}
    )


async def update_checkout(
    merchant_domain: str,
    checkout_id: str,
    *,
    line_items: List[Dict[str, Any]],
    address: Optional[Dict[str, Any]] = None,
    fulfillment_type: str = "shipping",
    buyer: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Set the destination so the merchant can price shipping and tax.

    `line_items` is required by the merchant's schema on update too, so the caller passes the
    same list back rather than us caching it — a cached cart that drifts from the merchant's is
    the shape that quietly changes what a cap is capping.
    """
    if not write_ops_enabled():
        raise MerchantUcpError(f"{_WRITE_FLAG} is not enabled")
    if not str(checkout_id or "").strip():
        raise MerchantUcpError("checkout_id is required", caller_fault=True)

    checkout: Dict[str, Any] = {"line_items": build_line_items(line_items)}
    if address:
        checkout["fulfillment"] = {
            "methods": [
                {"type": fulfillment_type, "destinations": [build_destination(address)]}
            ]
        }
    if buyer:
        checkout["buyer"] = buyer
    return await call_tool(
        merchant_domain,
        "update_checkout",
        {"meta": build_meta(), "id": str(checkout_id), "checkout": checkout},
    )


async def get_checkout(merchant_domain: str, checkout_id: str) -> Dict[str, Any]:
    """Read a checkout back. `id`, not `checkout_id` — the merchant's spelling, probed."""
    if not str(checkout_id or "").strip():
        raise MerchantUcpError("checkout_id is required", caller_fault=True)
    return await call_tool(
        merchant_domain, "get_checkout", {"meta": build_meta(), "id": str(checkout_id)}
    )

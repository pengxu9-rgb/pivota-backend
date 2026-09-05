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
token `dev.shopify.card` wants?) is settled against a sandbox. That scope is ENFORCED, not just
described: `_ALLOWED_TOOLS` below is the list of tool names the transport will send, and anything
else is refused before any I/O. A paragraph does not stop a call.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

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
_DISCOVERY_FLAG = "MERCHANT_UCP_ENDPOINT_DISCOVERY_ENABLED"

# What we tell the merchant referred the buyer. Not the API host: this is the surface a human
# would have seen, which is what an attribution field is for.
_DEFAULT_REFERRING_DOMAIN = "agent.pivota.cc"


class MerchantUcpError(ValueError):
    """A merchant-side UCP call could not be completed.

    TWO FLAGS, TWO DIFFERENT PARTIES, because one flag was being asked to mean both and the
    route read it the wrong way round:

      caller_fault  THE API CALLER got it wrong — a line item with no variant_id, an update
                    with no line_item_ids, a checkout the merchant rejected on its merits.
                    The agent can fix its request and retry. The route answers 4xx.

      our_fault     PIVOTA got it wrong — our agent profile is unreachable, our discovery
                    handshake was refused, our code asked for a tool it may not send. The
                    caller can do nothing about it and retrying changes nothing. The route
                    answers 502 and logs the specific cause; telling the agent 422 sent it
                    hunting a bug in its own request that was never there.

    `our_fault` wins where both could be argued: a caller cannot fix our misconfiguration.
    Never string-match the message.
    """

    def __init__(
        self,
        message: str,
        *,
        caller_fault: bool = False,
        our_fault: bool = False,
        rpc_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.caller_fault = caller_fault
        self.our_fault = our_fault
        self.rpc_code = rpc_code


def write_ops_enabled() -> bool:
    return str(os.getenv(_WRITE_FLAG) or "").strip().lower() in ("1", "true", "on", "yes")


def endpoint_discovery_enabled() -> bool:
    """Its OWN switch, defaulting OFF, read PER CALL.

    Endpoint discovery is a real widening: it lets a merchant name the host we fetch, and it is
    reachable through `get_checkout`, which does NOT gate on `_WRITE_FLAG` because it writes
    nothing. So the write flag is not a kill switch for it and the capability would otherwise
    ship with none at all.

    Default OFF follows this codebase's rule for anything with blast radius — the seed-variant
    sourcing lane ships the same way, and an empty brand list there means NONE. Read per call so
    the switch works without a redeploy. Consequence, stated so it cannot surprise: until this is
    armed, a Wix merchant still reports no door, which is the pre-existing behaviour and not a
    regression.
    """
    return str(os.getenv(_DISCOVERY_FLAG) or "").strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
    )


# The profile we serve today, as a CODE DEFAULT rather than a required variable. This URL
# answers 200 (verified 2026-09-02); the merchant fetches it during negotiation. It lives here
# because an env var with no default made a deployment that simply forgot one variable fail
# every mint — and fail it with the variable's NAME in the response body, handing an external
# caller a piece of our configuration surface in exchange for nothing it could act on.
# `UCP_AGENT_PROFILE_URL` still overrides, which is what a second serving host (mcp.pivota.cc)
# or a staging profile needs.
_DEFAULT_AGENT_PROFILE_URL = "https://ucp.pivota.cc/.well-known/ucp-agent"


def agent_profile_url() -> str:
    """Our UCP agent profile URI, which the merchant will FETCH.

    A dead pointer is strictly worse than a missing one: absent → the merchant answers
    un-negotiated, dead → the whole call 422s with a discovery error that reads like an auth
    problem. So this never returns nothing: unset, empty, or whitespace all resolve to the
    profile we actually serve. Serving hosts today: ucp.pivota.cc, mcp.pivota.cc.
    """
    return str(os.getenv("UCP_AGENT_PROFILE_URL") or "").strip() or _DEFAULT_AGENT_PROFILE_URL


def build_meta() -> Dict[str, Any]:
    # No "is it configured" guard: `agent_profile_url()` cannot answer empty, and a branch that
    # can never be taken reads as protection this module does not have. If the profile stops
    # being served, the merchant says so — with a discovery refusal `_unwrap` already names as
    # ours (our_fault) rather than the caller's.
    # The hyphen in `ucp-agent` is the merchant's spelling, not ours. Probed 2026-08-31.
    return {"ucp-agent": {"profile": agent_profile_url()}}


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

    `referring_domain` IS VALIDATED AS A BARE HOSTNAME, through the card rail's own domain
    guard. It is caller-influenced text that lands in the merchant's order record as a read-only
    snapshot and is read back by humans and by reconciliation — so a full URL, a hostname with a
    path or port, or something that is not a hostname at all would put a value in the merchant's
    admin that no lane can match on. Anything that does not validate falls back to the default
    rather than being sent: an unattributable order is recoverable, a mis-attributed one is not.
    """
    referrer = validate_merchant_domain(referring_domain) if referring_domain else None
    if referring_domain and not referrer:
        logger.warning(
            "merchant-ucp: referring_domain was not a bare hostname; stamping the default"
        )
    attribution: Dict[str, Any] = {
        "referring_domain": referrer or _DEFAULT_REFERRING_DOMAIN,
        "utm_source": "pivota",
        "utm_medium": "agent",
    }
    if click_id:
        attribution["click_id_tag"] = REFERRAL_CLICK_PARAM
        attribution["click_id_value"] = str(click_id)
    if campaign:
        attribution["utm_campaign"] = str(campaign)
    return attribution


_VARIANT_GID_PREFIX = "gid://shopify/ProductVariant/"


def _variant_gid(variant_id: str) -> str:
    """The merchant's `item.id` is a Shopify ProductVariant GID, not the bare number.

    Probed live 2026-09-02 against cosrx.com: `{"item": {"id": "51086327775448"}}` is refused
    with `invalid_input` "is not a valid ProductVariant GID"; the same id as
    `gid://shopify/ProductVariant/51086327775448` opens the checkout. Every variant id we hold
    (the offers-lane token ctx, `attached_variant_id`, the storefront .js stamp) is the bare
    number, so the wrap happens HERE, once, rather than at every call site. A value that is
    already a GID passes through; anything else is left as given — this module pins Shopify's
    `/api/ucp/mcp` path, so a non-numeric, non-GID id is an upstream data problem the merchant
    will name, not something to guess at.
    """
    if not variant_id:
        return variant_id
    if variant_id.isdigit():
        return f"{_VARIANT_GID_PREFIX}{variant_id}"
    return variant_id


def build_line_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize to the merchant's shape: `{"item": {"id": <variant id>}, "quantity": n}`.

    `item.id` is the PRODUCT VARIANT ID per the merchant's own schema description — not our
    catalog sig, not a product id. A row whose storefront variant identity we could not justify
    has no business reaching this call: the execution spec publishes `variant_id: None` for
    exactly those, and they belong on the referral rail.
    """
    out: List[Dict[str, Any]] = []
    for raw in items or []:
        # isinstance, not `(raw or {})`. That construct raises AttributeError on a TRUTHY
        # non-dict — `["x"]` — which is the identical bug `_endpoint_from_profile` was fixed for
        # and whose comment names it. Fixing one instance and not the pattern is how a ratchet
        # that matches one syntactic form permits the rest.
        raw = raw if isinstance(raw, dict) else {}
        variant_id = _variant_gid(str(raw.get("variant_id") or "").strip())
        if not variant_id:
            raise MerchantUcpError(
                "each line item needs a storefront variant_id", caller_fault=True
            )
        # ABSENT defaults to 1; PRESENT-BUT-ZERO is refused. `or 1` collapsed those two, turning
        # an explicit quantity of 0 into a line the buyer never asked for — and quantity is one
        # of the two numbers a cap is derived from.
        raw_qty = raw.get("quantity")
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


def message_codes(payload: Dict[str, Any]) -> List[str]:
    """The merchant's own reason codes out of a UCP `messages[]` array.

    Codes only, never `content`: the code is the machine-readable half a caller can branch on,
    and the free text is merchant-authored prose that has no business being pasted into our
    error surface. Missing/oddly-shaped entries are skipped rather than guessed at.
    """
    out: List[str] = []
    # isinstance, not `or []`. A TRUTHY non-list — `{"messages": 5}` — survives `or []` and then
    # `for entry in 5` is a TypeError, which nothing between here and the route translates. Same
    # truthy-non-dict class as `_endpoint_from_profile`; this is the sibling instance.
    messages = payload.get("messages")
    for entry in messages if isinstance(messages, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("code"), str) and entry["code"].strip():
            out.append(entry["code"].strip())
    return out


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
        # A profile/discovery refusal is OUR misconfiguration, not the merchant being down and
        # not the agent's request — and it is the failure this module exists to stop shipping,
        # so it gets named as ours. It used to be flagged `caller_fault`, which the route maps
        # to 422: our own broken profile presented to the agent as ITS bad request.
        our_fault = str(data.get("code") or "") in ("invalid_profile_url", "profile_unreachable")
        raise MerchantUcpError(
            f"merchant refused the call: {detail}", our_fault=our_fault, rpc_code=code
        )

    result = rpc.get("result")
    if not isinstance(result, dict):
        raise MerchantUcpError("merchant response carried no result")

    # VALIDATION ERRORS COME BACK AS A SUCCESSFUL TOOL CALL. Shopify's UCP door answers a schema
    # violation with `result.isError: true` and a plain-text chunk ("Invalid arguments: object
    # at `/checkout/fulfillment/methods/0` is missing required properties: line_item_ids"), not
    # a JSON-RPC `error`. Read as a payload that text is not JSON, so the old path fell through
    # to "carried no checkout payload" — the merchant told us exactly what was wrong and we
    # threw the message away. Probed live 2026-09-02. These are OUR malformed requests, hence
    # caller_fault.
    if result.get("isError"):
        # Same reason as `message_codes`: a truthy non-list `content` passes `or []` and then
        # raises TypeError on iteration.
        raw_content = result.get("content")
        texts = [
            str(chunk.get("text") or "").strip()
            for chunk in (raw_content if isinstance(raw_content, list) else [])
            if isinstance(chunk, dict) and chunk.get("type") == "text"
        ]
        # TWO isError shapes, probed live 2026-09-02. cosrx.com: plain text ("Invalid
        # arguments: ... missing required properties: line_item_ids") — a malformed request,
        # raise it. judydoll.com: a full UCP checkout PAYLOAD as JSON text, `ucp.status`
        # "success", with the refusal in `messages[]` — that is the same shape a sold-out item
        # comes back in WITHOUT isError, and callers already read `messages` off it. Return the
        # payload so the caller sees the merchant's own reason instead of a truncated dump.
        #
        # `ucp.status == "success"` IS THE WHOLE ADMISSION TEST, and it is not decoration. The
        # first cut returned any dict carrying `messages` or `ucp` — which admits a payload whose
        # own envelope says `ucp.status: "error"`. Such a payload can still carry `totals`, and
        # `resolve_merchant_quote` reads totals: a merchant ERROR would have minted a real,
        # spendable card capped against a checkout that does not exist. A payload that says it
        # failed is a REFUSAL, and it is raised as one, carrying the merchant's own reason codes.
        for t in texts:
            try:
                parsed = json.loads(t)
            except Exception:
                continue
            if not isinstance(parsed, dict) or not ("messages" in parsed or "ucp" in parsed):
                continue
            ucp = parsed.get("ucp")
            if isinstance(ucp, dict) and ucp.get("status") == "success":
                return parsed
            codes = ", ".join(message_codes(parsed)) or "unspecified"
            raise MerchantUcpError(
                f"merchant refused the checkout: {codes}", caller_fault=True
            )
        detail = " | ".join(t for t in texts if t) or "unspecified"
        raise MerchantUcpError(
            f"merchant rejected the call: {detail[:500]}", caller_fault=True
        )

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


# THE TOOLS THIS MODULE MAY SEND, enumerated. The module docstring says it builds and prices a
# checkout and never completes one — but a docstring does not stop a call, and `complete_checkout`
# is one string away from every send site here. That hop is the MONEY hop and is deliberately
# unbuilt (the credential question — can a Reap reveal handle produce the token `dev.shopify.card`
# wants? — is unsettled), so the refusal is made structural: a tool not on this list never reaches
# the wire, whatever a future caller passes. Widening this list is the decision, not a diff.
_ALLOWED_TOOLS = frozenset(
    {
        "get_checkout",
        "create_checkout",
        "update_checkout",
        "search_catalog",
        "lookup_catalog",
        "get_product",
    }
)


_MCP_PATH = "/api/ucp/mcp"

# Which statuses may trigger the one hop. NOT `300 <= code < 400`: 303 See Other means the POST
# WAS PROCESSED and the result should now be GET'd, so re-POSTing an identical `create_checkout`
# on a 303 is how you build the merchant a second cart. 300 and 304 carry no usable Location for
# a POST either. An apex<->www edge redirect is always one of these four.
_HOP_STATUSES = frozenset({301, 302, 307, 308})

_WELL_KNOWN_PATH = "/.well-known/ucp"

# A real UCP profile is a few KB. Shopify's is ~1.5KB, Wix's ~1KB. The same ceiling is applied
# to merchant POST responses: nothing legitimate on this rail is larger.
_MAX_PROFILE_BYTES = 256 * 1024


class _BoundedBody:
    """A merchant response read under a byte ceiling. Same surface `_call_tool` used before."""

    __slots__ = ("status_code", "headers", "content", "over_limit")

    def __init__(self, status_code, headers, content, over_limit=False):
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.over_limit = over_limit

    def json(self) -> Any:
        return json.loads(self.content)


async def _read_bounded(
    client: Any,
    method: str,
    url: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    limit: int = _MAX_PROFILE_BYTES,
) -> _BoundedBody:
    """Read a merchant response, stopping at `limit` DECODED bytes.

    WHY STREAMING, and why the previous attempt did not work. `client.get`/`client.post` read AND
    DECODE the entire body inside the call, so a cap applied to `resp.content` runs after the
    damage is done. Asking for `Accept-Encoding: identity` does not help either: httpx decodes on
    the RESPONSE's `Content-Encoding`, so the request header is advisory and a hostile merchant is
    precisely the one who ignores it. Measured: a 20,420-byte gzip body answered a request that
    sent `identity` and produced 20,971,530 bytes in `.content` — which is what the cap then
    inspected. The only real bound is to stop consuming.

    DECODED bytes, not raw, is the number that matters: 256KB of raw gzip is ~256MB inflated, so
    counting wire bytes would bound the wrong quantity.

    (The earlier comment here also claimed MemoryError is a BaseException that escapes
    `except Exception`. It is not — `issubclass(MemoryError, Exception)` is True. Both claims were
    wrong; this docstring states what was measured.)
    """
    kwargs: Dict[str, Any] = {}
    if json_body is not None:
        kwargs["json"] = json_body
    if headers is not None:
        kwargs["headers"] = headers
    async with client.stream(method, url, **kwargs) as resp:
        chunks: List[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > limit:
                return _BoundedBody(resp.status_code, resp.headers, b"", over_limit=True)
            chunks.append(chunk)
        return _BoundedBody(resp.status_code, resp.headers, b"".join(chunks))


_DISCOVER_STATUSES = frozenset({403, 404, 405})


def _sibling_host(
    domain: str, location: str, expected_path: str = _MCP_PATH
) -> Optional[str]:
    """The ONE redirect we follow: apex <-> www on the same registrable domain.

    A merchant whose door lives on `www` answers the apex with a 301, and refusing every 30x
    scored those merchants dead: robinsons.com.sg 301s to www.robinsons.com.sg and was recorded
    as `search_failed / HTTP 301`, while the www host prices a real cart. Widening the refusal
    to "follow redirects" would hand the merchant control of the host we fetch, which is exactly
    the SSRF surface `validate_merchant_domain` and `resolves_only_public` close — so the only
    accepted target is the apex/www sibling of the host we already validated, over https, on the
    pinned path, with no port of its own. Anything else is still not a merchant door.

    Returns the sibling host to retry, or None to keep refusing. The caller still runs BOTH
    guards on whatever comes back: this function decides shape, never trust.

    ON THE SCHEME/PATH/PORT CHECKS. They are not the security boundary and must not be read as
    one — the retry URL is REBUILT from `sibling` + `_MCP_PATH`, so a Location's scheme, path,
    port, userinfo, query and fragment can never reach the wire whatever they say. What they do
    is bound WHEN we spend a second request: we hop only for a redirect that looks like the
    path-preserving apex<->www move we are here to follow. A root Location (`https://www.x/`)
    is therefore refused today. That is a deliberate call, not an oversight, and
    `test_a_root_location_on_the_sibling_is_refused_today` pins it: every apex<->www redirect
    observed in the 2026-09-03 and 2026-09-04 sweeps was path-preserving, so relaxing it would
    buy coverage we have no evidence we need while making us fetch on vaguer signals. If a real
    merchant ever redirects to root, that test names exactly what to change.
    """
    if not location:
        return None
    try:
        parsed = urlsplit(location)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.path != expected_path:
        return None
    try:
        if parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return None
    sibling = domain[4:] if domain.startswith("www.") else f"www.{domain}"
    if host != sibling:
        return None
    return host


def _validated_endpoint(url: str) -> Optional[str]:
    """Admit a merchant-DECLARED MCP endpoint, or None.

    This is the hop the long note in `_call_tool` said was "the more correct hop" and deliberately
    not taken, because a discovered endpoint is merchant-controlled input that we then fetch. The
    note also said what doing it properly requires — "validating the discovered host with the same
    two guards" — and that is exactly what happens here and at the call site.

    The EXACT path is not pinned — pinning `/api/ucp/mcp` is what created the gap, since that is a
    Shopify convention and Wix serves `/ecom/ucp/<siteId>/mcp`. But "not pinned" is not "anything":
    the path must END IN `/mcp`, be printable ASCII, and be short, and the query is DROPPED
    entirely (no known platform uses one; both `_MCP_PATH` and Wix's path satisfy this).

    WHY THAT MATTERS, stated plainly because an earlier version of this docstring got it wrong.
    Admitting a host other than the merchant's own — `*.myshopify.com`, `www.wixapis.com` — is
    genuinely the same capability the caller already has: `merchant_domain` arrives from a request
    body and any public host may be named there. Admitting an arbitrary PATH is NOT. With a free
    path this function turned "POST to a UCP door on any public host" into "POST anywhere on any
    public host, and read the status back", which is a forced-request primitive with a response
    oracle. The `/mcp` suffix does not reduce that to nothing, and the residual is written down
    here rather than denied: a merchant can still aim our egress at a public URL ending `/mcp`,
    and `/mcp` is the MCP convention, so the bound SELECTS FOR public MCP servers rather than
    shrinking the set much.

    And it is a CONTENT oracle, not a status one — the earlier wording said "read the status
    back" and that understated it. What reaches the API caller is the target's status and
    hostname, its entire JSON-RPC `result` on a 2xx, and its `error.message` spliced verbatim
    into our error string. No credential is attached and the body is our own JSON-RPC — but that
    second half is true because of what is WIRED today, not by construction: `create_checkout`
    and `update_checkout` put caller-supplied buyer and address in that same body, so routing
    either of them makes this sentence false and nothing here re-checks it.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    if parsed.username or parsed.password:
        return None
    try:
        if parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or validate_merchant_domain(host) != host:
        return None
    # `resolves_only_public` resolves, and its own guard catches only OSError. A 64-character DNS
    # label passes `_DOMAIN_RE` but exceeds IDNA's 63, so getaddrinfo raises UnicodeError — a
    # ValueError. This function's whole contract is "admit, or return None", so it absorbs that
    # here rather than leaving a helper that can raise for any future caller to trip over.
    try:
        if not resolves_only_public(host):
            return None
    except (ValueError, UnicodeError):
        return None
    path = parsed.path or "/"
    # httpx refuses non-printable bytes and very long URLs at request-build time by raising
    # httpx.InvalidURL, which is NOT an httpx.HTTPError, so it would escape every net we have.
    # Refuse those here instead: a merchant must not be able to turn a profile into a 500.
    if len(path) > 512 or not path.isascii() or not path.isprintable():
        return None
    if not path.endswith("/mcp"):
        return None
    # A suffix is not a path bound. httpx forwards the path verbatim, so the TARGET decides what
    # `/admin/jobs/%2e%2e/mcp` or `/admin/jobs;/mcp` resolves to — a server that collapses dot
    # segments or strips `;` parameters after routing receives our POST somewhere that does not
    # end `/mcp` at all. Refuse the shapes that let a target's normaliser disagree with ours.
    lowered = path.lower()
    if ".." in path or ";" in path or "//" in path:
        return None
    # Percent-encoded delimiters too: a target that decodes BEFORE routing sees `.` `?` `#` NUL
    # and `\` where we saw an opaque path, and `%25` lets it double-decode its way to any of them.
    if any(tok in lowered for tok in ("%2e", "%2f", "%3f", "%23", "%00", "%5c", "%25")):
        return None
    # Rebuilt from validated parts — fragment, userinfo and QUERY never reach the wire.
    return f"https://{host}{path}"


def _endpoint_from_profile(profile: Any) -> Optional[str]:
    """Pull the `dev.ucp.shopping` MCP endpoint out of a UCP profile document."""
    if not isinstance(profile, dict):
        return None
    # Every level is isinstance-checked. `(profile.get("ucp") or {}).get(...)` looks defensive but
    # raises AttributeError on a TRUTHY non-dict — `{"ucp": "2026-04-08"}` is a real shape for a
    # merchant to serve, and it escaped as an unhandled 500.
    ucp = profile.get("ucp")
    if not isinstance(ucp, dict):
        return None
    services = ucp.get("services")
    if not isinstance(services, dict):
        return None
    entries = services.get("dev.ucp.shopping")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("transport") == "mcp":
            endpoint = entry.get("endpoint")
            if isinstance(endpoint, str) and endpoint:
                return endpoint
    return None


async def _discover_endpoint(client: Any, domain: str) -> Optional[str]:
    """Read the merchant's profile and return a validated MCP endpoint, or None.

    Never raises: discovery is a fallback, so a merchant that serves no profile, serves HTML, or
    serves a profile naming an endpoint we refuse must look exactly like "no door" — not like an
    error from the tool the caller asked for.
    """
    url = f"https://{domain}{_WELL_KNOWN_PATH}"
    # `identity` is a courtesy only — httpx decodes on the RESPONSE header, so this does not
    # bound anything. `_read_bounded` is what bounds it.
    _no_compression = {"Accept-Encoding": "identity"}
    try:
        resp = await _read_bounded(client, "GET", url, headers=_no_compression)
        if resp.status_code in _HOP_STATUSES:
            sibling = _sibling_host(
                domain, resp.headers.get("location", ""), _WELL_KNOWN_PATH
            )
            if (
                sibling
                and validate_merchant_domain(sibling) == sibling
                and resolves_only_public(sibling)
            ):
                resp = await _read_bounded(
                    client,
                    "GET",
                    f"https://{sibling}{_WELL_KNOWN_PATH}",
                    headers=_no_compression,
                )
        if resp.status_code != 200:
            return None
        # `_read_bounded` already stopped at the ceiling; this reports it. Size is not the parser
        # hazard anyway — see the RecursionError note on the except below.
        if resp.over_limit:
            logger.info(
                "merchant-ucp discovery profile exceeded %d bytes domain=%s",
                _MAX_PROFILE_BYTES,
                domain,
            )
            return None
        # INSIDE the try, deliberately. `resp.json()` raises ValueError on a non-JSON body, and
        # `_validated_endpoint` runs `resolves_only_public`, whose getaddrinfo raises UnicodeError
        # (a ValueError, NOT the OSError its own guard catches) for a 64-character DNS label —
        # which `_DOMAIN_RE` permits and IDNA does not. Both escaped to an unhandled 500 while
        # this function's docstring promised it never raises.
        endpoint = _validated_endpoint(_endpoint_from_profile(resp.json()))
    # `except Exception`, NOT an enumerated tuple. Enumerating was the bug: `b"[" * 1200` is a
    # 2.4KB body — three orders of magnitude under the size cap — and `json.loads` answers it
    # with RecursionError, a RuntimeError, which no tuple of (HTTPError, InvalidURL, ValueError,
    # TypeError) contains. DEPTH is the parser hazard and a byte cap cannot bound it. The main
    # read in `_call_tool` already used `except Exception` for exactly this reason; narrowing it
    # here lost that. The type is logged so a bug of OURS is still visible rather than silent.
    except Exception as err:
        logger.info(
            "merchant-ucp discovery failed domain=%s: %s", domain, type(err).__name__
        )
        return None
    if endpoint is None:
        # The one security-relevant event in this feature is a profile we REFUSED. Log the fact,
        # not the URL. (`_call_tool` DOES log the accepted endpoint — that one has already passed
        # `_validated_endpoint`, so it is bounded printable ASCII on a guarded host. A refused one
        # has passed nothing.)
        logger.info("merchant-ucp discovery yielded no usable endpoint domain=%s", domain)
    return endpoint


async def _call_tool(
    merchant_domain: str, tool: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """ONE transport for every merchant-side UCP call. PRIVATE — every caller is in this module.

    Underscored so that adding a send site is an edit HERE, next to the allowlist and the two
    SSRF guards, rather than an import somewhere else that inherits none of this file's rules.

    Host is caller input and we fetch it, so both guards run here. The scheme is pinned and the
    path is pinned ON THE FIRST ATTEMPT; once discovery runs, the path is BOUNDED rather than
    pinned and the host may be one the merchant named (see `_validated_endpoint`). Redirects are not followed,
    with ONE exception: an apex<->www 30x on the same registrable domain is retried once, and
    the sibling host is put through both guards before we fetch it (see _sibling_host). Any
    other 30x is still not a merchant door.

    NOTE ON THE ENDPOINT. We TRY `https://{domain}/api/ucp/mcp` first — a Shopify convention that
    answers directly on the apex for most of the corpus (verified on cosrx.com 2026-08-31, whose
    profile names cosrx-renewal.myshopify.com yet whose apex answers anyway). That pin used to be
    the only thing we tried, and this note used to say discovery was deliberately not done because
    a discovered endpoint is merchant-controlled input that we fetch. It also said what doing it
    properly would require: validating the discovered host with the same two guards.

    That is now done, because the pin was not merely incomplete, it was WRONG for a whole platform:
    Wix serves a real UCP door at `https://www.wixapis.com/ecom/ucp/<siteId>/mcp` and answers the
    pinned path 403, so every Wix merchant was recorded as having no door. On `_DISCOVER_STATUSES`
    we read the merchant's profile and re-validate whatever it names through `_validated_endpoint`,
    which is host-first and does NOT pin the path — pinning the path is what caused the gap.
    """
    if tool not in _ALLOWED_TOOLS:
        # BEFORE the guards and before any I/O: an unlisted tool is a bug in this module, not a
        # request to validate. `complete_checkout` and `cancel_checkout` are the two names this
        # is really about — see _ALLOWED_TOOLS.
        raise MerchantUcpError(
            f"{tool} is not a tool this client may send", our_fault=True
        )

    domain = validate_merchant_domain(merchant_domain)
    if not domain:
        raise MerchantUcpError(
            "merchant_domain is not a fetchable public hostname", caller_fault=True
        )
    # Wrapped for the same reason `_validated_endpoint` is: `_DOMAIN_RE` admits a 64-character
    # label, IDNA caps at 63, and getaddrinfo answers that with UnicodeError — a ValueError, not
    # the OSError this guard catches. `merchant_domain` is request-body input, so unwrapped this
    # was an unhandled 500 reachable with no flag armed at all. Assigned first and raised after:
    # MerchantUcpError IS a ValueError, so raising inside the try would catch our own refusal.
    try:
        domain_is_public = resolves_only_public(domain)
    except (ValueError, UnicodeError):
        domain_is_public = False
    if not domain_is_public:
        raise MerchantUcpError(
            "merchant_domain does not resolve to a public address", caller_fault=True
        )

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    # The host we actually fetched. It diverges from `domain` the moment we hop, and every
    # failure below is reported against THIS, not the host the caller named: an operator who
    # curls the apex and gets an instant 301 should not be told the apex timed out.
    fetched = domain
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            resp = await _read_bounded(
                client, "POST", f"https://{domain}{_MCP_PATH}", json_body=body
            )
            # Exactly one hop, and only to the apex/www sibling — see _sibling_host. The retry
            # re-runs BOTH guards on the new host: a redirect target is merchant-controlled
            # input, so it is validated like any other caller-supplied host, never trusted
            # because a 301 pointed at it. The URL is REBUILT from the sibling, so the merchant
            # chooses whether we hop and never where.
            if resp.status_code in _HOP_STATUSES:
                sibling = _sibling_host(domain, resp.headers.get("location", ""))
                if (
                    sibling
                    and validate_merchant_domain(sibling) == sibling
                    and resolves_only_public(sibling)
                ):
                    logger.info(
                        "merchant-ucp %s following apex<->www redirect domain=%s -> %s",
                        tool,
                        domain,
                        sibling,
                    )
                    fetched = sibling
                    resp = await _read_bounded(
                        client, "POST", f"https://{sibling}{_MCP_PATH}", json_body=body
                    )
            # The pinned path is Shopify's. When it says "no door here", ask the merchant's own
            # profile where the door is — the hop the note above called more correct. The
            # discovered endpoint is re-validated host-first by `_validated_endpoint`, and a
            # discovery that yields nothing leaves the original response untouched, so a merchant
            # with genuinely no door still reports its own status rather than a discovery error.
            # 403 has to stay in _DISCOVER_STATUSES — it is exactly what the motivating Wix
            # merchant answers on the pinned path — but 403 is also what a WAF returns to any
            # unexpected POST, so naming any public host costs that host a discovery GET. That
            # forced-request budget is the reason this is behind its own switch rather than
            # riding on the write flag, which `get_checkout` never checks.
            if resp.status_code in _DISCOVER_STATUSES and endpoint_discovery_enabled():
                discovered = await _discover_endpoint(client, domain)
                already_tried = {f"https://{domain}{_MCP_PATH}", f"https://{fetched}{_MCP_PATH}"}
                if discovered and discovered not in already_tried:
                    logger.info(
                        "merchant-ucp %s using discovered endpoint domain=%s -> %s",
                        tool,
                        domain,
                        discovered,
                    )
                    fetched = urlsplit(discovered).hostname or fetched
                    resp = await _read_bounded(client, "POST", discovered, json_body=body)
    except (httpx.HTTPError, httpx.InvalidURL) as err:
        logger.warning(
            "merchant-ucp %s failed domain=%s host=%s: %s",
            tool,
            domain,
            fetched,
            type(err).__name__,
        )
        raise MerchantUcpError("merchant endpoint unreachable") from err
    if resp.over_limit:
        raise MerchantUcpError(
            f"merchant response exceeded {_MAX_PROFILE_BYTES} bytes from {fetched}"
        )
    if resp.status_code >= 300:
        raise MerchantUcpError(
            f"merchant endpoint returned HTTP {resp.status_code} from {fetched}"
        )
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
    return await _call_tool(
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
    line_item_ids: Optional[List[str]] = None,
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
        # The merchant's schema REQUIRES `line_item_ids` on a fulfillment method — which lines
        # this shipping method covers. Those ids are the merchant's own (`line_items[].id` on the
        # CREATE response, `gid://shopify/CartLine/<uuid>?cart=<cart>`), not our variant ids,
        # so the caller has to carry them over from the checkout it created. Probed live
        # 2026-09-02: a method without them is rejected ("missing required properties:
        # line_item_ids"), and before the isError fix above that read as "no checkout payload".
        ids = [str(i).strip() for i in (line_item_ids or []) if str(i or "").strip()]
        if not ids:
            raise MerchantUcpError(
                "pricing a destination needs the checkout's line_item_ids "
                "(from create_checkout's line_items[].id)",
                caller_fault=True,
            )
        checkout["fulfillment"] = {
            "methods": [
                {
                    "type": fulfillment_type,
                    "line_item_ids": ids,
                    "destinations": [build_destination(address)],
                }
            ]
        }
    if buyer:
        checkout["buyer"] = buyer
    return await _call_tool(
        merchant_domain,
        "update_checkout",
        {"meta": build_meta(), "id": str(checkout_id), "checkout": checkout},
    )


async def get_checkout(merchant_domain: str, checkout_id: str) -> Dict[str, Any]:
    """Read a checkout back. `id`, not `checkout_id` — the merchant's spelling, probed."""
    if not str(checkout_id or "").strip():
        raise MerchantUcpError("checkout_id is required", caller_fault=True)
    return await _call_tool(
        merchant_domain, "get_checkout", {"meta": build_meta(), "id": str(checkout_id)}
    )

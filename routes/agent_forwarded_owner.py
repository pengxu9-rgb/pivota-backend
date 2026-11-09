"""The partner an MCP money call is made for, as its owner (FIX_PLAN O-04). Behind
PARTNER_IDENTITY_FORWARDING_ENABLED, default OFF.

The gateway's commerce kernel calls this service with its OWN service key on every MCP lane, so
without this every MCP order, quote, payment link and refund belongs to the gateway's agent: one
identity's scope, quotas, usage stats and webhooks for every partner. The gateway already signs
the agent it verified into X-Pivota-Issuing-Agent for offers.resolve (services/
issuing_agent_assertion.py). With the flag on it signs the money ops too, and this module makes
the asserted agent the request's AgentContext on the routes those ops reach:

  * only when the request is authenticated as one of Pivota's own services allowed to vouch
    (routes.agent_auth._is_service_caller, the same test offers.resolve uses). An agent's own key
    cannot vouch for another agent, and a request with no service identity cannot vouch at all;
  * only with a REQUEST-BOUND v2 assertion (services/bound_issuing_assertion.py): it must verify for
    THAT route's operation, method, path, resource id and exact body bytes, within 60 s, name an
    ACTIVE, non-service agent (the v1 subject rules), and carry a nonce never seen before
    (services/assertion_replay_guard.py). A v2 header that fails any of these REFUSES the request
    (409 PARTNER_ASSERTION_REJECTED). A v1 token is never adopted on these routes: it binds no
    request, so a captured one would be an "act as this partner" capability;
  * the adopted agent then pays its own rate limit and daily quota, exactly as get_agent_context
    charges a caller that presents its own key, and owns what the route writes.

Anything else leaves the context EXACTLY as get_agent_context built it (the gateway), with one
log line and a counter naming why: `absent` (the gateway sent no header), `unbound_v1` (a v1
token), `ignored_not_service` (a header from a caller that may not vouch). A refused v2 header is
counted as `rejected_<reason>`. Ownership is never taken from a request body field or from an
unverified header.

A checkout-token request is never touched: the token names its own agent and order.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from fastapi import Depends, HTTPException, Request

import routes.agent_auth as agent_auth
from routes.agent_auth import AgentContext, get_agent_context
import services.issuing_agent_assertion as ia
from services import assertion_replay_guard
from services.bound_issuing_assertion import (
    MAX_SKEW_SECONDS_V2,
    BoundSubject,
    body_sha256,
    is_v2_token,
    verify_bound_issuing_agent_assertion,
)
from services.issuing_agent_assertion import ISSUING_AGENT_ASSERTION_HEADER
from utils.logger import logger

PARTNER_IDENTITY_FORWARDING_ENV = "PARTNER_IDENTITY_FORWARDING_ENABLED"

#: The order-metadata key that records an adopted owner: which partner, which service vouched for
#: it, and for which operation. Written only by this service (create_order_v2 drops a caller's).
PARTNER_IDENTITY_METADATA_KEY = "partner_identity"
#: The same partner, flat, so a reporting query reads one JSON path.
PARTNER_AGENT_ID_METADATA_KEY = "partner_agent_id"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Outcome counts for this process: adopted / absent / invalid / ignored_not_service.
FORWARDED_OWNER_OUTCOMES: Counter = Counter()


def partner_identity_forwarding_enabled() -> bool:
    return str(os.getenv(PARTNER_IDENTITY_FORWARDING_ENV) or "").strip().lower() in _TRUTHY


def _note(outcome: str, *, op: str, request: Optional[Request], caller: Optional[str]) -> None:
    FORWARDED_OWNER_OUTCOMES[outcome] += 1
    try:
        path = request.url.path if request is not None else None
    except Exception:  # noqa: BLE001 -- a log label, never a reason to fail
        path = None
    logger.info(
        "partner_identity_forwarding outcome=%s op=%s path=%s caller_agent_id=%s",
        outcome, op, path, caller,
    )


def _header(request: Optional[Request]) -> Optional[str]:
    try:
        value = request.headers.get(ISSUING_AGENT_ASSERTION_HEADER) if request is not None else None
    except Exception:  # noqa: BLE001
        return None
    value = str(value or "").strip()
    return value or None


async def _charge_rate_and_quota(agent: Mapping[str, Any]) -> None:
    """The adopted agent's own limits, refused the way get_agent_context refuses a caller's."""
    agent_id = agent["agent_id"]
    rate_ok, current, limit = await agent_auth.check_rate_limit(agent_id, rate_limit=agent.get("rate_limit"))
    if not rate_ok:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {current}/{limit} requests per minute",
            headers={
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(max(0, limit - current)),
            },
        )
    quota_ok, used, quota = await agent_auth.check_daily_quota(agent_id, daily_quota=agent.get("daily_quota"))
    if not quota_ok:
        raise HTTPException(
            status_code=429,
            detail=f"Daily quota exceeded: {used}/{quota} requests",
            headers={
                "X-Quota-Limit": str(quota),
                "X-Quota-Remaining": str(max(0, quota - used)),
                "X-Quota-Reset": "00:00 UTC",
            },
        )


#: A refused assertion is 409, not 401/403: the gateway's private backend hop maps a backend 401/403 to
#: "backend_iam_rejected" (PIVOTA-Agent src/services/privateBackendHop.js), which would hide the reason.
REJECTED_STATUS = 409
REJECTED_CODE = "PARTNER_ASSERTION_REJECTED"

#: The nonce store keeps a nonce at least as long as the token can be accepted (both sides of the window).
REPLAY_TTL_SECONDS = 2 * MAX_SKEW_SECONDS_V2 + 10


def _raw_path(request: Request) -> str:
    """The path as the client sent it (percent-encoded, no query): what the gateway signed."""
    try:
        raw = request.scope.get("raw_path")
        if isinstance(raw, (bytes, bytearray)) and raw:
            return bytes(raw).decode("latin-1").split("?", 1)[0]
    except Exception:  # noqa: BLE001
        pass
    return request.url.path


async def _resource_id(request: Request, raw_body: bytes, resource: Optional[Tuple[str, str]]) -> Optional[str]:
    if not resource:
        return None
    where, name = resource
    if where == "path":
        value = request.path_params.get(name)
        return str(value) if value is not None else None
    try:
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    value = body.get(name)
    if value is None and isinstance(body.get("order_request"), dict):
        # The gateway's create_order fallback wraps the body (src/server.js isPydanticMissingBodyField).
        value = body["order_request"].get(name)
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None


async def _subject_agent_id(subject: BoundSubject) -> Optional[str]:
    """The ACTIVE, non-service agent a verified subject names: the v1 rules, unchanged."""
    excluded = agent_auth.issuing_excluded_agent_ids()
    try:
        if subject.kind == "agent":
            return await ia._active_agent_id(subject.agent_id, excluded)
        if subject.kind == "oauth":
            registered = await ia.agent_for_oauth_client(subject.issuer or "", subject.client_id or "")
            return await ia._active_agent_id(registered, excluded)
    except Exception as exc:  # noqa: BLE001 -- unreadable = not adoptable
        logger.warning("partner_identity_forwarding subject lookup failed: %s", type(exc).__name__)
    return None


def _reject(reason: str, *, op: str, request: Request, caller: Optional[str]) -> HTTPException:
    _note(f"rejected_{reason}", op=op, request=request, caller=caller)
    return HTTPException(
        status_code=REJECTED_STATUS,
        detail={
            "error": REJECTED_CODE,
            "reason": reason,
            "message": "The partner assertion on this request was refused; it was not made as any partner.",
        },
    )


async def verify_bound_forwarded_subject(
    request: Request, *, ops: Sequence[str], resource: Optional[Tuple[str, str]] = None,
) -> Tuple[Optional[str], Optional[str], str]:
    """(agent_id, op, reason) for this request's v2 assertion. Checks the binding (method, path,
    resource id, body hash), the window, the subject, and LAST claims the nonce (single use), so a
    token that fails any other check never burns one. Never raises."""
    token = _header(request)
    try:
        raw_body = await request.body()
    except Exception:  # noqa: BLE001
        raw_body = b""
    verdict = verify_bound_issuing_agent_assertion(
        token,
        ops=ops,
        method=request.method,
        path=_raw_path(request),
        resource_id=await _resource_id(request, raw_body, resource),
        body_hash=body_sha256(raw_body),
    )
    if verdict.subject is None:
        return None, None, verdict.reason
    agent_id = await _subject_agent_id(verdict.subject)
    if not agent_id:
        return None, None, "subject"
    first_use, store = await assertion_replay_guard.claim(verdict.subject.jti, REPLAY_TTL_SECONDS)
    if not first_use:
        logger.info("partner_identity_forwarding nonce reuse refused store=%s", store)
        return None, None, "replay"
    return agent_id, verdict.subject.op, "ok"


async def adopt_forwarded_owner(
    request: Request,
    context: AgentContext,
    *,
    ops: Sequence[str],
    resource: Optional[Tuple[str, str]] = None,
) -> AgentContext:
    """The AgentContext this request acts as: the asserted partner when the gateway vouched for it with
    a REQUEST-BOUND (v2) assertion and the flag is on, else `context` itself, unchanged.

    From the vouching service: no header, or a v1 token (unbound, e.g. an older gateway revision),
    leaves the gateway the owner (`absent` / `unbound_v1`); a v2 token that fails ANY check (MAC,
    op, window, method, path, resource, body, subject, nonce already used) refuses the request with
    409 PARTNER_ASSERTION_REJECTED, so a captured or edited header can never act, nor quietly fall
    back. From anyone else the header is ignored. Raises the adopted agent's 429 too."""
    if not partner_identity_forwarding_enabled():
        return context
    if getattr(context, "checkout_token_payload", None) is not None:
        return context
    op_label = "|".join(ops)
    caller = getattr(context, "agent_id", None)
    token = _header(request)
    if not token:
        # Counted only for the gateway: every other caller is its own owner and sends no header.
        if str(caller or "") in agent_auth.issuing_voucher_agent_ids():
            _note("absent", op=op_label, request=request, caller=caller)
        return context
    if not await agent_auth._is_service_caller(agent_auth.request_api_key(request)):
        _note("ignored_not_service", op=op_label, request=request, caller=caller)
        return context
    if not is_v2_token(token):
        _note("unbound_v1", op=op_label, request=request, caller=caller)
        return context

    agent_id, matched_op, reason = await verify_bound_forwarded_subject(request, ops=ops, resource=resource)
    if not agent_id:
        raise _reject(reason, op=op_label, request=request, caller=caller)
    agent: Optional[Dict[str, Any]] = None
    try:
        agent = await agent_auth.get_agent(agent_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable partner is refused, never a guess
        logger.warning("partner_identity_forwarding agent lookup failed: %s", type(exc).__name__)
        agent = None
    if not agent:
        raise _reject("subject", op=op_label, request=request, caller=caller)

    await _charge_rate_and_quota(agent)
    try:
        asyncio.create_task(agent_auth.update_agent_stats(agent_id, increment_requests=1))
    except Exception:  # noqa: BLE001 -- stats are best-effort, as in get_agent_context
        pass

    adopted = AgentContext(agent, request)
    adopted.forwarded_by_agent_id = caller
    adopted.forwarded_op = matched_op
    try:
        request.state.agent_id = agent_id
        request.state.forwarded_by_agent_id = caller
    except Exception:  # noqa: BLE001
        pass
    _note("adopted", op=matched_op or op_label, request=request, caller=caller)
    return adopted


def forwarded_owner_context(*ops: str, resource: Optional[Tuple[str, str]] = None):
    """A FastAPI dependency: get_agent_context, then adopt_forwarded_owner for these operations.
    `ops` are the GATEWAY's operation names whose upstream call lands on the route (one route can
    serve two: checkout-sessions is both submit_payment and create_payment_link). `resource` names
    where the route's resource id lives, ("path", "order_id") or ("body", "quote_id"), and the token
    must name the same one."""
    if not ops:
        raise ValueError("forwarded_owner_context needs at least one operation")

    async def _dependency(request: Request, context: AgentContext = Depends(get_agent_context)) -> AgentContext:
        return await adopt_forwarded_owner(request, context, ops=ops, resource=resource)

    return _dependency


def partner_identity_metadata(context: AgentContext) -> Optional[Dict[str, Any]]:
    """The order-metadata stamp for an adopted context, or None when the context was not adopted."""
    via = getattr(context, "forwarded_by_agent_id", None)
    if not via:
        return None
    return {
        "partner_agent_id": context.agent_id,
        "via_agent_id": via,
        "op": getattr(context, "forwarded_op", None),
    }


def _order_metadata(order: Mapping[str, Any]) -> Dict[str, Any]:
    raw = order.get("metadata")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def order_access_allowed(order: Mapping[str, Any], context: AgentContext) -> bool:
    """Does `context` own this order? The owner, as before. With the flag on, ALSO the service that
    vouched for the partner that owns it, when it calls WITHOUT the partner's assertion: that is the
    gateway after its own flag is turned back off, or on a call it could not sign. It held every
    MCP order before this change, so this is no wider than before, and it never reaches an order a
    partner created over its own key (no `via` is recorded on those)."""
    owner = str(order.get("agent_id") or "")
    caller = str(getattr(context, "agent_id", "") or "")
    if owner == caller:
        return True
    if not partner_identity_forwarding_enabled() or not owner or not caller:
        return False
    if caller not in agent_auth.issuing_voucher_agent_ids():
        return False
    stamp = _order_metadata(order).get(PARTNER_IDENTITY_METADATA_KEY)
    # The stamp must describe THIS order's owner: a stamp a partner planted on its own order through a
    # path that does not strip it (v1 create, v2 with the flag off) names nobody's partner but its own.
    allowed = (
        isinstance(stamp, dict)
        and stamp.get("via_agent_id") == caller
        and stamp.get("partner_agent_id") == owner
    )
    if allowed:
        _note("via_access", op="order_access", request=None, caller=caller)
    return allowed

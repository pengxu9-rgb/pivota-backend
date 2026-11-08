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
  * only when the header verifies for THAT route's operation (MAC, `op`, freshness) and names an
    ACTIVE, non-service agent (services.issuing_agent_assertion.resolve_asserted_issuing);
  * the adopted agent then pays its own rate limit and daily quota, exactly as get_agent_context
    charges a caller that presents its own key, and owns what the route writes.

Anything else leaves the context EXACTLY as get_agent_context built it (the gateway), with one
log line and a counter naming why: `absent` (the gateway sent no header), `invalid` (it did not
verify, or named no active agent), `ignored_not_service` (a header from a caller that may not
vouch). Ownership is never taken from a request body field or from an unverified header.

A checkout-token request is never touched: the token names its own agent and order.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from typing import Any, Dict, Mapping, Optional, Sequence

from fastapi import Depends, HTTPException, Request

import routes.agent_auth as agent_auth
from routes.agent_auth import AgentContext, get_agent_context
from services.issuing_agent_assertion import ISSUING_AGENT_ASSERTION_HEADER, resolve_asserted_issuing
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


async def adopt_forwarded_owner(request: Request, context: AgentContext, *, ops: Sequence[str]) -> AgentContext:
    """The AgentContext this request acts as: the asserted partner when the gateway vouched for it
    and the flag is on, else `context` itself, unchanged. Raises only the adopted agent's 429."""
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

    agent_id: Optional[str] = None
    matched_op: Optional[str] = None
    excluded = agent_auth.issuing_excluded_agent_ids()
    for op in ops:
        resolved = await resolve_asserted_issuing(token, op=op, excluded_agent_ids=excluded)
        if resolved.agent_id:
            agent_id, matched_op = resolved.agent_id, op
            break
    agent: Optional[Dict[str, Any]] = None
    if agent_id:
        try:
            agent = await agent_auth.get_agent(agent_id)
        except Exception as exc:  # noqa: BLE001 -- an unreadable partner is "not adopted", never a guess
            logger.warning("partner_identity_forwarding agent lookup failed: %s", type(exc).__name__)
            agent = None
    # resolve_asserted_issuing has already required an ACTIVE, non-service agent; this read is for
    # the row itself (scope, limits), and a row gone since is "not adopted".
    if not agent:
        _note("invalid", op=op_label, request=request, caller=caller)
        return context

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


def forwarded_owner_context(*ops: str):
    """A FastAPI dependency: get_agent_context, then adopt_forwarded_owner for these operations.
    `ops` are the GATEWAY's operation names whose upstream call lands on the route (one route can
    serve two: checkout-sessions is both submit_payment and create_payment_link)."""
    if not ops:
        raise ValueError("forwarded_owner_context needs at least one operation")

    async def _dependency(request: Request, context: AgentContext = Depends(get_agent_context)) -> AgentContext:
        return await adopt_forwarded_owner(request, context, ops=ops)

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
    return isinstance(stamp, dict) and stamp.get("via_agent_id") == caller

"""`/agent/v2/commerce/purchases`: the rail-neutral purchase read (payment orchestration P0).

One id (`pp_…`) and one state vocabulary for a purchase on any payment rail, with the rail's own
owner-facing body nested under `detail` exactly as the rail's route returns it. Reap is the only
rail today; a Reap id (`rp_…`) is accepted too, so a caller holding the id from
`POST /agent/v2/commerce/reap/purchases` can read the unified view without a lookup.

READ-ONLY, AND DARK BY DEFAULT. Nothing here creates, advances or pays for a purchase, and
nothing calls a rail. While `AGENT_PURCHASE_LEDGER_ENABLED` is off every request answers the
same 404, whatever its shape.

OWNERSHIP IS THE RAIL'S AND THE LEDGER'S, IN SQL. The parent read conjuncts on the agent and the
end user (db/agent_purchase_ledger.py), and the nested detail is read through the Reap route's own
owner-scoped `_owner_view`. "Not yours" and "does not exist" are the same 404.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Mapping, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import db.agent_purchase_ledger as purchases
import db.reap_agentic_ledger as reap_ledger
import routes.agent_commerce_reap as reap_routes
import services.reap_agentic_purchase as reap_svc
from db.buyer_vault import hash_agent_user_ref
from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from services.payment_orchestration import purchase_view
from services.payment_orchestration.rails import RAIL_ID_PREFIX

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/v2/commerce/purchases", tags=["agent-commerce-purchases"])

#: The exact id shapes this route reads: our `pp_` id and each rail's own id (all `<prefix><24 hex>`, see
#: db/agent_purchase_ledger.new_purchase_id and db/reap_agentic_ledger.new_purchase_id). Anything else is
#: the same 404 as an unknown id, decided BEFORE a bind: on Postgres a NUL byte in a bound string raises
#: out of asyncpg as a 500.
_PURCHASE_ID_RE = re.compile(r"(?:pp_|" + "|".join(re.escape(p) for p in RAIL_ID_PREFIX) + r")[0-9a-f]{24}")


class _LedgerUnavailable(Exception):
    """The agent_purchases table could not be read (missing, or the database refused)."""


def _ledger_unavailable() -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "ledger_unavailable"})


def _dark() -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_available"})


def _owner_hash(agent_user: Optional[AgentUserContext]) -> str:
    agent_user_ref = reap_routes._require_agent_user(agent_user)
    agent_user_ref_hash = hash_agent_user_ref(agent_user_ref)
    if not agent_user_ref_hash:
        raise reap_svc.PurchaseRefused("agent_user_required")
    return agent_user_ref_hash


async def _parent_for(purchase_id: str, agent_id: str, owner: str) -> Optional[Dict[str, Any]]:
    """The owner's parent row for a `pp_` or rail id, healing a rail purchase that has none yet.

    The heal runs only after the rail ledger's own owner-scoped read has said the purchase is this
    caller's, so it can never create a parent for somebody else's purchase.
    """
    try:
        if purchase_id.startswith("pp_"):
            return await purchases.get_for_owner(purchase_id, agent_id, owner)
        rail = next((r for prefix, r in RAIL_ID_PREFIX.items() if purchase_id.startswith(prefix)), None)
        if rail != "reap":
            return None
        parent = await purchases.get_by_rail_id_for_owner(rail, purchase_id, agent_id, owner)
        if parent is not None:
            return parent
        if await reap_ledger.get_purchase_for_owner(purchase_id, agent_id, owner) is None:
            return None
        await purchases.ensure_reap_parent(purchase_id, basis="backfill")
        return await purchases.get_by_rail_id_for_owner(rail, purchase_id, agent_id, owner)
    except Exception as exc:  # noqa: BLE001 - any ledger fault is "say nothing, retry", never a 500
        logger.warning("agent_purchases: ledger read failed error_type=%s", type(exc).__name__)
        raise _LedgerUnavailable() from None


async def _unified_for(parent: Mapping[str, Any], agent_id: str, owner: str):
    """(body, None) on success, (None, response) when the purchase cannot be answered."""
    body, problem = await purchase_view.unified_purchase(
        parent, agent_id=agent_id, agent_user_ref_hash=owner
    )
    if problem == "not_found":
        return None, reap_routes._not_found()
    if problem == "state_unmapped":
        logger.warning(
            "agent_purchases: unmapped rail state rail=%s purchase=%s", parent["rail"], parent["id"]
        )
        return None, JSONResponse(status_code=503, content={"error": "state_unmapped"})
    return body, None


@router.get("/{purchase_id}")
async def get_purchase(
    purchase_id: str,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    if not purchases.is_enabled():
        return _dark()
    try:
        owner = _owner_hash(agent_user)
    except reap_svc.PurchaseRefused as exc:
        return reap_routes._refused(exc)

    purchase_id = str(purchase_id or "").strip()
    if not _PURCHASE_ID_RE.fullmatch(purchase_id):
        return reap_routes._not_found()
    agent_id = str(context.agent_id)
    try:
        parent = await _parent_for(purchase_id, agent_id, owner)
    except _LedgerUnavailable:
        return _ledger_unavailable()
    if parent is None:
        return reap_routes._not_found()
    body, error = await _unified_for(parent, agent_id, owner)
    return error if error is not None else body


@router.get("")
async def list_purchases(
    request: Request,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    if not purchases.is_enabled():
        return _dark()
    try:
        owner = _owner_hash(agent_user)
        limit = reap_routes._limit_from(request)
    except reap_svc.PurchaseRefused as exc:
        return reap_routes._refused(exc)

    agent_id = str(context.agent_id)
    # This owner's Reap purchases that predate the ledger (or whose post-commit write failed)
    # get their parent now, so the first page is complete. A failure here only means the page
    # may miss rows the backfill will add; it never fails the read.
    try:
        await purchases.heal_reap_parents_for_owner(agent_id, owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_purchases: owner heal failed error_type=%s", type(exc).__name__)

    try:
        parents = await purchases.list_for_owner(agent_id, owner, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_purchases: ledger list failed error_type=%s", type(exc).__name__)
        return _ledger_unavailable()

    # ONE ANSWER FOR THE WHOLE PAGE. A row whose rail state has no unified word fails the page with the
    # same 503 the single read gives, rather than vanishing from the owner's history (a caller
    # reconciling history would read a missing row as a purchase that does not exist).
    out = []
    for parent in parents:
        body, error = await _unified_for(parent, agent_id, owner)
        if body is not None:
            out.append(body)
        elif error is not None and error.status_code == 503:
            return error
        else:
            # The rail row is gone or no longer this owner's, which the copied identity makes impossible;
            # say so in the log and leave it out rather than fail the page.
            logger.warning("agent_purchases: parent without readable rail row purchase=%s", parent["id"])
    return {"purchases": out, "limit": limit}

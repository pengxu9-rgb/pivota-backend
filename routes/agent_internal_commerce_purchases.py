"""`GET /agent/internal/commerce/purchases/{purchase_id}`: the server-to-server purchase read (AgenPas).

The buyer opens a signed AgenPas link on the gateway. They hold no agent key and no JWT, and the gateway
never stores a partner's credentials, so the gateway reads the purchase here, server to server.

  AUTH   `X-Internal-Key` must equal AGENPAS_INTERNAL_READ_KEY, a key DEDICATED to this read: a key that
         reads any agent's purchase must not share scope with the introspect key
         (AGENT_AUTH_INTROSPECT_INTERNAL_KEY), which this route never accepts. Compared in constant time,
         as bytes. Missing on the server, or shorter than INTERNAL_READ_KEY_MIN_LENGTH (32) characters:
         503, never open. Missing or wrong in the request: 401.
  SCOPE  `X-Agenpas-Agent-Id`, the agent id the gateway verified on the signed link. The read is scoped
         by (agent id, purchase id) IN SQL (db/agent_purchase_ledger.get_for_agent): another agent's
         purchase and no purchase are the same 404. NOT by the end user: the gateway's `usr_…` ref is not
         the backend's agent-user hash, so there is nothing to scope by. `X-Agenpas-Agent-User-Ref`, if
         sent, is ignored on purpose.
  BODY   the SAME unified body `GET /agent/v2/commerce/purchases/{id}` returns, built by the same
         builders (services/payment_orchestration/purchase_view, through the public route's
         `_unified_for`), from the parent's own owner identity: no PII, no rail ids, only vetted URLs.
  DIAL   AGENPAS_INTERNAL_READ_ENABLED, default off. Off: 404, the body of an unknown route, before the
         key is even looked at (the route does not exist until it is armed).

Read-only: it writes nothing, heals no parent (a rail purchase whose parent row is still missing is a
404 here until the events job or a public read heals it) and calls no rail. No rate limit yet (the key's
holders are exempt from the rate limiter): a follow-up.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse

import db.agent_purchase_ledger as purchases
from routes import agent_commerce_purchases as public
from services.payment_orchestration.rails import RAIL_ID_PREFIX

logger = logging.getLogger(__name__)

AGENPAS_INTERNAL_READ_ENABLED_ENV = "AGENPAS_INTERNAL_READ_ENABLED"
#: This route's own key. Not the introspect key: that one is held for other /agent/internal routers.
INTERNAL_KEY_ENV = "AGENPAS_INTERNAL_READ_KEY"
#: A configured key shorter than this counts as missing (503): a short key is a guessable key.
INTERNAL_READ_KEY_MIN_LENGTH = 32
_TRUTHY = frozenset({"1", "true", "on", "yes"})
#: An agent id as the gateway passes it: an opaque id, checked before any bind (a NUL on Postgres raises).
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")


def is_enabled() -> bool:
    return (os.getenv(AGENPAS_INTERNAL_READ_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def _read_enabled() -> None:
    # Off: exactly what FastAPI answers for a path it does not have.
    if not is_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _require_internal_key(x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key")) -> None:
    expected = str(os.getenv(INTERNAL_KEY_ENV) or "").strip()
    if len(expected) < INTERNAL_READ_KEY_MIN_LENGTH:
        # Never open: a server without the key (or with one too short to count) refuses everyone.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"error": "internal_key_not_configured"})
    provided = str(x_internal_key or "").strip()
    # Compare as BYTES: Starlette decodes headers latin-1, and hmac.compare_digest on str RAISES on
    # non-ASCII (routes/agent_internal_decision_slates.py has the history).
    if not provided or not hmac.compare_digest(provided.encode("utf-8", "surrogateescape"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "unauthorized"})


# The guards are on the ROUTER, not the handler, so a route added here later inherits them
# (tests/test_unauthenticated_admin_ops_routes.py has the history). The dial runs first.
router = APIRouter(
    prefix="/agent/internal/commerce/purchases",
    tags=["agent-internal-commerce-purchases"],
    dependencies=[Depends(_read_enabled), Depends(_require_internal_key)],
)


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "purchase_not_found"})


def _unavailable() -> JSONResponse:
    """A ledger or view fault: "try again", never a 500 (and never which part failed)."""
    return JSONResponse(status_code=503, content={"error": "temporarily_unavailable"})


async def _parent_for_agent(purchase_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
    if purchase_id.startswith("pp_"):
        return await purchases.get_for_agent(purchase_id, agent_id)
    rail = next((r for prefix, r in RAIL_ID_PREFIX.items() if purchase_id.startswith(prefix)), None)
    if rail is None:
        return None
    return await purchases.get_by_rail_id_for_agent(rail, purchase_id, agent_id)


@router.get("/{purchase_id}")
async def get_purchase_for_gateway(
    purchase_id: str,
    x_agenpas_agent_id: Optional[str] = Header(None, alias="X-Agenpas-Agent-Id"),
    # Read so the contract is visible, and IGNORED for scoping: the gateway's user ref is not the
    # backend's agent-user hash (see the module docstring). Never logged.
    x_agenpas_agent_user_ref: Optional[str] = Header(None, alias="X-Agenpas-Agent-User-Ref"),
):
    del x_agenpas_agent_user_ref
    agent_id = str(x_agenpas_agent_id or "").strip()
    if not _AGENT_ID_RE.match(agent_id):
        return JSONResponse(status_code=400, content={"error": "agent_id_required"})
    purchase_id = str(purchase_id or "").strip()
    if not purchases.is_enabled() or not public._PURCHASE_ID_RE.fullmatch(purchase_id):
        return _not_found()
    try:
        parent = await _parent_for_agent(purchase_id, agent_id)
    except Exception as exc:  # noqa: BLE001 - any ledger fault is "say nothing, retry", never a 500
        logger.warning("agent_internal_purchases: ledger read failed error_type=%s", type(exc).__name__)
        return _unavailable()
    owner = str((parent or {}).get("agent_user_ref_hash") or "")
    if parent is None or not owner:
        return _not_found()
    # The owner hash goes only to the owner-scoped view builders, never into the body.
    parent = {k: v for k, v in parent.items() if k != "agent_user_ref_hash"}
    try:
        body, error = await public._unified_for(parent, agent_id, owner)
    except Exception as exc:  # noqa: BLE001 - a rail read or view fault is "try again", never a 500
        logger.warning("agent_internal_purchases: view failed error_type=%s", type(exc).__name__)
        return _unavailable()
    return error if error is not None else body

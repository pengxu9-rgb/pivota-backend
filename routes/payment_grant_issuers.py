"""Payment-grant issuers — which PSPs may authorize money. The Antom lane's registry.

Admin (Pivota trust decision — NOT agent self-service, unlike identity issuers; a payment
grant moves money through complete_checkout's create_order+submit_payment):
  GET    /admin/payment-issuers          list all rows, disabled included (audit view)
  PUT    /admin/payment-issuers          register / replace one (validates shape, then
                                         dereferences the pinned JWKS before storing)
  DELETE /admin/payment-issuers/{id}     disable (never delete — a formerly-trusted payment
                                         issuer is audit evidence)

Gateway (server-to-server, X-Internal-Key = AGENT_AUTH_INTROSPECT_INTERNAL_KEY, the same key
and the same comparison the identity registry endpoint uses — no new secret):
  GET    /agent/internal/payment-issuers every ACTIVE row in the gateway verifier's
                                         issuer-entry shape ({iss, jwksUri, aud, algs, azp,
                                         methods, expectedVct})

What this buys: onboarding Antom becomes a registered row the gateway pulls on its TTL,
instead of editing PAYMENT_ISSUERS_JSON and redeploying the gateway per partner.
What it must never do: let an agent (or anyone below admin/employee) grant a PSP the power to
authorize charges.
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from db import payment_grant_issuers as store
from db.payment_grant_issuers import IssuerValidationError
from utils.auth import get_current_user
from utils.logger import logger

router = APIRouter(prefix="/admin", tags=["payment-grant-issuers"])
internal_router = APIRouter(prefix="/agent/internal", tags=["agent-internal-auth"])


class PaymentIssuerBody(BaseModel):
    # extra='forbid': a typo'd field name in a TRUST registration (say `audiences`) must be a
    # 4xx, not a silently-dropped constraint.
    model_config = ConfigDict(extra="forbid")

    issuer: str
    jwks_uri: str
    audience: str
    algs: Optional[List[str]] = None
    authorized_party: Optional[str] = None
    methods: Optional[List[str]] = None
    expected_vct: Optional[str] = None


def _require_admin(current_user: Dict[str, Any]) -> str:
    """Admin or employee only, and returns WHO for the registered_by audit column."""
    if current_user.get("role") not in ("admin", "employee"):
        raise HTTPException(status_code=403, detail="payment issuer registration is admin-only")
    return str(current_user.get("email") or current_user.get("user_id") or "admin")


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else (value if isinstance(value, str) else None)


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for k in ("last_jwks_ok_at", "created_at", "updated_at"):
        out[k] = _iso(out.get(k))
    return out


@router.get("/payment-issuers")
async def list_payment_issuers(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    _require_admin(current_user)
    rows = await store.list_all()
    return {"status": "success", "issuers": [_public(r) for r in rows]}


@router.put("/payment-issuers")
async def register_payment_issuer(
    body: PaymentIssuerBody,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    registered_by = _require_admin(current_user)
    try:
        reg = store.normalize_registration(body.model_dump())
        # Dereference the pinned JWKS BEFORE storing: a trust row whose keys cannot be fetched
        # is not "trust pending", it is a registration typo about to become a partner outage.
        await store.dereference_jwks(reg.jwks_uri)
    except IssuerValidationError as err:
        raise HTTPException(status_code=422, detail={"field": err.field, "message": str(err)})
    row = await store.upsert_issuer(reg, registered_by=registered_by, jwks_ok=True)
    logger.info(
        f"payment issuer registered issuer={reg.issuer} methods={reg.methods} by={registered_by}"
    )
    return {"status": "success", "issuer": _public(row)}


@router.delete("/payment-issuers/{issuer_id}")
async def disable_payment_issuer(
    issuer_id: int,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    registered_by = _require_admin(current_user)
    changed = await store.disable_issuer(issuer_id)
    if not changed:
        raise HTTPException(status_code=404, detail="no active payment issuer with that id")
    logger.info(f"payment issuer disabled id={issuer_id} by={registered_by}")
    return {"status": "success", "disabled": issuer_id}


# ── internal registry (gateway) ───────────────────────────────────────────────


def _expected_internal_key() -> str:
    return str(os.getenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY") or "").strip()


def _require_internal_key(x_internal_key: Optional[str]) -> None:
    expected = _expected_internal_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "CONFIG_MISSING", "message": "AGENT_AUTH_INTROSPECT_INTERNAL_KEY is not configured"},
        )
    provided = str(x_internal_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Missing or invalid X-Internal-Key"},
        )


@internal_router.get("/payment-issuers")
async def internal_payment_issuer_registry(
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> Dict[str, Any]:
    """Every ACTIVE issuer, in the gateway payment-verifier's issuer-entry shape."""
    _require_internal_key(x_internal_key)
    rows = await store.list_active()
    return {
        "status": "success",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "issuers": [
            {
                "iss": r["issuer"],
                "jwksUri": r["jwks_uri"],
                "aud": r["audience"],
                "algs": r["algs"],
                "azp": r.get("authorized_party"),
                "methods": r["methods"],
                "expectedVct": r.get("expected_vct"),
                "updated_at": _iso(r.get("updated_at")),
            }
            for r in rows
        ],
    }

"""
Agent Recommendations Proxy (A-path):

- External agents authenticate only with pivota-backend (X-API-Key / ak_live_*).
- pivota-backend forwards role selection + feed assembly requests to an internal
  Recommendations service (e.g. pivota-agent-backend), protected by X-Internal-Key.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from routes.agent_auth import AgentContext, get_agent_context
from utils.logger import logger


router = APIRouter(prefix="/agent/v1", tags=["agent-recommendations"])


DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"


def _service_base_url() -> str:
    base = (
        os.getenv("RECOMMENDATIONS_SERVICE_BASE_URL")
        or os.getenv("PIVOTA_AGENT_BACKEND_BASE_URL")
        or "http://localhost:3000"
    )
    return str(base).rstrip("/")


def _internal_key() -> str:
    return str(os.getenv("RECOMMENDATIONS_INTERNAL_KEY") or "").strip()


def _timeout_seconds() -> float:
    try:
        ms = int(os.getenv("RECOMMENDATIONS_SERVICE_TIMEOUT_MS") or "5000")
    except Exception:
        ms = 5000
    return max(0.5, min(60.0, ms / 1000.0))


async def _post_service_json(
    path: str,
    body: Dict[str, Any],
    *,
    request_id: str,
    agent_id: str,
    timeout_s: float,
) -> httpx.Response:
    base = _service_base_url()
    url = f"{base}{path}"

    key = _internal_key()
    if not key and not DEBUG_MODE:
        raise HTTPException(status_code=500, detail="Missing RECOMMENDATIONS_INTERNAL_KEY")

    headers = {
        "Content-Type": "application/json",
        "X-Request-Id": request_id,
        "X-Agent-Id": agent_id,
        **({"X-Internal-Key": key} if key else {}),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            return await client.post(url, json=body, headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "RECOMMENDATIONS_SERVICE_UNAVAILABLE", "message": str(exc)},
        )


class NormalizeRolesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    role_hints: List[str] = Field(..., alias="roleHints", min_length=1)
    max_suggestions: Optional[int] = Field(default=None, alias="maxSuggestions", ge=0, le=10)
    market: Optional[Literal["US", "JP"]] = None


class DiversityConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    domain_cap_per_role: Optional[int] = Field(default=None, alias="domainCapPerRole", ge=1, le=50)
    domain_cap_global: Optional[int] = Field(default=None, alias="domainCapGlobal", ge=1, le=200)
    dedupe: Optional[Literal["global", "perRole"]] = None


class DebugConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    include_mapping: Optional[bool] = Field(default=None, alias="includeMapping")
    include_filter_reasons: Optional[bool] = Field(default=None, alias="includeFilterReasons")


class FeedRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    market: Literal["US", "JP"]
    locale: Optional[str] = None
    request_id: Optional[str] = Field(default=None, alias="requestId")
    role_ids: Optional[List[str]] = Field(default=None, alias="roleIds")
    role_hints: Optional[List[str]] = Field(default=None, alias="roleHints")
    max_offers_per_role: Optional[int] = Field(default=None, alias="maxOffersPerRole", ge=1, le=10)
    max_total_offers: Optional[int] = Field(default=None, alias="maxTotalOffers", ge=1, le=100)
    diversity: Optional[DiversityConfig] = None
    context: Optional[Dict[str, Any]] = None
    debug: Optional[DebugConfig] = None
    resolve: Optional[Literal["none", "inline", "deferred"]] = None

    @model_validator(mode="after")
    def _ensure_roles_present(self) -> "FeedRequest":
        if not (self.role_ids and len(self.role_ids)) and not (self.role_hints and len(self.role_hints)):
            raise ValueError("Provide roleIds and/or roleHints")
        return self


@router.post("/recommendations/roles/normalize")
async def normalize_role_hints(
    payload: NormalizeRolesRequest,
    context: AgentContext = Depends(get_agent_context),
):
    request_id = str(uuid.uuid4())
    body: Dict[str, Any] = {
        "roleHints": payload.role_hints,
        **({"maxSuggestions": payload.max_suggestions} if payload.max_suggestions is not None else {}),
        **({"market": payload.market} if payload.market else {}),
    }

    resp = await _post_service_json(
        "/v1/recommendations/roles/normalize",
        body,
        request_id=request_id,
        agent_id=str(getattr(context, "agent_id", "")),
        timeout_s=_timeout_seconds(),
    )

    if resp.status_code < 200 or resp.status_code >= 300:
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        raise HTTPException(status_code=resp.status_code, detail=data)

    try:
        return resp.json()
    except Exception as exc:
        logger.warning(f"[agent_recommendations] normalize invalid json from service: {exc}")
        raise HTTPException(status_code=502, detail={"error": "RECOMMENDATIONS_INVALID_RESPONSE"})


@router.post("/recommendations/feed")
async def get_recommendations_feed(
    request: Request,
    payload: FeedRequest,
    context: AgentContext = Depends(get_agent_context),
):
    request_id = payload.request_id or request.headers.get("X-Request-Id") or str(uuid.uuid4())
    agent_id = str(getattr(context, "agent_id", ""))

    body: Dict[str, Any] = {
        "requestId": request_id,
        "market": payload.market,
        **({"locale": payload.locale} if payload.locale else {}),
        **({"roleIds": payload.role_ids} if payload.role_ids else {}),
        **({"roleHints": payload.role_hints} if payload.role_hints else {}),
        **({"maxOffersPerRole": payload.max_offers_per_role} if payload.max_offers_per_role is not None else {}),
        **({"maxTotalOffers": payload.max_total_offers} if payload.max_total_offers is not None else {}),
        **({"diversity": payload.diversity.model_dump(by_alias=True, exclude_none=True)} if payload.diversity else {}),
        **({"debug": payload.debug.model_dump(by_alias=True, exclude_none=True)} if payload.debug else {}),
        **({"resolve": payload.resolve} if payload.resolve else {}),
        "context": {
            **(payload.context or {}),
            "agentId": agent_id,
            "surface": (payload.context or {}).get("surface") or "agent_api",
        },
    }

    resp = await _post_service_json(
        "/v1/recommendations/feed",
        body,
        request_id=request_id,
        agent_id=agent_id,
        timeout_s=_timeout_seconds(),
    )

    if resp.status_code < 200 or resp.status_code >= 300:
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        raise HTTPException(status_code=resp.status_code, detail=data)

    try:
        return resp.json()
    except Exception as exc:
        logger.warning(f"[agent_recommendations] feed invalid json from service: {exc}")
        raise HTTPException(status_code=502, detail={"error": "RECOMMENDATIONS_INVALID_RESPONSE"})


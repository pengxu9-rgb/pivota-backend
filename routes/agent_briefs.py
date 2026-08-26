from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from config.platform import pytest_bypass_allowed
from db.briefs import insert_brief
from models.brief import (
    BriefBuildRequest,
    BriefBuildResponse,
    BriefClarifyRequest,
    BriefClarifyResponse,
    BriefCompatibilityRequest,
    BriefCompatibilityResponse,
    BriefQuestion,
    CompatibilityResult,
    StandardProductRef,
)
from mvp.constants import (
    EVENT_BRIEF_BUILT,
    EVENT_BRIEF_CLARIFICATION_ASKED,
    EVENT_BRIEF_COMPAT_CHECKED,
    SURFACE_BACKEND,
)
from mvp.events import emit_best_effort
from mvp.idempotency import InMemoryIdempotencyStore, PostgresIdempotencyStore
from routes.agent_auth import AgentContext, get_agent_context
from services.briefs_service import build_brief_v0, build_clarify, compatibility_check


router = APIRouter(prefix="/agent/v1/briefs", tags=["agent-briefs"])

_INMEM_IDEM = InMemoryIdempotencyStore()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/clarify", response_model=BriefClarifyResponse)
async def clarify_brief(
    req: BriefClarifyRequest,
    request: Request,
    context: AgentContext = Depends(get_agent_context),
):
    missing_fields, questions_raw = build_clarify(raw_query=req.raw_query, currency=req.currency)
    questions = [BriefQuestion(**q) for q in (questions_raw or [])]

    # Best-effort instrumentation.
    try:
        emit_best_effort(
            event_type=EVENT_BRIEF_CLARIFICATION_ASKED,
            payload={
                "agent_id": getattr(context, "agent_id", None),
                "suggested_vertical": "beauty",
                "missing_fields": missing_fields,
                "questions_count": len(questions),
                "request_id": getattr(request.state, "request_id", None),
            },
            merchant_id=None,
            geo=None,
            surface=SURFACE_BACKEND,
            adapter="briefs_clarify",
            risk_tier="unknown",
            idempotency_key=getattr(request.state, "request_id", None),
        )
    except Exception:
        pass

    return BriefClarifyResponse(
        suggested_vertical="beauty",
        missing_fields=missing_fields,
        questions=questions[:2],
    )


@router.post("/build", response_model=BriefBuildResponse)
async def build_brief(
    req: BriefBuildRequest,
    request: Request,
    context: AgentContext = Depends(get_agent_context),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Build and persist Brief v0.1.0.

    Deterministic: rule-based parsing + explicit unknowns + evidence.
    """
    idem_key = (idempotency_key or "").strip() or None

    # Idempotency replay (best-effort)
    if idem_key:
        try:
            idem = PostgresIdempotencyStore()
            existing = await idem.get(scope="brief_build", key=idem_key)
            if existing and isinstance(existing.value, dict):
                return existing.value
        except Exception:
            existing = await _INMEM_IDEM.get(scope="brief_build", key=idem_key)
            if existing and isinstance(existing.value, dict):
                return existing.value

    telemetry = dict(req.telemetry or {})
    if getattr(request.state, "request_id", None) and not telemetry.get("request_id"):
        telemetry["request_id"] = getattr(request.state, "request_id", None)

    brief, confidence = build_brief_v0(
        agent_id=context.agent_id,
        raw_query=req.raw_query,
        market=req.market,
        locale=req.locale,
        currency=req.currency,
        telemetry=telemetry,
    )

    # Persist (required for durable join key).
    try:
        await insert_brief(
            {
                "brief_id": brief.brief_id,
                "schema_version": brief.schema_version,
                "agent_id": context.agent_id,
                "vertical": brief.vertical,
                "market": brief.market.market,
                "locale": brief.market.locale,
                "currency": brief.market.currency,
                "raw_intent": brief.raw_intent.text,
                "brief_json": brief.model_dump(mode="json"),
                "status": "active",
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
            }
        )
    except Exception as e:
        # In production, brief persistence is required (durable join key).
        # In unit tests, allow degraded mode so the suite can run without a DB.
        # Fails closed in production the same way the demo admin login lanes
        # do (#1889) — see config.platform.pytest_bypass_allowed.
        if pytest_bypass_allowed(bypass_name="the brief-persist degraded mode"):
            pass
        else:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "BRIEF_PERSIST_FAILED",
                    "message": "Failed to persist brief",
                    "details": {"error": str(e)},
                },
            )

    response: dict = BriefBuildResponse(confidence=confidence, brief=brief).model_dump(mode="json")

    # Persist idempotency after successful write (best-effort).
    if idem_key:
        try:
            idem = PostgresIdempotencyStore()
            await idem.put(scope="brief_build", key=idem_key, value=response)
        except Exception:
            await _INMEM_IDEM.put(scope="brief_build", key=idem_key, value=response)

    # Best-effort instrumentation.
    try:
        emit_best_effort(
            event_type=EVENT_BRIEF_BUILT,
            payload={
                "brief_id": brief.brief_id,
                "brief_schema_version": brief.schema_version,
                "agent_id": context.agent_id,
                "vertical": brief.vertical,
                "market": brief.market.market,
                "currency": brief.market.currency,
                "confidence": confidence,
                "risk_tags": brief.risk_tags,
                "request_id": getattr(request.state, "request_id", None),
            },
            merchant_id=None,
            geo=None,
            surface=SURFACE_BACKEND,
            adapter="briefs_build",
            risk_tier="unknown",
            idempotency_key=idem_key or brief.brief_id,
        )
    except Exception:
        pass

    return response


@router.post("/compatibility/check", response_model=BriefCompatibilityResponse)
async def check_compatibility(
    req: BriefCompatibilityRequest,
    request: Request,
    context: AgentContext = Depends(get_agent_context),
):
    if not req.candidate_items:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "candidate_items[] is required"})

    results = []
    veto_count = 0
    total_score = 0.0
    for item in req.candidate_items:
        fit, reasons, required_changes, risk_tags, applied_rules = compatibility_check(
            brief=req.brief,
            candidate=item.model_dump(mode="json"),
        )
        if fit <= 0.0:
            veto_count += 1
        total_score += float(fit)
        results.append(
            CompatibilityResult(
                candidate=StandardProductRef(
                    merchant_id=item.merchant_id,
                    platform=item.platform,
                    product_id=item.product_id,
                    variant_id=item.variant_id,
                ),
                fit_score=float(fit),
                reasons=reasons,
                required_changes=required_changes,
                risk_tags=risk_tags,
                evidence={"applied_rules": [r.model_dump(mode="json") for r in applied_rules]},
            )
        )

    avg_fit = total_score / max(1, len(results))

    # Best-effort instrumentation.
    try:
        emit_best_effort(
            event_type=EVENT_BRIEF_COMPAT_CHECKED,
            payload={
                "brief_id": req.brief.brief_id,
                "brief_schema_version": req.brief.schema_version,
                "agent_id": context.agent_id,
                "candidate_count": len(results),
                "veto_count": veto_count,
                "avg_fit_score": avg_fit,
                "request_id": getattr(request.state, "request_id", None),
            },
            merchant_id=None,
            geo=None,
            surface=SURFACE_BACKEND,
            adapter="briefs_compat_check",
            risk_tier="unknown",
            idempotency_key=f"{req.brief.brief_id}:{len(results)}",
        )
    except Exception:
        pass

    return BriefCompatibilityResponse(results=results)

import csv
import io
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, asc, desc, select, update

from db.database import database
from db.outbound_links import outbound_link_rules, outbound_click_events, outbound_link_allowed_domains
from utils.auth import get_current_employee
from services.outbound_links_service import (
    DEFAULT_DISCLOSURE_TEXT,
    make_rule_id,
    normalize_market,
    normalize_scope,
    normalize_scope_id,
    normalize_tool,
    parse_and_verify_redirect_token,
    resolve_outbound_link,
    log_outbound_click,
)


api_router = APIRouter(prefix="/api/links", tags=["outbound-links"])
admin_router = APIRouter(prefix="/agent/internal/links", tags=["outbound-links-admin"])
employee_router = APIRouter(prefix="/employee/links", tags=["outbound-links-employee"])
public_router = APIRouter(tags=["outbound-links"])


async def require_links_admin(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


class ResolveCandidates(BaseModel):
    skuId: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    roleId: Optional[str] = None


class ResolveRequest(BaseModel):
    market: str = Field(default="US")
    tool: str = Field(default="*")
    candidates: ResolveCandidates = Field(default_factory=ResolveCandidates)
    context: Dict[str, Any] = Field(default_factory=dict)


class ResolvedPayload(BaseModel):
    destinationUrl: str
    redirectUrl: str
    purchaseEnabled: bool
    purchaseEnabledOverride: Optional[bool] = None
    ruleId: Optional[str] = None
    partnerType: Optional[str] = None
    disclosureText: str = DEFAULT_DISCLOSURE_TEXT


class ResolveResponse(BaseModel):
    matched: bool
    resolved: Optional[ResolvedPayload] = None
    reason: Optional[str] = None


@api_router.post("/resolve", response_model=ResolveResponse)
async def resolve_endpoint(req: Request, body: ResolveRequest) -> ResolveResponse:
    """
    Resolve an outbound link for a candidate (sku/brand/category/role) for a given tool+market.

    Returns { matched: false } instead of 4xx so callers can fall back to other sources.
    """
    try:
        resolved = await resolve_outbound_link(
            {
                "market": body.market,
                "tool": body.tool,
                "candidates": body.candidates.model_dump(),
                "context": body.context,
            },
            request_base_url=str(req.base_url),
        )
        return ResolveResponse(
            matched=True,
            resolved=ResolvedPayload(
                destinationUrl=resolved.destination_url,
                redirectUrl=resolved.redirect_url,
                purchaseEnabled=resolved.purchase_enabled,
                purchaseEnabledOverride=resolved.purchase_enabled_override,
                ruleId=resolved.rule_id,
                partnerType=resolved.partner_type,
                disclosureText=resolved.disclosure_text,
            ),
        )
    except Exception as exc:
        code = str(getattr(exc, "args", ["NO_MATCH"])[0] or "NO_MATCH")
        return ResolveResponse(matched=False, reason=code)


@public_router.get("/r")
async def redirect_endpoint(req: Request, token: str = Query(..., min_length=10)) -> RedirectResponse:
    """
    Signed redirect endpoint for outbound links.

    - Prevents open redirect via HMAC token
    - Records best-effort click telemetry
    - 302 to the destination URL (already includes UTM if configured)
    """
    payload = parse_and_verify_redirect_token(token)
    dest = str(payload.get("dest") or "")
    if not dest.startswith("http://") and not dest.startswith("https://"):
        raise HTTPException(status_code=400, detail="INVALID_DEST")

    # Best-effort click logging.
    try:
        await log_outbound_click(
            token_payload=payload,
            request_meta={
                "user_agent": req.headers.get("user-agent"),
                "ip": getattr(getattr(req, "client", None), "host", None),
            },
        )
    except Exception:
        pass

    return RedirectResponse(url=dest, status_code=302)


class LinkRuleIn(BaseModel):
    market: str = "US"
    tool: str = "*"
    scope: str
    scopeId: str
    destinationUrl: str
    purchaseEnabledOverride: Optional[bool] = None
    priority: int = 0
    partnerType: str = "unknown"
    disclosureText: Optional[str] = None
    utmTemplate: Optional[str] = None
    startAt: Optional[datetime] = None
    endAt: Optional[datetime] = None
    tags: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    createdBy: Optional[str] = None


class LinkRuleOut(BaseModel):
    id: str
    market: str
    tool: str
    scope: str
    scopeId: str
    destinationUrl: str
    purchaseEnabledOverride: Optional[bool] = None
    priority: int
    partnerType: str
    disclosureText: Optional[str] = None
    utmTemplate: Optional[str] = None
    startAt: Optional[datetime] = None
    endAt: Optional[datetime] = None
    status: str
    publishedAt: Optional[datetime] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


def _to_out(row: Any) -> LinkRuleOut:
    d = dict(row)
    return LinkRuleOut(
        id=d["id"],
        market=d["market"],
        tool=d["tool"],
        scope=d["scope"],
        scopeId=d["scope_id"],
        destinationUrl=d["destination_url"],
        purchaseEnabledOverride=d.get("purchase_enabled_override"),
        priority=int(d.get("priority") or 0),
        partnerType=d.get("partner_type") or "unknown",
        disclosureText=d.get("disclosure_text"),
        utmTemplate=d.get("utm_template"),
        startAt=d.get("start_at"),
        endAt=d.get("end_at"),
        status=d.get("status") or "draft",
        publishedAt=d.get("published_at"),
        createdAt=d.get("created_at"),
        updatedAt=d.get("updated_at"),
    )


@admin_router.get("/rules", response_model=Dict[str, Any])
async def list_rules(
    market: Optional[str] = Query(None),
    tool: Optional[str] = Query(None),
    status_: Optional[str] = Query(None, alias="status"),
    scope: Optional[str] = Query(None),
    scopeId: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_links_admin),
) -> Dict[str, Any]:
    clauses = []
    if market:
        clauses.append(outbound_link_rules.c.market == normalize_market(market))
    if tool:
        clauses.append(outbound_link_rules.c.tool == normalize_tool(tool))
    if status_:
        clauses.append(outbound_link_rules.c.status == str(status_).strip().lower())
    if scope:
        clauses.append(outbound_link_rules.c.scope == normalize_scope(scope))
    if scopeId:
        # Allow callers to pass raw; normalize only for non-default scopes.
        s = normalize_scope(scope or "sku") if scope else None
        clauses.append(outbound_link_rules.c.scope_id == (normalize_scope_id(s, scopeId) if s else scopeId))

    stmt = select(outbound_link_rules)
    if clauses:
        stmt = stmt.where(and_(*clauses))
    stmt = stmt.order_by(desc(outbound_link_rules.c.updated_at), desc(outbound_link_rules.c.created_at)).limit(limit).offset(offset)
    rows = await database.fetch_all(stmt)
    return {"rules": [_to_out(r).model_dump() for r in rows], "count": len(rows), "offset": offset, "limit": limit}


@employee_router.get("/rules", response_model=Dict[str, Any])
async def employee_list_rules(
    market: Optional[str] = Query(None),
    tool: Optional[str] = Query(None),
    status_: Optional[str] = Query(None, alias="status"),
    scope: Optional[str] = Query(None),
    scopeId: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    # Same implementation as admin list_rules but with employee auth.
    _ = current_user
    clauses = []
    if market:
        clauses.append(outbound_link_rules.c.market == normalize_market(market))
    if tool:
        clauses.append(outbound_link_rules.c.tool == normalize_tool(tool))
    if status_:
        clauses.append(outbound_link_rules.c.status == str(status_).strip().lower())
    if scope:
        clauses.append(outbound_link_rules.c.scope == normalize_scope(scope))
    if scopeId:
        s = normalize_scope(scope or "sku") if scope else None
        clauses.append(outbound_link_rules.c.scope_id == (normalize_scope_id(s, scopeId) if s else scopeId))

    stmt = select(outbound_link_rules)
    if clauses:
        stmt = stmt.where(and_(*clauses))
    stmt = stmt.order_by(desc(outbound_link_rules.c.updated_at), desc(outbound_link_rules.c.created_at)).limit(limit).offset(offset)
    rows = await database.fetch_all(stmt)
    return {"rules": [_to_out(r).model_dump() for r in rows], "count": len(rows), "offset": offset, "limit": limit}


@admin_router.post("/rules", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: LinkRuleIn,
    _: None = Depends(require_links_admin),
) -> Dict[str, Any]:
    market = normalize_market(body.market)
    tool = normalize_tool(body.tool)
    scope = normalize_scope(body.scope)
    scope_id = normalize_scope_id(scope, body.scopeId)
    dest = str(body.destinationUrl).strip()
    if not dest.startswith("http://") and not dest.startswith("https://"):
        raise HTTPException(status_code=400, detail={"code": "INVALID_URL"})

    rule_id = make_rule_id()
    row = {
        "id": rule_id,
        "market": market,
        "tool": tool,
        "scope": scope,
        "scope_id": scope_id,
        "destination_url": dest,
        "purchase_enabled_override": body.purchaseEnabledOverride,
        "priority": int(body.priority or 0),
        "partner_type": str(body.partnerType or "unknown").strip().lower() or "unknown",
        "disclosure_text": body.disclosureText,
        "utm_template": body.utmTemplate,
        "tags": body.tags,
        "notes": body.notes,
        "start_at": body.startAt,
        "end_at": body.endAt,
        "status": "draft",
        "created_by": body.createdBy,
    }

    await database.execute(outbound_link_rules.insert(), row)
    stored = await database.fetch_one(select(outbound_link_rules).where(outbound_link_rules.c.id == rule_id))
    return {"rule": _to_out(stored).model_dump()}


@employee_router.post("/rules", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def employee_create_rule(
    body: LinkRuleIn,
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    # Mirror admin create behavior; stamp createdBy by default.
    created_by = body.createdBy or str(current_user.get("email") or current_user.get("sub") or "")
    body2 = body.copy(update={"createdBy": created_by})
    return await create_rule(body=body2, _=None)  # type: ignore[arg-type]


class PublishRequest(BaseModel):
    ruleIds: List[str] = Field(default_factory=list)
    approvedBy: Optional[str] = None


@admin_router.post("/publish", response_model=Dict[str, Any])
async def publish_rules(
    body: PublishRequest,
    _: None = Depends(require_links_admin),
) -> Dict[str, Any]:
    if not body.ruleIds:
        raise HTTPException(status_code=400, detail={"code": "MISSING_RULE_IDS"})

    stmt = (
        update(outbound_link_rules)
        .where(outbound_link_rules.c.id.in_(body.ruleIds))
        .values(status="published", published_at=datetime.utcnow(), approved_by=body.approvedBy)
    )
    await database.execute(stmt)

    rows = await database.fetch_all(select(outbound_link_rules).where(outbound_link_rules.c.id.in_(body.ruleIds)).order_by(asc(outbound_link_rules.c.id)))
    return {"published": [_to_out(r).model_dump() for r in rows]}


@employee_router.post("/publish", response_model=Dict[str, Any])
async def employee_publish_rules(
    body: PublishRequest,
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    approved_by = body.approvedBy or str(current_user.get("email") or current_user.get("sub") or "")
    body2 = body.copy(update={"approvedBy": approved_by})
    return await publish_rules(body=body2, _=None)  # type: ignore[arg-type]


class CsvImportResponse(BaseModel):
    created: int
    updated: int = 0
    errors: List[str] = Field(default_factory=list)
    ruleIds: List[str] = Field(default_factory=list)


@admin_router.post("/import-csv", response_model=CsvImportResponse)
async def import_csv(
    req: Request,
    _: None = Depends(require_links_admin),
    tool: str = Query("*"),
    market: str = Query("US"),
    mode: str = Query("create"),
    createdBy: Optional[str] = Query(None),
) -> CsvImportResponse:
    """
    MVP CSV import endpoint for Ops.

    Content-Type: text/csv (body is raw CSV text)

    Required CSV columns:
      - scope, scope_id, destination_url
    Optional:
      - priority, purchase_enabled_override, partner_type, disclosure_text, utm_template, start_at, end_at, notes
    """
    text = (await req.body()).decode("utf-8", errors="replace")
    if not text.strip():
        raise HTTPException(status_code=400, detail={"code": "EMPTY_CSV"})

    tool_norm = normalize_tool(tool)
    market_norm = normalize_market(market)

    reader = csv.DictReader(io.StringIO(text))
    created = 0
    updated = 0
    errors: List[str] = []
    rule_ids: List[str] = []
    mode_norm = str(mode or "create").strip().lower()

    for idx, row in enumerate(reader, start=2):
        try:
            scope = normalize_scope(str(row.get("scope") or "").strip())
            scope_id = normalize_scope_id(scope, str(row.get("scope_id") or "").strip())
            dest = str(row.get("destination_url") or row.get("url") or "").strip()
            if not dest.startswith("http://") and not dest.startswith("https://"):
                raise ValueError("INVALID_URL")

            priority = int(str(row.get("priority") or "0").strip() or "0")
            poe_raw = str(row.get("purchase_enabled_override") or "").strip().lower()
            poe: Optional[bool]
            if poe_raw in ("true", "1", "yes"):
                poe = True
            elif poe_raw in ("false", "0", "no"):
                poe = False
            else:
                poe = None

            partner_type = str(row.get("partner_type") or "unknown").strip().lower() or "unknown"
            disclosure_text = str(row.get("disclosure_text") or "").strip() or None
            utm_template = str(row.get("utm_template") or "").strip() or None
            notes = str(row.get("notes") or "").strip() or None

            start_at = str(row.get("start_at") or "").strip() or None
            end_at = str(row.get("end_at") or "").strip() or None
            start_dt = datetime.fromisoformat(start_at) if start_at else None
            end_dt = datetime.fromisoformat(end_at) if end_at else None

            now = datetime.utcnow()

            # Upsert mode: update an existing draft rule for the same key (market+tool+scope+scope_id).
            # Never mutate published rules; if only published exists, create a new draft.
            existing_id: Optional[str] = None
            if mode_norm == "upsert":
                existing = await database.fetch_one(
                    select(outbound_link_rules.c.id)
                    .where(
                        and_(
                            outbound_link_rules.c.market == market_norm,
                            outbound_link_rules.c.tool == tool_norm,
                            outbound_link_rules.c.scope == scope,
                            outbound_link_rules.c.scope_id == scope_id,
                            outbound_link_rules.c.status == "draft",
                        )
                    )
                    .order_by(desc(outbound_link_rules.c.updated_at), desc(outbound_link_rules.c.created_at))
                    .limit(1)
                )
                if existing:
                    existing_id = dict(existing).get("id")

            if existing_id:
                stmt = (
                    update(outbound_link_rules)
                    .where(outbound_link_rules.c.id == existing_id)
                    .values(
                        destination_url=dest,
                        purchase_enabled_override=poe,
                        priority=priority,
                        partner_type=partner_type,
                        disclosure_text=disclosure_text,
                        utm_template=utm_template,
                        notes=notes,
                        start_at=start_dt,
                        end_at=end_dt,
                        updated_at=now,
                    )
                )
                await database.execute(stmt)
                updated += 1
                rule_ids.append(existing_id)
            else:
                rid = make_rule_id()
                await database.execute(
                    outbound_link_rules.insert(),
                    {
                        "id": rid,
                        "market": market_norm,
                        "tool": tool_norm,
                        "scope": scope,
                        "scope_id": scope_id,
                        "destination_url": dest,
                        "purchase_enabled_override": poe,
                        "priority": priority,
                        "partner_type": partner_type,
                        "disclosure_text": disclosure_text,
                        "utm_template": utm_template,
                        "notes": notes,
                        "start_at": start_dt,
                        "end_at": end_dt,
                        "status": "draft",
                        "created_by": createdBy,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                created += 1
                rule_ids.append(rid)
        except Exception as exc:
            errors.append(f"Row {idx}: {str(exc)}")

    return CsvImportResponse(created=created, updated=updated, errors=errors, ruleIds=rule_ids)

@employee_router.post("/import-csv", response_model=CsvImportResponse)
async def employee_import_csv(
    req: Request,
    current_user: dict = Depends(get_current_employee),
    tool: str = Query("*"),
    market: str = Query("US"),
    mode: str = Query("create"),
) -> CsvImportResponse:
    return await import_csv(
        req=req,
        _=None,
        tool=tool,
        market=market,
        mode=mode,
        createdBy=str(current_user.get("email") or ""),
    )  # type: ignore[arg-type]


class ClickEventOut(BaseModel):
    id: int
    createdAt: Optional[datetime] = None
    market: str
    tool: str
    ruleId: Optional[str] = None
    jobId: Optional[str] = None
    sessionId: Optional[str] = None
    skuId: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    area: Optional[str] = None
    kind: Optional[str] = None
    destDomain: Optional[str] = None
    destinationUrl: Optional[str] = None


def _to_click_out(row: Any) -> ClickEventOut:
    d = dict(row)
    return ClickEventOut(
        id=int(d["id"]),
        createdAt=d.get("created_at"),
        market=str(d.get("market") or ""),
        tool=str(d.get("tool") or ""),
        ruleId=d.get("rule_id"),
        jobId=d.get("job_id"),
        sessionId=d.get("session_id"),
        skuId=d.get("sku_id"),
        brand=d.get("brand"),
        category=d.get("category"),
        area=d.get("area"),
        kind=d.get("kind"),
        destDomain=d.get("dest_domain"),
        destinationUrl=d.get("destination_url"),
    )


@employee_router.get("/clicks", response_model=Dict[str, Any])
async def employee_list_clicks(
    market: Optional[str] = Query(None),
    tool: Optional[str] = Query(None),
    ruleId: Optional[str] = Query(None),
    jobId: Optional[str] = Query(None),
    destDomain: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    _ = current_user
    clauses = []
    if market:
        clauses.append(outbound_click_events.c.market == normalize_market(market))
    if tool:
        clauses.append(outbound_click_events.c.tool == normalize_tool(tool))
    if ruleId:
        clauses.append(outbound_click_events.c.rule_id == str(ruleId).strip())
    if jobId:
        clauses.append(outbound_click_events.c.job_id == str(jobId).strip())
    if destDomain:
        clauses.append(outbound_click_events.c.dest_domain == str(destDomain).strip().lower())

    stmt = select(outbound_click_events)
    if clauses:
        stmt = stmt.where(and_(*clauses))
    stmt = stmt.order_by(desc(outbound_click_events.c.created_at)).limit(limit).offset(offset)
    rows = await database.fetch_all(stmt)
    return {"clicks": [_to_click_out(r).model_dump() for r in rows], "count": len(rows), "offset": offset, "limit": limit}


class AllowedDomainIn(BaseModel):
    market: str = "US"
    domain: str = Field(..., min_length=3)
    status: str = Field(default="active")  # active|disabled
    notes: Optional[str] = None


class AllowedDomainOut(BaseModel):
    id: int
    market: str
    domain: str
    status: str
    notes: Optional[str] = None
    createdBy: Optional[str] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


def _to_allowed_domain_out(row: Any) -> AllowedDomainOut:
    d = dict(row)
    return AllowedDomainOut(
        id=int(d["id"]),
        market=str(d.get("market") or ""),
        domain=str(d.get("domain") or ""),
        status=str(d.get("status") or "active"),
        notes=d.get("notes"),
        createdBy=d.get("created_by"),
        createdAt=d.get("created_at"),
        updatedAt=d.get("updated_at"),
    )


@employee_router.get("/allowed-domains", response_model=Dict[str, Any])
async def employee_list_allowed_domains(
    market: str = Query("US"),
    status_: Optional[str] = Query(None, alias="status"),
    q: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    _ = current_user
    market_norm = normalize_market(market)
    clauses = [outbound_link_allowed_domains.c.market == market_norm]
    if status_:
        clauses.append(outbound_link_allowed_domains.c.status == str(status_).strip().lower())
    if q:
        needle = f"%{str(q).strip().lower()}%"
        clauses.append(outbound_link_allowed_domains.c.domain.ilike(needle))

    stmt = (
        select(outbound_link_allowed_domains)
        .where(and_(*clauses))
        .order_by(asc(outbound_link_allowed_domains.c.domain))
        .limit(limit)
        .offset(offset)
    )
    rows = await database.fetch_all(stmt)
    return {"domains": [_to_allowed_domain_out(r).model_dump() for r in rows], "count": len(rows), "offset": offset, "limit": limit}


@employee_router.post("/allowed-domains", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def employee_upsert_allowed_domain(
    body: AllowedDomainIn,
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    market_norm = normalize_market(body.market)
    domain_norm = str(body.domain or "").strip().lower().lstrip(".")
    if not domain_norm or "." not in domain_norm:
        raise HTTPException(status_code=400, detail={"code": "INVALID_DOMAIN"})
    status_norm = str(body.status or "active").strip().lower()
    if status_norm not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATUS"})

    now = datetime.utcnow()
    created_by = str(current_user.get("email") or current_user.get("sub") or "")

    existing = await database.fetch_one(
        select(outbound_link_allowed_domains).where(
            and_(
                outbound_link_allowed_domains.c.market == market_norm,
                outbound_link_allowed_domains.c.domain == domain_norm,
            )
        )
    )
    if existing:
        await database.execute(
            update(outbound_link_allowed_domains)
            .where(outbound_link_allowed_domains.c.id == dict(existing)["id"])
            .values(status=status_norm, notes=body.notes, updated_at=now),
        )
        row = await database.fetch_one(select(outbound_link_allowed_domains).where(outbound_link_allowed_domains.c.id == dict(existing)["id"]))
        return {"domain": _to_allowed_domain_out(row).model_dump()}

    await database.execute(
        outbound_link_allowed_domains.insert(),
        {
            "market": market_norm,
            "domain": domain_norm,
            "status": status_norm,
            "notes": body.notes,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        },
    )
    row = await database.fetch_one(
        select(outbound_link_allowed_domains)
        .where(and_(outbound_link_allowed_domains.c.market == market_norm, outbound_link_allowed_domains.c.domain == domain_norm))
        .limit(1)
    )
    return {"domain": _to_allowed_domain_out(row).model_dump()}


@employee_router.patch("/allowed-domains/{domain_id}", response_model=Dict[str, Any])
async def employee_update_allowed_domain(
    domain_id: int,
    body: AllowedDomainIn,
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    _ = current_user
    status_norm = str(body.status or "").strip().lower()
    if status_norm and status_norm not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATUS"})
    update_values: Dict[str, Any] = {"updated_at": datetime.utcnow()}
    if status_norm:
        update_values["status"] = status_norm
    if body.notes is not None:
        update_values["notes"] = body.notes
    stmt = update(outbound_link_allowed_domains).where(outbound_link_allowed_domains.c.id == int(domain_id)).values(**update_values)
    await database.execute(stmt)
    row = await database.fetch_one(select(outbound_link_allowed_domains).where(outbound_link_allowed_domains.c.id == int(domain_id)))
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
    return {"domain": _to_allowed_domain_out(row).model_dump()}


# Composite router exported for main.py include_router()
router = APIRouter()
router.include_router(api_router)
router.include_router(admin_router)
router.include_router(employee_router)
router.include_router(public_router)

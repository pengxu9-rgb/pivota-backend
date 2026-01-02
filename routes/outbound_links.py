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
from db.outbound_links import outbound_link_rules
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


class CsvImportResponse(BaseModel):
    created: int
    errors: List[str] = Field(default_factory=list)
    ruleIds: List[str] = Field(default_factory=list)


@admin_router.post("/import-csv", response_model=CsvImportResponse)
async def import_csv(
    req: Request,
    _: None = Depends(require_links_admin),
    tool: str = Query("*"),
    market: str = Query("US"),
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
    errors: List[str] = []
    rule_ids: List[str] = []

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
                },
            )
            created += 1
            rule_ids.append(rid)
        except Exception as exc:
            errors.append(f"Row {idx}: {str(exc)}")

    return CsvImportResponse(created=created, errors=errors, ruleIds=rule_ids)


# Composite router exported for main.py include_router()
router = APIRouter()
router.include_router(api_router)
router.include_router(admin_router)
router.include_router(public_router)

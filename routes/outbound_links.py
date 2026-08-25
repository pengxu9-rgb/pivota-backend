import csv
import io
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, asc, case, desc, func, or_, select, update

from db.database import database
from db.outbound_links import outbound_link_rules, outbound_click_events, outbound_link_allowed_domains
from utils.auth import get_current_employee
from services.outbound_links_service import (
    DEFAULT_DISCLOSURE_TEXT,
    make_rule_id,
    make_report_token,
    normalize_market,
    normalize_scope,
    normalize_scope_id,
    normalize_tool,
    parse_and_verify_report_token,
    parse_and_verify_redirect_token,
    parse_redirect_token_verified,
    resolve_outbound_link,
    log_outbound_click,
    log_outbound_event,
)
from config.settings import settings
from services.outbound_warm_handoff import (
    evaluate_warm_eligibility,
    memo_get,
    memo_set,
    resolve_warm_handoff,
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


def _env_truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


async def require_links_resolve_caller(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-ADMIN-KEY"),
    x_links_service_key: Optional[str] = Header(None, alias="X-Links-Service-Key"),
) -> None:
    """
    Service auth for POST /api/links/resolve (attributed-redirect lane P1 security fix).

    /resolve mints signed /r redirect tokens with caller-chosen ctx — once those tokens carry
    money significance (click attribution → GMV → take), an open minter is an abuse surface
    (attribution stuffing, open-redirect laundering through allowed domains).

    Accepted credentials: admin key (ops/smokes) OR the shared service key the gateway sends
    (OUTBOUND_LINKS_SERVICE_KEY, header X-Links-Service-Key).

    Rollout-safe enforcement: deny only when OUTBOUND_LINKS_RESOLVE_REQUIRE_KEY is truthy.
    Until then, unauthenticated calls are ALLOWED but logged at WARNING so we can confirm all
    legit callers carry the key before flipping enforcement (same pattern as shadow-mode gates).
    """
    admin_expected = os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY")
    service_expected = os.getenv("OUTBOUND_LINKS_SERVICE_KEY")
    if admin_expected and x_admin_key == admin_expected:
        return
    if service_expected and x_links_service_key == service_expected:
        return
    if _env_truthy(os.getenv("OUTBOUND_LINKS_RESOLVE_REQUIRE_KEY")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")
    import logging

    logging.getLogger(__name__).warning(
        "outbound-links /resolve called without a valid key (enforcement off) client=%s",
        getattr(getattr(request, "client", None), "host", None),
    )


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
async def resolve_endpoint(
    req: Request,
    body: ResolveRequest,
    _: None = Depends(require_links_resolve_caller),
) -> ResolveResponse:
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
    try:
        payload, is_expired = parse_redirect_token_verified(token)
    except Exception as exc:
        code = str(getattr(exc, "args", ["INVALID_TOKEN"])[0] or "INVALID_TOKEN")
        raise HTTPException(status_code=400, detail=code) from exc
    dest = str(payload.get("dest") or "")
    if not dest.startswith("http://") and not dest.startswith("https://"):
        raise HTTPException(status_code=400, detail="INVALID_DEST")

    # Attributed-redirect lane D3: a VALIDLY-SIGNED but EXPIRED token (e.g. a feed link cached by
    # an agent platform past the 7-day TTL) still 302s to its destination — a dead link punishes
    # the buyer and the merchant for our cache horizon — but the click is NOT logged, so expired
    # links can never farm attribution. Bad signatures/shapes still 400 above. Expired links also
    # never attempt a warm handoff (no cart is ever built for a stale link).
    if is_expired:
        return RedirectResponse(url=dest, status_code=302)

    # Warm-handoff click lane (Pivota_Warm_Handoff_Click_Lane_Spec_2026-07-22.md; flag-gated,
    # DEFAULT OFF — flag off is byte-identical to the cold redirect below). An eligible cold
    # brand redirect is upgraded at click time to a PRE-BUILT cart on the brand's own Shopify
    # checkout via the gateway's internal resolve endpoint. Best-effort throughout: any miss
    # (ineligible / unreachable / timeout / off-brand continue_url) falls back to `dest`, and
    # the outcome is recorded on the click event ctx (`handoff` / `warm_reason`) — that pair is
    # the substitution-rate instrument.
    # (HEAD needs no handling here: the route is GET-only, so HEAD is a side-effect-free 405
    # at the framework layer — no warm attempt, no cart, no click log.)
    #
    # CONTRACT NOTE — this lane can falsify an answer we ALREADY SENT. `offers.resolve`
    # returns `cart_prefilled` on external offers (routes/agent_shop_gateway.py), computed
    # at RESOLVE time from the same resolve_cart_permalink that chose `dest`. A `false`
    # there is a positive claim to the agent — "this link lands on a product page, the buyer
    # picks the variant" — and eligibility below fires on exactly that cold population, so a
    # warm upgrade would make that already-sent `false` wrong with no way to correct it.
    # (`true` is unaffected: this lane only ever BUILDS a cart.)
    #
    # Handled on the CLAIM side, not here: _cart_prefilled_claim asks
    # could_upgrade_at_click_time and answers `null` rather than a `false` this lane could
    # contradict. The upgrade itself is deliberately unsuppressed — the buyer gets the better
    # landing either way. So the rules below are now load-bearing for an agent-facing
    # contract as well as for the redirect: CHANGING THEM CHANGES WHAT WE PROMISE. Widening
    # the brand allowlist still invalidates `false` answers on tokens minted before the
    # widening and still inside their 7-day TTL — that tail is not closed. See
    # docs/runbooks/outbound_warm_handoff_rollout.md.
    warm_redirect_url: Optional[str] = None
    if settings.outbound_warm_handoff_enabled:
        # The WHOLE lane is throw-guarded: an unexpected error anywhere in eligibility,
        # memo, or resolve must degrade to the cold redirect — never a 500 on a real click.
        try:
            warm_ctx: Dict[str, str]
            eligible, reason = evaluate_warm_eligibility(
                dest=dest,
                user_agent=req.headers.get("user-agent"),
                token=token,
                # The signed ctx carries `join_mode`, so a dest that is ALREADY a prefilled
                # cart is knocked out (`already_cart`) instead of being rebuilt from a
                # request that carries no product identity.
                ctx=payload.get("ctx") if isinstance(payload.get("ctx"), dict) else {},
                settings=settings,
            )
            if eligible:
                # Per-token memo: agent-platform prefetch + the real human click build ONE
                # cart, and the human click 302s instantly off the memo.
                hit, resolved = memo_get(token)
                if not hit:
                    resolved = await resolve_warm_handoff(
                        dest=dest,
                        ctx=payload.get("ctx") if isinstance(payload.get("ctx"), dict) else {},
                        settings=settings,
                    )
                    memo_set(token, resolved)
                if resolved and resolved.get("continue_url"):
                    warm_redirect_url = str(resolved["continue_url"])
                    warm_ctx = {"handoff": "warm", "warm_reason": "ok"}
                else:
                    warm_ctx = {"handoff": "cold", "warm_reason": "unresolved"}
            else:
                warm_ctx = {"handoff": "cold", "warm_reason": reason}
            ctx_out = dict(payload.get("ctx")) if isinstance(payload.get("ctx"), dict) else {}
            ctx_out.update(warm_ctx)
            payload["ctx"] = ctx_out
        except Exception:
            warm_redirect_url = None

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

    return RedirectResponse(url=warm_redirect_url or dest, status_code=302)


class ImpressionRequest(BaseModel):
    token: str = Field(..., min_length=10)
    context: Dict[str, Any] = Field(default_factory=dict)


@api_router.post("/impression", response_model=Dict[str, Any])
async def impression_endpoint(req: Request, body: ImpressionRequest) -> Dict[str, Any]:
    """
    Record a best-effort *impression* event for a signed redirect token.

    Callers should pass the same `token` embedded in the redirect URL (GET /r?token=...).
    This avoids leaking raw destination URLs to clients while still allowing analytics.
    """
    try:
        payload = parse_and_verify_redirect_token(body.token)
    except Exception as exc:
        code = str(getattr(exc, "args", ["INVALID_TOKEN"])[0] or "INVALID_TOKEN")
        raise HTTPException(status_code=400, detail=code) from exc
    dest = str(payload.get("dest") or "")
    if not dest.startswith("http://") and not dest.startswith("https://"):
        raise HTTPException(status_code=400, detail="INVALID_DEST")

    # Merge extra context (best-effort) without overwriting token-provided identifiers.
    ctx = payload.get("ctx") if isinstance(payload.get("ctx"), dict) else {}
    ctx2 = dict(ctx or {})
    extra = body.context if isinstance(body.context, dict) else {}
    for k, v in (extra or {}).items():
        if k in ctx2 and ctx2.get(k) not in (None, "", [], {}):
            continue
        ctx2[k] = v
    payload["ctx"] = ctx2

    try:
        await log_outbound_event(
            token_payload=payload,
            request_meta={
                "user_agent": req.headers.get("user-agent"),
                "ip": getattr(getattr(req, "client", None), "host", None),
            },
            event_type="impression",
        )
    except Exception:
        pass

    return {"ok": True}


class ShareTrafficReportIn(BaseModel):
    market: str = "US"
    tool: str = "*"
    destDomain: str = Field(..., min_length=3)
    startAt: Optional[str] = None
    endAt: Optional[str] = None
    granularity: str = "day"  # total|day
    ttlSeconds: int = 30 * 24 * 3600


@employee_router.post("/reports/share-traffic", response_model=Dict[str, Any])
async def employee_share_traffic_report(
    req: Request,
    body: ShareTrafficReportIn,
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    """
    Create a signed, public traffic report URL (no employee auth needed to view).
    Intended to be shared with merchants as early "proof of traffic".
    """
    _ = current_user
    market = normalize_market(body.market)
    tool = normalize_tool(body.tool)
    dest_domain = str(body.destDomain or "").strip().lower().lstrip(".")
    if "." not in dest_domain:
        raise HTTPException(status_code=400, detail={"code": "INVALID_DOMAIN"})

    granularity = str(body.granularity or "day").strip()
    if granularity not in {"total", "day"}:
        raise HTTPException(status_code=400, detail={"code": "INVALID_GRANULARITY"})

    ttl = int(body.ttlSeconds or 0)
    if ttl < 60 or ttl > 180 * 24 * 3600:
        raise HTTPException(status_code=400, detail={"code": "INVALID_TTL"})

    token = make_report_token(
        {
            "market": market,
            "tool": tool,
            "destDomain": dest_domain,
            "startAt": body.startAt,
            "endAt": body.endAt,
            "granularity": granularity,
        },
        ttl_seconds=ttl,
    )
    base = str(req.base_url).rstrip("/")
    return {"ok": True, "reportUrl": f"{base}/api/links/report?token={token}"}


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

    # Backward compatible: treat missing eventType as "click", and exclude impressions.
    event_type_expr = outbound_click_events.c.context["eventType"].astext
    clauses.append(or_(event_type_expr.is_(None), event_type_expr != "impression"))

    stmt = select(outbound_click_events)
    if clauses:
        stmt = stmt.where(and_(*clauses))
    stmt = stmt.order_by(desc(outbound_click_events.c.created_at)).limit(limit).offset(offset)
    rows = await database.fetch_all(stmt)
    return {"clicks": [_to_click_out(r).model_dump() for r in rows], "count": len(rows), "offset": offset, "limit": limit}


class ClickReportRow(BaseModel):
    key: str
    clicks: int
    jobs: int
    sessions: int
    firstAt: Optional[str] = None
    lastAt: Optional[str] = None


@employee_router.get("/clicks/report", response_model=Dict[str, Any])
async def employee_clicks_report(
    market: Optional[str] = Query(None),
    tool: Optional[str] = Query(None),
    startAt: Optional[str] = Query(None),
    endAt: Optional[str] = Query(None),
    groupBy: str = Query("destDomain"),
    minClicks: int = Query(1, ge=1, le=1000000),
    limit: int = Query(200, ge=1, le=500),
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    """
    Aggregate outbound click events for ops reporting.

    groupBy:
      - destDomain (default)
      - destinationUrl
      - ruleId
      - jobId
      - brand
      - category
      - area
      - day (UTC)
    """
    _ = current_user

    def parse_dt(v: Optional[str]) -> Optional[datetime]:
        if not v:
            return None
        s = str(v).strip()
        if not s:
            return None
        # Accept YYYY-MM-DD by treating it as UTC midnight.
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            try:
                return datetime.fromisoformat(f"{s}T00:00:00")
            except Exception:
                return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    start_dt = parse_dt(startAt)
    end_dt = parse_dt(endAt)

    group = str(groupBy or "destDomain").strip()
    if group not in {"destDomain", "destinationUrl", "ruleId", "jobId", "brand", "category", "area", "day"}:
        raise HTTPException(status_code=400, detail={"code": "INVALID_GROUP_BY"})

    key_expr = None
    if group == "destDomain":
        key_expr = outbound_click_events.c.dest_domain
    elif group == "destinationUrl":
        key_expr = outbound_click_events.c.destination_url
    elif group == "ruleId":
        key_expr = outbound_click_events.c.rule_id
    elif group == "jobId":
        key_expr = outbound_click_events.c.job_id
    elif group == "brand":
        key_expr = outbound_click_events.c.brand
    elif group == "category":
        key_expr = outbound_click_events.c.category
    elif group == "area":
        key_expr = outbound_click_events.c.area
    else:
        key_expr = func.date_trunc("day", outbound_click_events.c.created_at)

    clauses = []
    if market:
        clauses.append(outbound_click_events.c.market == normalize_market(market))
    if tool:
        clauses.append(outbound_click_events.c.tool == normalize_tool(tool))
    if start_dt:
        clauses.append(outbound_click_events.c.created_at >= start_dt)
    if end_dt:
        clauses.append(outbound_click_events.c.created_at < end_dt)

    # Backward compatible: treat missing eventType as "click", and exclude impressions.
    event_type_expr = outbound_click_events.c.context["eventType"].astext
    clauses.append(or_(event_type_expr.is_(None), event_type_expr != "impression"))

    stmt = (
        select(
            key_expr.label("key"),
            func.count(outbound_click_events.c.id).label("clicks"),
            func.count(func.distinct(outbound_click_events.c.job_id)).label("jobs"),
            func.count(func.distinct(outbound_click_events.c.session_id)).label("sessions"),
            func.min(outbound_click_events.c.created_at).label("first_at"),
            func.max(outbound_click_events.c.created_at).label("last_at"),
        )
        .select_from(outbound_click_events)
    )
    if clauses:
        stmt = stmt.where(and_(*clauses))
    stmt = stmt.group_by(key_expr).having(func.count(outbound_click_events.c.id) >= int(minClicks)).order_by(desc(func.count(outbound_click_events.c.id))).limit(limit)

    rows = await database.fetch_all(stmt)
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        k = d.get("key")
        if isinstance(k, datetime):
            key = k.date().isoformat()
        else:
            key = str(k) if k is not None and str(k).strip() else "—"
        out.append(
            ClickReportRow(
                key=key,
                clicks=int(d.get("clicks") or 0),
                jobs=int(d.get("jobs") or 0),
                sessions=int(d.get("sessions") or 0),
                firstAt=(d.get("first_at").isoformat() if isinstance(d.get("first_at"), datetime) else None),
                lastAt=(d.get("last_at").isoformat() if isinstance(d.get("last_at"), datetime) else None),
            ).model_dump()
        )

    return {
        "groupBy": group,
        "startAt": startAt,
        "endAt": endAt,
        "minClicks": minClicks,
        "rows": out,
        "count": len(out),
        "limit": limit,
    }


@employee_router.get("/impressions", response_model=Dict[str, Any])
async def employee_list_impressions(
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

    event_type_expr = outbound_click_events.c.context["eventType"].astext
    clauses.append(event_type_expr == "impression")

    stmt = select(outbound_click_events)
    if clauses:
        stmt = stmt.where(and_(*clauses))
    stmt = stmt.order_by(desc(outbound_click_events.c.created_at)).limit(limit).offset(offset)
    rows = await database.fetch_all(stmt)
    return {"impressions": [_to_click_out(r).model_dump() for r in rows], "count": len(rows), "offset": offset, "limit": limit}


class ImpressionReportRow(BaseModel):
    key: str
    impressions: int
    jobs: int
    sessions: int
    firstAt: Optional[str] = None
    lastAt: Optional[str] = None


@employee_router.get("/impressions/report", response_model=Dict[str, Any])
async def employee_impressions_report(
    market: Optional[str] = Query(None),
    tool: Optional[str] = Query(None),
    startAt: Optional[str] = Query(None),
    endAt: Optional[str] = Query(None),
    groupBy: str = Query("destDomain"),
    minImpressions: int = Query(1, ge=1, le=1000000),
    limit: int = Query(200, ge=1, le=500),
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    """
    Aggregate outbound *impression* events for ops reporting.

    groupBy:
      - destDomain (default)
      - destinationUrl
      - ruleId
      - jobId
      - brand
      - category
      - area
      - day (UTC)
    """
    _ = current_user

    def parse_dt(v: Optional[str]) -> Optional[datetime]:
        if not v:
            return None
        s = str(v).strip()
        if not s:
            return None
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            try:
                return datetime.fromisoformat(f"{s}T00:00:00")
            except Exception:
                return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    start_dt = parse_dt(startAt)
    end_dt = parse_dt(endAt)

    group = str(groupBy or "destDomain").strip()
    if group not in {"destDomain", "destinationUrl", "ruleId", "jobId", "brand", "category", "area", "day"}:
        raise HTTPException(status_code=400, detail={"code": "INVALID_GROUP_BY"})

    key_expr = None
    if group == "destDomain":
        key_expr = outbound_click_events.c.dest_domain
    elif group == "destinationUrl":
        key_expr = outbound_click_events.c.destination_url
    elif group == "ruleId":
        key_expr = outbound_click_events.c.rule_id
    elif group == "jobId":
        key_expr = outbound_click_events.c.job_id
    elif group == "brand":
        key_expr = outbound_click_events.c.brand
    elif group == "category":
        key_expr = outbound_click_events.c.category
    elif group == "area":
        key_expr = outbound_click_events.c.area
    else:
        key_expr = func.date_trunc("day", outbound_click_events.c.created_at)

    clauses = []
    if market:
        clauses.append(outbound_click_events.c.market == normalize_market(market))
    if tool:
        clauses.append(outbound_click_events.c.tool == normalize_tool(tool))
    if start_dt:
        clauses.append(outbound_click_events.c.created_at >= start_dt)
    if end_dt:
        clauses.append(outbound_click_events.c.created_at < end_dt)

    event_type_expr = outbound_click_events.c.context["eventType"].astext
    clauses.append(event_type_expr == "impression")

    stmt = (
        select(
            key_expr.label("key"),
            func.count(outbound_click_events.c.id).label("impressions"),
            func.count(func.distinct(outbound_click_events.c.job_id)).label("jobs"),
            func.count(func.distinct(outbound_click_events.c.session_id)).label("sessions"),
            func.min(outbound_click_events.c.created_at).label("first_at"),
            func.max(outbound_click_events.c.created_at).label("last_at"),
        )
        .select_from(outbound_click_events)
    )
    if clauses:
        stmt = stmt.where(and_(*clauses))
    stmt = (
        stmt.group_by(key_expr)
        .having(func.count(outbound_click_events.c.id) >= int(minImpressions))
        .order_by(desc(func.count(outbound_click_events.c.id)))
        .limit(limit)
    )

    rows = await database.fetch_all(stmt)
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        k = d.get("key")
        if isinstance(k, datetime):
            key = k.date().isoformat()
        else:
            key = str(k) if k is not None and str(k).strip() else "—"
        out.append(
            ImpressionReportRow(
                key=key,
                impressions=int(d.get("impressions") or 0),
                jobs=int(d.get("jobs") or 0),
                sessions=int(d.get("sessions") or 0),
                firstAt=(d.get("first_at").isoformat() if isinstance(d.get("first_at"), datetime) else None),
                lastAt=(d.get("last_at").isoformat() if isinstance(d.get("last_at"), datetime) else None),
            ).model_dump()
        )

    return {
        "groupBy": group,
        "startAt": startAt,
        "endAt": endAt,
        "minImpressions": minImpressions,
        "rows": out,
        "count": len(out),
        "limit": limit,
    }


@api_router.get("/report", response_model=Dict[str, Any])
async def public_traffic_report(
    token: str = Query(..., min_length=10),
) -> Dict[str, Any]:
    """
    Public traffic report endpoint protected by a signed token.

    The token is minted via `POST /employee/links/reports/share-traffic` and encodes a fixed
    scope (market/tool/domain + optional time range). This endpoint returns only aggregated
    metrics (no IP/user-agent).
    """
    try:
        payload = parse_and_verify_report_token(token)
    except Exception as exc:
        code = str(getattr(exc, "args", ["INVALID_TOKEN"])[0] or "INVALID_TOKEN")
        raise HTTPException(status_code=400, detail=code) from exc
    market = normalize_market(payload.get("market"))
    tool = normalize_tool(payload.get("tool"))
    dest_domain = str(payload.get("destDomain") or "").strip().lower().lstrip(".")
    if "." not in dest_domain:
        raise HTTPException(status_code=400, detail={"code": "INVALID_DOMAIN"})

    granularity = str(payload.get("granularity") or "day").strip()
    if granularity not in {"total", "day"}:
        raise HTTPException(status_code=400, detail={"code": "INVALID_GRANULARITY"})

    def parse_dt(v: Optional[str]) -> Optional[datetime]:
        if not v:
            return None
        s = str(v).strip()
        if not s:
            return None
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            try:
                return datetime.fromisoformat(f"{s}T00:00:00")
            except Exception:
                return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    start_dt = parse_dt(payload.get("startAt"))
    end_dt = parse_dt(payload.get("endAt"))

    # Default to last 30d if no explicit window.
    if not start_dt and not end_dt:
        end_dt = datetime.utcnow()
        start_dt = end_dt - timedelta(days=30)

    if start_dt and end_dt:
        if end_dt < start_dt:
            raise HTTPException(status_code=400, detail={"code": "INVALID_TIME_RANGE"})
        if (end_dt - start_dt).total_seconds() > 180 * 24 * 3600:
            raise HTTPException(status_code=400, detail={"code": "TIME_RANGE_TOO_LARGE"})

    clauses = [
        outbound_click_events.c.market == market,
        outbound_click_events.c.tool == tool,
        outbound_click_events.c.dest_domain == dest_domain,
    ]
    if start_dt:
        clauses.append(outbound_click_events.c.created_at >= start_dt)
    if end_dt:
        clauses.append(outbound_click_events.c.created_at < end_dt)

    event_type_expr = outbound_click_events.c.context["eventType"].astext
    is_impression = case((event_type_expr == "impression", 1), else_=0)
    is_click = case((or_(event_type_expr.is_(None), event_type_expr != "impression"), 1), else_=0)

    totals_stmt = (
        select(
            func.sum(is_impression).label("impressions"),
            func.sum(is_click).label("clicks"),
            func.count(func.distinct(outbound_click_events.c.session_id)).label("sessions"),
            func.count(func.distinct(outbound_click_events.c.job_id)).label("jobs"),
            func.min(outbound_click_events.c.created_at).label("first_at"),
            func.max(outbound_click_events.c.created_at).label("last_at"),
        )
        .select_from(outbound_click_events)
        .where(and_(*clauses))
    )
    totals = dict(await database.fetch_one(totals_stmt) or {})

    out: Dict[str, Any] = {
        "ok": True,
        "scope": {
            "market": market,
            "tool": tool,
            "destDomain": dest_domain,
            "startAt": payload.get("startAt"),
            "endAt": payload.get("endAt"),
        },
        "totals": {
            "impressions": int(totals.get("impressions") or 0),
            "clicks": int(totals.get("clicks") or 0),
            "sessions": int(totals.get("sessions") or 0),
            "jobs": int(totals.get("jobs") or 0),
            "firstAt": (totals.get("first_at").isoformat() if isinstance(totals.get("first_at"), datetime) else None),
            "lastAt": (totals.get("last_at").isoformat() if isinstance(totals.get("last_at"), datetime) else None),
        },
    }

    if granularity == "day":
        day_expr = func.date_trunc("day", outbound_click_events.c.created_at)
        by_day_stmt = (
            select(
                day_expr.label("day"),
                func.sum(is_impression).label("impressions"),
                func.sum(is_click).label("clicks"),
                func.count(func.distinct(outbound_click_events.c.session_id)).label("sessions"),
                func.count(func.distinct(outbound_click_events.c.job_id)).label("jobs"),
            )
            .select_from(outbound_click_events)
            .where(and_(*clauses))
            .group_by(day_expr)
            .order_by(day_expr.asc())
            .limit(366)
        )
        rows = await database.fetch_all(by_day_stmt)
        out["byDay"] = [
            {
                "day": (dict(r).get("day").date().isoformat() if isinstance(dict(r).get("day"), datetime) else None),
                "impressions": int(dict(r).get("impressions") or 0),
                "clicks": int(dict(r).get("clicks") or 0),
                "sessions": int(dict(r).get("sessions") or 0),
                "jobs": int(dict(r).get("jobs") or 0),
            }
            for r in rows
        ]
    return out


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

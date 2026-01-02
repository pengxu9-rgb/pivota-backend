import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from sqlalchemy import and_, desc, or_, select

from db.database import database
from db.outbound_links import outbound_link_rules, outbound_click_events


DEFAULT_UTM_TEMPLATE = "utm_source=pivota&utm_medium={{tool}}&utm_campaign={{market}}"
DEFAULT_DISCLOSURE_TEXT = "Prices may change. We may earn a commission from qualifying purchases."


SCOPE_ORDER = ["sku", "brand", "category", "role", "default"]


def _now_ts() -> int:
    return int(time.time())


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def _signing_secret() -> str:
    return (
        os.getenv("OUTBOUND_LINKS_SIGNING_SECRET")
        or os.getenv("ADMIN_API_KEY")  # fallback in dev; not recommended for prod
        or os.getenv("JWT_SECRET_KEY")
        or "dev-insecure-secret"
    )


def make_rule_id() -> str:
    return f"lr_{uuid.uuid4().hex[:24]}"


def normalize_tool(tool: Optional[str]) -> str:
    t = str(tool or "").strip()
    return t or "*"


def normalize_market(market: Optional[str]) -> str:
    m = str(market or "").strip().upper()
    return m or "US"


def normalize_scope(scope: str) -> str:
    s = str(scope or "").strip().lower()
    if s not in {"sku", "brand", "category", "role", "default"}:
        raise ValueError("INVALID_SCOPE")
    return s


def normalize_scope_id(scope: str, scope_id: Optional[str]) -> str:
    raw = str(scope_id or "").strip()
    if scope == "default":
        return "*"
    if not raw:
        raise ValueError("MISSING_SCOPE_ID")
    if scope == "brand":
        return raw.lower()
    if scope == "category":
        return raw.lower()
    return raw


def apply_utm(destination_url: str, utm_template: str, tokens: Dict[str, str]) -> str:
    tmpl = utm_template or DEFAULT_UTM_TEMPLATE
    rendered = tmpl
    for k, v in tokens.items():
        rendered = rendered.replace(f"{{{{{k}}}}}", v)
    utm_pairs = dict(parse_qsl(rendered, keep_blank_values=True))

    parsed = urlparse(destination_url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))

    # Do not override explicit existing utm_* if already present.
    for k, v in utm_pairs.items():
        if k in existing:
            continue
        existing[k] = v

    new_query = urlencode(existing, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def url_domain(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def make_redirect_token(payload: Dict[str, Any], ttl_seconds: int = 7 * 24 * 3600) -> str:
    now = _now_ts()
    body = {
        "v": 0,
        **payload,
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    payload_b64 = _b64url_encode(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(_signing_secret().encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def parse_and_verify_redirect_token(token: str) -> Dict[str, Any]:
    if not token or "." not in token:
        raise ValueError("INVALID_TOKEN")
    payload_b64, sig_b64 = token.split(".", 1)
    expected = hmac.new(_signing_secret().encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_encode(expected), sig_b64):
        raise ValueError("INVALID_SIGNATURE")
    payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("INVALID_TOKEN")
    exp = int(payload.get("exp") or 0)
    if exp and _now_ts() > exp:
        raise ValueError("TOKEN_EXPIRED")
    return payload


@dataclass(frozen=True)
class ResolvedLink:
    destination_url: str
    redirect_url: str
    purchase_enabled: bool
    purchase_enabled_override: Optional[bool]
    rule_id: Optional[str]
    disclosure_text: str
    partner_type: str


async def _select_best_rule(
    *,
    market: str,
    tool: str,
    scope: str,
    scope_id: str,
) -> Optional[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    # Allow tool="*" fallback.
    stmt = (
        select(outbound_link_rules)
        .where(
            and_(
                outbound_link_rules.c.market == market,
                outbound_link_rules.c.status == "published",
                outbound_link_rules.c.scope == scope,
                outbound_link_rules.c.scope_id == scope_id,
                or_(outbound_link_rules.c.tool == tool, outbound_link_rules.c.tool == "*"),
                or_(outbound_link_rules.c.start_at.is_(None), outbound_link_rules.c.start_at <= now),
                or_(outbound_link_rules.c.end_at.is_(None), outbound_link_rules.c.end_at >= now),
            )
        )
        .order_by(
            # Prefer exact tool match over "*"
            desc(outbound_link_rules.c.tool == tool),
            desc(outbound_link_rules.c.priority),
            desc(outbound_link_rules.c.published_at.isnot(None)),
        )
        .limit(1)
    )
    row = await database.fetch_one(stmt)
    return dict(row) if row else None


async def resolve_outbound_link(input: Dict[str, Any], request_base_url: str) -> ResolvedLink:
    market = normalize_market(input.get("market"))
    tool = normalize_tool(input.get("tool"))
    candidates = input.get("candidates") or {}
    context = input.get("context") or {}

    # Normalize candidate identifiers for matching.
    sku_id = str(candidates.get("skuId") or candidates.get("sku_id") or "").strip() or None
    brand = str(candidates.get("brand") or "").strip() or None
    category = str(candidates.get("category") or "").strip() or None
    role_id = str(candidates.get("roleId") or candidates.get("role_id") or "").strip() or None

    # Search scopes in priority order.
    scopes: Tuple[Tuple[str, Optional[str]], ...] = (
        ("sku", sku_id),
        ("brand", brand.lower() if brand else None),
        ("category", category.lower() if category else None),
        ("role", role_id),
        ("default", "*"),
    )

    matched: Optional[Dict[str, Any]] = None
    matched_scope: Optional[str] = None
    for scope, sid in scopes:
        if scope != "default" and not sid:
            continue
        try:
            sid_norm = normalize_scope_id(scope, sid)
        except Exception:
            continue
        rule = await _select_best_rule(market=market, tool=tool, scope=scope, scope_id=sid_norm)
        if rule:
            matched = rule
            matched_scope = scope
            break

    if not matched:
        raise ValueError("NO_MATCH")

    dest = str(matched.get("destination_url") or "").strip()
    if not dest.startswith("http://") and not dest.startswith("https://"):
        raise ValueError("INVALID_DESTINATION_URL")

    purchase_override = matched.get("purchase_enabled_override", None)
    purchase_enabled = bool(purchase_override) if purchase_override is not None else True

    disclosure_text = str(matched.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT)
    partner_type = str(matched.get("partner_type") or "unknown")
    utm_template = str(matched.get("utm_template") or DEFAULT_UTM_TEMPLATE)

    tokens = {
        "tool": tool,
        "market": market,
    }
    dest_with_utm = apply_utm(dest, utm_template, tokens)

    token_payload = {
        "market": market,
        "tool": tool,
        "ruleId": matched.get("id"),
        "dest": dest_with_utm,
        "ctx": {
            **({ "jobId": str(context.get("jobId")) } if context.get("jobId") else {}),
            **({ "sessionId": str(context.get("sessionId")) } if context.get("sessionId") else {}),
            **({ "skuId": sku_id } if sku_id else {}),
            **({ "brand": brand } if brand else {}),
            **({ "category": category } if category else {}),
            **({ "area": str(context.get("area")) } if context.get("area") else {}),
            **({ "kind": str(context.get("kind")) } if context.get("kind") else {}),
            **({ "scope": matched_scope } if matched_scope else {}),
        },
    }

    token = make_redirect_token(token_payload)
    base = str(request_base_url or "").rstrip("/")
    redirect_url = f"{base}/r?token={token}"

    return ResolvedLink(
        destination_url=dest_with_utm,
        redirect_url=redirect_url,
        purchase_enabled=purchase_enabled,
        purchase_enabled_override=purchase_override,
        rule_id=matched.get("id"),
        disclosure_text=disclosure_text,
        partner_type=partner_type,
    )


async def log_outbound_click(*, token_payload: Dict[str, Any], request_meta: Dict[str, Any]) -> None:
    ctx = token_payload.get("ctx") if isinstance(token_payload.get("ctx"), dict) else {}
    dest = str(token_payload.get("dest") or "")
    row = {
        "market": str(token_payload.get("market") or ""),
        "tool": str(token_payload.get("tool") or ""),
        "rule_id": str(token_payload.get("ruleId") or "") or None,
        "job_id": str(ctx.get("jobId") or "") or None,
        "session_id": str(ctx.get("sessionId") or "") or None,
        "sku_id": str(ctx.get("skuId") or "") or None,
        "brand": str(ctx.get("brand") or "") or None,
        "category": str(ctx.get("category") or "") or None,
        "area": str(ctx.get("area") or "") or None,
        "kind": str(ctx.get("kind") or "") or None,
        "dest_domain": url_domain(dest) or None,
        "destination_url": dest or None,
        "context": ctx or None,
        "user_agent": request_meta.get("user_agent"),
        "ip": request_meta.get("ip"),
    }
    try:
        await database.execute(outbound_click_events.insert(), row)
    except Exception:
        # Best-effort: never break redirect.
        return

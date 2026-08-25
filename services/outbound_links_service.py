import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse, urlunparse, parse_qsl

from sqlalchemy import and_, desc, func, or_, select

from db.database import database
from db.outbound_links import outbound_link_rules, outbound_click_events, outbound_link_allowed_domains
from services.commerce_attribution_service import (
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_PROMPT_CLUSTER,
    PVT_SURFACE,
    PVT_VARIANT_ID,
    apply_pvt_params,
    materialize_attribution_context,
    record_surface_event,
)


DEFAULT_UTM_TEMPLATE = "utm_source=pivota&utm_medium={{tool}}&utm_campaign={{market}}"
DEFAULT_DISCLOSURE_TEXT = "Prices may change. We may earn a commission from qualifying purchases."


SCOPE_ORDER = ["sku", "brand", "category", "role", "default"]
ALLOWED_DOMAIN_CACHE_TTL_MS = max(
    1000,
    min(
        24 * 60 * 60 * 1000,
        int(os.getenv("FIND_PRODUCTS_MULTI_SEED_ALLOWLIST_CACHE_TTL_MS", "300000") or 300000),
    ),
)
_ALLOWED_DOMAIN_CACHE: Dict[str, Tuple[int, List[str]]] = {}
_ALLOWED_DOMAIN_INFLIGHT: Dict[str, asyncio.Task[List[str]]] = {}


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

def _reports_signing_secret() -> str:
    # Prefer a dedicated secret so outbound report-share links can be rotated independently.
    return os.getenv("OUTBOUND_REPORTS_SIGNING_SECRET") or _signing_secret()


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


# --- T2-1: stable click-id join keys on external redirects -------------------
# The click id minted at redirect-build time must survive to two places:
#   1. the surface_click_events row (carried in the signed token ctx), and
#   2. the merchant's order (carried onto the destination URL).
# For Shopify destinations a cart permalink carries the id as a cart attribute,
# which Shopify persists into the order's note_attributes with zero merchant
# setup (order-side join, closable by T2-2). Non-Shopify destinations get the id
# as a plain query param for click-side attribution only (honest degradation).

# Public: the execution spec has to tell an agent WHICH carrier holds the join key on the URL
# it was handed. `attributes[...]` for a cart, REFERRAL_CLICK_PARAM for a referral — naming
# the wrong one sends the agent looking for a key that is not in the string.
SHOPIFY_CART_CLICK_ATTRIBUTE = "attributes[pivota_click_id]"
_SHOPIFY_CART_CLICK_ATTRIBUTE = SHOPIFY_CART_CLICK_ATTRIBUTE  # back-compat alias
REFERRAL_CLICK_PARAM = "pvt_click_id"


def normalize_shop_host(value: Optional[str]) -> str:
    """Reduce a shop domain / full URL to a bare lowercase host (no scheme/path/port)."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    raw = raw.split("@")[-1]
    raw = raw.split(":")[0]
    return raw.strip().strip(".")


def extract_shopify_numeric_variant_id(raw: Optional[str]) -> Optional[str]:
    """Return the numeric Shopify variant id for a Shopify variant reference, else None.

    Accepts a bare numeric id or a ``gid://shopify/ProductVariant/<n>`` form. A
    non-numeric SKU string yields None — we never fabricate a variant id for the
    cart permalink.
    """
    s = str(raw or "").strip()
    if not s:
        return None
    match = re.search(r"gid://shopify/ProductVariant/(\d+)", s)
    if match:
        return match.group(1)
    if s.isdigit():
        return s
    return None


def _append_query_fragment(url: str, fragment: str) -> str:
    if not fragment:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{fragment}"


def append_shopify_cart_click_attribute(url: str, click_id: str) -> str:
    """Append the order-surviving cart attribute. Brackets are kept literal per
    Shopify's documented cart-permalink form (``attributes[key]=value``)."""
    cid = str(click_id or "").strip()
    if not cid:
        return url
    return _append_query_fragment(url, f"{_SHOPIFY_CART_CLICK_ATTRIBUTE}={quote(cid, safe='')}")


def append_referral_click_param(url: str, click_id: str) -> str:
    """Append the click id to a non-Shopify destination.

    Two carriers, both appended:
      - ``pvt_click_id`` — Pivota's own click-side attribution param (legacy carrier).
      - ``utm_content``  — WooCommerce 8.5+ core Order Attribution persists standard
        ``utm_*`` landing params onto the order as ``_wc_order_attribution_utm_*``
        meta, which gives referral_only destinations an ORDER-SIDE join after all
        (T2-2c Woo closure polls orders and recovers the click id from that meta).
        Only added when the URL does not already carry a ``utm_content`` (a
        merchant-configured utm_template wins).
    """
    cid = str(click_id or "").strip()
    if not cid:
        return url
    out = _append_query_fragment(url, f"{REFERRAL_CLICK_PARAM}={quote(cid, safe='')}")
    if "utm_content=" not in out:
        out = _append_query_fragment(out, f"utm_content={quote(cid, safe='')}")
    return out


def shopify_cart_base_url(
    *, shop_domain: Optional[str], variant_id: Optional[str], quantity: int = 1
) -> Optional[str]:
    """Build the Shopify cart-permalink base (no query) when a shop host AND a numeric
    variant id are both known; otherwise None. Never fabricates a variant id."""
    host = normalize_shop_host(shop_domain)
    numeric_variant = extract_shopify_numeric_variant_id(variant_id)
    if not host or not numeric_variant:
        return None
    try:
        qty = max(1, int(quantity or 1))
    except (TypeError, ValueError):
        qty = 1
    return f"https://{host}/cart/{numeric_variant}:{qty}"


def build_shopify_cart_permalink(
    *, shop_domain: Optional[str], variant_id: Optional[str], click_id: str, quantity: int = 1
) -> Optional[str]:
    """Full Shopify cart permalink carrying the pivota click id as an order-surviving
    attribute, or None when a cart permalink cannot be built (missing host /
    non-numeric variant id)."""
    base = shopify_cart_base_url(shop_domain=shop_domain, variant_id=variant_id, quantity=quantity)
    if not base:
        return None
    return append_shopify_cart_click_attribute(base, click_id)


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
        "t": "redirect",
        **payload,
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    payload_b64 = _b64url_encode(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(_signing_secret().encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def parse_redirect_token_verified(token: str) -> tuple[Dict[str, Any], bool]:
    """
    Verify signature/shape and return ``(payload, is_expired)`` WITHOUT raising on expiry.

    Attributed-redirect lane D3: /r links are embedded in agent-platform product feeds that
    outlive the token TTL in caches. An EXPIRED-but-VALIDLY-SIGNED token must still resolve to
    its destination (the /r route degrades to a plain 302 without click logging) rather than
    dead-ending a cached feed link on an error page. Signature/shape failures still raise.
    """
    if not token or "." not in token:
        raise ValueError("INVALID_TOKEN")
    payload_b64, sig_b64 = token.split(".", 1)
    expected = hmac.new(_signing_secret().encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_encode(expected), sig_b64):
        raise ValueError("INVALID_SIGNATURE")
    payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("INVALID_TOKEN")
    if payload.get("t") not in (None, "redirect"):
        raise ValueError("INVALID_TOKEN")
    exp = int(payload.get("exp") or 0)
    is_expired = bool(exp and _now_ts() > exp)
    return payload, is_expired


def parse_and_verify_redirect_token(token: str) -> Dict[str, Any]:
    payload, is_expired = parse_redirect_token_verified(token)
    if is_expired:
        raise ValueError("TOKEN_EXPIRED")
    return payload


def make_report_token(payload: Dict[str, Any], ttl_seconds: int = 30 * 24 * 3600) -> str:
    now = _now_ts()
    body = {
        "v": 0,
        "t": "report",
        **payload,
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    payload_b64 = _b64url_encode(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(_reports_signing_secret().encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def parse_and_verify_report_token(token: str) -> Dict[str, Any]:
    if not token or "." not in token:
        raise ValueError("INVALID_TOKEN")
    payload_b64, sig_b64 = token.split(".", 1)
    expected = hmac.new(_reports_signing_secret().encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_encode(expected), sig_b64):
        raise ValueError("INVALID_SIGNATURE")
    payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("t") != "report":
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
    # Use a DB-side "now" to avoid tz-aware/naive mismatches across deployments where
    # outbound_link_rules.start_at/end_at may have been created as TIMESTAMP or TIMESTAMPTZ.
    now = func.now()
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


def _domain_matches(dest_domain: str, allowed_domain: str) -> bool:
    d = str(dest_domain or "").strip().lower().lstrip(".")
    a = str(allowed_domain or "").strip().lower().lstrip(".")
    if not d or not a:
        return False
    return d == a or d.endswith(f".{a}")


def _normalize_allowed_domains(rows: Any) -> list[str]:
    allowed = [str(dict(r).get("domain") or "").strip().lower() for r in (rows or [])]
    return [d for d in allowed if d]


def is_destination_domain_allowed(*, destination_url: str, allowed_domains: Optional[List[str]]) -> bool:
    """
    Evaluate destination domain against a preloaded allowlist.

    Backward-compatible behavior:
    - Empty/None allowlist => allow all valid domains.
    """
    dest_domain = url_domain(destination_url).strip().lower()
    if not dest_domain:
        return False
    normalized = [str(d or "").strip().lower() for d in (allowed_domains or []) if str(d or "").strip()]
    if not normalized:
        return True
    return any(_domain_matches(dest_domain, a) for a in normalized)


def is_domain_allowed_for_market_domains(*, destination_url: str, allowed_domains: Optional[list[str]]) -> bool:
    # Backward-compatible wrapper.
    return is_destination_domain_allowed(
        destination_url=destination_url,
        allowed_domains=[str(d) for d in (allowed_domains or [])],
    )


def _normalize_market_key(market: str) -> str:
    return str(market or "").strip().upper() or "US"


async def _fetch_allowed_domains_for_market(cache_market: str) -> List[str]:
    """
    Return active allowlist domains for a market.

    Backward-compatible behavior:
    - Missing allowlist table => return [] (interpreted as allow-all by callers).
    """
    try:
        rows = await database.fetch_all(
            select(outbound_link_allowed_domains.c.domain)
            .where(
                and_(
                    outbound_link_allowed_domains.c.market == cache_market,
                    outbound_link_allowed_domains.c.status == "active",
                )
            )
            .order_by(outbound_link_allowed_domains.c.domain.asc())
        )
    except Exception as exc:
        msg = str(exc)
        if "outbound_link_allowed_domains" in msg and ("does not exist" in msg or "UndefinedTable" in msg):
            return []
        raise
    return _normalize_allowed_domains(rows)


async def get_allowed_domains_for_market(*, market: str) -> List[str]:
    now_ms = int(time.time() * 1000)
    cache_market = _normalize_market_key(market)
    cached = _ALLOWED_DOMAIN_CACHE.get(cache_market)
    if cached and cached[0] > now_ms:
        return list(cached[1])

    inflight = _ALLOWED_DOMAIN_INFLIGHT.get(cache_market)
    if inflight is not None:
        return list(await inflight)

    async def _load() -> List[str]:
        domains = await _fetch_allowed_domains_for_market(cache_market)
        _ALLOWED_DOMAIN_CACHE[cache_market] = (
            int(time.time() * 1000) + ALLOWED_DOMAIN_CACHE_TTL_MS,
            list(domains),
        )
        return list(domains)

    task = asyncio.create_task(_load())
    _ALLOWED_DOMAIN_INFLIGHT[cache_market] = task
    try:
        return list(await task)
    finally:
        if _ALLOWED_DOMAIN_INFLIGHT.get(cache_market) is task:
            _ALLOWED_DOMAIN_INFLIGHT.pop(cache_market, None)


async def get_active_allowed_domains_for_market(*, market: str) -> list[str]:
    # Backward-compatible wrapper.
    return list(await get_allowed_domains_for_market(market=market))


async def _is_domain_allowed(*, market: str, destination_url: str) -> bool:
    """
    Backward compatible:
    - If allowlist is empty for this market, allow everything.
    - If allowlist has at least 1 active entry, enforce it.
    """
    dest_domain = url_domain(destination_url).strip().lower()
    if not dest_domain:
        return False

    allowed = await get_allowed_domains_for_market(market=market)
    return is_destination_domain_allowed(
        destination_url=destination_url,
        allowed_domains=allowed,
    )


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
    matched_scope_id: Optional[str] = None
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
            matched_scope_id = sid_norm
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
        **({ "scope": matched_scope } if matched_scope else {}),
        **({ "scope_id": matched_scope_id } if matched_scope_id else {}),
        **({ "rule_id": str(matched.get("id")) } if matched.get("id") else {}),
    }
    attribution = materialize_attribution_context(
        {
            **context,
            **candidates,
        },
        default_surface=str(context.get("surface") or tool or ""),
        merchant_id=str(context.get("merchantId") or context.get("merchant_id") or "").strip() or None,
    )
    dest_with_utm = apply_utm(dest, utm_template, tokens)
    dest_with_tracking = apply_pvt_params(dest_with_utm, attribution)

    if not await _is_domain_allowed(market=market, destination_url=dest_with_tracking):
        raise ValueError("DOMAIN_NOT_ALLOWED")

    token_payload = {
        "market": market,
        "tool": tool,
        "ruleId": matched.get("id"),
        "dest": dest_with_tracking,
        "ctx": {
            **({ "jobId": str(context.get("jobId")) } if context.get("jobId") else {}),
            **({ "sessionId": str(context.get("sessionId")) } if context.get("sessionId") else {}),
            **({ "merchantId": str(context.get("merchantId") or context.get("merchant_id")) } if (context.get("merchantId") or context.get("merchant_id")) else {}),
            **({ "skuId": sku_id } if sku_id else {}),
            **({ "brand": brand } if brand else {}),
            **({ "category": category } if category else {}),
            **({ "area": str(context.get("area")) } if context.get("area") else {}),
            **({ "kind": str(context.get("kind")) } if context.get("kind") else {}),
            **({ "scope": matched_scope } if matched_scope else {}),
            **({ PVT_SURFACE: attribution.get(PVT_SURFACE) } if attribution.get(PVT_SURFACE) else {}),
            **({ PVT_CLICK_ID: attribution.get(PVT_CLICK_ID) } if attribution.get(PVT_CLICK_ID) else {}),
            **({ PVT_PRODUCT_ID: attribution.get(PVT_PRODUCT_ID) } if attribution.get(PVT_PRODUCT_ID) else {}),
            **({ PVT_VARIANT_ID: attribution.get(PVT_VARIANT_ID) } if attribution.get(PVT_VARIANT_ID) else {}),
            **({ PVT_PROMPT_CLUSTER: attribution.get(PVT_PROMPT_CLUSTER) } if attribution.get(PVT_PROMPT_CLUSTER) else {}),
            **({ "source_channel": attribution.get("source_channel") } if attribution.get("source_channel") else {}),
            **({ "source_family": attribution.get("source_family") } if attribution.get("source_family") else {}),
            **({ "query_source": attribution.get("query_source") } if attribution.get("query_source") else {}),
            **({ "agent_id": attribution.get("agent_id") } if attribution.get("agent_id") else {}),
            **({ "protocol_name": attribution.get("protocol_name") } if attribution.get("protocol_name") else {}),
            **({ "commerce_surface": attribution.get("commerce_surface") } if attribution.get("commerce_surface") else {}),
            **({ "llm_provider": attribution.get("llm_provider") } if attribution.get("llm_provider") else {}),
            **({ "llm_model": attribution.get("llm_model") } if attribution.get("llm_model") else {}),
            **({ "caller_id": attribution.get("caller_id") } if attribution.get("caller_id") else {}),
        },
    }

    token = make_redirect_token(token_payload)
    base = str(request_base_url or "").rstrip("/")
    redirect_url = f"{base}/r?token={token}"

    return ResolvedLink(
        destination_url=dest_with_tracking,
        redirect_url=redirect_url,
        purchase_enabled=purchase_enabled,
        purchase_enabled_override=purchase_override,
        rule_id=matched.get("id"),
        disclosure_text=disclosure_text,
        partner_type=partner_type,
    )


async def log_outbound_event(*, token_payload: Dict[str, Any], request_meta: Dict[str, Any], event_type: str) -> None:
    """
    Best-effort outbound telemetry.

    We store `eventType` under the JSONB `context` field to avoid schema changes and keep
    existing `area`/`kind` semantics (UI surface classification).
    """
    ctx = token_payload.get("ctx") if isinstance(token_payload.get("ctx"), dict) else {}
    ctx2 = dict(ctx or {})
    if event_type and not ctx2.get("eventType"):
        ctx2["eventType"] = str(event_type)

    dest = str(token_payload.get("dest") or "")
    row = {
        "market": str(token_payload.get("market") or ""),
        "tool": str(token_payload.get("tool") or ""),
        "rule_id": str(token_payload.get("ruleId") or "") or None,
        "job_id": str(ctx2.get("jobId") or "") or None,
        "session_id": str(ctx2.get("sessionId") or "") or None,
        "sku_id": str(ctx2.get("skuId") or "") or None,
        "brand": str(ctx2.get("brand") or "") or None,
        "category": str(ctx2.get("category") or "") or None,
        "area": str(ctx2.get("area") or "") or None,
        "kind": str(ctx2.get("kind") or "") or None,
        "dest_domain": url_domain(dest) or None,
        "destination_url": dest or None,
        "context": ctx2 or None,
        "user_agent": request_meta.get("user_agent"),
        "ip": request_meta.get("ip"),
    }
    try:
        await database.execute(outbound_click_events.insert(), row)
    except Exception:
        # Best-effort: never break redirect or callers.
        pass
    try:
        await record_surface_event(
            token_payload={
                **token_payload,
                "dest_domain": url_domain(dest) or None,
            },
            request_meta=request_meta,
            event_type=event_type,
        )
    except Exception:
        return


async def log_outbound_click(*, token_payload: Dict[str, Any], request_meta: Dict[str, Any]) -> None:
    await log_outbound_event(token_payload=token_payload, request_meta=request_meta, event_type="click")

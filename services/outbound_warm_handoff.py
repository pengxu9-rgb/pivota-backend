"""Warm-handoff click lane — eligibility + gateway resolve for the public ``GET /r`` redirect.

Phase 1 of ``Pivota_Warm_Handoff_Click_Lane_Spec_2026-07-22.md``. At click time (the shopper
clicked a Pivota attributed-redirect link inside a third-party agent UI), an eligible cold
brand redirect is upgraded to a PRE-BUILT cart on the brand's own Shopify checkout by calling
the PIVOTA-Agent gateway's internal resolve endpoint. Everything here is best-effort and
fail-open to the cold redirect:

- eligibility is decided locally (flag, canary allowlist / pct rollout, affiliate denylist,
  bot/prefetch hygiene) — cheap, no network;
- the gateway call is bounded by a hard timeout and ANY miss returns ``None``;
- a per-token memo means agent-platform prefetch + the real human click build ONE cart, and
  the human click 302s instantly off the memo.

HARD BOUNDS (inherited from the gateway lane): cart-build + continue_url only. Never
complete_checkout, never payment, never opening the continue_url server-side.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("outbound_warm_handoff")

# Affiliate/redirector networks are NEVER warm-handed (founder decision, spec §4.1): a warm
# handoff on an affiliate link bypasses the network and forfeits the commission — and the
# destination host isn't the brand anyway. Small, static, suffix-matched.
AFFILIATE_HOST_SUFFIXES: Tuple[str, ...] = (
    "linksynergy.com",
    "rakuten.com",
    "rakutenadvertising.com",
    "shareasale.com",
    "skimresources.com",
    "awin1.com",
    "go2cloud.org",
    "impact.com",
    "prf.hn",
    "sjv.io",
    "pxf.io",
)

# Prefetchers / link-preview bots (agent platforms fetch /r before any human clicks). A bot
# gets today's cold redirect — no cart is ever built for a prefetch. Marker-matched on a
# lowercased User-Agent. Human click-outs arrive with the human's own browser UA.
BOT_UA_MARKERS: Tuple[str, ...] = (
    "bot",
    "crawler",
    "spider",
    "preview",
    "prefetch",
    "facebookexternalhit",
    "slackbot",
    "discord",
    "telegram",
    "whatsapp",
    "skypeuripreview",
    "embedly",
    "curl/",
    "wget/",
    "python-requests",
    "python-httpx",
    "go-http-client",
    "headlesschrome",
    "chatgpt-user",
    "oai-searchbot",
    "gptbot",
    "claudebot",
    "claude-web",
    "anthropic",
    "perplexity",
    "gemini-deep-research",
    "google-extended",
    "googleother",
)

_HANDLE_RE = re.compile(r"/products/([a-z0-9][a-z0-9\-_.]*)", re.IGNORECASE)

# Per-token memo: prefetch + human click share ONE resolution (and one cart). Bounded,
# TTL'd, in-process — parity with the gateway lane's own cache posture.
_MEMO_TTL_SECONDS = 600.0
_MEMO_MAX_ENTRIES = 500
_memo: "OrderedDict[str, Tuple[float, Optional[Dict[str, Any]]]]" = OrderedDict()


def _now() -> float:
    return time.monotonic()


def _memo_key(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:32]


def memo_get(token: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """(hit, value) — value may be None (a memoized miss)."""
    key = _memo_key(token)
    entry = _memo.get(key)
    if entry is None:
        return False, None
    expires_at, value = entry
    if _now() >= expires_at:
        _memo.pop(key, None)
        return False, None
    return True, value


def memo_set(token: str, value: Optional[Dict[str, Any]]) -> None:
    key = _memo_key(token)
    _memo.pop(key, None)
    _memo[key] = (_now() + _MEMO_TTL_SECONDS, value)
    while len(_memo) > _MEMO_MAX_ENTRIES:
        _memo.popitem(last=False)


def memo_clear() -> None:  # test hook
    _memo.clear()


def _host_of(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        host = (urlparse(raw).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def is_affiliate_destination(dest: str) -> bool:
    host = _host_of(dest)
    if not host:
        return False
    return any(_host_matches(host, s) for s in AFFILIATE_HOST_SUFFIXES)


def is_bot_user_agent(user_agent: Optional[str]) -> bool:
    ua = str(user_agent or "").strip().lower()
    if not ua:
        return True  # no UA at all — treat as non-human, cold redirect
    return any(marker in ua for marker in BOT_UA_MARKERS)


def rollout_bucket(token: str, pct: int) -> bool:
    """Stable hash-of-token assignment so prefetch + click land in the same bucket."""
    p = max(0, min(100, int(pct or 0)))
    if p <= 0:
        return False
    if p >= 100:
        return True
    digest = hashlib.sha256(str(token or "").encode("utf-8")).digest()
    return (int.from_bytes(digest[:4], "big") % 100) < p


def extract_product_handle(dest: str) -> Optional[str]:
    try:
        path = urlparse(str(dest or "")).path or ""
    except Exception:
        return None
    match = _HANDLE_RE.search(path)
    return match.group(1) if match else None


def evaluate_warm_eligibility(
    *,
    dest: str,
    user_agent: Optional[str],
    token: str,
    settings: Any,
) -> Tuple[bool, str]:
    """Local, no-network eligibility for a warm-handoff attempt. Returns (eligible, reason).

    Ordering is deliberate: cheap knockouts first, then the canary allowlist / pct rollout.
    The caller has already handled flag-off, expired tokens, and HEAD requests.
    """
    if not str(settings.outbound_warm_handoff_internal_key or "").strip():
        return False, "no_internal_key"
    host = _host_of(dest)
    if not host:
        return False, "no_dest_host"
    if is_affiliate_destination(dest):
        return False, "affiliate"
    if is_bot_user_agent(user_agent):
        return False, "bot"
    brands = settings.outbound_warm_handoff_brands
    if brands:
        if any(_host_matches(host, b) for b in brands):
            return True, "allowlisted"
        return False, "not_allowlisted"
    if rollout_bucket(token, settings.outbound_warm_handoff_rollout_pct):
        return True, "rollout"
    return False, "control"


def _validate_continue_url(continue_url: str, brand_host: str) -> bool:
    """The 302 target must be the brand's own storefront: https, and the host is either the
    brand domain (suffix match) or a *.myshopify.com storefront. Anything else is refused —
    the gateway is trusted infrastructure, but a redirect target gets belt-and-braces."""
    raw = str(continue_url or "")
    # Reject authority-confusion payloads BEFORE trusting urlparse's hostname: urlparse and
    # the browser's WHATWG parser disagree on '\' (urlparse("https://evil.com\\@good.com")
    # yields hostname good.com while a browser navigates to evil.com), and a real Shopify
    # cart URL never carries backslashes, whitespace, control chars, or userinfo.
    if "\\" in raw or any(ch.isspace() or ord(ch) < 0x20 for ch in raw):
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    if "@" in (parsed.netloc or ""):
        return False  # userinfo in a cart URL is never legitimate
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    bare = host[4:] if host.startswith("www.") else host
    if brand_host and _host_matches(bare, brand_host):
        return True
    # Reverse direction (dest host was a subdomain of the cart host, e.g. dest shop.cosrx.com
    # -> cart cosrx.com): require the cart host to be a real registrable domain (>= 2 labels)
    # so a bare public suffix like "com" can never validate.
    if brand_host and "." in bare and _host_matches(brand_host, bare):
        return True
    return bare.endswith(".myshopify.com")


async def resolve_warm_handoff(
    *,
    dest: str,
    ctx: Optional[Dict[str, Any]],
    settings: Any,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Dict[str, Any]]:
    """Call the gateway's internal resolve endpoint. Returns {continue_url, cart_id} or None.

    Best-effort by contract: timeouts, non-200s, invalid bodies, and off-brand continue_urls
    all resolve to None so the caller cold-redirects.
    """
    brand_host = _host_of(dest)
    if not brand_host:
        return None
    context = ctx if isinstance(ctx, dict) else {}
    payload: Dict[str, Any] = {
        "brand_domain": brand_host,
        "product_url": str(dest or ""),
    }
    handle = extract_product_handle(dest)
    if handle:
        payload["product_handle"] = handle
    variant_hint = str(context.get("shopify_variant_id") or "").strip()
    if variant_hint:
        payload["variant_id"] = variant_hint
    click_id = str(context.get("pvt_click_id") or "").strip()
    if click_id:
        # Threaded into the UCP create_cart `attribution` arg — whether Shopify persists it
        # onto the order is the spec's Phase 0 empirical question; passing it costs nothing.
        payload["attribution"] = {"pivota_click_id": click_id}

    # A shopper is waiting on the 302: `total_deadline` is a TRUE wall-clock ceiling via
    # asyncio.wait_for (httpx.Timeout alone is per-phase — connect+read could stack past it).
    # Deliberately NO tighter connect sub-cap: wait_for already bounds the whole call, and a
    # 1.0s connect cap proved flaky on high-RTT paths (TLS to the EU gateway from Asia
    # exceeds 1s — E2E finding 2026-07-22) while adding nothing that wait_for doesn't.
    total_deadline = float(settings.outbound_warm_handoff_timeout_seconds or 2.5)
    timeout = httpx.Timeout(total_deadline)
    headers = {"X-Internal-Key": str(settings.outbound_warm_handoff_internal_key or "")}

    async def _post() -> Any:
        if client is not None:
            return await client.post(
                settings.outbound_warm_handoff_resolve_url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        async with httpx.AsyncClient(timeout=timeout) as owned:
            return await owned.post(
                settings.outbound_warm_handoff_resolve_url,
                json=payload,
                headers=headers,
            )

    try:
        response = await asyncio.wait_for(_post(), timeout=total_deadline)
    except Exception as exc:  # noqa: BLE001 — the click path must never break on this lane
        logger.info("warm_handoff resolve call failed host=%s: %s", brand_host, str(exc)[:200])
        return None

    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    continue_url = str(body.get("continue_url") or "").strip()
    if not continue_url or not _validate_continue_url(continue_url, brand_host):
        return None
    return {
        "continue_url": continue_url,
        "cart_id": str(body.get("cart_id") or "").strip() or None,
    }

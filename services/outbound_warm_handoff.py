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

# A warm handoff may only 302 to a CART or CHECKOUT. Measured against the shapes the gateway
# actually returns (PIVOTA-Agent `extractHandoffUrl` yields the merchant's UCP
# continue_url | checkout_url | permalink | url), BOTH families are legitimate and live:
# Shopify's `/cart/c/<token>` and `/cart/{variant}:{qty}` AND its `/checkouts/<token>`. A
# `/cart`-only rule would have broken the legitimate PDP-upgrade path on every storefront
# that answers with the checkout form.
#
# Matched on whole, lowercased path SEGMENTS, and on ANY segment rather than only the first,
# so a locale-prefixed `/en/cart/c/abc` still passes. Segment EQUALITY is what keeps
# any-position matching safe: a PDP like `/products/cart-organizer` has no segment equal to
# "cart", and the homepage, a 404 page, and an unrelated PDP all have none either.
# A PREFILLED cart always names WHAT is in it — `/cart/c/<token>`, `/cart/{variant}:{qty}`,
# `/checkouts/<token>`. So a cart segment must be FOLLOWED by at least one more segment. That
# is what separates a prefilled cart from the storefront's EMPTY cart page (`/cart`), a bare
# `/checkout`, and pages that merely end in the word: `/products/cart`, `/pages/cart`,
# `/blogs/news/cart`. Host+scheme alone accepted all of those, and 302-ing a shopper off a
# correct PDP onto an empty cart is a wrong landing, not merely a missed upgrade.
#
# KNOWN NARROW, deliberately. Non-Shopify and localized carts do NOT match and will fall back
# to the cold PDP: Wix `/cart-page`, BigCommerce `/cart.php`, Salesforce `Cart-Show`,
# `/basket/...`, `/panier`, `/warenkorb`, `/carrito`, and any %2F-encoded path. That costs an
# upgrade, never a wrong landing, and every currently allowlisted brand is Shopify. Widen this
# set from OBSERVED gateway responses when a non-Shopify merchant is allowlisted — never
# speculatively, since every addition widens what we are willing to redirect a shopper to.
_CART_PATH_SEGMENTS = frozenset({"cart", "carts", "checkout", "checkouts"})


def _path_of(url: str) -> str:
    try:
        return urlparse(str(url or "")).path or ""
    except Exception:
        return ""


def _is_cart_shaped_path(path: str) -> bool:
    segments = [seg for seg in str(path or "").split("/") if seg]
    # Matched on ANY position, not just the first, so a locale prefix (`/en/cart/c/abc`)
    # passes. Segment EQUALITY keeps that safe: `/products/cart-organizer` has no segment
    # equal to "cart". `[:-1]` is the "must be followed by something" rule above.
    return any(seg.lower() in _CART_PATH_SEGMENTS for seg in segments[:-1])


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


def is_already_cart_join(ctx: Optional[Dict[str, Any]]) -> bool:
    """True when the signed token says its `dest` is ALREADY a prefilled cart permalink.

    `_make_external_redirect_url` (routes/agent_shop_gateway) decides this once, at mint time,
    from `resolve_cart_permalink`, and stamps the answer into the token ctx as `join_mode`.
    Reading it back here is exact and free: it is the mint-time truth, it rides inside the
    HMAC-signed payload (so a click cannot forge it), and it needs no new data on the wire.

    Deliberately keyed on `join_mode` rather than sniffing `/cart/` out of the dest path: the
    two can only agree by luck, and the token already carries the authoritative answer.
    """
    if not isinstance(ctx, dict):
        return False
    return str(ctx.get("join_mode") or "").strip().lower() == "cart_permalink"


def evaluate_warm_eligibility(
    *,
    dest: str,
    user_agent: Optional[str],
    token: str,
    ctx: Optional[Dict[str, Any]],
    settings: Any,
    assume_human: bool = False,
) -> Tuple[bool, str]:
    """Local, no-network eligibility for a warm-handoff attempt. Returns (eligible, reason).

    Ordering is deliberate: cheap knockouts first, then the canary allowlist / pct rollout.
    The caller has already handled flag-off, expired tokens, and HEAD requests.

    ``assume_human`` skips the user-agent knockout for callers that do not have one — see
    ``could_upgrade_at_click_time``. The click path never passes it (default False).

    `ctx` is the signed token's context and is REQUIRED — no default. A defaulted one makes
    OMISSION silent: a new call site (or a deleted line) would simply stop knocking out
    already-prefilled carts with nothing failing, which is precisely the regression this
    parameter exists to prevent. Requiring it turns that into a TypeError the suite catches.
    """
    if not str(settings.outbound_warm_handoff_internal_key or "").strip():
        return False, "no_internal_key"
    host = _host_of(dest)
    if not host:
        return False, "no_dest_host"
    if is_affiliate_destination(dest):
        return False, "affiliate"
    if not assume_human and is_bot_user_agent(user_agent):
        return False, "bot"
    brands = settings.outbound_warm_handoff_brands
    if brands:
        if not any(_host_matches(host, b) for b in brands):
            return False, "not_allowlisted"
        eligible_reason = "allowlisted"
    elif rollout_bucket(token, settings.outbound_warm_handoff_rollout_pct):
        eligible_reason = "rollout"
    else:
        return False, "control"

    # ALREADY-A-CART knockout, deliberately LAST.
    #
    # The lane's whole purpose is upgrading a COLD PDP redirect into a prefilled cart. When
    # `dest` is already a cart there is nothing to upgrade, and attempting it is strictly
    # destructive: the rebuild request carries NO product identity (a `/cart` path cannot
    # match `_HANDLE_RE`, and no variant id is ever stamped into the token ctx — see the
    # provenance note on `_make_external_redirect_url`'s `cart_variant_id`), so the gateway
    # answers from `brand_domain` alone. A correct cart, for the right variant, carrying the
    # `attributes[pivota_click_id]` order-side attribution join, would be replaced by whatever
    # that identity-less request returned — plausibly a different product, join discarded.
    #
    # LAST, not first, so `warm_reason=already_cart` counts ONLY clicks that would otherwise
    # have been warmed. Ordered first it also swallowed bots/prefetch, non-allowlisted hosts
    # and affiliate links — none of which were ever at risk — which would have made the
    # rollout dial (see the runbook) read high for reasons that are not this defect. Behaviour
    # is identical either way; every one of those paths is ineligible regardless.
    #
    # TWO independent signals, OR-ed, because neither alone is sufficient:
    #
    #   * `join_mode` is the mint-time answer from `resolve_cart_permalink` and rides inside
    #     the HMAC, so it cannot be forged — but it records "WE BUILT a cart", not "dest IS a
    #     cart". A `destination_url` that was ALREADY a cart while no variant id could be
    #     recovered mints `referral_only` with a cart dest, and reading `join_mode` alone
    #     misses it. Four other token minters make this worse: outbound_links_service and
    #     employee_products omit `join_mode` entirely, while agent_api and agent_sdk_fixed
    #     HARDCODE `referral_only` regardless of dest — and all of them reach this same
    #     public `/r` route.
    #   * the dest PATH shape catches every one of those, whatever minted the token, and
    #     costs nothing on the legitimate population: a `/products/...` PDP has no cart
    #     segment, so this can only ever fire on something that is already a cart.
    #
    # Measured on prod 2026-08-24: ZERO of 11,352 active seeds and ZERO of 4,200
    # outbound_link_rules currently carry a cart-shaped destination — so the path arm is
    # latent today. It is here because it is free, and because the four minters above mean a
    # single future writer of a cart-shaped destination_url would silently reopen the defect.
    if is_already_cart_join(ctx) or _is_cart_shaped_path(_path_of(dest)):
        return False, "already_cart"
    return True, eligible_reason


def could_upgrade_at_click_time(
    *, dest: str, token: str, ctx: Optional[Dict[str, Any]], settings: Any
) -> bool:
    """Can a click on this ALREADY-MINTED redirect still be upgraded to a prefilled cart?

    Answered at RESOLVE time, before the link has been handed to anyone, so `offers.resolve`
    can avoid making a `cart_prefilled: false` claim this lane would later contradict (an
    answer already sent to an agent cannot be corrected). See
    docs/runbooks/outbound_warm_handoff_rollout.md.

    A SOUND OVER-APPROXIMATION, deliberately: `False` here guarantees the click path also
    says ineligible, while `True` only means "possibly". Every input to
    `evaluate_warm_eligibility` is available at mint time — dest host, flag, internal key,
    brand allowlist, rollout pct, and the token itself (`rollout_bucket` is a stable hash of
    it, which is exactly why prefetch and click land in the same bucket) — except the
    user-agent, and that one can only ever REMOVE eligibility (a bot gets the cold redirect).
    So assuming a human cannot lose a case; it can only over-report.

    Deliberately calls the SAME evaluate_warm_eligibility the click path calls rather than
    re-deriving the rules: a second implementation of an eligibility predicate is exactly the
    twin-implementation drift resolve_cart_permalink was extracted to avoid, and here a drift
    would silently restore the false claim this exists to prevent.

    NOT a promise the upgrade will happen: even an eligible click can fall back to the cold
    redirect (gateway timeout, non-200, off-brand continue_url, bot UA). That is precisely
    why the caller answers "unknown" rather than flipping the claim to `true`.

    Env is read at CLICK time, so this is only sound for the config as of THIS call: widening
    the allowlist retroactively falsifies `false` answers on tokens still inside their TTL
    (Constraint 2 in the runbook).
    """
    # The click lane checks the flag in the route, before eligibility is ever consulted.
    if not getattr(settings, "outbound_warm_handoff_enabled", False):
        return False
    eligible, _reason = evaluate_warm_eligibility(
        dest=dest,
        user_agent=None,
        token=token,
        ctx=ctx,
        settings=settings,
        assume_human=True,
    )
    return eligible


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
    # The 302 target must be a CART/CHECKOUT, not merely on the right host. Host+scheme alone
    # accepted the brand homepage, a 404 page, and an unrelated PDP — so a gateway answer that
    # resolved to none of the intended thing still replaced the shopper's destination. Failing
    # this check is SAFE BY CONSTRUCTION: the caller falls back to the cold `dest`, which is
    # the correct product page, so a shape we fail to recognise costs an upgrade, never a
    # wrong landing.
    if not _is_cart_shaped_path(parsed.path):
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
    # NO VARIANT HINT IS SENT, AND NONE CAN BE. This used to read
    # `ctx["shopify_variant_id"]` — a key NOTHING has ever written into a redirect token.
    # `_make_external_redirect_url` carries the recovered numeric Shopify variant id on
    # `cart_variant_id`, a channel only the cart-permalink construction reads, and stamping it
    # into the token ctx is FORBIDDEN by the round-4 review of #1813: attribution cross-fills
    # product<->variant ids both ways, so a numeric variant id there leaks up a grain into
    # surface_click_events.canonical_product_id. So the read was not a wiring gap to be
    # closed — it was a branch that could never fire, reading as though variant identity were
    # being passed when it never was. Removed rather than wired up. (The only product identity
    # this payload can carry is the `product_handle` set above, which needs a `/products/...`
    # dest — so a cart-permalink dest yields none at all. That is why the caller now refuses
    # to attempt one; see `is_already_cart_join`.)
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

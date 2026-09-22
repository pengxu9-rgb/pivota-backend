"""Warm-handoff click lane on the public ``GET /r`` redirect (Phase 1 of
``Pivota_Warm_Handoff_Click_Lane_Spec_2026-07-22.md``).

Covers: flag-off byte-identical behavior, canary allowlist / rollout / affiliate / bot
eligibility, warm 302 to the brand cart, cold fallback on an unresolved handoff, expired
tokens never warming, HEAD prefetch hygiene, per-token memo (prefetch + click = one
resolve), continue_url host validation, and the ctx instrumentation on the click event.
"""

from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from main import app
from config.settings import settings
import routes.outbound_links as outbound_routes
import services.outbound_warm_handoff as warm

HUMAN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BRAND_DEST = "https://www.cosrx.com/products/peptide-132-hair-home-care-kit"
CONTINUE_URL = "https://cosrx-renewal.myshopify.com/cart/c/abc123?key=k"


def _mint_token(dest: str = BRAND_DEST, ttl_seconds: int = 3600, ctx: Optional[Dict[str, Any]] = None) -> str:
    from services.outbound_links_service import make_redirect_token

    return make_redirect_token(
        {"market": "US", "tool": "*", "dest": dest, "ctx": {"pvt_click_id": "clk_test", **(ctx or {})}},
        ttl_seconds=ttl_seconds,
    )


@pytest.fixture(autouse=True)
def _lane_defaults(monkeypatch):
    """Every test starts flag-on with a canary allowlist + key; individual tests override.
    The memo is cleared so tests never share resolutions."""
    monkeypatch.setattr(settings, "outbound_warm_handoff_enabled", True)
    monkeypatch.setattr(settings, "outbound_warm_handoff_internal_key", "test-key")
    monkeypatch.setattr(settings, "outbound_warm_handoff_brands_raw", "cosrx.com")
    monkeypatch.setattr(settings, "outbound_warm_handoff_rollout_pct", 0)
    warm.memo_clear()
    yield
    warm.memo_clear()


def _spy_resolver(monkeypatch, result: Optional[Dict[str, Any]]):
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(outbound_routes, "resolve_warm_handoff", _fake)
    return calls


def _spy_logger(monkeypatch):
    logged = []

    async def _fake(**kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(outbound_routes, "log_outbound_click", _fake)
    return logged


def test_flag_off_is_byte_identical_cold_redirect(monkeypatch) -> None:
    monkeypatch.setattr(settings, "outbound_warm_handoff_enabled", False)
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    res = client.get(f"/r?token={_mint_token()}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == BRAND_DEST
    assert calls == [], "flag off must never attempt a warm handoff"
    assert len(logged) == 1
    assert "handoff" not in (logged[0]["token_payload"].get("ctx") or {}), (
        "flag off must not add lane fields to the click ctx"
    )


def test_warm_302_to_brand_cart_and_ctx_instrumentation(monkeypatch) -> None:
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL, "cart_id": "gid://shopify/Cart/abc"})
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    res = client.get(f"/r?token={_mint_token()}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == CONTINUE_URL
    assert len(calls) == 1
    assert calls[0]["ctx"]["pvt_click_id"] == "clk_test", (
        "the ROUTE must forward the real token ctx to the resolver — it is what carries the "
        "click id into the gateway's cart `attribution` arg"
    )
    ctx = logged[0]["token_payload"]["ctx"]
    assert ctx["handoff"] == "warm"
    assert ctx["warm_reason"] == "ok"
    assert ctx["pvt_click_id"] == "clk_test", "existing attribution ctx must survive enrichment"


def test_unresolved_handoff_falls_back_cold_with_reason(monkeypatch) -> None:
    _spy_resolver(monkeypatch, None)
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    res = client.get(f"/r?token={_mint_token()}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == BRAND_DEST
    ctx = logged[0]["token_payload"]["ctx"]
    assert ctx["handoff"] == "cold"
    assert ctx["warm_reason"] == "unresolved"


def test_not_allowlisted_brand_is_cold_without_resolver_call(monkeypatch) -> None:
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    token = _mint_token(dest="https://www.some-other-brand.com/products/thing")
    res = client.get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "https://www.some-other-brand.com/products/thing"
    assert calls == []
    assert logged[0]["token_payload"]["ctx"]["warm_reason"] == "not_allowlisted"


def test_affiliate_destination_never_warms(monkeypatch) -> None:
    monkeypatch.setattr(settings, "outbound_warm_handoff_brands_raw", "")
    monkeypatch.setattr(settings, "outbound_warm_handoff_rollout_pct", 100)
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    token = _mint_token(dest="https://click.linksynergy.com/deeplink?id=x&murl=https%3A%2F%2Fcosrx.com")
    res = client.get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 302
    assert calls == [], "affiliate destinations forfeit commission if warmed — never attempt"
    assert logged[0]["token_payload"]["ctx"]["warm_reason"] == "affiliate"


def test_bot_user_agent_never_warms(monkeypatch) -> None:
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    res = client.get(
        f"/r?token={_mint_token()}",
        headers={"user-agent": "Mozilla/5.0 (compatible; ChatGPT-User/1.0; +https://openai.com/bot)"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert res.headers["location"] == BRAND_DEST
    assert calls == [], "prefetchers must never build carts"
    assert logged[0]["token_payload"]["ctx"]["warm_reason"] == "bot"


def test_missing_internal_key_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(settings, "outbound_warm_handoff_internal_key", None)
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    res = client.get(f"/r?token={_mint_token()}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == BRAND_DEST
    assert calls == []
    assert logged[0]["token_payload"]["ctx"]["warm_reason"] == "no_internal_key"


def test_expired_token_never_warms_and_never_logs(monkeypatch) -> None:
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    res = client.get(f"/r?token={_mint_token(ttl_seconds=-60)}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == BRAND_DEST
    assert calls == [], "no cart is ever built for a stale link"
    assert logged == [], "expired token must not log a click (existing D3 invariant)"


def test_head_request_never_warms_or_logs(monkeypatch) -> None:
    # The route is GET-only, so HEAD is a framework-level 405 with zero side effects —
    # prefetchers probing with HEAD can never build a cart or farm a click event.
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    res = client.head(f"/r?token={_mint_token()}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 405
    assert calls == []
    assert logged == []


def test_memo_dedupes_prefetch_and_click_into_one_resolve(monkeypatch) -> None:
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    _spy_logger(monkeypatch)
    client = TestClient(app)
    token = _mint_token()
    first = client.get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    second = client.get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert first.headers["location"] == CONTINUE_URL
    assert second.headers["location"] == CONTINUE_URL
    assert len(calls) == 1, "prefetch + human click must share one cart resolution"


def test_rollout_pct_control_bucket_is_cold(monkeypatch) -> None:
    monkeypatch.setattr(settings, "outbound_warm_handoff_brands_raw", "")
    monkeypatch.setattr(settings, "outbound_warm_handoff_rollout_pct", 0)
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    res = client.get(f"/r?token={_mint_token()}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == BRAND_DEST
    assert calls == []
    assert logged[0]["token_payload"]["ctx"]["warm_reason"] == "control"


# ---- unit tests on the service module (no route) ---------------------------------


def test_rollout_bucket_is_stable_per_token() -> None:
    token = "some.token"
    assert warm.rollout_bucket(token, 100) is True
    assert warm.rollout_bucket(token, 0) is False
    first = warm.rollout_bucket(token, 37)
    assert all(warm.rollout_bucket(token, 37) == first for _ in range(5))


@pytest.mark.parametrize(
    ("continue_url", "brand_host", "ok"),
    [
        (CONTINUE_URL, "cosrx.com", True),  # *.myshopify.com storefront
        ("https://www.cosrx.com/cart/c/x?key=k", "cosrx.com", True),  # brand's own domain
        ("http://cosrx-renewal.myshopify.com/cart/c/x", "cosrx.com", False),  # not https
        ("https://evil.example.com/cart", "cosrx.com", False),  # off-brand host
        ("", "cosrx.com", False),
        # Authority-confusion payloads: urlparse and the browser's WHATWG parser disagree on
        # '\' (urlparse sees cosrx.com; the browser navigates to evil.com) — must be rejected
        # before hostname is trusted.
        ("https://evil.com\\@cosrx-renewal.myshopify.com/cart/c/x", "cosrx.com", False),
        ("https://user@cosrx.com/cart/c/x", "cosrx.com", False),  # userinfo never legitimate
        ("https://cosrx.com/cart/c/x y", "cosrx.com", False),  # whitespace never legitimate
        ("https://com/cart", "cosrx.com", False),  # bare public suffix can never validate
    ],
)
def test_continue_url_host_validation(continue_url: str, brand_host: str, ok: bool) -> None:
    assert warm._validate_continue_url(continue_url, brand_host) is ok


def test_throwing_lane_degrades_to_cold_redirect_not_500(monkeypatch) -> None:
    # The whole flag-ON block is throw-guarded: an unexpected error inside the lane must
    # degrade to the cold 302, never a 500 on a real click.
    async def _boom(**kwargs):
        raise RuntimeError("unexpected lane failure")

    monkeypatch.setattr(outbound_routes, "resolve_warm_handoff", _boom)
    logged = _spy_logger(monkeypatch)
    client = TestClient(app)
    res = client.get(f"/r?token={_mint_token()}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == BRAND_DEST
    assert len(logged) == 1, "the click is still logged on a lane failure"


@pytest.mark.asyncio
async def test_resolve_warm_handoff_rejects_off_brand_continue_url(monkeypatch) -> None:
    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> Dict[str, Any]:
            return {"continue_url": "https://evil.example.com/cart/c/x?key=k"}

    class _FakeClient:
        async def post(self, *args: Any, **kwargs: Any) -> Any:
            return _FakeResponse()

    out = await warm.resolve_warm_handoff(
        dest=BRAND_DEST, ctx={"pvt_click_id": "clk_1"}, settings=settings, client=_FakeClient()
    )
    assert out is None


@pytest.mark.asyncio
async def test_resolve_warm_handoff_sends_handle_and_attribution_but_never_a_variant(monkeypatch) -> None:
    """The payload carries the product handle + attribution — and NO variant id, ever.

    This test previously hand-built a ctx containing `shopify_variant_id` and asserted the
    value was forwarded. It passed, and it was misleading: NOTHING writes that key into a
    redirect token, so the branch it covered could never fire in production. The fixture
    manufactured the very evidence the assertion checked. It is kept here as a knockout in
    the opposite direction — even when that key IS present, no variant id is sent — with the
    real invariant (no producer exists) pinned separately in
    `test_real_mint_stamps_cart_permalink_and_never_a_variant_id`.
    """
    seen: Dict[str, Any] = {}

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> Dict[str, Any]:
            return {"continue_url": CONTINUE_URL, "cart_id": "gid://shopify/Cart/abc"}

    class _FakeClient:
        async def post(self, url: str, **kwargs: Any) -> Any:
            seen["url"] = url
            seen["json"] = kwargs.get("json")
            seen["headers"] = kwargs.get("headers")
            return _FakeResponse()

    out = await warm.resolve_warm_handoff(
        dest=BRAND_DEST,
        ctx={"pvt_click_id": "clk_1", "shopify_variant_id": "51895645012184"},
        settings=settings,
        client=_FakeClient(),
    )
    assert out == {"continue_url": CONTINUE_URL, "cart_id": "gid://shopify/Cart/abc"}
    assert seen["json"]["brand_domain"] == "cosrx.com"
    assert seen["json"]["product_handle"] == "peptide-132-hair-home-care-kit"
    assert "variant_id" not in seen["json"], (
        "no variant id may be sent: the only Shopify-issued id we hold travels on "
        "cart_variant_id, which #1813 round 4 forbids stamping into the token ctx"
    )
    assert seen["json"]["attribution"] == {"pivota_click_id": "clk_1"}
    assert seen["headers"]["X-Internal-Key"] == "test-key"


# ---------------------------------------------------------------------------
# could_upgrade_at_click_time — the resolve-time over-approximation that keeps
# `offers.resolve` from claiming `cart_prefilled: false` on an offer this lane would
# later upgrade. See docs/runbooks/outbound_warm_handoff_rollout.md.
# ---------------------------------------------------------------------------

# `expected` is the ABSOLUTE answer, not just "whatever the click path says". Agreement alone
# is worthless here: both sides call the SAME evaluate_warm_eligibility, so INVERTING any
# knockout keeps them equal and an agreement-only assertion stays green. Measured — inverting
# `no_dest_host` to `return True` survived the entire 11,038-test suite when this matrix
# asserted agreement only, and nothing else in the repo covers that knockout.
TOKEN_DEPENDENT = "token-dependent"

_ELIGIBILITY_MATRIX = [
    # (label, dest, brands_raw, pct, enabled, key, expected)
    ("allowlisted brand", BRAND_DEST, "cosrx.com", 0, True, "k", True),
    ("brand off the allowlist", BRAND_DEST, "someone-else.com", 0, True, "k", False),
    ("no allowlist, full rollout", BRAND_DEST, "", 100, True, "k", True),
    ("no allowlist, no rollout", BRAND_DEST, "", 0, True, "k", False),
    # The only row whose answer rides on the token hash — the minted token embeds `iat`, so
    # it differs per run. Agreement is all this row can honestly assert.
    ("no allowlist, half rollout", BRAND_DEST, "", 50, True, "k", TOKEN_DEPENDENT),
    ("affiliate destination", "https://track.linksynergy.com/x?u=1", "", 100, True, "k", False),
    ("hostless destination", "not-a-url", "", 100, True, "k", False),
    ("lane disabled", BRAND_DEST, "cosrx.com", 100, False, "k", False),
    ("no internal key", BRAND_DEST, "cosrx.com", 100, True, None, False),
]


@pytest.mark.parametrize("label,dest,brands,pct,enabled,key,expected", _ELIGIBILITY_MATRIX)
def test_could_upgrade_at_click_time_never_under_reports(
    monkeypatch, label, dest, brands, pct, enabled, key, expected
):
    """SOUNDNESS: a resolve-time `False` must guarantee the click also refuses.

    That direction is the one the caller relies on — it is what licenses emitting an explicit
    `cart_prefilled: false`. Over-reporting (True where the click would refuse) is allowed and
    costs only a `null`; under-reporting reinstates the false claim.

    Each row pins the ABSOLUTE verdict as well as the agreement, so an inverted knockout is
    caught here rather than sliding through on both sides moving together.
    """
    monkeypatch.setattr(settings, "outbound_warm_handoff_enabled", enabled)
    monkeypatch.setattr(settings, "outbound_warm_handoff_internal_key", key)
    monkeypatch.setattr(settings, "outbound_warm_handoff_brands_raw", brands)
    monkeypatch.setattr(settings, "outbound_warm_handoff_rollout_pct", pct)
    token = _mint_token(dest)

    resolve_says = warm.could_upgrade_at_click_time(dest=dest, token=token, ctx={}, settings=settings)
    if expected is not TOKEN_DEPENDENT:
        assert resolve_says is expected, f"{label}: expected {expected}, got {resolve_says}"
    if not enabled:
        return
    click_says, reason = warm.evaluate_warm_eligibility(
        ctx={},
        dest=dest, user_agent=HUMAN_UA, token=token, settings=settings
    )
    assert resolve_says == click_says, f"{label}: resolve={resolve_says} click={click_says} ({reason})"


@pytest.mark.parametrize("pct", [25, 50, 75])
def test_rollout_bucket_actually_reads_the_token(pct):
    """The claim "the bucket is a stable hash of the token" is what makes recovering the REAL
    token load-bearing — and nothing asserted it. A bucket that ignored its token entirely, or
    flipped `<` to `>=`, satisfied every existing rollout test.

    Pinned two ways: the assignment must SPLIT a fixed token set (not all-in / all-out, which
    is what a constant-hash or inverted comparison produces), and one golden value.
    """
    tokens = [f"tok-{i}" for i in range(20)]
    in_bucket = sum(warm.rollout_bucket(t, pct) for t in tokens)
    assert 0 < in_bucket < len(tokens), (
        f"pct={pct} put {in_bucket}/20 tokens in the bucket — the assignment is not reading "
        "the token (a constant hash or an inverted comparison lands on 0 or 20)"
    )
    # Golden: pins the direction of the comparison, which a distribution check cannot.
    assert warm.rollout_bucket("tok-0", 50) is True
    assert warm.rollout_bucket("tok-0", 0) is False
    assert warm.rollout_bucket("tok-0", 100) is True


def test_could_upgrade_at_click_time_over_reports_for_a_bot_and_that_is_correct(monkeypatch):
    """The user-agent is the ONLY click-time-only input, and it can only REMOVE eligibility.

    We do not know at resolve time who will click, so we assume a human. A bot then gets the
    cold redirect while we said "unknown" — an over-report, which is the safe direction. The
    reverse (claiming `false` and serving a cart) is the defect.
    """
    token = _mint_token()
    assert warm.could_upgrade_at_click_time(dest=BRAND_DEST, token=token, ctx={}, settings=settings) is True
    bot_eligible, reason = warm.evaluate_warm_eligibility(
        ctx={},
        dest=BRAND_DEST, user_agent="Mozilla/5.0 (compatible; GPTBot/1.0)", token=token, settings=settings
    )
    assert (bot_eligible, reason) == (False, "bot")


def test_assume_human_skips_only_the_user_agent_knockout(monkeypatch):
    """`assume_human` must not become a skeleton key that waves through every other rule."""
    token = _mint_token()
    # It does waive the UA knockout...
    assert warm.evaluate_warm_eligibility(
        ctx={},
        dest=BRAND_DEST, user_agent="curl/8.4.0", token=token, settings=settings, assume_human=True
    ) == (True, "allowlisted")
    # ...and nothing else. Affiliate hosts and non-allowlisted brands still refuse.
    assert warm.evaluate_warm_eligibility(
        ctx={},
        dest="https://track.linksynergy.com/x?u=1", user_agent=None, token=token,
        settings=settings, assume_human=True
    ) == (False, "affiliate")
    monkeypatch.setattr(settings, "outbound_warm_handoff_brands_raw", "someone-else.com")
    assert warm.evaluate_warm_eligibility(
        ctx={},
        dest=BRAND_DEST, user_agent=None, token=token, settings=settings, assume_human=True
    ) == (False, "not_allowlisted")
    monkeypatch.setattr(settings, "outbound_warm_handoff_internal_key", "")
    assert warm.evaluate_warm_eligibility(
        ctx={},
        dest=BRAND_DEST, user_agent=None, token=token, settings=settings, assume_human=True
    ) == (False, "no_internal_key")


def test_the_click_path_still_knocks_out_bots_by_default(monkeypatch):
    """The new parameter must default OFF — the click lane passes no `assume_human`."""
    token = _mint_token()
    assert warm.evaluate_warm_eligibility(
        ctx={},
        dest=BRAND_DEST, user_agent="Mozilla/5.0 (compatible; ClaudeBot/1.0)",
        token=token, settings=settings
    ) == (False, "bot")
# ---------------------------------------------------------------------------------------------
# An ALREADY-PREFILLED cart is never rebuilt.
#
# The lane exists to upgrade a COLD PDP redirect into a cart. On a dest that is already a cart
# permalink there is nothing to upgrade, and the rebuild is strictly destructive: the resolve
# request carries NO product identity (a `/cart/...` path cannot match `_HANDLE_RE`, and no
# variant id is ever stamped into a token ctx), so a correct cart — right variant, carrying the
# `attributes[pivota_click_id]` order-side attribution join — could be replaced at click time by
# whatever an identity-less request happened to return.
# ---------------------------------------------------------------------------------------------

CART_DEST = (
    "https://cosrx.com/cart/51895645012184:1"
    "?utm_source=pivota&attributes[pivota_click_id]=clk_test"
)


async def _mint_real_redirect(**overrides: Any) -> str:
    """Mint through the REAL production builder, so the ctx under test is the one prod stamps.

    `allowed_domains` is passed explicitly only to keep the market allowlist off the database;
    every other input is the builder's own.
    """
    from routes.agent_shop_gateway import _make_external_redirect_url

    kwargs: Dict[str, Any] = {
        "market": "US",
        "tool": "*",
        "destination_url": "https://www.cosrx.com/products/peptide-132-hair-home-care-kit",
        "utm_template": None,
        "ctx": {},
        "allowed_domains": ["cosrx.com"],
        "cart_variant_id": "51895645012184",
        "shop_domain": "cosrx.com",
        "platform": "shopify",
        "quantity": 1,
    }
    kwargs.update(overrides)
    url = await _make_external_redirect_url(**kwargs)
    assert url, "the builder must produce a redirect for this fixture"
    return url.split("token=", 1)[1]


def _decode_ctx(token: str) -> Dict[str, Any]:
    import base64
    import json as _json

    body = token.split(".", 1)[0]
    body += "=" * (-len(body) % 4)
    return _json.loads(base64.urlsafe_b64decode(body))


@pytest.mark.asyncio
async def test_real_mint_stamps_cart_permalink_and_never_a_variant_id() -> None:
    """The mint->click contract this whole guard rests on, pinned against the real builder.

    Two facts, neither of which was covered anywhere: a cart-capable offer stamps
    `join_mode='cart_permalink'` (so the knockout has something to read), and the ctx does NOT
    carry `shopify_variant_id` (so the resolve payload can never name the product). The second
    is the invariant the deleted dead branch falsely implied was satisfied.
    """
    token = await _mint_real_redirect()
    payload = _decode_ctx(token)
    ctx = payload["ctx"]

    assert ctx["join_mode"] == "cart_permalink"
    assert "/cart/51895645012184:1" in payload["dest"]
    assert "attributes[pivota_click_id]=" in payload["dest"], (
        "the order-side attribution join is exactly what a rebuild would discard"
    )
    assert "shopify_variant_id" not in ctx, (
        "no producer may stamp a numeric variant id into the token ctx (#1813 round 4): "
        "attribution cross-fills product<->variant ids, so it would leak up a grain into "
        "surface_click_events.canonical_product_id"
    )
    # And the dest the lane would have tried to rebuild yields no product identity at all.
    assert warm.extract_product_handle(payload["dest"]) is None


@pytest.mark.asyncio
async def test_real_cart_permalink_token_is_never_warmed_end_to_end(monkeypatch) -> None:
    """End to end on a REAL minted token: no resolver call, and the cart 302 is untouched."""
    token = await _mint_real_redirect()
    dest = _decode_ctx(token)["dest"]
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)

    client = TestClient(app)
    res = client.get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)

    assert res.status_code == 302
    assert res.headers["location"] == dest, "an already-prefilled cart must 302 unchanged"
    assert "attributes[pivota_click_id]=" in res.headers["location"], (
        "the order-side attribution join must survive the click"
    )
    assert calls == [], "the gateway must not be called for a dest that is already a cart"
    assert logged[0]["token_payload"]["ctx"]["warm_reason"] == "already_cart"
    assert logged[0]["token_payload"]["ctx"]["handoff"] == "cold"


def test_already_cart_knockout_beats_the_allowlist(monkeypatch) -> None:
    """The knockout fires on an allowlisted brand — the population that is actually live."""
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    token = _mint_token(dest=CART_DEST, ctx={"join_mode": "cart_permalink"})
    client = TestClient(app)
    res = client.get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.headers["location"] == CART_DEST
    assert calls == []
    ctx = logged[0]["token_payload"]["ctx"]
    assert (ctx["handoff"], ctx["warm_reason"]) == ("cold", "already_cart")


def test_referral_only_pdp_still_warms(monkeypatch) -> None:
    """The legitimate upgrade path is untouched — the guard must not disarm the whole lane."""
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    token = _mint_token(dest=BRAND_DEST, ctx={"join_mode": "referral_only"})
    client = TestClient(app)
    res = client.get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)
    assert res.headers["location"] == CONTINUE_URL
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        ({"join_mode": "cart_permalink"}, True),
        ({"join_mode": "CART_PERMALINK"}, True),  # case-insensitive
        ({"join_mode": " cart_permalink "}, True),  # whitespace-tolerant
        ({"join_mode": "referral_only"}, False),
        ({}, False),  # legacy token with no join_mode -> lane behaves as before
        ({"join_mode": None}, False),
        ({"join_mode": "cart"}, False),  # only the exact token counts
        (None, False),
        # A non-dict ctx must not crash and must not be treated as a cart. `(ctx or {}).get`
        # would raise on a string; the isinstance guard is what makes these safe.
        ("cart_permalink", False),
        (["cart_permalink"], False),
        (42, False),
    ],
)
def test_is_already_cart_join(ctx: Optional[Dict[str, Any]], expected: bool) -> None:
    assert warm.is_already_cart_join(ctx) is expected


def test_evaluate_warm_eligibility_requires_ctx() -> None:
    """`ctx` has NO default on purpose: a call site that forgets it must fail loudly rather
    than silently stop knocking out already-prefilled carts."""
    with pytest.raises(TypeError, match="ctx"):
        warm.evaluate_warm_eligibility(  # type: ignore[call-arg]
            dest=CART_DEST, user_agent=HUMAN_UA, token="t" * 20, settings=settings
        )


@pytest.mark.parametrize(
    ("path_url", "ok"),
    [
        # Both shapes the gateway actually returns are legitimate and must keep working.
        ("https://cosrx.com/cart/c/abc123?key=k", True),
        ("https://cosrx.com/cart/51895645012184:1", True),
        ("https://cosrx.com/checkouts/xyz", True),
        ("https://cosrx.com/en/cart/c/abc", True),  # locale prefix
        ("https://cosrx.com/CART/c/abc", True),  # case-folded before matching
        ("https://cosrx.com/Checkouts/xyz", True),
        # Everything host+scheme alone used to wave through.
        ("https://cosrx.com/", False),
        ("https://cosrx.com", False),
        ("https://cosrx.com/404-not-found", False),
        ("https://cosrx.com/products/some-other-product", False),
        # Segment EQUALITY, not substring — this is what makes any-position matching safe.
        ("https://cosrx.com/products/cart-organizer", False),
        ("https://cosrx.com/collections/checkout-bags", False),
        # A PREFILLED cart always names WHAT is in it. These are cart-WORDED but carry no
        # cart identity, so they are refused: 302-ing a shopper off a correct PDP onto the
        # storefront's EMPTY cart page is a wrong landing, not merely a missed upgrade.
        ("https://cosrx.com/cart", False),  # the empty cart page
        ("https://cosrx.com/checkout", False),  # bare checkout, no cart token
        ("https://cosrx.com/products/cart", False),  # a PDP whose handle IS "cart"
        ("https://cosrx.com/pages/cart", False),
        ("https://cosrx.com/blogs/news/cart", False),
    ],
)
def test_continue_url_must_be_cart_shaped(path_url: str, ok: bool) -> None:
    assert warm._validate_continue_url(path_url, "cosrx.com") is ok


@pytest.mark.asyncio
async def test_non_cart_continue_url_is_refused_and_click_lands_on_the_pdp(monkeypatch) -> None:
    """A gateway answer that is not a cart resolves to None, so the shopper keeps the PDP."""

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> Dict[str, Any]:
            return {"continue_url": "https://cosrx.com/404-not-found"}

    class _FakeClient:
        async def post(self, *args: Any, **kwargs: Any) -> Any:
            return _FakeResponse()

    out = await warm.resolve_warm_handoff(
        dest=BRAND_DEST, ctx={"pvt_click_id": "c"}, settings=settings, client=_FakeClient()
    )
    assert out is None


# ---------------------------------------------------------------------------------------------
# Adversarial-review findings (2026-08-24).
#
# 1. `join_mode` records "we BUILT a cart", not "dest IS a cart". A destination_url that was
#    ALREADY a cart, with no recoverable variant id, mints `referral_only` — so reading
#    join_mode alone left the original defect wide open on that population. Four other token
#    minters make it worse: two omit join_mode, two HARDCODE "referral_only".
# 2. Ordered first, the knockout swallowed bots, non-allowlisted hosts and affiliate links —
#    none ever at risk — corrupting the rollout dial the runbook points operators at.
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_a_cart_is_knocked_out_even_when_join_mode_says_referral_only() -> None:
    """The false negative that `join_mode` alone could not see.

    Built through the REAL production builder with a destination that is already a cart and
    no recoverable variant id — which is exactly what mints `referral_only` over a cart dest.
    """
    token = await _mint_real_redirect(
        destination_url="https://cosrx.com/cart/51895645012184:1", cart_variant_id=None
    )
    payload = _decode_ctx(token)

    assert payload["ctx"]["join_mode"] == "referral_only", "the premise of this test"
    assert warm.is_already_cart_join(payload["ctx"]) is False, "join_mode alone cannot see it"
    assert warm.extract_product_handle(payload["dest"]) is None, "so no identity would be sent"

    eligible, reason = warm.evaluate_warm_eligibility(
        dest=payload["dest"], user_agent=HUMAN_UA, token=token,
        ctx=payload["ctx"], settings=settings,
    )
    assert (eligible, reason) == (False, "already_cart"), (
        "the dest PATH arm must catch what join_mode misses"
    )


def test_cart_dest_is_knocked_out_for_tokens_that_never_stamp_join_mode() -> None:
    """Minters that omit join_mode, or hardcode 'referral_only', are covered by the path arm."""
    for ctx in ({}, {"join_mode": "referral_only"}, {"join_mode": None}):
        eligible, reason = warm.evaluate_warm_eligibility(
            dest=CART_DEST, user_agent=HUMAN_UA, token="t" * 20, ctx=ctx, settings=settings,
        )
        assert (eligible, reason) == (False, "already_cart"), f"ctx={ctx}"


@pytest.mark.parametrize(
    ("dest", "user_agent", "brands", "expected_reason"),
    [
        # The knockout runs LAST, so populations that were never at risk keep their own
        # reason and stay out of the already_cart dial.
        (CART_DEST, "Googlebot/2.1 (+http://www.google.com/bot.html)", "cosrx.com", "bot"),
        (CART_DEST, HUMAN_UA, "example.com", "not_allowlisted"),
        ("https://www.linksynergy.com/cart/c/abc", HUMAN_UA, "cosrx.com", "affiliate"),
        (CART_DEST, None, "cosrx.com", "bot"),  # no UA at all is treated as non-human
        # ...and a click that WOULD have been warmed is the one that reports already_cart.
        (CART_DEST, HUMAN_UA, "cosrx.com", "already_cart"),
    ],
)
def test_already_cart_dial_counts_only_clicks_that_would_have_been_warmed(
    monkeypatch, dest: str, user_agent: Optional[str], brands: str, expected_reason: str
) -> None:
    monkeypatch.setattr(settings, "outbound_warm_handoff_brands_raw", brands)
    eligible, reason = warm.evaluate_warm_eligibility(
        dest=dest, user_agent=user_agent, token="t" * 20,
        ctx={"join_mode": "cart_permalink"}, settings=settings,
    )
    assert eligible is False
    assert reason == expected_reason


def test_pdp_dest_is_never_seen_as_a_cart() -> None:
    """The path arm must cost nothing on the legitimate upgrade population."""
    for dest in (
        BRAND_DEST,
        "https://cosrx.com/products/cart-organizer",
        "https://cosrx.com/collections/checkout-bags",
        "https://cosrx.com/products/cart",
    ):
        assert warm._is_cart_shaped_path(warm._path_of(dest)) is False, dest


def test_join_mode_arm_catches_a_cart_shape_the_path_set_does_not_know() -> None:
    """The `join_mode` arm is NOT redundant with the path arm, and this pins it.

    Today the two agree on every real cart, so a test using a real minted cart dest is killed
    by the path arm alone and would let the join_mode arm rot. The arms differ precisely where
    a cart URL does not use a segment in `_CART_PATH_SEGMENTS` — a non-Shopify or localized
    permalink (Wix `/cart-page`, `/panier/...`) that a future `resolve_cart_permalink` learns
    to build. `join_mode` is shape-independent, so it still knocks that out.
    """
    exotic_cart = "https://cosrx.com/cart-page/abc123"
    assert warm._is_cart_shaped_path(warm._path_of(exotic_cart)) is False, "the premise"

    eligible, reason = warm.evaluate_warm_eligibility(
        dest=exotic_cart, user_agent=HUMAN_UA, token="t" * 20,
        ctx={"join_mode": "cart_permalink"}, settings=settings,
    )
    assert (eligible, reason) == (False, "already_cart")


def test_route_forwards_ctx_so_join_mode_alone_can_knock_out(monkeypatch) -> None:
    """The ROUTE must pass the real signed ctx to eligibility, not an empty dict.

    Uses a cart shape the path arm does NOT recognise, so the knockout can only fire via
    `join_mode` — which it can only see if the route actually forwarded the ctx. With a
    cart-shaped dest this would pass even on an empty ctx, and the wiring would rot.
    """
    exotic_cart = "https://cosrx.com/cart-page/abc123"
    assert warm._is_cart_shaped_path(warm._path_of(exotic_cart)) is False, "the premise"

    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    token = _mint_token(dest=exotic_cart, ctx={"join_mode": "cart_permalink"})

    client = TestClient(app)
    res = client.get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)

    assert res.headers["location"] == exotic_cart
    assert calls == [], "an empty ctx here would have warmed an already-prefilled cart"
    assert logged[0]["token_payload"]["ctx"]["warm_reason"] == "already_cart"


# ---------------------------------------------------------------------------------------------
# The buyer MARKET on the warm-handoff body (PIVOTA-Agent #2259 purchasability gate).
#
# The gateway keys the merchant-purchasability fact on (domain, market) and REFUSES to
# substitute its own deployment market: a body with no usable `market` is
# `merchant_purchasability_unkeyable` and the gate keeps the previous behaviour — the exact
# lane flowerbeauty.com travelled. So the market we send must be the one THIS CLICK WAS
# SERVED FOR (the signed token's own top-level `market`), never a server-side default, and an
# unusable value must be OMITTED rather than coerced.
#
# See docs/runbooks/merchant_purchasability.md, "Gateway (PIVOTA-Agent) change".
# ---------------------------------------------------------------------------------------------


def _mint_token_with_market(market: Any, dest: str = BRAND_DEST) -> str:
    """A signed `/r` token whose TOP-LEVEL `market` is exactly `market`.

    `market=_ABSENT` mints a token with no `market` key at all — the shape the four real
    minters produce when nothing named a market.
    """
    from services.outbound_links_service import make_redirect_token

    payload: Dict[str, Any] = {"tool": "*", "dest": dest, "ctx": {"pvt_click_id": "clk_test"}}
    if market is not _ABSENT:
        payload["market"] = market
    return make_redirect_token(payload, ttl_seconds=3600)


_ABSENT = object()


class _BodySpy:
    """Captures the exact body handed to httpx, and answers a valid warm handoff."""

    def __init__(self) -> None:
        self.bodies: list = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.bodies.append(kwargs.get("json"))

        class _R:
            status_code = 200

            @staticmethod
            def json() -> Dict[str, Any]:
                return {"continue_url": CONTINUE_URL, "cart_id": "gid://shopify/Cart/abc"}

        return _R()

    @property
    def body(self) -> Dict[str, Any]:
        assert len(self.bodies) == 1, f"expected exactly one POST, saw {len(self.bodies)}"
        return self.bodies[0]


async def _body_for_market(market: Any) -> Dict[str, Any]:
    spy = _BodySpy()
    out = await warm.resolve_warm_handoff(
        dest=BRAND_DEST,
        ctx={"pvt_click_id": "clk_1"},
        settings=settings,
        market=market,
        client=spy,
    )
    assert out is not None, "the handoff itself must still resolve"
    return spy.body


# --- the validator, in isolation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("US", "US"),
        ("SG", "SG"),
        ("jp", "JP"),  # lower case is the SAME market, upper-cased
        ("Gb", "GB"),
        ("  sg  ", "SG"),  # surrounding whitespace is not a different market
        ("USA", None),  # alpha-3 is NOT alpha-2 — omit, never truncate to "US"
        ("usa", None),
        ("", None),
        ("   ", None),
        ("U", None),
        ("U1", None),
        ("1S", None),
        ("U-S", None),
        ("us-east-1", None),
        ("*", None),
        (None, None),
        (123, None),
        (["US"], None),
        (True, None),
    ],
)
def test_click_market_accepts_only_iso2(raw: Any, expected: Optional[str]) -> None:
    assert warm.click_market(raw) == expected


def test_click_market_never_invents_a_market() -> None:
    """The refusing half of the rule, stated on its own: nothing unusable becomes a market.

    `"USA"` must NOT become `"US"` by truncation and `None` must NOT become the deployment's
    market. A coerced value asks the purchasability gate about a vantage the buyer is not in,
    and a positive fact from another vantage is exactly what made flowerbeauty.com look
    payable (runbook §2).
    """
    assert warm.click_market("USA") is None
    assert warm.click_market(None) is None
    assert warm.click_market("") is None


# --- the wire body ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_body_carries_the_market_when_the_click_has_one() -> None:
    assert (await _body_for_market("SG"))["market"] == "SG"


@pytest.mark.asyncio
async def test_body_uppercases_a_lowercase_market() -> None:
    body = await _body_for_market("sg")
    assert body["market"] == "SG", "the gateway's contract is ISO-2 UPPERCASE"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", "   ", "USA", "usa", "U", "u1", 7, ["SG"]])
async def test_body_omits_the_key_when_the_market_is_unusable(bad: Any) -> None:
    """Absent, not empty and not coerced. The gateway reads a missing key as `unkeyable` and
    keeps its previous behaviour; a `""` or a wrong-vantage code would be a silent answer."""
    body = await _body_for_market(bad)
    assert "market" not in body, f"{bad!r} must send NO market key, got {body.get('market')!r}"


@pytest.mark.asyncio
async def test_body_is_byte_identical_to_the_pre_market_snapshot_when_no_market() -> None:
    """The no-market body is EXACTLY what this lane sent before `market` existed.

    Pinned as bytes, key order included, so an accidental `"market": null`, an empty string,
    a reordering, or any new field at all fails here rather than in production.
    """
    import json

    snapshot = (
        '{"brand_domain": "cosrx.com", '
        '"product_url": "https://www.cosrx.com/products/peptide-132-hair-home-care-kit", '
        '"product_handle": "peptide-132-hair-home-care-kit", '
        '"attribution": {"pivota_click_id": "clk_1"}}'
    )
    assert json.dumps(await _body_for_market(None)) == snapshot


@pytest.mark.asyncio
async def test_market_is_the_only_addition_to_the_body() -> None:
    """With a market, the body is the snapshot PLUS `market` and nothing else."""
    without = await _body_for_market(None)
    with_market = await _body_for_market("SG")
    assert set(with_market) - set(without) == {"market"}
    assert {k: v for k, v in with_market.items() if k != "market"} == without


@pytest.mark.asyncio
async def test_body_never_carries_buyer_email_or_address() -> None:
    """A market is a country code, not a buyer. Nothing identifying may ride along.

    Scans the SERIALIZED body, so a PII value nested under any key is caught, and asserts the
    key set exactly — which is what stops a future "helpful" buyer field being added.
    """
    import json

    ctx_with_pii = {
        "pvt_click_id": "clk_1",
        "email": "shopper@example.com",
        "buyer_email": "shopper@example.com",
        "address1": "1 Raffles Place",
        "zip": "048616",
        "phone": "+65 6123 4567",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "ip": "203.0.113.7",
    }
    spy = _BodySpy()
    await warm.resolve_warm_handoff(
        dest=BRAND_DEST, ctx=ctx_with_pii, settings=settings, market="SG", client=spy
    )
    wire = json.dumps(spy.body).lower()
    for leaked in (
        "shopper@example.com",
        "raffles",
        "048616",
        "6123",
        "ada",
        "lovelace",
        "203.0.113.7",
        "email",
        "address",
        "phone",
        "zip",
    ):
        assert leaked not in wire, f"{leaked!r} reached the gateway body"
    assert set(spy.body) == {"brand_domain", "product_url", "product_handle", "attribution", "market"}
    assert set(spy.body["attribution"]) == {"pivota_click_id"}


@pytest.mark.asyncio
async def test_default_market_argument_sends_nothing() -> None:
    """A caller that passes no `market` at all sends no key — the callers converted before the
    producer, so an un-migrated one degrades to today's behaviour, never to a wrong vantage."""
    spy = _BodySpy()
    await warm.resolve_warm_handoff(
        dest=BRAND_DEST, ctx={"pvt_click_id": "clk_1"}, settings=settings, client=spy
    )
    assert "market" not in spy.body


# --- the route: the market must be the CLICK's, through the real signed token ----------------


def test_route_sends_the_markets_the_token_was_minted_with(monkeypatch) -> None:
    """A JP token sends JP and an SG token sends SG — through the real mint + real route.

    Two different non-default markets, so a hardcoded `"US"` (or any single constant) fails
    here. This is the whole point: the gate must be keyed on the buyer's vantage.
    """
    for market in ("JP", "SG"):
        warm.memo_clear()
        calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
        _spy_logger(monkeypatch)
        token = _mint_token_with_market(market)

        client = TestClient(app)
        res = client.get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)

        assert res.headers["location"] == CONTINUE_URL
        assert len(calls) == 1
        assert calls[0]["market"] == market, (
            f"the gate must be keyed on the click's own market, not {calls[0]['market']!r}"
        )


def test_route_lowercase_token_market_reaches_the_gateway_uppercased(monkeypatch) -> None:
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    _spy_logger(monkeypatch)
    token = _mint_token_with_market("jp")

    TestClient(app).get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)

    assert calls[0]["market"] == "JP"


@pytest.mark.parametrize("bad", [_ABSENT, "", "USA", None])
def test_route_sends_no_market_when_the_token_has_none(monkeypatch, bad: Any) -> None:
    """No market on the token ⇒ no market on the wire ⇒ the gateway keeps previous behaviour.

    A `"US"` here would be the defect: the deployment's market standing in for the buyer's.
    """
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    token = _mint_token_with_market(bad)

    TestClient(app).get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)

    assert calls[0]["market"] is None, f"expected no market, got {calls[0]['market']!r}"
    assert logged[0]["token_payload"]["ctx"]["warm_market"] == "none", (
        "an un-keyable click must be COUNTED on the existing instrument, not lost"
    )


def test_route_counts_the_keyed_market_on_the_click_event(monkeypatch) -> None:
    """The other arm of the counter: a keyed click records the ISO-2 code it was keyed on."""
    _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    token = _mint_token_with_market("SG")

    TestClient(app).get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)

    ctx = logged[0]["token_payload"]["ctx"]
    assert ctx["warm_market"] == "SG"
    assert ctx["handoff"] == "warm", "the counter must not disturb the substitution instrument"
    assert ctx["warm_reason"] == "ok"


def test_ineligible_click_gets_no_market_counter(monkeypatch) -> None:
    """The counter measures the gate-keying population only — clicks that never asked.

    An un-allowlisted brand never reaches the gateway, so counting it as `warm_market=none`
    would inflate the un-keyable population with clicks that were never candidates.
    """
    monkeypatch.setattr(settings, "outbound_warm_handoff_brands_raw", "someone-else.com")
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    token = _mint_token_with_market("SG")

    TestClient(app).get(f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False)

    assert calls == []
    assert "warm_market" not in logged[0]["token_payload"]["ctx"]


def test_flag_off_is_still_byte_identical(monkeypatch) -> None:
    """The market work must not have woken the lane up when the flag is off."""
    monkeypatch.setattr(settings, "outbound_warm_handoff_enabled", False)
    calls = _spy_resolver(monkeypatch, {"continue_url": CONTINUE_URL})
    logged = _spy_logger(monkeypatch)
    token = _mint_token_with_market("SG")

    res = TestClient(app).get(
        f"/r?token={token}", headers={"user-agent": HUMAN_UA}, follow_redirects=False
    )

    assert res.headers["location"] == BRAND_DEST
    assert calls == []
    assert "warm_market" not in (logged[0]["token_payload"].get("ctx") or {})

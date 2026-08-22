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
async def test_resolve_warm_handoff_passes_variant_hint_and_attribution(monkeypatch) -> None:
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
    assert seen["json"]["variant_id"] == "51895645012184"
    assert seen["json"]["attribution"] == {"pivota_click_id": "clk_1"}
    assert seen["headers"]["X-Internal-Key"] == "test-key"


# ---------------------------------------------------------------------------
# could_upgrade_at_click_time — the resolve-time over-approximation that keeps
# `offers.resolve` from claiming `cart_prefilled: false` on an offer this lane would
# later upgrade. See docs/runbooks/outbound_warm_handoff_rollout.md.
# ---------------------------------------------------------------------------

_ELIGIBILITY_MATRIX = [
    # (label, dest, brands_raw, pct, enabled, key)
    ("allowlisted brand", BRAND_DEST, "cosrx.com", 0, True, "k"),
    ("brand off the allowlist", BRAND_DEST, "someone-else.com", 0, True, "k"),
    ("no allowlist, full rollout", BRAND_DEST, "", 100, True, "k"),
    ("no allowlist, no rollout", BRAND_DEST, "", 0, True, "k"),
    ("no allowlist, half rollout", BRAND_DEST, "", 50, True, "k"),
    ("affiliate destination", "https://track.linksynergy.com/x?u=1", "", 100, True, "k"),
    ("hostless destination", "not-a-url", "", 100, True, "k"),
    ("lane disabled", BRAND_DEST, "cosrx.com", 100, False, "k"),
    ("no internal key", BRAND_DEST, "cosrx.com", 100, True, None),
]


@pytest.mark.parametrize("label,dest,brands,pct,enabled,key", _ELIGIBILITY_MATRIX)
def test_could_upgrade_at_click_time_never_under_reports(
    monkeypatch, label, dest, brands, pct, enabled, key
):
    """SOUNDNESS: a resolve-time `False` must guarantee the click also refuses.

    That direction is the one the caller relies on — it is what licenses emitting an explicit
    `cart_prefilled: false`. Over-reporting (True where the click would refuse) is allowed and
    costs only a `null`; under-reporting reinstates the false claim.
    """
    monkeypatch.setattr(settings, "outbound_warm_handoff_enabled", enabled)
    monkeypatch.setattr(settings, "outbound_warm_handoff_internal_key", key)
    monkeypatch.setattr(settings, "outbound_warm_handoff_brands_raw", brands)
    monkeypatch.setattr(settings, "outbound_warm_handoff_rollout_pct", pct)
    token = _mint_token(dest)

    resolve_says = warm.could_upgrade_at_click_time(dest=dest, token=token, settings=settings)
    click_says, reason = warm.evaluate_warm_eligibility(
        dest=dest, user_agent=HUMAN_UA, token=token, settings=settings
    )
    if not enabled:
        assert resolve_says is False, label
        return
    assert resolve_says == click_says, f"{label}: resolve={resolve_says} click={click_says} ({reason})"


def test_could_upgrade_at_click_time_over_reports_for_a_bot_and_that_is_correct(monkeypatch):
    """The user-agent is the ONLY click-time-only input, and it can only REMOVE eligibility.

    We do not know at resolve time who will click, so we assume a human. A bot then gets the
    cold redirect while we said "unknown" — an over-report, which is the safe direction. The
    reverse (claiming `false` and serving a cart) is the defect.
    """
    token = _mint_token()
    assert warm.could_upgrade_at_click_time(dest=BRAND_DEST, token=token, settings=settings) is True
    bot_eligible, reason = warm.evaluate_warm_eligibility(
        dest=BRAND_DEST, user_agent="Mozilla/5.0 (compatible; GPTBot/1.0)", token=token, settings=settings
    )
    assert (bot_eligible, reason) == (False, "bot")


def test_assume_human_skips_only_the_user_agent_knockout(monkeypatch):
    """`assume_human` must not become a skeleton key that waves through every other rule."""
    token = _mint_token()
    # It does waive the UA knockout...
    assert warm.evaluate_warm_eligibility(
        dest=BRAND_DEST, user_agent="curl/8.4.0", token=token, settings=settings, assume_human=True
    ) == (True, "allowlisted")
    # ...and nothing else. Affiliate hosts and non-allowlisted brands still refuse.
    assert warm.evaluate_warm_eligibility(
        dest="https://track.linksynergy.com/x?u=1", user_agent=None, token=token,
        settings=settings, assume_human=True
    ) == (False, "affiliate")
    monkeypatch.setattr(settings, "outbound_warm_handoff_brands_raw", "someone-else.com")
    assert warm.evaluate_warm_eligibility(
        dest=BRAND_DEST, user_agent=None, token=token, settings=settings, assume_human=True
    ) == (False, "not_allowlisted")
    monkeypatch.setattr(settings, "outbound_warm_handoff_internal_key", "")
    assert warm.evaluate_warm_eligibility(
        dest=BRAND_DEST, user_agent=None, token=token, settings=settings, assume_human=True
    ) == (False, "no_internal_key")


def test_the_click_path_still_knocks_out_bots_by_default(monkeypatch):
    """The new parameter must default OFF — the click lane passes no `assume_human`."""
    token = _mint_token()
    assert warm.evaluate_warm_eligibility(
        dest=BRAND_DEST, user_agent="Mozilla/5.0 (compatible; ClaudeBot/1.0)",
        token=token, settings=settings
    ) == (False, "bot")

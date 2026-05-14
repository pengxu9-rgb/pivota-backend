"""PR: social-intel handle detection.

In the PR-8 prod run, both TikTok + Instagram own_presence entries
came back with handle:null. Root cause: `run_brand_report` (the
main audit path) calls `infer_social_intelligence(detected_handles=
None)` — no homepage HTML is fetched, so the social probes have no
handle to anchor on, and when the LLM also doesn't supply one the
result carries handle:null.

Fix: `infer_social_intelligence` now self-serves a homepage fetch +
`_extract_social_handles` scrape when no `detected_handles` were
threaded in. The cold-start flow still passes its own scraped
handles; only the no-handles path triggers the fallback fetch.

These tests mock the network helpers so nothing is fetched for real.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from services.bd_brand_signals import (
    _fetch_homepage_html,
    infer_social_intelligence,
)


_HOMEPAGE_WITH_HANDLES = """
<html><head><title>Beauty of Joseon</title></head>
<body>
  <footer>
    <a href="https://www.tiktok.com/@beautyofjoseon">TikTok</a>
    <a href="https://www.instagram.com/beautyofjoseon_official">Instagram</a>
  </footer>
</body></html>
"""

_HOMEPAGE_NO_HANDLES = """
<html><head><title>Beauty of Joseon</title></head>
<body><p>No social links here.</p></body></html>
"""


def _presence(handle: Optional[str], followers: Optional[int]) -> Dict[str, Any]:
    return {
        "platform": "tiktok",
        "handle": handle,
        "follower_estimate": followers,
        "follower_band": "100k-1M" if followers else None,
        "view_per_post_estimate": 50000 if followers else None,
        "content_focus": "product demos" if followers else None,
        "post_frequency": None,
        "verified_account": None,
        "grounding": "grounded" if followers else "ungrounded",
    }


# =========================================================================
# _fetch_homepage_html — the fetch helper
# =========================================================================


@pytest.mark.asyncio
async def test_fetch_homepage_html_empty_domain_returns_none():
    assert await _fetch_homepage_html("") is None
    assert await _fetch_homepage_html("   ") is None


@pytest.mark.asyncio
async def test_fetch_homepage_html_prepends_scheme():
    """A bare domain gets https:// prepended before the GET."""
    captured = {}

    class _FakeResponse:
        status_code = 200
        text = "<html></html>"

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, **kw):
            captured["url"] = url
            return _FakeResponse()

    with patch("services.bd_brand_signals.httpx.AsyncClient", _FakeClient):
        result = await _fetch_homepage_html("beautyofjoseon.com")
    assert result == "<html></html>"
    assert captured["url"] == "https://beautyofjoseon.com"


@pytest.mark.asyncio
async def test_fetch_homepage_html_non_200_returns_none():
    class _FakeResponse:
        status_code = 404
        text = "not found"

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, **kw):
            return _FakeResponse()

    with patch("services.bd_brand_signals.httpx.AsyncClient", _FakeClient):
        assert await _fetch_homepage_html("beautyofjoseon.com") is None


# =========================================================================
# Handle resolution in infer_social_intelligence
# =========================================================================


def _patch_infer(own_presence_side_effect):
    """Context-manager bundle: patch api-key, the three sub-calls.
    `_fetch_homepage_html` is intentionally NOT patched here — each
    test patches it to control the fallback-scrape path.

    Sub-calls return (result, failure_reason) tuples — the kol +
    competitive mocks return the no-op tuple shape; the
    own-presence side_effect is supplied per-test."""
    return patch.multiple(
        "services.bd_brand_signals",
        _resolve_gemini_api_key=lambda: "fake-key",
        _infer_own_presence=AsyncMock(side_effect=own_presence_side_effect),
        _infer_kol_endorsements=AsyncMock(return_value=(None, "no_data")),
        _infer_competitive_social=AsyncMock(return_value=(None, None)),
    )


@pytest.mark.asyncio
async def test_handle_from_caller_param_used_directly():
    """When the caller threads detected_handles, NO homepage fetch
    happens — the caller's handle is passed straight to the probe."""
    seen_handles = {}

    async def capture_own(brand, platform, handle, api_key):
        seen_handles[platform] = handle
        return (_presence(handle, 662000), None)

    with patch(
        "services.bd_brand_signals._fetch_homepage_html",
        new_callable=AsyncMock,
    ) as mock_fetch, _patch_infer(capture_own):
        await infer_social_intelligence(
            "Beauty of Joseon",
            "beautyofjoseon.com",
            detected_handles=[
                {"platform": "tiktok", "handle": "boj_tiktok"},
                {"platform": "instagram", "handle": "boj_insta"},
            ],
        )
    mock_fetch.assert_not_called()
    assert seen_handles["tiktok"] == "boj_tiktok"
    assert seen_handles["instagram"] == "boj_insta"


@pytest.mark.asyncio
async def test_handle_from_homepage_scrape_fallback():
    """No detected_handles → infer_social_intelligence fetches the
    homepage and scrapes handles from it."""
    seen_handles = {}

    async def capture_own(brand, platform, handle, api_key):
        seen_handles[platform] = handle
        return (_presence(handle, 662000), None)

    with patch(
        "services.bd_brand_signals._fetch_homepage_html",
        new_callable=AsyncMock,
        return_value=_HOMEPAGE_WITH_HANDLES,
    ) as mock_fetch, _patch_infer(capture_own):
        await infer_social_intelligence(
            "Beauty of Joseon",
            "beautyofjoseon.com",
            detected_handles=None,
        )
    mock_fetch.assert_awaited_once()
    assert seen_handles["tiktok"] == "beautyofjoseon"
    assert seen_handles["instagram"] == "beautyofjoseon_official"


@pytest.mark.asyncio
async def test_handle_none_when_homepage_has_no_links():
    """Homepage fetched but no social links → probes get handle=None
    (fall back to "search for the account" prompting)."""
    seen_handles = {}

    async def capture_own(brand, platform, handle, api_key):
        seen_handles[platform] = handle
        return (_presence(handle, 662000), None)

    with patch(
        "services.bd_brand_signals._fetch_homepage_html",
        new_callable=AsyncMock,
        return_value=_HOMEPAGE_NO_HANDLES,
    ), _patch_infer(capture_own):
        await infer_social_intelligence(
            "Beauty of Joseon", "beautyofjoseon.com", detected_handles=None,
        )
    assert seen_handles["tiktok"] is None
    assert seen_handles["instagram"] is None


@pytest.mark.asyncio
async def test_handle_none_when_homepage_fetch_fails():
    """Homepage fetch returns None (timeout / non-200) → probes get
    handle=None, no crash."""
    seen_handles = {}

    async def capture_own(brand, platform, handle, api_key):
        seen_handles[platform] = handle
        return (_presence(handle, 662000), None)

    with patch(
        "services.bd_brand_signals._fetch_homepage_html",
        new_callable=AsyncMock,
        return_value=None,
    ), _patch_infer(capture_own):
        result = await infer_social_intelligence(
            "Beauty of Joseon", "beautyofjoseon.com", detected_handles=None,
        )
    assert seen_handles["tiktok"] is None
    # Probe still ran + returned data — handle absence doesn't block it.
    assert result["own_presence"]["tiktok"]["follower_estimate"] == 662000


@pytest.mark.asyncio
async def test_rich_response_kept_even_when_handle_null():
    """A grounded probe with real follower data but handle=None is
    KEPT — the numbers are still useful; only fully-thin results are
    dropped (that drop is the existing _infer_own_presence emptiness
    check, unchanged by this PR)."""

    async def own_with_data_no_handle(brand, platform, handle, api_key):
        # LLM didn't return a handle, caller/scrape didn't supply one,
        # but the probe still produced grounded follower data.
        return (_presence(None, 662000), None)

    with patch(
        "services.bd_brand_signals._fetch_homepage_html",
        new_callable=AsyncMock,
        return_value=None,
    ), _patch_infer(own_with_data_no_handle):
        result = await infer_social_intelligence(
            "Beauty of Joseon", "beautyofjoseon.com", detected_handles=None,
        )
    tt = result["own_presence"]["tiktok"]
    assert tt is not None
    assert tt["handle"] is None
    assert tt["follower_estimate"] == 662000

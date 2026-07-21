"""
Phase D wire-up tests — OAuth flow endpoints + token-refresh logic.

Mocks Google's token + Sites API endpoints. Verifies state signing
prevents CSRF, missing creds fail loud (not silent), refresh-token
flow works, and the gsc_url_submissions upserts produce the right
shape for the audit's tracking surface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def enabled_settings(monkeypatch):
    """Configure settings so the OAuth flow + API calls are
    permitted to run. Tests that need them to fail loud override
    individually."""
    from config import settings as settings_module
    monkeypatch.setattr(settings_module.settings, "gsc_integration_enabled", True)
    monkeypatch.setattr(settings_module.settings, "google_oauth_client_id", "test-client-id")
    monkeypatch.setattr(settings_module.settings, "google_oauth_client_secret", "test-secret")
    monkeypatch.setattr(
        settings_module.settings,
        "google_oauth_redirect_uri",
        "https://test.example/api/gsc/oauth/callback",
    )


@pytest.fixture
def stubbed_state_secret(monkeypatch):
    """State signing uses crypto_service.connector_key as the HMAC
    key. Stub it so tests don't pull the cryptography module (heavy
    transitive dep) just to assert HMAC behavior."""
    import routes.gsc_oauth_routes as mod
    monkeypatch.setattr(mod, "_state_secret", lambda: b"x" * 32)


# -----------------------------------------------------------------
# State signing — CSRF protection
# -----------------------------------------------------------------


def test_state_round_trips_with_valid_signature(enabled_settings, stubbed_state_secret):
    from routes.gsc_oauth_routes import _sign_state, _verify_state
    payload = {
        "merchant_id": "merch_test",
        "return_to": "/dashboard",
        "nonce": "abc123",
        "expires_at": int(
            (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
        ),
    }
    state = _sign_state(payload)
    out = _verify_state(state)
    assert out["merchant_id"] == "merch_test"
    assert out["nonce"] == "abc123"


def test_state_rejects_tampered_payload(enabled_settings, stubbed_state_secret):
    """Any modification to the body invalidates the signature."""
    from fastapi import HTTPException
    from routes.gsc_oauth_routes import _sign_state, _verify_state
    payload = {
        "merchant_id": "merch_orig",
        "return_to": "",
        "nonce": "x",
        "expires_at": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    state = _sign_state(payload)
    body, sig = state.split(".")
    # Tamper with body — substitute merch_orig for merch_attacker
    tampered_body_bytes = json.dumps(
        {**payload, "merchant_id": "merch_attacker"},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    tampered_b64 = base64.urlsafe_b64encode(tampered_body_bytes).decode("ascii").rstrip("=")
    tampered_state = f"{tampered_b64}.{sig}"
    with pytest.raises(HTTPException) as ei:
        _verify_state(tampered_state)
    assert ei.value.status_code == 400


def test_state_rejects_expired(enabled_settings, stubbed_state_secret):
    from fastapi import HTTPException
    from routes.gsc_oauth_routes import _sign_state, _verify_state
    payload = {
        "merchant_id": "merch_test",
        "return_to": "",
        "nonce": "x",
        "expires_at": int(
            (datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()
        ),
    }
    state = _sign_state(payload)
    with pytest.raises(HTTPException) as ei:
        _verify_state(state)
    assert ei.value.status_code == 400
    assert "expired" in ei.value.detail.lower()


# -----------------------------------------------------------------
# /start endpoint behavior
# -----------------------------------------------------------------


def test_require_enabled_raises_when_flag_off(monkeypatch):
    from fastapi import HTTPException
    from routes.gsc_oauth_routes import _require_enabled
    from config import settings as settings_module
    monkeypatch.setattr(settings_module.settings, "gsc_integration_enabled", False)
    with pytest.raises(HTTPException) as ei:
        _require_enabled()
    assert ei.value.status_code == 503


def test_require_enabled_raises_when_creds_missing(monkeypatch):
    from fastapi import HTTPException
    from routes.gsc_oauth_routes import _require_enabled
    from config import settings as settings_module
    monkeypatch.setattr(settings_module.settings, "gsc_integration_enabled", True)
    monkeypatch.setattr(settings_module.settings, "google_oauth_client_id", "")
    with pytest.raises(HTTPException) as ei:
        _require_enabled()
    assert ei.value.status_code == 503


# -----------------------------------------------------------------
# Site picker
# -----------------------------------------------------------------


def test_pick_primary_site_prefers_owner_or_full_user():
    from routes.gsc_oauth_routes import _pick_primary_site
    response = {
        "siteEntry": [
            {"siteUrl": "https://restricted.example/", "permissionLevel": "siteRestrictedUser"},
            {"siteUrl": "https://owned.example/", "permissionLevel": "siteOwner"},
            {"siteUrl": "https://full.example/", "permissionLevel": "siteFullUser"},
        ],
    }
    # First eligible (siteOwner) wins.
    assert _pick_primary_site(response) == "https://owned.example/"


def test_pick_primary_site_returns_none_when_no_eligible():
    from routes.gsc_oauth_routes import _pick_primary_site
    response = {
        "siteEntry": [
            {"siteUrl": "https://r.example/", "permissionLevel": "siteRestrictedUser"},
        ],
    }
    assert _pick_primary_site(response) is None


def test_pick_primary_site_handles_empty():
    from routes.gsc_oauth_routes import _pick_primary_site
    assert _pick_primary_site({}) is None
    assert _pick_primary_site({"siteEntry": []}) is None


# -----------------------------------------------------------------
# Token refresh logic in services.gsc_integration
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_access_token_success(enabled_settings):
    from services import gsc_integration as mod

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(
        return_value={"access_token": "new-token", "expires_in": 3600}
    )

    class _Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def post(self, url, **kwargs):
            return mock_response

    with patch("httpx.AsyncClient", return_value=_Client()):
        access, expires_at, err = await mod._refresh_access_token("rt-123")
    assert err is None
    assert access == "new-token"
    assert isinstance(expires_at, datetime)


@pytest.mark.asyncio
async def test_refresh_access_token_propagates_google_error(enabled_settings):
    from services import gsc_integration as mod

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "invalid_grant"

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, url, **kwargs):
            return mock_response

    with patch("httpx.AsyncClient", return_value=_Client()):
        access, expires_at, err = await mod._refresh_access_token("rt-bad")
    assert access is None
    assert expires_at is None
    assert "http_400" in err


# -----------------------------------------------------------------
# submit_url_to_gsc — full flow with mocked Google API
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_url_to_gsc_returns_submitted_on_200(enabled_settings):
    from services import gsc_integration as mod

    # Stub the access-token getter to skip the DB + refresh path.
    async def _fake_token(_merchant_id):
        return "live-access-token"

    # Capture upsert + http call.
    upsert_calls = []
    async def _fake_upsert(**kwargs):
        upsert_calls.append(kwargs)

    mock_response = MagicMock()
    mock_response.status_code = 200

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, url, **kwargs):
            assert url == mod.INDEXING_API_PUBLISH_URL
            assert kwargs["json"]["url"] == "https://m.example/p/1"
            assert kwargs["json"]["type"] == "URL_UPDATED"
            return mock_response

    with patch.object(mod, "_get_valid_access_token", AsyncMock(side_effect=_fake_token)):
        with patch.object(mod, "_upsert_url_submission", AsyncMock(side_effect=_fake_upsert)):
            with patch("httpx.AsyncClient", return_value=_Client()):
                result = await mod.submit_url_to_gsc(
                    "merch_test", "https://m.example/p/1",
                    audit_run_id="run-1",
                )
    assert result["status"] == "submitted"
    assert len(upsert_calls) == 1
    assert upsert_calls[0]["last_status"] == "submitted"
    assert upsert_calls[0]["audit_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_submit_url_to_gsc_records_error_on_non_200(enabled_settings):
    from services import gsc_integration as mod

    async def _fake_token(_merchant_id):
        return "live-access-token"

    upsert_calls = []
    async def _fake_upsert(**kwargs):
        upsert_calls.append(kwargs)

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "Quota exceeded"

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs):
            return mock_response

    with patch.object(mod, "_get_valid_access_token", AsyncMock(side_effect=_fake_token)):
        with patch.object(mod, "_upsert_url_submission", AsyncMock(side_effect=_fake_upsert)):
            with patch("httpx.AsyncClient", return_value=_Client()):
                result = await mod.submit_url_to_gsc("m", "https://e/p")
    assert result["status"] == "error"
    assert "429" in result["message"]
    # Error-state upsert recorded
    assert len(upsert_calls) == 1
    assert upsert_calls[0]["last_status"] == "error"


@pytest.mark.asyncio
async def test_submit_url_to_gsc_raises_when_no_token(enabled_settings):
    """Merchant hasn't completed OAuth → no token row → loud error."""
    from services import gsc_integration as mod
    from services.gsc_integration import GscNotConfiguredError

    async def _no_token(_merchant_id):
        return None

    with patch.object(mod, "_get_valid_access_token", AsyncMock(side_effect=_no_token)):
        with pytest.raises(GscNotConfiguredError):
            await mod.submit_url_to_gsc("m", "https://e/p")


# -----------------------------------------------------------------
# get_index_status — verdict mapping
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_index_status_maps_pass_to_indexed(enabled_settings):
    from services import gsc_integration as mod

    async def _fake_token(_): return "tok"
    async def _fake_site(_): return "https://m.example/"
    async def _fake_upsert(**kwargs):
        pass

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={
        "inspectionResult": {
            "indexStatusResult": {
                "verdict": "PASS",
                "coverageState": "Submitted and indexed",
            },
        },
    })

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return mock_response

    with patch.object(mod, "_get_valid_access_token", AsyncMock(side_effect=_fake_token)):
        with patch.object(mod, "_get_authorized_site_url", AsyncMock(side_effect=_fake_site)):
            with patch.object(mod, "_upsert_url_submission", AsyncMock(side_effect=_fake_upsert)):
                with patch("httpx.AsyncClient", return_value=_Client()):
                    result = await mod.get_index_status("m", "https://m.example/p/1")
    assert result["status"] == "indexed"
    assert result["verdict"] == "PASS"


@pytest.mark.asyncio
async def test_submit_audit_canonical_urls_skips_when_flag_off(monkeypatch):
    """Feature flag off → empty list, no API calls attempted."""
    from services import gsc_integration as mod
    from config import settings as settings_module
    monkeypatch.setattr(settings_module.settings, "gsc_integration_enabled", False)
    result = await mod.submit_audit_canonical_urls(
        merchant_id="m",
        brand_report={
            "per_product": [
                {
                    "merchant_pdp_url": "https://agent.pivota.cc/products/sig_x",
                    "merchant_view": {"headline": {"url_source": "pivota_canonical_pdp"}},
                },
            ],
        },
    )
    assert result == []


# submit_audit_canonical_urls now delegates to the Pivota-credential batch
# helper (ADR-006): canonical PDPs live on agent.pivota.cc, so they are
# submitted under Pivota's service account — never the merchant's OAuth, and
# with NO is_gsc_integrated() gate. The batch mechanics (dedupe, single token
# mint, all-error-on-no-token, credential routing) are pinned in
# tests/test_gsc_pivota_submission.py; these cover the audit-report walk.

def _pivota_submit_enabled(monkeypatch):
    from config import settings as settings_module
    monkeypatch.setattr(settings_module.settings, "gsc_pivota_submit_enabled", True)


@pytest.mark.asyncio
async def test_submit_audit_canonical_urls_skips_non_pivota_canonical(
    enabled_settings, monkeypatch,
):
    """Only url_source=pivota_canonical_pdp gets submitted. External
    URLs / merchant.com URLs flow through different auth (they need
    per-merchant OAuth on the merchant's own domain)."""
    from services import gsc_integration as mod

    _pivota_submit_enabled(monkeypatch)

    submit_calls = []
    async def _fake_submit(url, *, merchant_id, audit_run_id=None, access_token=None):
        submit_calls.append(url)
        return {"status": "submitted", "url": url}

    with patch.object(mod, "_get_pivota_access_token", AsyncMock(return_value="tok")):
        with patch.object(mod, "submit_pivota_canonical_url", AsyncMock(side_effect=_fake_submit)):
            result = await mod.submit_audit_canonical_urls(
                merchant_id="m",
                brand_report={
                    "per_product": [
                        {
                            "merchant_pdp_url": "https://merchant.com/p/1",
                            "merchant_view": {"headline": {"url_source": "merchant_external"}},
                        },
                        {
                            "merchant_pdp_url": "https://agent.pivota.cc/products/sig_a",
                            "merchant_view": {"headline": {"url_source": "pivota_canonical_pdp"}},
                        },
                    ],
                },
            )
    # Only the canonical URL was submitted
    assert submit_calls == ["https://agent.pivota.cc/products/sig_a"]
    assert len(result) == 1
    assert result[0]["status"] == "submitted"


@pytest.mark.asyncio
async def test_submit_audit_canonical_urls_noop_when_flag_off(enabled_settings, monkeypatch):
    """GSC_PIVOTA_SUBMIT_ENABLED=false → no submissions attempted, empty
    result — the audit response succeeds without touching Google."""
    from config import settings as settings_module
    from services import gsc_integration as mod

    monkeypatch.setattr(settings_module.settings, "gsc_pivota_submit_enabled", False)

    submit = AsyncMock(return_value={"status": "submitted"})
    with patch.object(mod, "submit_pivota_canonical_url", submit):
        result = await mod.submit_audit_canonical_urls(
            merchant_id="m",
            brand_report={
                "per_product": [
                    {
                        "merchant_pdp_url": "https://agent.pivota.cc/products/sig_a",
                        "merchant_view": {"headline": {"url_source": "pivota_canonical_pdp"}},
                    },
                ],
            },
        )
    assert result == []
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_audit_canonical_urls_dedupes(enabled_settings, monkeypatch):
    """Same URL across per_product reports submitted once."""
    from services import gsc_integration as mod

    _pivota_submit_enabled(monkeypatch)

    submit_calls = []
    async def _fake_submit(url, *, merchant_id, audit_run_id=None, access_token=None):
        submit_calls.append(url)
        return {"status": "submitted"}

    with patch.object(mod, "_get_pivota_access_token", AsyncMock(return_value="tok")):
        with patch.object(mod, "submit_pivota_canonical_url", AsyncMock(side_effect=_fake_submit)):
            await mod.submit_audit_canonical_urls(
                merchant_id="m",
                brand_report={
                    "per_product": [
                        {
                            "merchant_pdp_url": "https://agent.pivota.cc/products/sig_a",
                            "merchant_view": {"headline": {"url_source": "pivota_canonical_pdp"}},
                        },
                        {
                            "merchant_pdp_url": "https://agent.pivota.cc/products/sig_a",
                            "merchant_view": {"headline": {"url_source": "pivota_canonical_pdp"}},
                        },
                    ],
                },
            )
    assert submit_calls == ["https://agent.pivota.cc/products/sig_a"]


@pytest.mark.asyncio
async def test_submit_audit_canonical_urls_swallows_per_url_failures(
    enabled_settings, monkeypatch,
):
    """If one URL fails, others still get submitted. Failures are
    caught + logged, never raised — audit response succeeds."""
    from services import gsc_integration as mod

    _pivota_submit_enabled(monkeypatch)

    async def _fake_submit(url, *, merchant_id, audit_run_id=None, access_token=None):
        if "fail" in url:
            raise RuntimeError("simulated network failure")
        return {"status": "submitted"}

    with patch.object(mod, "_get_pivota_access_token", AsyncMock(return_value="tok")):
        with patch.object(mod, "submit_pivota_canonical_url", AsyncMock(side_effect=_fake_submit)):
            results = await mod.submit_audit_canonical_urls(
                merchant_id="m",
                brand_report={
                    "per_product": [
                        {"merchant_pdp_url": "https://agent.pivota.cc/products/sig_ok",
                         "merchant_view": {"headline": {"url_source": "pivota_canonical_pdp"}}},
                        {"merchant_pdp_url": "https://agent.pivota.cc/products/sig_fail",
                         "merchant_view": {"headline": {"url_source": "pivota_canonical_pdp"}}},
                        {"merchant_pdp_url": "https://agent.pivota.cc/products/sig_ok2",
                         "merchant_view": {"headline": {"url_source": "pivota_canonical_pdp"}}},
                    ],
                },
            )
    assert len(results) == 3
    # 2 succeeded, 1 errored
    statuses = sorted([r["status"] for r in results])
    assert statuses == ["error", "submitted", "submitted"]


@pytest.mark.asyncio
async def test_get_index_status_maps_neutral_to_pending(enabled_settings):
    from services import gsc_integration as mod

    async def _fake_token(_): return "tok"
    async def _fake_site(_): return "https://m.example/"
    async def _fake_upsert(**kwargs): pass

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={
        "inspectionResult": {
            "indexStatusResult": {
                "verdict": "NEUTRAL",
                "coverageState": "Submitted, currently not indexed",
            },
        },
    })

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return mock_response

    with patch.object(mod, "_get_valid_access_token", AsyncMock(side_effect=_fake_token)):
        with patch.object(mod, "_get_authorized_site_url", AsyncMock(side_effect=_fake_site)):
            with patch.object(mod, "_upsert_url_submission", AsyncMock(side_effect=_fake_upsert)):
                with patch("httpx.AsyncClient", return_value=_Client()):
                    result = await mod.get_index_status("m", "https://m.example/p/1")
    assert result["status"] == "pending"

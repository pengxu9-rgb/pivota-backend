"""ADR-006 — Pivota-owned-credential GSC submission path.

Covers the flag/credential gates, the submit + status-read-back shapes
(with the Google HTTP calls and DB upserts mocked), the batch helper, and
the service-account JWT-bearer token exchange (real RS256 key; we decode the
posted assertion and assert its claims). Nothing here hits Google or the DB.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest

from config import settings as settings_module
from services import gsc_integration as mod


# ---- fakes for httpx -------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient."""
    def __init__(self, resp, capture=None, counter=None):
        self._resp = resp
        self._capture = capture
        self._counter = counter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        if self._capture is not None:
            self._capture.clear()
            self._capture.update({"url": url, **kw})
        if self._counter is not None:
            self._counter["n"] += 1
        return self._resp


def _enable_pivota(monkeypatch, *, sa_json="{}", prop="https://agent.pivota.cc/"):
    monkeypatch.setattr(settings_module.settings, "gsc_pivota_submit_enabled", True)
    monkeypatch.setattr(settings_module.settings, "gsc_pivota_service_account_json", sa_json)
    monkeypatch.setattr(settings_module.settings, "gsc_pivota_property_url", prop)


def _rsa_service_account_json():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return json.dumps({
        "type": "service_account",
        "private_key": pem,
        "private_key_id": "kid-123",
        "client_email": "svc@proj.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    })


@pytest.fixture(autouse=True)
def _clear_token_cache():
    mod._pivota_token_cache.clear()
    yield
    mod._pivota_token_cache.clear()


# ---- gate behavior ---------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_raises_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings_module.settings, "gsc_pivota_submit_enabled", False)
    with pytest.raises(mod.GscNotConfiguredError):
        await mod.submit_pivota_canonical_url(
            "https://agent.pivota.cc/products/sig_1", merchant_id="m1",
        )


@pytest.mark.asyncio
async def test_require_raises_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(settings_module.settings, "gsc_pivota_submit_enabled", True)
    monkeypatch.setattr(settings_module.settings, "gsc_pivota_service_account_json", "")
    monkeypatch.setattr(settings_module.settings, "gsc_pivota_property_url", "")
    with pytest.raises(mod.GscNotConfiguredError):
        mod._require_pivota_enabled()


@pytest.mark.asyncio
async def test_batch_returns_empty_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings_module.settings, "gsc_pivota_submit_enabled", False)
    out = await mod.submit_pivota_canonical_urls(
        merchant_id="m1", urls=["https://agent.pivota.cc/products/sig_1"],
    )
    assert out == []


# ---- submit happy / error paths --------------------------------------------

@pytest.mark.asyncio
async def test_submit_happy_path_upserts_submitted(monkeypatch):
    _enable_pivota(monkeypatch)
    upserts = []

    async def _fake_upsert(**kw):
        upserts.append(kw)

    with patch.object(mod, "_get_pivota_access_token", AsyncMock(return_value="tok")), \
         patch.object(mod, "_indexing_api_publish", AsyncMock(return_value=(200, "{}"))), \
         patch.object(mod, "_upsert_url_submission", AsyncMock(side_effect=_fake_upsert)):
        res = await mod.submit_pivota_canonical_url(
            "https://agent.pivota.cc/products/sig_1", merchant_id="m1",
        )
    assert res["status"] == "submitted"
    assert res["last_status"] == "submitted"
    assert upserts and upserts[0]["last_status"] == "submitted"
    assert upserts[0]["merchant_id"] == "m1"  # attribution kept


@pytest.mark.asyncio
async def test_submit_records_error_on_403(monkeypatch):
    """Google's 'user is not an owner' rejection → error row, no raise."""
    _enable_pivota(monkeypatch)
    errors = []

    async def _fake_err(merchant_id, url, error, audit_run_id):
        errors.append((merchant_id, url, error))

    with patch.object(mod, "_get_pivota_access_token", AsyncMock(return_value="tok")), \
         patch.object(mod, "_indexing_api_publish",
                      AsyncMock(return_value=(403, "user is not an owner"))), \
         patch.object(mod, "_record_url_submission_error", AsyncMock(side_effect=_fake_err)):
        res = await mod.submit_pivota_canonical_url(
            "https://agent.pivota.cc/products/sig_1", merchant_id="m1",
        )
    assert res["status"] == "error"
    assert errors and "403" in errors[0][2]


@pytest.mark.asyncio
async def test_submit_raises_when_token_unavailable(monkeypatch):
    _enable_pivota(monkeypatch)
    with patch.object(mod, "_get_pivota_access_token", AsyncMock(return_value=None)):
        with pytest.raises(mod.GscNotConfiguredError):
            await mod.submit_pivota_canonical_url(
                "https://agent.pivota.cc/products/sig_1", merchant_id="m1",
            )


# ---- status read-back ------------------------------------------------------

@pytest.mark.asyncio
async def test_index_status_marks_indexed_on_pass(monkeypatch):
    _enable_pivota(monkeypatch)
    body = {"inspectionResult": {"indexStatusResult": {
        "verdict": "PASS", "coverageState": "Submitted and indexed"}}}
    upserts = []

    async def _fake_upsert(**kw):
        upserts.append(kw)

    with patch.object(mod, "_get_pivota_access_token", AsyncMock(return_value="tok")), \
         patch.object(mod, "_url_inspection", AsyncMock(return_value=(200, body))), \
         patch.object(mod, "_upsert_url_submission", AsyncMock(side_effect=_fake_upsert)):
        res = await mod.get_pivota_index_status(
            "https://agent.pivota.cc/products/sig_1", merchant_id="m1",
        )
    assert res["status"] == "indexed"
    assert upserts[0]["last_status"] == "indexed"
    assert upserts[0]["indexed_at"] is not None


# ---- batch dedupe ----------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_dedupes_and_submits(monkeypatch):
    _enable_pivota(monkeypatch)
    submitted = []

    async def _fake_submit(url, *, merchant_id, audit_run_id=None, access_token=None):
        submitted.append(url)
        return {"status": "submitted", "url": url}

    with patch.object(mod, "_get_pivota_access_token", AsyncMock(return_value="tok")), \
         patch.object(mod, "submit_pivota_canonical_url", AsyncMock(side_effect=_fake_submit)):
        out = await mod.submit_pivota_canonical_urls(
            merchant_id="m1",
            urls=["https://a/sig_1", "https://a/sig_1", "https://a/sig_2", "", None],
        )
    assert sorted(submitted) == ["https://a/sig_1", "https://a/sig_2"]
    assert len(out) == 2


@pytest.mark.asyncio
async def test_batch_mints_token_once_for_whole_fan_out(monkeypatch):
    """Cold cache + N URLs must trigger exactly ONE token exchange, not N
    (ADR-006 open-question #4: token-endpoint quota)."""
    _enable_pivota(monkeypatch, sa_json=_rsa_service_account_json())
    counter = {"n": 0}
    token_resp = _FakeResp(200, {"access_token": "ya29.fake", "expires_in": 3599})

    async def _fake_publish(access_token, url):
        assert access_token == "ya29.fake"  # the threaded, pre-minted token
        return (200, "{}")

    with patch("httpx.AsyncClient", lambda *a, **k: _FakeClient(token_resp, counter=counter)), \
         patch.object(mod, "_indexing_api_publish", AsyncMock(side_effect=_fake_publish)), \
         patch.object(mod, "_upsert_url_submission", AsyncMock()):
        out = await mod.submit_pivota_canonical_urls(
            merchant_id="m1",
            urls=["https://a/sig_1", "https://a/sig_2", "https://a/sig_3"],
        )
    assert len(out) == 3
    assert all(r["status"] == "submitted" for r in out)
    assert counter["n"] == 1  # one token exchange for the whole batch


@pytest.mark.asyncio
async def test_batch_all_error_when_token_unavailable(monkeypatch):
    _enable_pivota(monkeypatch)
    with patch.object(mod, "_get_pivota_access_token", AsyncMock(return_value=None)):
        out = await mod.submit_pivota_canonical_urls(
            merchant_id="m1", urls=["https://a/sig_1", "https://a/sig_2"],
        )
    assert len(out) == 2
    assert all(r["status"] == "error" for r in out)


# ---- JWT-bearer token exchange ---------------------------------------------

@pytest.mark.asyncio
async def test_token_exchange_builds_valid_jwt_assertion(monkeypatch):
    _enable_pivota(monkeypatch, sa_json=_rsa_service_account_json())
    capture: dict = {}
    resp = _FakeResp(200, {"access_token": "ya29.fake", "expires_in": 3599})

    with patch("httpx.AsyncClient", lambda *a, **k: _FakeClient(resp, capture=capture)):
        token = await mod._get_pivota_access_token()

    assert token == "ya29.fake"
    data = capture["data"]
    assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    # decode the posted assertion (signature already produced by our RSA key)
    claims = pyjwt.decode(data["assertion"], options={"verify_signature": False})
    assert claims["iss"] == "svc@proj.iam.gserviceaccount.com"
    assert claims["aud"] == "https://oauth2.googleapis.com/token"
    assert "indexing" in claims["scope"]
    assert "webmasters" in claims["scope"]
    assert claims["exp"] > claims["iat"]


@pytest.mark.asyncio
async def test_token_is_cached_between_calls(monkeypatch):
    _enable_pivota(monkeypatch, sa_json=_rsa_service_account_json())
    counter = {"n": 0}
    resp = _FakeResp(200, {"access_token": "ya29.fake", "expires_in": 3599})

    with patch("httpx.AsyncClient", lambda *a, **k: _FakeClient(resp, counter=counter)):
        t1 = await mod._get_pivota_access_token()
        t2 = await mod._get_pivota_access_token()
    assert t1 == t2 == "ya29.fake"
    assert counter["n"] == 1  # second call served from cache, no second exchange


@pytest.mark.asyncio
async def test_token_returns_none_on_bad_json(monkeypatch):
    _enable_pivota(monkeypatch, sa_json="not json{")
    assert await mod._get_pivota_access_token() is None


@pytest.mark.asyncio
async def test_no_secret_leak_in_logs_on_token_exchange_failure(monkeypatch, caplog):
    """A failed token exchange must not log the signed JWT assertion or the
    service-account private key — only Google's (token-free) error body."""
    _enable_pivota(monkeypatch, sa_json=_rsa_service_account_json())
    # Google rejects with a typical error body that carries NO token.
    resp = _FakeResp(400, text='{"error":"invalid_grant"}')
    with caplog.at_level("ERROR"), \
         patch("httpx.AsyncClient", lambda *a, **k: _FakeClient(resp)):
        out = await mod._get_pivota_access_token()
    assert out is None
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "PRIVATE KEY" not in logged          # no PEM body
    assert "eyJ" not in logged                   # no JWT assertion (base64 'eyJ...')
    assert "invalid_grant" in logged             # but the safe error IS logged

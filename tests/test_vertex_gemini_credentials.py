"""Credential resolution for the Vertex transport seam.

The live token mint needs GCP, but which credential SOURCE gets chosen — and
what happens when there is none — is the part that broke on staging, and it is
checkable here.
"""

import importlib
import json

import pytest


@pytest.fixture
def vg(monkeypatch):
    """Reload the module so settings pick up patched env each time."""

    def _load(**env):
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        import config.settings as cs

        importlib.reload(cs)
        import services.vertex_gemini as mod

        importlib.reload(mod)
        mod.reset_credentials_cache()
        return mod

    return _load


def test_flag_off_gates_on_api_key(vg):
    mod = vg(VERTEX_AI_ENABLED="false", GEMINI_API_KEY="ak-test")
    assert mod.credentials_available() is True

    mod = vg(VERTEX_AI_ENABLED="false", GEMINI_API_KEY="")
    assert mod.credentials_available() is False


def test_flag_on_with_no_credential_fails_closed(vg):
    """Staging's exact shape: flag on, project set, no credential anywhere.

    Must degrade rather than report available — callers treat this as "is the
    LLM usable", and a false positive turns a clean degradation into a
    mid-request exception.
    """
    mod = vg(
        VERTEX_AI_ENABLED="true",
        GOOGLE_CLOUD_PROJECT="project-f165a637-145f-4de2-89d",
        GOOGLE_APPLICATION_CREDENTIALS=None,
        GOOGLE_APPLICATION_CREDENTIALS_JSON=None,
    )
    assert mod.credentials_available() is False


def test_inline_json_credential_is_preferred_over_adc(vg, monkeypatch):
    """GOOGLE_APPLICATION_CREDENTIALS_JSON must be read as key material.

    google.auth.default() only understands a FILE PATH, which is unusable on a
    host that can only supply env vars.
    """
    info = {"type": "service_account", "project_id": "proj-from-key"}
    mod = vg(
        VERTEX_AI_ENABLED="true",
        GOOGLE_CLOUD_PROJECT="proj-from-key",
        GOOGLE_APPLICATION_CREDENTIALS_JSON=json.dumps(info),
    )

    seen = {}

    class _FakeCreds:
        token = "tok-123"
        valid = True

    class _FakeSA:
        @staticmethod
        def from_service_account_info(parsed, scopes=None):
            seen["info"] = parsed
            seen["scopes"] = scopes
            return _FakeCreds()

    import sys
    import types

    def _boom(*_a, **_k):  # google.auth.default must NOT be consulted
        raise AssertionError("google.auth.default() called despite inline JSON")

    # Stub the whole package chain rather than importing: this asserts which
    # credential SOURCE the module picks, which is independent of google-auth
    # being installed in the test environment.
    google_mod = types.ModuleType("google")
    oauth2_mod = types.ModuleType("google.oauth2")
    sa_mod = types.ModuleType("google.oauth2.service_account")
    auth_mod = types.ModuleType("google.auth")
    sa_mod.Credentials = _FakeSA
    auth_mod.default = _boom
    oauth2_mod.service_account = sa_mod
    google_mod.oauth2 = oauth2_mod
    google_mod.auth = auth_mod
    for name, module in (
        ("google", google_mod),
        ("google.oauth2", oauth2_mod),
        ("google.oauth2.service_account", sa_mod),
        ("google.auth", auth_mod),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    creds = mod._load_credentials()
    assert creds.token == "tok-123"
    assert seen["info"] == info
    assert "https://www.googleapis.com/auth/cloud-platform" in seen["scopes"]

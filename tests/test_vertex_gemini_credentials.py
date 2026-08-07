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
    """Reload the module so settings pick up patched env each time, then put
    the process back the way it was found.

    THE RESTORE IS NOT TIDINESS. `monkeypatch` unwinds env vars and
    `sys.modules` entries, but it has no idea these modules were RELOADED — and
    a reload bakes the patched env into module state that outlives the test.
    Without the teardown below, the last reload here (`VERTEX_AI_ENABLED=true`)
    left `services.vertex_gemini.settings.vertex_ai_enabled` True, and
    `_load_credentials()` left the `_FakeCreds` stub cached in the module-global
    `_credentials`, for the whole rest of the session. Thirteen non-test modules
    read that state.

    The visible damage was two tests failing ONLY in a full-suite run, both
    downstream of this file alphabetically, and both mystifying in isolation:

      - `test_w2_retailer_seller_model` — `credentials_available("")` answered
        True off the stale cache, so the validator took the LIVE Vertex path and
        died on `ModuleNotFoundError: No module named 'google'` (the fake
        `google` package IS unwound by monkeypatch; the credential minted from
        it is not).
      - `test_winnable_prompts` — the same stale True made gemini look usable,
        so the deepseek fallback never fired.

    Ordering matters and is the whole trick: `monkeypatch` is torn down AFTER
    this fixture's finalizer, so reloading here naively would rebuild the
    modules from the STILL-PATCHED env and restore nothing. `monkeypatch.undo()`
    puts the real env back first; the reloads then rebuild from it.
    """

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

    yield _load

    # Real env first, then rebuild from it. Unconditional: a test that failed
    # partway through is exactly when the leak would otherwise escape.
    #
    # No `reset_credentials_cache()` here, deliberately: reloading re-executes
    # the module body, which rebinds `_credentials = None` on its own. A mutation
    # that deleted the extra call could not be killed — it was doing nothing.
    # Both reloads below ARE load-bearing; dropping either one is caught.
    monkeypatch.undo()
    import config.settings as cs

    importlib.reload(cs)
    import services.vertex_gemini as mod

    importlib.reload(mod)


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


def test_the_fixture_leaves_no_vertex_state_behind():
    """Defined LAST on purpose: pytest runs tests in definition order, so this
    executes after every `vg` test above and sees exactly what the next FILE
    would see.

    It deliberately does not request `vg` — it inspects the module the way an
    unrelated downstream test does. Before the fixture restored state, both
    assertions below failed here, which is the leak that broke
    `test_w2_retailer_seller_model` and `test_winnable_prompts` in full-suite
    runs while both passed in isolation.

    A cross-file leak is normally invisible until some unrelated test fails for
    reasons that make no sense at its own call site. This turns that into a
    local, named failure in the file that causes it.
    """
    import services.vertex_gemini as mod

    assert mod.vertex_enabled() is False, (
        "VERTEX_AI_ENABLED leaked out of this file — every later test now takes "
        "the live Vertex path")
    assert mod._credentials is None, (
        "a credential minted from this file's stubbed google package is still "
        "cached — later callers will think Vertex is usable and fail on the "
        "real import")

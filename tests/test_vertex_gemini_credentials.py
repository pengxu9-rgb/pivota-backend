"""Credential resolution for the Vertex transport seam.

The live token mint needs GCP, but which credential SOURCE gets chosen — and
what happens when there is none — is the part that broke on staging, and it is
checkable here.
"""

import importlib
import json
import sys

import pytest

import config.settings as _config_settings

# The `settings` object this process had before this file did anything, captured
# at import time. `importlib.reload(config.settings)` MINTS A NEW Settings
# object, and the ~76 modules that did `from config.settings import settings` at
# their own import time keep referring to the ORIGINAL. Restoring only values
# would leave those modules reading an object that `config.settings.settings` no
# longer names — so a later `patch("config.settings.settings.gemini_api_key", …)`
# would patch something nobody reads. The teardown restores this identity, and
# `test_the_fixture_leaves_no_vertex_state_behind` pins it.
_SETTINGS_AT_IMPORT = _config_settings.settings


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
    `_credentials`, for the whole rest of the session. Eleven non-test modules
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

    THE TWO SYMPTOMS DIFFER IN WHERE THEY APPEAR, which is why only one of them
    ever earned a CI quarantine entry. `google-auth` is in `requirements.txt`,
    so CI has it and a bare dev venv usually does not. Without it, w2 dies on
    the import above; with it, the import succeeds, the stubbed credential
    yields a token, the outbound call fails, and the assertion passes anyway.
    So w2 is a LOCAL-ONLY symptom and `backend-test-sweep` stayed green on main,
    while `test_winnable_prompts` fails either way and was deselected there. Same
    leak, two blast radii — do not conclude from a green CI run that this class
    of bug is absent.

    Neither leak breaks them ALONE — driven both ways. With only the flag
    leaked, `credentials_available()` reaches `_load_credentials()`, finds no
    usable ADC, and returns False; later tests take the DEGRADED path, not the
    live one. It takes the flag AND the cached stub together to make a dead
    credential look usable.

    The teardown is deliberately TWO lines, and the order between them is the
    only ordering that matters: restore the canonical `settings` OBJECT first,
    then reload `services.vertex_gemini` so it rebinds to that object and resets
    its own globals. Reversed, the module binds to a throwaway Settings instance
    and a quieter identity split survives.

    What is NOT here, because mutation testing showed it did nothing:

      - `monkeypatch.undo()`. An earlier draft called it first, reasoning that
        `monkeypatch` finalizes AFTER this fixture so the env would still be
        patched. The premise is true; the conclusion was not. Nothing reloaded
        here reads env at module level — `services/vertex_gemini.py` reads
        `GOOGLE_APPLICATION_CREDENTIALS_JSON` inside a function, and
        `config/settings.py`'s only dynamic global is `settings` itself, which
        this teardown overwrites outright. Deleting the call killed no mutation.
        It also carried a real hazard: `undo()` unwinds the SHARED instance,
        including patches the test made itself.
      - `importlib.reload(config.settings)`. Same reason — its only effect was
        to rebuild the object on the very next line.
      - `reset_credentials_cache()`. `importlib.reload` re-executes the module
        body, which rebinds `_credentials = None` and `_credentials_failed =
        False` already.
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
        return mod

    yield _load

    # Unconditional: a test that failed partway through is exactly when the leak
    # would otherwise escape. `monkeypatch` handles env and `sys.modules` on its
    # own; these two lines handle what it cannot see. Both are load-bearing and
    # so is their order — every mutation of either is caught by the pin at the
    # end of this file.
    _config_settings.settings = _SETTINGS_AT_IMPORT
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

    import types

    def _boom(*_a, **_k):  # google.auth.default must NOT be consulted
        raise AssertionError("google.auth.default() called despite inline JSON")

    # Stub the whole package chain rather than importing: this asserts which
    # credential SOURCE the module picks, which is independent of google-auth
    # being installed in the test environment.
    #
    # `monkeypatch.setitem` and not a bare `sys.modules[name] = module`: the
    # bare form leaves these fakes in place for every later import in the
    # session. The pin below catches that specific slip, because nothing else
    # would.
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

    It deliberately does not request `vg` — it inspects the process the way an
    unrelated downstream test does. A cross-file leak is normally invisible
    until some unrelated test fails for reasons that make no sense at its own
    call site; this turns that into a local, named failure in the file that
    causes it.

    Every assertion below corresponds to a mutation that leaked real state past
    this file. It covers the whole surface this fixture touches — module
    globals, settings identity, and `sys.modules` — rather than only the two
    globals behind the original bug, because an adversarial pass found three
    leaking mutations that an assertion pair could not see.

    LIMIT, so nobody reads more into a green run than it means: this pin is an
    ordinary test and therefore deselectable. `-k`, an explicit node id, or
    `--lf` can run the polluting tests above without running this one, and
    nothing goes red. That costs regression DETECTION only — the teardown is
    unconditional, so the leak is still fixed in those runs — and the CI job
    that contains this file runs the whole tree, where the pin does fire.
    """
    import services.vertex_gemini as mod

    # 1. The env-derived flag. Alone this yields the DEGRADED path, not the live
    #    one — it takes this plus a cached credential to fake a usable Vertex.
    assert mod.vertex_enabled() is False, (
        "VERTEX_AI_ENABLED leaked out of this file")

    # 2. The credential minted from this file's STUBBED google package. Paired
    #    with (1), later callers think Vertex is usable and then die on the real
    #    import, which is not installed here.
    assert mod._credentials is None, (
        "a credential minted from this file's stubbed google package is still "
        "cached")

    # 3. The failure latch is a third global on the same module; a leaked True
    #    would make Vertex look permanently broken to every later caller.
    assert mod._credentials_failed is False, (
        "the credential failure latch leaked — later callers will treat Vertex "
        "as permanently unavailable")

    # 4. Settings IDENTITY, not just values. `importlib.reload` mints a new
    #    Settings object; if `config.settings.settings` no longer names the one
    #    ~76 already-imported modules hold, a later
    #    `patch("config.settings.settings.<attr>")` silently patches an object
    #    nobody reads — a fresh order-dependent bug of exactly the shape this
    #    teardown exists to prevent.
    assert _config_settings.settings is _SETTINGS_AT_IMPORT, (
        "config.settings.settings was replaced — every module that imported it "
        "earlier now reads a different object than later patches target")

    # 4b. The same check one level down. Reloading vertex_gemini re-runs its
    #     `from config.settings import settings`, so it binds to whatever
    #     `config.settings.settings` is AT THAT MOMENT. Restore the canonical
    #     object after that reload instead of before and this module alone ends
    #     up holding a throwaway Settings — values right, identity wrong, and
    #     assertion 4 above none the wiser.
    assert mod.settings is _SETTINGS_AT_IMPORT, (
        "services.vertex_gemini bound a different Settings object than the "
        "canonical one — the teardown's two lines are in the wrong order")

    # 5. The fake `google` package chain. A hand-built `types.ModuleType` has
    #    `__spec__ is None`; a genuinely imported module always has one. So the
    #    contract is "absent, or real", which stays correct on a machine where
    #    google-auth IS installed.
    for name in ("google", "google.auth", "google.oauth2", "google.oauth2.service_account"):
        stub = sys.modules.get(name)
        assert stub is None or getattr(stub, "__spec__", None) is not None, (
            f"a stub {name!r} module leaked into sys.modules — later imports "
            f"resolve to this file's fake instead of the real package")

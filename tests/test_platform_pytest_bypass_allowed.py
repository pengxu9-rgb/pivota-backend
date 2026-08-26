"""Unit tests for :func:`config.platform.pytest_bypass_allowed`.

This is the shared helper extracted while closing two more instances of the
bug PR #1893 fixed in ``utils/auth.py``: a `PYTEST_CURRENT_TEST`-gated
shortcut with no `is_production()` check, in `routes/agent_auth.py` and
`routes/agent_briefs.py`. Centralizing the check here means a new call site
can't forget the deployment conjunct the way these two did.

The gate is `not (is_deployed() or is_production())`, NOT `not
is_production()`. Both halves are pinned below, because neither implies the
other:

  * `is_deployed()` alone would leave `PIVOTA_ENV=production` on an unmanaged
    host armed (`platform_name()` is "local" there).
  * `is_production()` alone left every staging revision and every
    `PIVOTA_ENV=development` revision armed, which is what this file's
    `_DEPLOYED_NON_PROD_ENVS` cases exist to kill.
"""
from __future__ import annotations

import pytest

from config import platform as P
from tests.test_platform_shim import _ALL_KEYS


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    P.reset_platform_state()
    yield
    P.reset_platform_state()


def test_false_when_pytest_current_test_is_unset():
    # PYTEST_CURRENT_TEST is genuinely set in os.environ for the duration of
    # this test run (pytest sets it itself), so this passes an explicit empty
    # env mapping rather than trying to unset the real one.
    assert P.pytest_bypass_allowed(env={}) is False


def test_true_when_pytest_current_test_is_set_outside_production(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y (call)")
    assert P.pytest_bypass_allowed() is True


#: Environments that resolve to `production`. These were already blocked by
#: PR #1897 and stay blocked. `PIVOTA_ENV` alone here is the case that keeps
#: the `or is_production(env)` conjunct load-bearing: no platform marker is
#: set, so `is_deployed()` is False and only `is_production()` refuses it.
_PRODUCTION_ENVS = {
    "pivota_env_production_unmanaged_host": {"PIVOTA_ENV": "production"},
    "railway_production": {"RAILWAY_ENVIRONMENT": "production"},
    "cloud_run_fail_closed_no_pivota_env": {"K_SERVICE": "pivota-backend-prod"},
    "cloud_run_pivota_env_typo_fail_closed": {
        "K_SERVICE": "pivota-backend",
        "PIVOTA_ENV": "prodution",
    },
}

#: Deployed hosts that do NOT resolve to `production`. Every one of these was
#: ARMED under PR #1897's `not is_production()` gate; they are the actual
#: behavior change and only `is_deployed()` refuses them.
_DEPLOYED_NON_PROD_ENVS = {
    "cloud_run_staging": {
        "K_SERVICE": "pivota-backend-staging",
        "PIVOTA_ENV": "staging",
    },
    "cloud_run_development": {
        "K_SERVICE": "pivota-backend",
        "PIVOTA_ENV": "development",
    },
    "railway_staging": {"RAILWAY_ENVIRONMENT": "staging"},
    "cloud_run_marker_only_k_revision": {
        "K_REVISION": "pivota-backend-staging-00042-abc",
        "PIVOTA_ENV": "staging",
    },
    "railway_marker_only_service_id": {
        "RAILWAY_SERVICE_ID": "svc_123",
        "PIVOTA_ENV": "staging",
    },
    "pivota_platform_override_cloud_run": {
        "PIVOTA_PLATFORM": "cloud_run",
        "PIVOTA_ENV": "development",
    },
}


def _apply(monkeypatch, env: dict) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y (call)")
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("env", _PRODUCTION_ENVS.values(), ids=_PRODUCTION_ENVS)
def test_refuses_in_production_even_with_pytest_current_test_set(monkeypatch, env):
    """Mutant check: PYTEST_CURRENT_TEST alone must not be enough."""
    _apply(monkeypatch, env)
    assert P.is_production() is True
    assert P.pytest_bypass_allowed() is False


@pytest.mark.parametrize(
    "env", _DEPLOYED_NON_PROD_ENVS.values(), ids=_DEPLOYED_NON_PROD_ENVS
)
def test_refuses_on_any_deployed_host_even_outside_production(monkeypatch, env):
    """Mutant check: reverting to `not is_production()` re-arms all of these.

    Each case is asserted to be genuinely non-production first, so the test
    cannot pass for the wrong reason (e.g. a typo making it fail closed to
    production and get refused by the other conjunct).
    """
    _apply(monkeypatch, env)
    assert P.is_deployed() is True
    assert P.is_production() is False
    assert P.pytest_bypass_allowed() is False


def test_still_allowed_on_an_unmanaged_host(monkeypatch):
    """The suite's own case, and the one row this gate does NOT close.

    No platform markers at all -> `platform_name()` is "local" and the bypass
    stays armed. That is required (this is how CI and a developer laptop
    look), and it is also why a bare `docker run` of a test-stage image is
    still armed: it is environmentally indistinguishable from a laptop. Pinned
    so the limitation is visible rather than assumed closed.
    """
    _apply(monkeypatch, {})
    assert P.platform_name() == "local"
    assert P.is_deployed() is False
    assert P.pytest_bypass_allowed() is True


def test_ci_and_local_are_unmanaged_so_the_suite_is_unaffected():
    """The live process running this suite must resolve to an unmanaged host.

    If a CI workflow ever starts exporting `K_SERVICE`/`RAILWAY_*`/
    `PIVOTA_ENV`, every test that authenticates with `test-token` or
    `test-agent-key` breaks at once. This fails first and says why.
    """
    assert P.platform_name() == "local"
    assert P.is_deployed() is False
    assert P.pytest_bypass_allowed() is True


@pytest.mark.parametrize(
    "env",
    [_PRODUCTION_ENVS["pivota_env_production_unmanaged_host"]]
    + [_DEPLOYED_NON_PROD_ENVS["cloud_run_staging"]],
    ids=["production", "staging"],
)
def test_logs_a_warning_when_refused(monkeypatch, caplog, env):
    _apply(monkeypatch, env)
    with caplog.at_level("WARNING"):
        assert P.pytest_bypass_allowed(bypass_name="the widget bypass") is False
    assert any("the widget bypass" in record.message for record in caplog.records)


def test_the_warning_is_not_memoised(monkeypatch, caplog):
    """Deliberate: this is an attack signal, not a config-noise line.

    `_warn_once` guards the fail-closed *resolution* path, which fires on
    every accessor call. This one fires only when a test credential is
    presented to a deployed server, so every occurrence is a distinct attempt
    and collapsing repeats would hide the volume.
    """
    _apply(monkeypatch, _DEPLOYED_NON_PROD_ENVS["cloud_run_staging"])
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert P.pytest_bypass_allowed(bypass_name="the widget bypass") is False
    hits = [r for r in caplog.records if "the widget bypass" in r.message]
    assert len(hits) == 3, f"expected one line per attempt, got {len(hits)}"

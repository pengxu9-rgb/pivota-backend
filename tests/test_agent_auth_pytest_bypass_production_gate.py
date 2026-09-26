"""Gate tests for `get_agent_context`'s pytest-only test-key bypass.

`routes.agent_auth.get_agent_context` short-circuits to a hardcoded
unrestricted (`allowed_merchants=None`) agent identity whenever
`PYTEST_CURRENT_TEST` is set in the process AND the caller presents the
literal API key `test-agent-key` or `test-api-key` — no signature, no
database lookup, no rate/quota enforcement. This is the same class of bug PR
#1893 fixed for `utils.auth.get_current_user`'s `test-token` bypass: found
while reviewing that PR (see `docs/` / PR #1893 follow-up), because
`PYTEST_CURRENT_TEST` is just an environment variable that a debug image
built from a test stage, a copied `.env`, or a misconfigured smoke harness
could leak into a real server process.

The bypass is heavily load-bearing (dozens of tests authenticate with
`X-API-Key: test-agent-key`), so it is not removed here. Instead it routes
through `config.platform.pytest_bypass_allowed`, which refuses it on ANY
deployed host — `not (is_deployed() or is_production())`, not merely `not
is_production()` as PR #1897 first shipped. Staging is the case that matters:
it runs a restored production snapshot and real third-party credentials, so a
leaked `PYTEST_CURRENT_TEST` there hands anyone who knows `test-agent-key` an
agent context with `allowed_merchants=None` and no rate/quota enforcement.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from config import platform as P
from tests.test_platform_shim import _ALL_KEYS


def _request(path: str = "/agent/v2/commerce/checkouts") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _clean_platform_env(monkeypatch):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    P.reset_platform_state()
    yield
    P.reset_platform_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("test_key", ["test-agent-key", "test-api-key"])
async def test_test_key_bypass_works_outside_production(monkeypatch, test_key):
    """Sanity check: the bypass must still work for the rest of the suite."""
    import routes.agent_auth as module

    request = _request()
    context = await module.get_agent_context(request, api_key=test_key, checkout_token=None)

    assert context.agent_id == "agent_test"
    assert context.can_access_merchant("any_merchant") is True


#: Deployed environments the bypass must refuse. The `production` group was
#: already blocked by PR #1897; the `deployed_non_production` group is the
#: behavior change — every one of those was ARMED under `not is_production()`.
_REFUSING_ENVS = {
    "production_pivota_env": ({"PIVOTA_ENV": "production"}, True),
    "production_railway": ({"RAILWAY_ENVIRONMENT": "production"}, True),
    "production_cloud_run_fail_closed": ({"K_SERVICE": "pivota-backend-prod"}, True),
    "staging_cloud_run": (
        {"K_SERVICE": "pivota-backend-staging", "PIVOTA_ENV": "staging"},
        False,
    ),
    "staging_railway": ({"RAILWAY_ENVIRONMENT": "staging"}, False),
    "development_cloud_run": (
        {"K_SERVICE": "pivota-backend", "PIVOTA_ENV": "development"},
        False,
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env,expect_production", _REFUSING_ENVS.values(), ids=_REFUSING_ENVS
)
async def test_test_key_bypass_refuses_on_any_deployed_host(
    monkeypatch, env, expect_production
):
    """Mutant check: PYTEST_CURRENT_TEST alone must not arm the bypass.

    PYTEST_CURRENT_TEST is genuinely set here (pytest sets it for every
    running test), so this only passes if the deployment conjunct, via
    pytest_bypass_allowed(), is load-bearing. The `expect_production=False`
    cases additionally pin that `is_production()` is NOT what refuses them —
    reverting the gate to `not is_production()` re-arms exactly those rows.
    """
    import routes.agent_auth as module

    assert __import__("os").getenv("PYTEST_CURRENT_TEST")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert P.is_production() is expect_production
    monkeypatch.setattr(module, "_INTERNAL_TRUSTED_API_KEYS", ())

    with pytest.raises(Exception) as excinfo:
        await module.get_agent_context(_request(), api_key="test-agent-key", checkout_token=None)

    # The literal "test-agent-key" doesn't match the real API-key format, so
    # once the bypass is refused it falls through to the format check.
    assert getattr(excinfo.value, "status_code", None) == 401

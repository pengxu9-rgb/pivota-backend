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
`X-API-Key: test-agent-key`), so it is not removed here. Instead it now
routes through `config.platform.pytest_bypass_allowed`, which adds the same
`not is_production()` conjunct PR #1889 added to the demo login lanes.
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


@pytest.mark.asyncio
@pytest.mark.parametrize("prod_var", ["PIVOTA_ENV", "RAILWAY_ENVIRONMENT", "K_SERVICE"])
async def test_test_key_bypass_refuses_in_production_even_with_pytest_env(
    monkeypatch, prod_var
):
    """Mutant check: PYTEST_CURRENT_TEST alone must not arm the bypass.

    PYTEST_CURRENT_TEST is genuinely set here (pytest sets it for every
    running test), so this only passes if the new is_production() conjunct,
    via pytest_bypass_allowed(), is load-bearing.
    """
    import routes.agent_auth as module

    assert __import__("os").getenv("PYTEST_CURRENT_TEST")
    monkeypatch.setenv(
        prod_var,
        "pivota-backend-prod" if prod_var == "K_SERVICE" else "production",
    )
    monkeypatch.setattr(module, "_INTERNAL_TRUSTED_API_KEYS", ())

    with pytest.raises(Exception) as excinfo:
        await module.get_agent_context(_request(), api_key="test-agent-key", checkout_token=None)

    # The literal "test-agent-key" doesn't match the real API-key format, so
    # once the bypass is refused it falls through to the format check.
    assert getattr(excinfo.value, "status_code", None) == 401

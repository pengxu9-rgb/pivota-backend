"""`/config-check` must not hand configuration to the internet.

THE DEFECT, measured on prod 2026-08-11. The route shipped public and
unauthenticated — "Public endpoint to check environment variable configuration
(no auth required)" was its docstring — and returned 200 to any caller with:

  * FOUR LITERAL VALUES: `adyen_merchant_account` (prod answered
    "WoopayECOM"), `shopify_store_url`, `shopify_redirect_uri`,
    `wix_store_url`. Naming a real payment-processor merchant account to the
    internet is the leak.
  * a presence map of every payment/platform secret — which PSPs are wired,
    whether Shopify OAuth is configured, whether the nightly PSP backfill
    runs. That is the reconnaissance map an attacker would otherwise guess at.

It was flagged in the 2026-08-08 agent-readability audit as an aside and then
outlived four phases of that plan, because nothing failed when it was wrong.

THE RULE THESE TESTS ENFORCE. `/config-check` requires an admin credential,
and it answers only whether each setting is SET — never its value. Values are
REMOVED rather than masked: the endpoint's own `instructions` field says "if
any values show NOT SET, add them in Railway", so presence is the whole
contract, and nothing here has to decide how many characters of a merchant
account are safe to publish.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def test_anonymous_callers_are_refused(client: TestClient) -> None:
    """The whole defect in one assertion: no credential, no configuration."""
    res = client.get("/config-check")

    assert res.status_code in (401, 403), res.text
    body = res.text
    # Nothing about the deployment leaks in the refusal itself.
    for leaked in ("WoopayECOM", "myshopify", "SET", "stripe_secret_key"):
        assert leaked not in body, f"refusal body leaked {leaked!r}"


def test_the_route_is_gated_by_a_dependency_not_by_convention(client: TestClient) -> None:
    """Pin the GATE, not just today's response.

    A future edit that drops the dependency would restore a public endpoint
    while every response-shape test still passed, so assert the dependency
    exists on the route itself.
    """
    import main

    route = next(
        r for r in main.app.routes if getattr(r, "path", None) == "/config-check"
    )
    assert route.dependant.dependencies, "/config-check has no auth dependency"


def test_no_setting_value_is_ever_returned() -> None:
    """Presence-only, asserted against the handler's own field list.

    Iterating `_CONFIG_CHECK_SETTINGS` rather than a hand-copied list means a
    newly added setting cannot quietly start echoing its value: the assertion
    covers whatever the route actually reports.
    """
    import asyncio

    import main
    from config.settings import settings

    payload = asyncio.run(main.config_check(_admin={"role": "admin"}))
    config = payload["config"]

    for name in main._CONFIG_CHECK_SETTINGS:
        assert config[name] in ("✅ SET", "❌ NOT SET"), (
            f"{name} reported something other than presence: {config[name]!r}"
        )
        value = getattr(settings, name, None)
        if value:
            assert str(value) not in str(config[name]), f"{name} leaked its value"

    # The two non-credential diagnostics stay, deliberately.
    assert "metrics_query_version" in config
    assert config["enable_nightly_psp_id_backfill"] in ("✅ ENABLED", "❌ DISABLED")


def test_presence_reporting_is_still_correct() -> None:
    """Hardening must not break the thing the endpoint exists for."""
    import asyncio

    import main
    from config.settings import settings

    config = asyncio.run(main.config_check(_admin={"role": "admin"}))["config"]

    for name in main._CONFIG_CHECK_SETTINGS:
        expected = "✅ SET" if getattr(settings, name, None) else "❌ NOT SET"
        assert config[name] == expected, f"{name} misreported presence"

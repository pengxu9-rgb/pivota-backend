"""`/config-check` must not hand configuration to the internet.

THE DEFECT, live on prod until 2026-08-11. The route shipped public and
unauthenticated — "Public endpoint to check environment variable configuration
(no auth required)" was its docstring — and returned 200 to any caller with:

  * FOUR LITERAL VALUES: `adyen_merchant_account` (answered "WoopayECOM"),
    `shopify_store_url`, `shopify_redirect_uri`, `wix_store_url`.
  * a presence map of every payment/platform secret — which PSPs are wired,
    whether Shopify OAuth is configured, whether the nightly PSP backfill
    runs. That is the reconnaissance map an attacker would otherwise guess at.

It was flagged in the 2026-08-08 agent-readability audit as an aside and then
outlived four phases of that plan, because nothing failed when it was wrong.

THE RULE: an ADMIN credential is required — not merely *a* credential — and
the response reports only whether each setting is SET, never its value.

WHY THESE TESTS LOOK PARANOID. An earlier cut of this file passed against
three separate mutants, i.e. it would not have noticed the fix being undone:

  1. deleting `require_admin` (so any merchant/agent/employee token got the
     full config) — no test minted a non-admin credential;
  2. restoring the original literal echo for `shopify_store_url`,
     `wix_store_url`, `shopify_redirect_uri` — those settings have no default,
     so in CI they are None, the `or "NOT SET"` branch fires, and
     literal-echoing code is indistinguishable from presence-only;
  3. adding a new value-echoing key to the response — the loop iterated the
     handler's field TUPLE rather than the response it actually returns.

Each test below names the mutant it exists to kill. Keep it that way: a
security test that cannot fail is worse than no test, because it advertises a
guarantee nobody is checking.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# The four fields whose literals leaked. Given non-empty sentinel values below
# so "presence-only" is proven against something that WOULD show up if echoed.
_LEAKED_FIELDS = (
    "adyen_merchant_account",
    "shopify_store_url",
    "shopify_redirect_uri",
    "wix_store_url",
)

_PRESENCE_VALUES = ("✅ SET", "❌ NOT SET")


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _token(role: str) -> str:
    """A real signed JWT. Deliberately NOT the `test-token` placeholder, whose
    pytest-only bypass in utils.auth hands back role=admin and would make a
    non-admin assertion impossible to write."""
    from utils.auth import create_access_token

    return create_access_token({"sub": "u1", "email": "u1@example.com", "role": role})


def test_anonymous_callers_are_refused(client: TestClient) -> None:
    res = client.get("/config-check")

    assert res.status_code in (401, 403), res.text
    for leaked in ("WoopayECOM", "myshopify", "✅ SET", "stripe_secret_key"):
        assert leaked not in res.text, f"refusal body leaked {leaked!r}"


@pytest.mark.parametrize("role", ["merchant", "agent", "employee", "user", "viewer"])
def test_a_valid_non_admin_credential_is_still_refused(client: TestClient, role: str) -> None:
    """KILLS MUTANT 1: dropping `require_admin` while keeping authentication.

    "Admin-only" is the actual claim; authentication alone would hand the
    configuration map to every merchant and agent holding a token — a far
    larger population than the admins this endpoint is for.
    """
    res = client.get("/config-check", headers={"Authorization": f"Bearer {_token(role)}"})

    assert res.status_code == 403, f"role={role} was not refused: {res.text}"
    assert "stripe_secret_key" not in res.text


def test_admin_gets_presence_only_over_http(client: TestClient, monkeypatch) -> None:
    """KILLS MUTANT 2, through the real dependency stack.

    Every leak-prone setting is given a non-empty SENTINEL first. Without this,
    the fields have no default, read None in CI, and code that echoes the
    literal is indistinguishable from code that reports presence.
    """
    import main
    from config.settings import settings

    for name in main._CONFIG_CHECK_SETTINGS:
        monkeypatch.setattr(settings, name, f"SENTINEL-{name}", raising=False)

    res = client.get("/config-check", headers={"Authorization": "Bearer test-token"})

    assert res.status_code == 200, res.text
    body = res.text
    assert "SENTINEL-" not in body, "a setting value reached the response"
    config = res.json()["config"]
    for name in _LEAKED_FIELDS:
        assert config[name] == "✅ SET", f"{name} did not report presence"


def test_no_response_field_ever_carries_a_setting_value(monkeypatch) -> None:
    """KILLS MUTANT 3: a NEW key that echoes a value.

    Iterates the RESPONSE, not the handler's field tuple — the tuple cannot see
    a key added beside it (the handler already has two such keys), which is
    exactly how a future edit would reintroduce the leak.
    """
    import asyncio

    import main
    from config.settings import settings

    for name in main._CONFIG_CHECK_SETTINGS:
        monkeypatch.setattr(settings, name, f"SENTINEL-{name}", raising=False)

    config = asyncio.run(main.config_check(_admin={"role": "admin"}))["config"]

    allowed_non_presence = {"metrics_query_version", "enable_nightly_psp_id_backfill"}
    for key, value in config.items():
        assert "SENTINEL-" not in str(value), f"response key {key!r} echoed a setting value"
        if key not in allowed_non_presence:
            assert value in _PRESENCE_VALUES, (
                f"response key {key!r} reported something other than presence: {value!r}"
            )


def test_the_route_is_gated_by_a_dependency(client: TestClient) -> None:
    """Belt to the HTTP tests' braces: assert the gate exists structurally, so
    a refactor that keeps the handler but loses the Depends is visible here as
    well as in the 403 assertions above."""
    import main

    route = next(r for r in main.app.routes if getattr(r, "path", None) == "/config-check")
    assert route.dependant.dependencies, "/config-check has no auth dependency"


def test_presence_reporting_is_still_correct() -> None:
    """Hardening must not break the thing the endpoint exists for."""
    import asyncio

    import main
    from config.settings import settings

    config = asyncio.run(main.config_check(_admin={"role": "admin"}))["config"]

    for name in main._CONFIG_CHECK_SETTINGS:
        expected = "✅ SET" if getattr(settings, name, None) else "❌ NOT SET"
        assert config[name] == expected, f"{name} misreported presence"


def test_version_endpoint_does_not_publish_the_deployer_identity(
    client: TestClient, monkeypatch
) -> None:
    """`/version` is public (an unauthenticated monitoring probe and a pinned
    settings contract both read it), so it stays public — but RAILWAY_GIT_AUTHOR
    is a named individual and nothing consumes it.

    RAILWAY_GIT_COMMIT_SHA must be set or the handler takes its local-git
    branch, which never had an `author` key — the assertion would pass without
    ever exercising the code path that leaked. (Found exactly that way: the
    first version of this test passed against the mutant that restores it.)
    """
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "0" * 40)
    monkeypatch.setenv("RAILWAY_GIT_BRANCH", "main")
    monkeypatch.setenv("RAILWAY_GIT_AUTHOR", "A Real Person")

    res = client.get("/version")

    assert res.status_code == 200, res.text
    body = res.json()
    # Prove the Railway branch really ran, so this cannot silently go vacuous.
    assert body["full_sha"] == "0" * 40, "did not exercise the Railway branch"
    assert "author" not in body, "/version published the deployer identity"
    assert "A Real Person" not in res.text


# ── /health and /version: verdict public, diagnostics admin-only ─────────────
# Same defect class as /config-check, found by the same review. These probes
# published, to any anonymous caller: the exact rate-limit threshold, whether
# Shopify discount reconciliation is ENFORCING or merely observing, a
# mounted-route map, and (when degraded) the names of missing schema columns.
#
# Build identity (commit SHA, deployment id) is deliberately still public:
# `add_service_version_headers` stamps X-Service-Commit on EVERY response, so
# removing it from these bodies would be theatre.
#
# THE HARD CONSTRAINT: /health is Railway's healthcheck path and this repo lost
# 12.5 hours to a health-endpoint failure. Authentication here is ADDITIVE —
# it selects detail, never gates the response — so an anonymous or
# bad-token caller must still get the correct 200/503.

_ADMIN = {"Authorization": "Bearer test-token"}
_DIAGNOSTIC_KEYS = ("settings_contract", "runtime_contracts", "missing_columns")


def _probe_body(response) -> dict:
    """Unwrap the global error envelope.

    ErrorHandlerMiddleware normalizes every JSON response >=400 into
    {"status": "error", "error": {...}}, so a 503 /health payload arrives nested
    under error.details. Unwrapping matters for the security claim, not just
    ergonomics: 503 is the DEGRADED path, the only one where `missing_columns`
    is non-empty, so the redaction has to be asserted in that shape too.
    """
    payload = response.json()
    if isinstance(payload, dict) and payload.get("status") == "error" and "error" in payload:
        return payload["error"].get("details", payload)
    return payload


# ALLOWLIST, not a denylist. An earlier cut asserted only that three named keys
# were absent — so adding a NEW leaky field (say `db_url_host` or
# `enabled_psps`) to the public body shipped GREEN. That is precisely MUTANT 3
# from this file's own header, which the /config-check tests learned and the
# first /health tests failed to carry over. Pinning the exact key set is the
# only form that survives a field nobody has thought of yet.
_PUBLIC_HEALTH_KEYS = {
    "status",
    "timestamp",
    "elapsed_ms",
    "db_ok",
    "error",
    "build",
    "version",
    "missing_columns_count",
}


def test_health_answers_anonymously_with_a_verdict_and_no_diagnostics(
    client: TestClient,
) -> None:
    res = client.get("/health")

    # The verdict — all Railway reads — is intact.
    assert res.status_code in (200, 503), res.text
    body = _probe_body(res)
    assert body["status"] in ("ok", "unhealthy")
    assert "db_ok" in body
    # THE GUARANTEE: the anonymous body is exactly this set. A new field must
    # be added here deliberately, which is the review step that catches a leak.
    assert set(body.keys()) == _PUBLIC_HEALTH_KEYS, (
        f"anonymous /health key set changed: "
        f"unexpected={sorted(set(body) - _PUBLIC_HEALTH_KEYS)} "
        f"missing={sorted(_PUBLIC_HEALTH_KEYS - set(body))}"
    )
    # Body-level pin. NOTE this is NOT a claim that rate_limit_rpm is secret:
    # middleware/rate_limiter.py stamps X-RateLimit-Limit on every /agent/*
    # response carrying any x-api-key, so the value is readable one hop away.
    # See test_rate_limit_threshold_is_still_published_by_headers.
    assert "rate_limit_rpm" not in res.text
    assert "shopify_discount_reconciliation_mode" not in res.text


def test_health_still_signals_schema_drift_without_naming_columns(
    client: TestClient,
) -> None:
    """The outage signal must survive the redaction.

    An operator watching the public probe still learns that drift EXISTS; the
    column names need a token. Redacting the signal along with the detail would
    have traded a recon leak for a blind spot.
    """
    body = _probe_body(client.get("/health"))

    assert "missing_columns_count" in body
    assert isinstance(body["missing_columns_count"], int)


def test_schema_drift_count_is_real_and_survives_authenticating(
    client: TestClient, monkeypatch
) -> None:
    """Force drift so the count proves the SIGNAL, not just the key.

    Without forced drift a helper hardcoding 0 would ship green. And the count
    must appear for admins too — authenticating must never REMOVE a field, or a
    dashboard trending it breaks the day it is given a token.
    """
    import db.schema_guard as guard
    import main

    async def _drifted():
        return {"catalog_products": ["col_a", "col_b"], "agent_pdp_view": ["col_c"]}

    async def _db_ok(*_a, **_kw):
        return None

    # BOTH stubs are required: the schema check runs only when the DB probe
    # succeeds, and there is no database in this environment — so patching the
    # schema alone leaves the drift branch unreachable and the count at 0.
    # (That is how the first version of this test failed, which is the point of
    # asserting a non-zero value rather than just the key's presence.)
    monkeypatch.setattr(main, "probe_database_health", _db_ok)
    monkeypatch.setattr(guard, "check_required_schema", _drifted)

    anon = _probe_body(client.get("/health"))
    assert anon["missing_columns_count"] == 3
    assert "missing_columns" not in anon, "column NAMES leaked anonymously"
    assert "col_a" not in client.get("/health").text

    admin = _probe_body(client.get("/health", headers=_ADMIN))
    assert admin["missing_columns_count"] == 3, "authenticating removed a field"
    assert admin["missing_columns"]["catalog_products"] == ["col_a", "col_b"]


def test_public_health_key_set_holds_on_the_HEALTHY_path_too(
    client: TestClient, monkeypatch
) -> None:
    """The allowlist must hold at 200, not only at 503.

    There is no database in this environment, so every other test here sees the
    503 branch. CI's real-Postgres job sees the 200 branch — meaning a key set
    that were wrong when healthy would fail only there, and local runs would be
    structurally blind to it. Pin both branches.
    """
    import db.schema_guard as guard
    import main

    async def _db_ok(*_a, **_kw):
        return None

    async def _no_drift():
        return {}

    monkeypatch.setattr(main, "probe_database_health", _db_ok)
    monkeypatch.setattr(guard, "check_required_schema", _no_drift)

    res = client.get("/health")

    assert res.status_code == 200, res.text
    body = res.json()  # 200 is not wrapped by ErrorHandlerMiddleware
    assert body["status"] == "ok"
    assert set(body.keys()) == _PUBLIC_HEALTH_KEYS, (
        f"healthy-path key set differs from the 503 path: "
        f"unexpected={sorted(set(body) - _PUBLIC_HEALTH_KEYS)} "
        f"missing={sorted(_PUBLIC_HEALTH_KEYS - set(body))}"
    )
    for key in _DIAGNOSTIC_KEYS:
        assert key not in body


def test_rate_limit_threshold_is_NOT_published_by_headers(client: TestClient) -> None:
    """The threshold is withheld from unauthenticated callers on /agent/* too.

    This test previously asserted the OPPOSITE — it pinned the asymmetry that
    /health redaction was cosmetic because the rate-limit middleware published
    rate_limit_rpm to any caller with an x-api-key. Its docstring promised it
    "fails if that ever changes, which is the moment to update the claim in
    main.py".

    It did not fail when that changed. The assertion was wrapped in
    `if limit is not None:`, so the moment the header disappeared — the exact
    event it existed to detect — it started passing vacuously. A conditional
    around the assertion disarms the tripwire in precisely the case you built it
    for. That is the third instance of this defect class in three PRs, so it is
    written down here rather than quietly fixed.

    Now that the middleware only publishes the ENFORCED limit to callers who
    authenticated, the guarantee is unconditional and so is the assertion.
    """
    from config.settings import settings

    res = client.get("/agent/definitely-not-a-route", headers={"x-api-key": "invalid"})

    assert "x-ratelimit-limit" not in {k.lower() for k in res.headers}
    # And the value nowhere in the response, under any header name.
    assert str(settings.rate_limit_rpm) not in str(dict(res.headers))


def test_health_gives_an_admin_the_full_drift_contract(client: TestClient) -> None:
    res = client.get("/health", headers=_ADMIN)

    assert res.status_code in (200, 503), res.text
    body = _probe_body(res)
    for key in _DIAGNOSTIC_KEYS:
        assert key in body, f"admin did not receive {key}"
    assert body["settings_contract"]["rate_limit_rpm_present"] is True


@pytest.mark.parametrize("role", ["merchant", "agent", "employee"])
def test_health_diagnostics_need_admin_not_merely_a_token(
    client: TestClient, role: str
) -> None:
    """KILLS the mutant that checks authentication but not ROLE."""
    res = client.get("/health", headers={"Authorization": f"Bearer {_token(role)}"})

    assert res.status_code in (200, 503), res.text
    for key in _DIAGNOSTIC_KEYS:
        assert key not in _probe_body(res), f"role={role} received {key}"
    # Raw text too: a future edit that NESTS the diagnostics under another key
    # would pass every top-level assertion above.
    assert "shopify_discount_reconciliation_mode" not in res.text
    assert "canonical_mutating_routes" not in res.text


def test_a_malformed_token_degrades_to_public_and_never_breaks_the_probe(
    client: TestClient,
) -> None:
    """THE OUTAGE GUARD. /health is Railway's healthcheck: a bad Authorization
    header must yield the public view, never 401/500 — a restart loop is a far
    worse outcome than a redacted body."""
    for header in ("Bearer garbage.token.here", "Bearer ", "Basic abc", "nonsense"):
        res = client.get("/health", headers={"Authorization": header})
        assert res.status_code in (200, 503), f"{header!r} broke /health: {res.status_code}"
        assert "settings_contract" not in _probe_body(res)


def test_version_hides_the_settings_contract_from_anonymous_callers(
    client: TestClient,
) -> None:
    res = client.get("/version")

    assert res.status_code == 200, res.text
    assert "settings_contract" not in res.json()
    assert "rate_limit_rpm" not in res.text


def test_version_gives_an_admin_the_settings_contract(client: TestClient) -> None:
    body = client.get("/version", headers=_ADMIN).json()

    assert "settings_contract" in body
    assert body["settings_contract"]["rate_limit_rpm_present"] is True


def test_version_local_and_unknown_branches_are_gated_too(
    client: TestClient, monkeypatch
) -> None:
    """All THREE return branches (Railway / local-git / unknown) published the
    contract; gating only the Railway one would relocate the disclosure rather
    than close it. Force the non-Railway paths and re-assert.
    """
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)

    body = client.get("/version").json()
    assert body["environment"] in ("local", "unknown")
    assert "settings_contract" not in body

    admin_body = client.get("/version", headers=_ADMIN).json()
    assert "settings_contract" in admin_body


def test_build_identity_stays_public_on_purpose(client: TestClient) -> None:
    """Not a leak to close: the middleware publishes X-Service-Commit on every
    response, so redacting the body would be theatre while deploy verification
    legitimately needs it. Pinned so nobody 'fixes' it asymmetrically.
    """
    res = client.get("/health")

    assert _probe_body(res)["build"]["service"] == "pivota-backend"
    assert res.headers["x-service-build-id"]

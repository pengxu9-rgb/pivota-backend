"""Every safety-critical guard must fire on Cloud Run, not just on Railway.

THE FAILURE THIS FILE EXISTS TO CATCH
-------------------------------------
Production has no NODE_ENV and no ENV. For years the answer to "am I in
production" has been Railway's ``RAILWAY_ENVIRONMENT``. On Cloud Run that
variable — and every other ``RAILWAY_*`` — is simply absent. A guard that still
reads it directly does not error, does not warn, and does not fail a test: it
quietly evaluates to "not production" and runs its dev branch against live
traffic. Unsigned webhooks accepted. Debug endpoints mounted. Test-mode Stripe
events mutating live orders.

So every guard here is asserted THREE times:

  (a) RAILWAY   — RAILWAY_ENVIRONMENT=production, no PIVOTA_*, no K_*.
                  Proves the refactor did not break today's production.
  (b) CLOUD RUN — K_SERVICE set + PIVOTA_ENV=production, NO RAILWAY_* at all.
                  This is the assertion that would have failed before the shim.
  (c) UNRESOLVED — K_SERVICE set and nothing else. The guard must still fire:
                  the shim fails closed to "production" so a revision deployed
                  without PIVOTA_ENV is guarded, not naked.

And, where the guard has a real "off" state, a fourth case pins that the test
is measuring the guard rather than a constant: on staging it must NOT fire.
Without that, `return True` passes every case above.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from config import platform as P

REPO_ROOT = Path(__file__).resolve().parents[1]

# Cleared before every test: a developer shell (or a CI runner exporting
# COMMIT_SHA) must not be able to decide the answer.
_PLATFORM_KEYS = (
    "PIVOTA_PLATFORM",
    "PIVOTA_ENV",
    "PIVOTA_SERVICE_NAME",
    "PIVOTA_COMMIT_SHA",
    "PIVOTA_DEPLOYMENT_ID",
    "PIVOTA_GIT_BRANCH",
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_SERVICE_NAME",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_GIT_COMMIT_SHA",
    "RAILWAY_GIT_BRANCH",
    # NOT platform markers — Railway CLI credentials and a routing target, all
    # three routinely exported in a developer's shell profile because the
    # documented ops workflow reaches the prod DB through the Railway proxy.
    # Scrubbed so this suite is hermetic on a laptop as well as on CI: before
    # the allowlist fix in config/platform.py, `RAILWAY_TOKEN=…` alone made 14
    # of this branch's 125 new tests fail by resolving the machine as
    # production. See test_a_railway_cli_token_alone_is_not_a_deployment.
    "RAILWAY_TOKEN",
    "RAILWAY_API_TOKEN",
    "RAILWAY_PRIVATE_DOMAIN",
    "K_SERVICE",
    "K_REVISION",
    "K_CONFIGURATION",
    "COMMIT_SHA",
    "SOURCE_VERSION",
    "GIT_COMMIT_SHA",
    "VERCEL_GIT_COMMIT_SHA",
    "GIT_BRANCH",
    # The pre-existing, non-Railway prod signals every one of these guards also
    # honours. Left set by another test they would mask a broken shim.
    "ENVIRONMENT",
    "APP_ENV",
)

# The three shapes a production process can present itself in.
PRODUCTION_SHAPES = {
    "railway": {"RAILWAY_ENVIRONMENT": "production"},
    "cloud_run": {"K_SERVICE": "pivota-backend", "PIVOTA_ENV": "production"},
    "cloud_run_unresolved": {"K_SERVICE": "pivota-backend"},
}

STAGING_SHAPES = {
    "railway": {"RAILWAY_ENVIRONMENT": "staging"},
    "cloud_run": {"K_SERVICE": "pivota-backend", "PIVOTA_ENV": "staging"},
}


@pytest.fixture(autouse=True)
def _clean_platform_env(monkeypatch):
    for key in _PLATFORM_KEYS:
        monkeypatch.delenv(key, raising=False)
    P.reset_platform_state()
    yield
    P.reset_platform_state()


def _apply(monkeypatch, shape: dict) -> None:
    for key, value in shape.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(params=sorted(PRODUCTION_SHAPES), ids=sorted(PRODUCTION_SHAPES))
def production_shape(request, monkeypatch):
    """Parametrised over railway / cloud_run / cloud_run_unresolved."""
    _apply(monkeypatch, PRODUCTION_SHAPES[request.param])
    return request.param


@pytest.fixture(params=sorted(STAGING_SHAPES), ids=sorted(STAGING_SHAPES))
def staging_shape(request, monkeypatch):
    _apply(monkeypatch, STAGING_SHAPES[request.param])
    return request.param


# ---------------------------------------------------------------------------
# The shim itself, as the premise the rest of the file rests on
# ---------------------------------------------------------------------------


def test_every_production_shape_reads_as_production(production_shape):
    assert P.is_production() is True
    assert P.is_deployed() is True


def test_every_staging_shape_reads_as_staging(staging_shape):
    assert P.is_production() is False
    assert P.is_staging() is True
    assert P.is_deployed() is True


# ---------------------------------------------------------------------------
# A DEVELOPER'S SHELL IS NOT A DEPLOYMENT
#
# The regression these pin: `_on_railway()` used to scan the whole `RAILWAY_`
# prefix, so the Railway CLI's own credentials counted as proof of deployment.
# This repo's documented ops workflow is "reach the prod DB through the Railway
# proxy", so `RAILWAY_TOKEN` genuinely lives in developer shell profiles. The
# consequence was not cosmetic: settlement transfers have INVERTED polarity, so
# a laptop with a token exported had the real merchant payout path ARMED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["RAILWAY_TOKEN", "RAILWAY_API_TOKEN", "RAILWAY_PRIVATE_DOMAIN"],
)
def test_a_railway_cli_token_alone_is_not_a_deployment(monkeypatch, key):
    monkeypatch.setenv(key, "deadbeefdeadbeefdeadbeefdeadbeef")

    assert P.platform_name() == "local"
    assert P.is_deployed() is False
    assert P.platform_env() == "development"
    assert P.is_production() is False

    # The money path, stated explicitly rather than left implied.
    from services.settlement_file_service import _transfer_allowed_in_this_environment

    assert _transfer_allowed_in_this_environment() is False

    # And local boot must not die: require_platform_env() raises only on a
    # managed host it cannot resolve.
    assert P.require_platform_env() == "development"


@pytest.mark.parametrize(
    "key",
    [
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_ENVIRONMENT_NAME",
        "RAILWAY_SERVICE_ID",
        "RAILWAY_SERVICE_NAME",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_DEPLOYMENT_ID",
    ],
)
def test_every_railway_deployment_marker_still_proves_deployment(monkeypatch, key):
    """The other side of the allowlist.

    Without this, `_on_railway()` could be narrowed to `return False` — or the
    allowlist emptied — and the test above would still pass while today's
    Railway production silently became "local".
    """
    monkeypatch.setenv(key, "production" if "ENVIRONMENT" in key else "some-value")

    assert P.platform_name() == "railway"
    assert P.is_deployed() is True
    # Either it resolves (the ENVIRONMENT keys) or it fails closed — never
    # "development", which is what would disarm every guard.
    assert P.platform_env() == "production"


# ---------------------------------------------------------------------------
# GUARD: shakeout debug endpoints (fixture/demo surface, writes to the prod DB)
# ---------------------------------------------------------------------------


def test_shakeout_debug_is_refused_in_every_production_shape(production_shape):
    from routes.shakeout_debug import _require_non_prod

    with pytest.raises(HTTPException) as excinfo:
        _require_non_prod()
    assert excinfo.value.status_code == 403


def test_shakeout_debug_is_allowed_on_staging(staging_shape):
    """The mutant this kills: `raise` unconditionally, or `if True:`."""
    from routes.shakeout_debug import _require_non_prod

    assert _require_non_prod() is None


def test_shakeout_debug_is_allowed_locally():
    from routes.shakeout_debug import _require_non_prod

    assert _require_non_prod() is None


# ===========================================================================
# THE WEBHOOK / SIGNATURE GATE FAMILY
#
# This file covered seven guards and imported none of the five webhook-facing
# modules below. Two mutants proved the hole by surviving the FULL sweep:
#
#   M5  routes/webhook_routes.py  is_deployed() -> is_production()
#   M7  routes/shopify_setup.py   is_deployed() -> is_production()
#
# M5's failure mode: on Railway staging, or any Cloud Run staging revision,
# Shopify webhook HMAC verification degrades from ENFORCED to SKIPPED and the
# persistence-failure hard-fail stops firing. Nothing errors, nothing warns,
# and CI stays green.
#
# is_deployed() vs is_production() is therefore the assertion that matters, and
# it can only be made by the STAGING side of each pair. A production-only test
# passes identically under both — which is exactly how these mutants survived.
# Every guard below is asserted in all three production shapes AND on staging.
# ===========================================================================


# --- Shopify webhook strictness: HMAC + persistence hard-fail (M5) ----------


def test_shopify_webhook_strictness_is_on_in_every_production_shape(production_shape):
    from routes.webhook_routes import _shopify_prod_runtime

    assert _shopify_prod_runtime() is True


def test_shopify_webhook_strictness_is_on_for_staging_too(staging_shape):
    """THE M5 ASSERTION.

    The pre-shim expression was ``bool(os.getenv("RAILWAY_GIT_COMMIT_SHA"))``,
    true on Railway staging as well — so staging has ALWAYS enforced Shopify
    HMAC. is_deployed() is the faithful translation; is_production() is not,
    and swapping them leaves every staging deployment accepting unsigned
    webhooks. This test is the only thing that can tell the two apart.
    """
    from routes.webhook_routes import _shopify_prod_runtime

    assert _shopify_prod_runtime() is True


def test_shopify_webhook_strictness_is_off_locally():
    """The other half of the pair: without this, `return True` passes."""
    from routes.webhook_routes import _shopify_prod_runtime

    assert _shopify_prod_runtime() is False


@pytest.mark.parametrize("var", ["APP_ENV", "ENVIRONMENT"])
def test_shopify_webhook_strictness_honours_its_legacy_env_vars(monkeypatch, var):
    from routes.webhook_routes import _shopify_prod_runtime

    monkeypatch.setenv(var, "production")
    assert _shopify_prod_runtime() is True


# --- Stripe order/PSP webhook livemode gate --------------------------------


def test_stripe_livemode_gate_is_active_in_every_production_shape(production_shape):
    from routes.webhook_routes import _stripe_livemode_gate_active

    assert _stripe_livemode_gate_active() is True


def test_stripe_livemode_gate_is_inactive_on_staging(staging_shape):
    """Deliberately NARROWER than the Shopify gate: staging must keep accepting
    test-mode Stripe events. is_production(), not is_deployed()."""
    from routes.webhook_routes import _stripe_livemode_gate_active

    assert _stripe_livemode_gate_active() is False


def test_stripe_livemode_gate_is_inactive_locally():
    from routes.webhook_routes import _stripe_livemode_gate_active

    assert _stripe_livemode_gate_active() is False


def test_stripe_livemode_gate_honours_its_legacy_env_var(monkeypatch):
    from routes.webhook_routes import _stripe_livemode_gate_active

    monkeypatch.setenv("ENVIRONMENT", "production")
    assert _stripe_livemode_gate_active() is True


# --- Stripe BILLING webhook livemode gate ----------------------------------


def test_billing_livemode_gate_is_active_in_every_production_shape(production_shape):
    from routes.billing_routes import _billing_livemode_gate_active

    assert _billing_livemode_gate_active() is True


def test_billing_livemode_gate_is_inactive_on_staging(staging_shape):
    from routes.billing_routes import _billing_livemode_gate_active

    assert _billing_livemode_gate_active() is False


def test_billing_livemode_gate_is_inactive_locally():
    from routes.billing_routes import _billing_livemode_gate_active

    assert _billing_livemode_gate_active() is False


def test_billing_livemode_gate_honours_its_legacy_env_var(monkeypatch):
    from routes.billing_routes import _billing_livemode_gate_active

    monkeypatch.setenv("ENVIRONMENT", "production")
    assert _billing_livemode_gate_active() is True


# --- Checkout.com webhook: no secret configured => production refuses -------


def test_unsigned_checkout_webhook_is_fatal_in_every_production_shape(production_shape):
    from routes.payment_routes import _unsigned_webhook_is_fatal

    assert _unsigned_webhook_is_fatal() is True


def test_unsigned_checkout_webhook_is_tolerated_on_staging(staging_shape):
    from routes.payment_routes import _unsigned_webhook_is_fatal

    assert _unsigned_webhook_is_fatal() is False


def test_unsigned_checkout_webhook_is_tolerated_locally():
    from routes.payment_routes import _unsigned_webhook_is_fatal

    assert _unsigned_webhook_is_fatal() is False


def test_unsigned_checkout_webhook_honours_its_legacy_env_var(monkeypatch):
    from routes.payment_routes import _unsigned_webhook_is_fatal

    monkeypatch.setenv("ENVIRONMENT", "production")
    assert _unsigned_webhook_is_fatal() is True


# --- Shopify setup endpoints: credential-overwrite surface (M7) ------------


def test_shopify_setup_is_gated_in_every_production_shape(production_shape):
    from routes.shopify_setup import _is_production, _shopify_setup_enabled

    assert _is_production() is True
    assert _shopify_setup_enabled() is False  # the opt-in is not set


def test_shopify_setup_is_gated_on_staging_too(staging_shape):
    """THE M7 ASSERTION — same shape, same reason, as M5.

    These endpoints overwrite stored Shopify credentials. `bool(
    RAILWAY_GIT_COMMIT_SHA)` closed them on staging as well; is_production()
    would reopen them there.
    """
    from routes.shopify_setup import _is_production

    assert _is_production() is True


def test_shopify_setup_is_open_locally():
    from routes.shopify_setup import _is_production

    assert _is_production() is False


def test_shopify_setup_opt_in_reopens_it(monkeypatch, production_shape):
    """Pins that the gate is `_is_production() and not _shopify_setup_enabled()`
    — a mutant dropping the opt-in would otherwise go unnoticed."""
    from routes.shopify_setup import _is_production, _shopify_setup_enabled

    monkeypatch.setenv("ENABLE_SHOPIFY_SETUP_ENDPOINTS", "true")
    assert _is_production() is True
    assert _shopify_setup_enabled() is True


# --- Photo schema self-heal: never ensure-on-request in production ----------


def test_photo_schema_ensure_is_off_in_every_production_shape(production_shape):
    from routes.photos import _is_production_env

    assert _is_production_env() is True


def test_photo_schema_ensure_is_on_for_staging(staging_shape):
    """Narrower than the webhook gates on purpose: this one is is_production(),
    so staging keeps the self-healing DDL path."""
    from routes.photos import _is_production_env

    assert _is_production_env() is False


def test_photo_schema_ensure_is_on_locally():
    from routes.photos import _is_production_env

    assert _is_production_env() is False


@pytest.mark.parametrize("var", ["ENVIRONMENT", "APP_ENV"])
def test_photo_schema_gate_honours_its_legacy_env_vars(monkeypatch, var):
    from routes.photos import _is_production_env

    monkeypatch.setenv(var, "production")
    assert _is_production_env() is True


def test_photo_schema_explicit_non_prod_env_var_beats_the_platform(monkeypatch):
    """This module's precedence is deliberate and different: an explicit
    ENVIRONMENT/APP_ENV wins outright, even on a production platform."""
    from routes.photos import _is_production_env

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert _is_production_env() is False


# ---------------------------------------------------------------------------
# GUARD: production-only route gate (utils.runtime_safety)
# ---------------------------------------------------------------------------


def test_runtime_gate_closes_routes_in_every_production_shape(production_shape):
    from utils.runtime_safety import is_production_runtime, require_runtime_gate

    assert is_production_runtime() is True
    with pytest.raises(HTTPException) as excinfo:
        require_runtime_gate("SOME_UNSET_OPT_IN_FLAG")
    assert excinfo.value.status_code == 404


def test_runtime_gate_treats_any_deployment_as_production(staging_shape):
    """Deliberate: this gate has always meant "deployed", not "prod env".

    ``bool(RAILWAY_GIT_COMMIT_SHA)`` was true on Railway staging too, so
    is_deployed() — not is_production() — is the faithful translation, and a
    staging deploy must stay closed exactly as it was.
    """
    from utils.runtime_safety import is_production_runtime

    assert is_production_runtime() is True


def test_runtime_gate_is_open_locally():
    from utils.runtime_safety import is_production_runtime, require_runtime_gate

    assert is_production_runtime() is False
    assert require_runtime_gate("SOME_UNSET_OPT_IN_FLAG") is None


def test_runtime_gate_still_honours_its_opt_in_flag(monkeypatch, production_shape):
    from utils.runtime_safety import require_runtime_gate

    monkeypatch.setenv("SOME_OPT_IN_FLAG", "true")
    assert require_runtime_gate("SOME_OPT_IN_FLAG") is None


# ---------------------------------------------------------------------------
# GUARD: service-token strength (rejects a weak secret in production)
# ---------------------------------------------------------------------------


def test_weak_service_token_is_rejected_in_every_production_shape(production_shape):
    from utils.service_token import validate_service_token

    with pytest.raises(ValueError):
        validate_service_token("short", label="acp")


def test_weak_service_token_is_tolerated_off_production(staging_shape):
    from utils.service_token import validate_service_token

    assert validate_service_token("short", label="acp") is None


# ---------------------------------------------------------------------------
# GUARD: settlement transfers (real money leaves the platform here)
# ---------------------------------------------------------------------------


def test_settlement_transfers_are_enabled_in_every_production_shape(production_shape):
    """Inverted polarity vs. the others, on purpose.

    Here "production" ENABLES the action. Before the shim this read
    ``RAILWAY_ENVIRONMENT == "production"``, so the first Cloud Run revision
    would have silently stopped paying merchants out — no error, no alert, just
    a transfer path that returns False forever. That is why the unresolved
    shape must resolve to production too.
    """
    from services.settlement_file_service import _transfer_allowed_in_this_environment

    assert _transfer_allowed_in_this_environment() is True


def test_settlement_transfers_stay_off_on_staging_without_the_opt_in(staging_shape):
    from services.settlement_file_service import _transfer_allowed_in_this_environment

    assert _transfer_allowed_in_this_environment() is False


def test_settlement_transfers_staging_opt_in_still_works(monkeypatch, staging_shape):
    from services.settlement_file_service import _transfer_allowed_in_this_environment

    monkeypatch.setenv("SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING", "true")
    assert _transfer_allowed_in_this_environment() is True


# ---------------------------------------------------------------------------
# GUARD: heavy-startup skip (a deployed service must not blow its healthcheck)
# ---------------------------------------------------------------------------


def test_heavy_startup_is_skipped_in_every_production_shape(production_shape):
    from utils.startup_mode import should_skip_heavy_startup

    assert should_skip_heavy_startup() is True


def test_heavy_startup_is_skipped_on_any_deployed_staging(staging_shape):
    from utils.startup_mode import should_skip_heavy_startup

    assert should_skip_heavy_startup() is True


def test_heavy_startup_runs_locally():
    from utils.startup_mode import should_skip_heavy_startup

    assert should_skip_heavy_startup() is False


def test_heavy_startup_explicit_override_still_wins(production_shape, monkeypatch):
    from utils.startup_mode import should_skip_heavy_startup

    monkeypatch.setenv("SKIP_HEAVY_STARTUP_INIT", "false")
    assert should_skip_heavy_startup() is False


# main.startup() has its OWN, narrower rule (production only, not any deploy).
# Both exist; both must survive the platform move.


def test_main_startup_skips_heavy_init_in_every_production_shape(
    monkeypatch, production_shape
):
    from main import _should_skip_heavy_startup_init

    monkeypatch.delenv("SKIP_HEAVY_STARTUP_INIT", raising=False)
    assert _should_skip_heavy_startup_init() is True


def test_main_startup_runs_heavy_init_on_staging(monkeypatch, staging_shape):
    """The difference from utils.startup_mode is deliberate and pinned here:
    staging DOES run the full startup DDL."""
    from main import _should_skip_heavy_startup_init

    monkeypatch.delenv("SKIP_HEAVY_STARTUP_INIT", raising=False)
    assert _should_skip_heavy_startup_init() is False


def test_main_startup_runs_heavy_init_locally(monkeypatch):
    from main import _should_skip_heavy_startup_init

    monkeypatch.delenv("SKIP_HEAVY_STARTUP_INIT", raising=False)
    assert _should_skip_heavy_startup_init() is False


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("YES", True), ("false", False), ("0", False)],
)
def test_main_startup_explicit_override_wins(monkeypatch, value, expected):
    from main import _should_skip_heavy_startup_init

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("SKIP_HEAVY_STARTUP_INIT", value)
    assert _should_skip_heavy_startup_init() is expected


# ---------------------------------------------------------------------------
# GUARD: shared-queue scheduler worker (staging must not poach prod runs)
# ---------------------------------------------------------------------------


def test_queue_worker_drains_in_every_production_shape(monkeypatch, production_shape):
    from services.audit_scheduler import _queue_worker_enabled

    monkeypatch.delenv("AUDIT_WORKER_ENABLED", raising=False)
    assert _queue_worker_enabled() is True


def test_queue_worker_is_disabled_on_staging(monkeypatch, staging_shape):
    from services.audit_scheduler import _queue_worker_enabled

    monkeypatch.delenv("AUDIT_WORKER_ENABLED", raising=False)
    assert _queue_worker_enabled() is False


def test_queue_worker_is_disabled_by_a_cloud_run_staging_service_name(monkeypatch):
    """K_SERVICE feeds the same "-staging" substring rule RAILWAY_SERVICE_NAME did."""
    from services.audit_scheduler import _queue_worker_enabled

    monkeypatch.delenv("AUDIT_WORKER_ENABLED", raising=False)
    monkeypatch.setenv("K_SERVICE", "pivota-backend-staging")
    monkeypatch.setenv("PIVOTA_ENV", "production")  # mislabelled on purpose
    assert _queue_worker_enabled() is False


def test_queue_worker_stays_enabled_off_platform(monkeypatch):
    """Fail-safe toward ENABLED is the pre-shim behaviour and must not drift."""
    from services.audit_scheduler import _queue_worker_enabled

    monkeypatch.delenv("AUDIT_WORKER_ENABLED", raising=False)
    assert _queue_worker_enabled() is True


# ---------------------------------------------------------------------------
# GUARD: Sentry environment tag + release
# ---------------------------------------------------------------------------


def _init_sentry_capturing(monkeypatch):
    """Call init_sentry() against a fake sentry_sdk and return the init kwargs."""
    import types

    import config.sentry_config as sentry_config

    captured: dict = {}
    tags: dict = {}

    fake_sdk = types.ModuleType("sentry_sdk")
    fake_sdk.init = lambda **kwargs: captured.update(kwargs)  # type: ignore[attr-defined]
    fake_sdk.set_tag = lambda k, v: tags.__setitem__(k, v)  # type: ignore[attr-defined]

    fake_fastapi = types.ModuleType("sentry_sdk.integrations.fastapi")
    fake_fastapi.FastApiIntegration = lambda *a, **k: object()  # type: ignore[attr-defined]
    fake_sqla = types.ModuleType("sentry_sdk.integrations.sqlalchemy")
    fake_sqla.SqlalchemyIntegration = lambda *a, **k: object()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", types.ModuleType("sentry_sdk.integrations"))
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.fastapi", fake_fastapi)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.sqlalchemy", fake_sqla)
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")

    assert sentry_config.init_sentry() is True
    captured["_tags"] = tags
    return captured


def test_sentry_environment_is_production_in_every_production_shape(
    monkeypatch, production_shape
):
    captured = _init_sentry_capturing(monkeypatch)
    assert captured["environment"] == "production"


def test_sentry_environment_is_staging_on_staging(monkeypatch, staging_shape):
    """The pre-shim default filed staging errors under "production".

    ``os.getenv("ENVIRONMENT", "production")`` returned the literal
    "production" on every service here, because ENVIRONMENT is set nowhere.
    """
    captured = _init_sentry_capturing(monkeypatch)
    assert captured["environment"] == "staging"


def test_sentry_release_is_the_commit_sha_on_cloud_run(monkeypatch):
    """release MUST be a git sha, never K_REVISION.

    deployment_id() resolves to K_REVISION on Cloud Run ("web-00042-abc").
    Sentry cannot map that to a commit, so suspect-commits and regression
    detection break after cutover — silently, since Sentry accepts any string
    as a release. The revision is still published, as a TAG.
    """
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("K_REVISION", "pivota-backend-00042-abc")
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("PIVOTA_COMMIT_SHA", "c" * 40)
    captured = _init_sentry_capturing(monkeypatch)
    assert captured["release"] == "c" * 40
    assert captured["_tags"]["deployment_id"] == "pivota-backend-00042-abc"
    assert captured["_tags"]["platform"] == "cloud_run"


def test_sentry_release_is_the_commit_sha_on_railway(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "dep-abc-123")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "d" * 40)
    captured = _init_sentry_capturing(monkeypatch)
    assert captured["release"] == "d" * 40
    assert captured["_tags"]["deployment_id"] == "dep-abc-123"
    assert captured["_tags"]["platform"] == "railway"


def test_sentry_release_falls_back_to_the_deployment_id(monkeypatch):
    """A revision deployed without any commit sha still gets SOME release
    rather than None — but the fallback must not be mistaken for the fix."""
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("K_REVISION", "pivota-backend-00042-abc")
    monkeypatch.setenv("PIVOTA_ENV", "production")
    captured = _init_sentry_capturing(monkeypatch)
    assert captured["release"] == "pivota-backend-00042-abc"


# ---------------------------------------------------------------------------
# GUARD: DB_SSL_NO_VERIFY must never be honoured on a deployed host
#
# This one lives at db.database IMPORT time, so it can only be exercised in a
# fresh interpreter. Reloading the module in-process would swap the shared
# `database` object out from under every other test in the session.
# ---------------------------------------------------------------------------


def _import_db_database(extra_env: dict):
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("RAILWAY_", "PIVOTA_", "K_"))
    }
    env.update(
        {
            "DATABASE_URL": "postgresql://u:p@db.invalid:5432/pivota",
            "DB_SSL_NO_VERIFY": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # Isolates the guard under test. `db.database` pulls in
            # config.settings, whose _enforce_jwt_secret_strength() also refuses
        }
    )
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", "import db.database"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.parametrize(
    "shape",
    [
        {"RAILWAY_ENVIRONMENT": "production"},
        {"K_SERVICE": "pivota-backend", "PIVOTA_ENV": "production"},
        {"K_SERVICE": "pivota-backend"},
    ],
    ids=["railway", "cloud_run", "cloud_run_unresolved"],
)
def test_db_ssl_no_verify_is_refused_on_every_deployed_shape(shape):
    result = _import_db_database(shape)
    assert result.returncode != 0, (
        "certificate verification was silently disabled for the whole pool on a "
        f"deployed host: {result.stdout[-2000:]}"
    )
    assert "DB_SSL_NO_VERIFY must never be set" in result.stderr


def test_db_ssl_no_verify_is_still_allowed_for_a_local_ops_run():
    """The mutant this kills: raising unconditionally, which would break the
    local read-only ops CLIs this escape hatch exists for."""
    result = _import_db_database({})
    assert result.returncode == 0, result.stderr[-2000:]


# ---------------------------------------------------------------------------
# STARTUP: the app must refuse to boot rather than guess
# ---------------------------------------------------------------------------


def test_app_refuses_to_boot_on_an_unresolved_managed_platform(monkeypatch):
    """A Cloud Run revision deployed without PIVOTA_ENV must die at boot.

    require_platform_env() is the first statement in the lifespan, so this
    raises before any DB work. If someone deletes that call, this test is what
    notices — every other guard would keep passing on the fail-closed guess.
    """
    from fastapi.testclient import TestClient

    from main import app

    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    with pytest.raises(RuntimeError, match="PIVOTA_ENV"):
        with TestClient(app):
            pass


def _import_in_a_clean_process(module: str, extra_env: dict):
    """Import `module` in a fresh interpreter with a scrubbed environment."""
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("RAILWAY_", "PIVOTA_", "K_"))
        and k not in ("ENVIRONMENT", "APP_ENV")
    }
    env.update(
        {
            "DATABASE_URL": "postgresql://u:p@db.invalid:5432/pivota",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_proof_issuer_refuses_to_boot_on_an_unresolved_managed_platform():
    """The SECOND FastAPI service in this repo needs the assertion too.

    main.py had the only require_platform_env() call site, via its lifespan.
    proof_issuer_main.py is deployed separately from the same repo and had
    none — so on Cloud Run without PIVOTA_ENV it came up happily with every
    guard resolved by the fail-closed guess. It now asserts at import time,
    before the ASGI server can bind a port and start passing health checks.
    """
    result = _import_in_a_clean_process("proof_issuer_main", {"K_SERVICE": "proof-issuer"})
    assert result.returncode != 0, (
        "the proof issuer booted on an unresolvable managed host: "
        f"{result.stdout[-2000:]}"
    )
    assert "PIVOTA_ENV" in result.stderr


def test_proof_issuer_boots_when_the_environment_is_declared():
    """The mutant this kills: raising unconditionally, which would brick the
    service on every platform including the one it runs on today."""
    result = _import_in_a_clean_process(
        "proof_issuer_main", {"K_SERVICE": "proof-issuer", "PIVOTA_ENV": "production"}
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_proof_issuer_boots_locally():
    result = _import_in_a_clean_process("proof_issuer_main", {})
    assert result.returncode == 0, result.stderr[-2000:]


# ---------------------------------------------------------------------------
# PUBLISHED BYTES: the health/build payload must not change shape on Railway
# ---------------------------------------------------------------------------


def _build_payload(monkeypatch, shape: dict) -> dict:
    from main import _runtime_build_payload, _service_version_payload

    _apply(monkeypatch, shape)
    _service_version_payload.cache_clear()
    _runtime_build_payload.cache_clear()
    try:
        return _runtime_build_payload()
    finally:
        _service_version_payload.cache_clear()
        _runtime_build_payload.cache_clear()


def test_build_payload_railway_block_is_byte_identical_on_railway(monkeypatch):
    """The published contract: same keys, same values, same platform."""
    payload = _build_payload(
        monkeypatch,
        {
            "RAILWAY_ENVIRONMENT": "production",
            "RAILWAY_SERVICE_NAME": "web",
            "RAILWAY_SERVICE_ID": "svc-1",
            "RAILWAY_DEPLOYMENT_ID": "dep-1",
            "RAILWAY_GIT_COMMIT_SHA": "a" * 40,
            "RAILWAY_GIT_BRANCH": "main",
        },
    )
    assert payload["railway"] == {
        "environment": "production",
        "deployment_id": "dep-1",
        "service_id": "svc-1",
        "service_name": "web",
    }
    assert payload["git"] == {"commit_sha": "a" * 40, "branch": "main"}


def test_build_payload_resolves_the_same_fields_on_cloud_run(monkeypatch):
    payload = _build_payload(
        monkeypatch,
        {
            "K_SERVICE": "web",
            "K_REVISION": "web-00042-abc",
            "K_CONFIGURATION": "web",
            "PIVOTA_ENV": "production",
            "PIVOTA_COMMIT_SHA": "b" * 40,
            "PIVOTA_GIT_BRANCH": "main",
        },
    )
    assert payload["railway"]["service_name"] == "web"
    assert payload["railway"]["deployment_id"] == "web-00042-abc"
    assert payload["git"] == {"commit_sha": "b" * 40, "branch": "main"}
    assert payload["platform"]["platform"] == "cloud_run"
    assert payload["platform"]["environment"] == "production"
    assert payload["platform"]["environment_source"] == "pivota_env"


def test_build_payload_flags_a_fail_closed_guess(monkeypatch):
    """Ops must be able to see, from /health, that prod was inferred not read."""
    payload = _build_payload(monkeypatch, {"K_SERVICE": "web"})
    assert payload["platform"]["environment"] == "production"
    assert payload["platform"]["environment_source"] == "fail_closed"

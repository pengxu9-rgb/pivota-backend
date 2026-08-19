"""Unit tests for :mod:`config.platform` — the Railway → Cloud Run shim.

Every precedence branch, both platform families, and the FAIL-CLOSED branch.
The fail-closed branch is the one that matters: on Cloud Run every RAILWAY_*
variable is unset, so a shim that answered "development" would silently unarm
every production guard in the service.
"""
from __future__ import annotations

import logging

import pytest

from config import platform as P

# Everything the shim looks at.  Cleared before each test so a developer's real
# shell (or a CI runner that happens to export COMMIT_SHA) cannot make a case
# pass or fail for the wrong reason.
_ALL_KEYS = (
    "PIVOTA_PLATFORM",
    "PIVOTA_ENV",
    "PIVOTA_SERVICE_NAME",
    "PIVOTA_SERVICE_ID",
    "PIVOTA_PROJECT_ID",
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
    "RAILWAY_GIT_AUTHOR",
    "RAILWAY_PRIVATE_DOMAIN",
    "K_SERVICE",
    "K_REVISION",
    "K_CONFIGURATION",
    "COMMIT_SHA",
    "SOURCE_VERSION",
    "GIT_COMMIT_SHA",
    "VERCEL_GIT_COMMIT_SHA",
    "GIT_BRANCH",
    "GOOGLE_CLOUD_PROJECT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    P.reset_platform_state()
    yield
    P.reset_platform_state()


# ---------------------------------------------------------------------------
# platform_name
# ---------------------------------------------------------------------------


def test_platform_name_is_local_with_no_markers():
    assert P.platform_name() == "local"
    assert P.is_deployed() is False


def test_platform_name_is_railway_from_any_deployment_marker(monkeypatch):
    # Not just RAILWAY_ENVIRONMENT: a worker service is identified by any of the
    # variables Railway injects into a running container.
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc-abc123")
    assert P.platform_name() == "railway"
    assert P.is_deployed() is True


def test_railway_build_metadata_alone_is_not_a_deployment(monkeypatch):
    """RAILWAY_GIT_* describes a COMMIT, not the host executing this process.

    It was originally treated as proof of deployment (the whole detection was a
    `RAILWAY_` prefix scan). That scan also swallowed `RAILWAY_TOKEN`, the CLI
    credential developers export to reach the prod DB through the proxy — which
    resolved a laptop as production and armed the settlement transfer path. The
    git vars are still read as VALUES by commit_sha() / git_branch(); they just
    no longer decide which platform we are on. Every real Railway deployment
    also carries RAILWAY_ENVIRONMENT and RAILWAY_SERVICE_ID, so nothing that is
    genuinely deployed loses its identity here.
    """
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123")
    monkeypatch.setenv("RAILWAY_GIT_BRANCH", "main")
    assert P.platform_name() == "local"
    assert P.is_deployed() is False
    assert P.is_production() is False
    # …but the value is still readable.
    assert P.commit_sha() == "abc123"
    assert P.git_branch() == "main"


def test_platform_name_is_cloud_run_from_k_service(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    assert P.platform_name() == "cloud_run"
    assert P.is_deployed() is True


def test_empty_railway_var_is_not_a_platform_signal(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "")
    assert P.platform_name() == "local"
    assert P.is_deployed() is False


def test_railway_wins_over_cloud_run_during_a_dual_run(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    assert P.platform_name() == "railway"


def test_pivota_platform_overrides_detection(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("PIVOTA_PLATFORM", "cloud_run")
    assert P.platform_name() == "cloud_run"


# ---------------------------------------------------------------------------
# platform_env precedence
# ---------------------------------------------------------------------------


def test_local_default_is_development():
    assert P.platform_env() == "development"
    assert P.is_production() is False
    assert P.is_staging() is False
    assert P.is_development() is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("production", "production"),
        ("PRODUCTION", "production"),
        ("  prod ", "production"),
        ("live", "production"),
        ("staging", "staging"),
        ("preview", "staging"),
        ("development", "development"),
        ("dev", "development"),
        ("test", "test"),
        ("ci", "test"),
    ],
)
def test_pivota_env_is_normalised(monkeypatch, raw, expected):
    monkeypatch.setenv("PIVOTA_ENV", raw)
    assert P.platform_env() == expected


def test_pivota_env_beats_railway(monkeypatch):
    # The cutover shape: PIVOTA_ENV is authoritative so a service can be moved
    # without touching whatever the old platform still injects.
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "staging")
    monkeypatch.setenv("PIVOTA_ENV", "production")
    assert P.platform_env() == "production"
    assert P.is_production() is True


def test_railway_environment_resolves(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert P.platform_env() == "production"
    assert P.is_production() is True


def test_railway_environment_name_is_the_fallback(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    assert P.platform_env() == "staging"
    assert P.is_staging() is True
    assert P.is_production() is False


def test_railway_environment_beats_railway_environment_name(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    assert P.platform_env() == "production"


def test_cloud_run_with_pivota_env_resolves(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("K_REVISION", "pivota-backend-00042-abc")
    monkeypatch.setenv("PIVOTA_ENV", "production")
    assert P.platform_name() == "cloud_run"
    assert P.platform_env() == "production"
    assert P.is_production() is True


def test_cloud_run_staging_is_not_production(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    assert P.is_production() is False
    assert P.is_staging() is True


# ---------------------------------------------------------------------------
# FAIL CLOSED
# ---------------------------------------------------------------------------


def test_cloud_run_without_env_fails_closed_to_production(monkeypatch):
    """The migration's worst case: a revision deployed with no PIVOTA_ENV.

    Answering "development" here would unarm every guard in the service at
    once, so the shim answers "production".
    """
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    assert P.platform_env() == "production"
    assert P.is_production() is True
    assert P.is_development() is False


def test_railway_marker_without_env_fails_closed_to_production(monkeypatch):
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "web")
    assert P.platform_env() == "production"


def test_unrecognised_env_value_on_a_managed_host_fails_closed(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("PIVOTA_ENV", "prd-us-west1")  # typo'd / non-vocabulary
    assert P.platform_env() == "production"


def test_unrecognised_railway_env_value_fails_closed(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "prod-canary")
    assert P.platform_env() == "production"


def test_fail_closed_logs_loudly(monkeypatch, caplog):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    with caplog.at_level(logging.ERROR, logger="config.platform"):
        P.platform_env()
    assert any(
        rec.levelno >= logging.ERROR and "FAILING CLOSED" in rec.getMessage()
        for rec in caplog.records
    ), "the fail-closed branch must be loud, not silent"


def test_fail_closed_log_is_deduped_but_the_value_is_not_cached(monkeypatch, caplog):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    with caplog.at_level(logging.ERROR, logger="config.platform"):
        P.platform_env()
        P.platform_env()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "per-request log spam"

    # The VALUE must still track the live environment — memoising the warning
    # must not memoise the answer.
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    assert P.platform_env() == "staging"


def test_local_dev_does_not_fail_closed_or_log(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR, logger="config.platform"):
        assert P.platform_env() == "development"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------------------
# require_platform_env
# ---------------------------------------------------------------------------


def test_require_platform_env_is_silent_locally():
    assert P.require_platform_env() == "development"


def test_require_platform_env_accepts_railway_production(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert P.require_platform_env() == "production"


def test_require_platform_env_accepts_cloud_run_with_pivota_env(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("PIVOTA_ENV", "production")
    assert P.require_platform_env() == "production"


def test_require_platform_env_raises_on_unresolved_cloud_run(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    with pytest.raises(RuntimeError) as excinfo:
        P.require_platform_env()
    assert "PIVOTA_ENV" in str(excinfo.value)


def test_require_platform_env_raises_on_unrecognised_value(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("PIVOTA_ENV", "prd")
    with pytest.raises(RuntimeError):
        P.require_platform_env()


# ---------------------------------------------------------------------------
# metadata accessors
# ---------------------------------------------------------------------------


def test_service_name_precedence(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "cr-name")
    assert P.service_name() == "cr-name"
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "rw-name")
    assert P.service_name() == "rw-name"
    monkeypatch.setenv("PIVOTA_SERVICE_NAME", "explicit")
    assert P.service_name() == "explicit"


def test_deployment_id_precedence(monkeypatch):
    monkeypatch.setenv("K_REVISION", "svc-00042-abc")
    assert P.deployment_id() == "svc-00042-abc"
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "rw-dep")
    assert P.deployment_id() == "rw-dep"
    monkeypatch.setenv("PIVOTA_DEPLOYMENT_ID", "explicit")
    assert P.deployment_id() == "explicit"


def test_service_id_precedence(monkeypatch):
    monkeypatch.setenv("K_CONFIGURATION", "cr-config")
    assert P.service_id() == "cr-config"
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "rw-id")
    assert P.service_id() == "rw-id"


def test_git_branch_precedence(monkeypatch):
    monkeypatch.setenv("GIT_BRANCH", "legacy")
    assert P.git_branch() == "legacy"
    monkeypatch.setenv("RAILWAY_GIT_BRANCH", "main")
    assert P.git_branch() == "main"
    monkeypatch.setenv("PIVOTA_GIT_BRANCH", "explicit")
    assert P.git_branch() == "explicit"


def test_project_id_precedence(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gcp-proj")
    assert P.project_id() == "gcp-proj"
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "rw-proj")
    assert P.project_id() == "rw-proj"


def test_commit_sha_has_no_platform_source_on_cloud_run(monkeypatch):
    """Cloud Run injects NO commit sha — the deploy must supply one."""
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("K_REVISION", "svc-00042-abc")
    assert P.commit_sha() is None


@pytest.mark.parametrize(
    "key", ["PIVOTA_COMMIT_SHA", "RAILWAY_GIT_COMMIT_SHA", "COMMIT_SHA", "SOURCE_VERSION"]
)
def test_commit_sha_sources(monkeypatch, key):
    monkeypatch.setenv(key, "deadbeef")
    assert P.commit_sha() == "deadbeef"


def test_commit_sha_precedence_order(monkeypatch):
    monkeypatch.setenv("SOURCE_VERSION", "sv")
    monkeypatch.setenv("COMMIT_SHA", "cs")
    assert P.commit_sha() == "cs"
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "rw")
    assert P.commit_sha() == "rw"
    monkeypatch.setenv("PIVOTA_COMMIT_SHA", "explicit")
    assert P.commit_sha() == "explicit"


def test_raw_environment_label_is_unnormalised(monkeypatch):
    """Health payloads publish the platform's own bytes, not our vocabulary."""
    assert P.raw_environment_label() is None
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "PRODUCTION")
    assert P.raw_environment_label() == "PRODUCTION"
    assert P.platform_env() == "production"


def test_platform_metadata_shape(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("K_REVISION", "pivota-backend-00042-abc")
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("PIVOTA_COMMIT_SHA", "abc1234")
    meta = P.platform_metadata()
    assert meta["platform"] == "cloud_run"
    assert meta["environment"] == "production"
    assert meta["environment_source"] == "pivota_env"
    assert meta["deployed"] is True
    assert meta["service_name"] == "pivota-backend"
    assert meta["deployment_id"] == "pivota-backend-00042-abc"
    assert meta["commit_sha"] == "abc1234"


def test_platform_metadata_marks_the_fail_closed_source(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    meta = P.platform_metadata()
    assert meta["environment"] == "production"
    assert meta["environment_source"] == "fail_closed", (
        "a health endpoint must be able to tell a real production from a guess"
    )


# ---------------------------------------------------------------------------
# injected mappings (utils.startup_mode passes a dict)
# ---------------------------------------------------------------------------


def test_accessors_accept_an_injected_mapping(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    # The injected mapping must WIN over the real process environment,
    # otherwise a caller that passes {} is silently reading os.environ.
    assert P.platform_env({}) == "development"
    assert P.is_deployed({}) is False
    assert P.platform_env({"RAILWAY_ENVIRONMENT": "staging"}) == "staging"
    assert P.is_deployed({"K_SERVICE": "x"}) is True
    assert P.service_name({"RAILWAY_SERVICE_NAME": "web"}) == "web"


def test_no_import_time_caching(monkeypatch):
    """Values are read per call — the whole test suite depends on this."""
    assert P.platform_env() == "development"
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert P.platform_env() == "production"
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "staging")
    assert P.platform_env() == "staging"
    monkeypatch.delenv("RAILWAY_ENVIRONMENT")
    assert P.platform_env() == "development"

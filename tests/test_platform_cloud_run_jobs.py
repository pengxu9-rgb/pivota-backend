"""Cloud Run **Jobs** must resolve as a deployed host.

THE DEFECT THIS FILE PINS
-------------------------
``_CLOUD_RUN_KEYS`` used to be ``("K_SERVICE", "K_REVISION",
"K_CONFIGURATION")``. Those three are injected by Cloud Run *services*. A Cloud
Run **Job** is a different product and gets none of them; it gets
``CLOUD_RUN_JOB``, ``CLOUD_RUN_EXECUTION`` and the per-task
``CLOUD_RUN_TASK_INDEX`` / ``_COUNT`` / ``_ATTEMPT`` instead. Live-confirmed on
2026-08-26 by probing the running prod ``content-canonical-election`` job: its
entire environment was those five plus our own ``PIVOTA_COMMIT_SHA`` /
``PIVOTA_ENV`` / ``PIVOTA_SERVICE_NAME``.

So ``platform_name()`` answered ``"local"`` and ``is_deployed()`` answered
``False`` inside every job container. In pivota-prod the jobs all set
``PIVOTA_ENV=production``, so ``is_production()`` still caught them and nothing
was exposed. A **staging** job answered ``False`` to BOTH — every gate written
on either predicate failed open there. That row is ``staging_job`` below.

WHY EACH CASE ASSERTS THREE THINGS
----------------------------------
Same shape as ``tests/test_platform_pytest_bypass_allowed.py`` (PR #1900): each
row pins ``platform_name()``, ``is_deployed()`` AND ``is_production()``
together. Asserting ``is_deployed()`` alone would let a row pass for the wrong
reason — a typo in ``PIVOTA_ENV`` makes the shim fail closed to production, and
a case meant to prove "deployed but NOT production" would then be quietly
testing the production branch instead.
"""
from __future__ import annotations

import os

import pytest

from config import platform as P
from tests.test_platform_shim import _ALL_KEYS

#: The real process environment, captured at IMPORT time.
#:
#: This must not be read inside a test body. The autouse ``_clean_env`` fixture
#: below deletes every key in ``_ALL_KEYS`` — which now includes the Cloud Run
#: Jobs markers — before any test runs, so a test that asks ``os.environ`` at
#: call time is asking a mapping the fixture has already sanitised and can
#: never fail. Module import happens before collection, hence before fixtures.
_REAL_ENV_AT_IMPORT = dict(os.environ)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    P.reset_platform_state()
    yield
    P.reset_platform_state()


#: The exact environment observed inside the live prod job on 2026-08-26,
#: minus values that do not affect resolution. Kept verbatim so a future
#: reader can see that this is a transcript, not a guess.
_LIVE_PROD_JOB = {
    "CLOUD_RUN_EXECUTION": "content-canonical-election-hq4kx",
    "CLOUD_RUN_JOB": "content-canonical-election",
    "CLOUD_RUN_TASK_ATTEMPT": "0",
    "CLOUD_RUN_TASK_COUNT": "1",
    "CLOUD_RUN_TASK_INDEX": "0",
    "PIVOTA_COMMIT_SHA": "deadbeef",
    "PIVOTA_ENV": "production",
    "PIVOTA_SERVICE_NAME": "content-canonical-election",
}

#: ``(env, platform_name, is_deployed, is_production)``.
_ROWS = {
    # ---- the row that was open ------------------------------------------
    "staging_job": (
        {
            "CLOUD_RUN_JOB": "content-canonical-election",
            "CLOUD_RUN_EXECUTION": "content-canonical-election-abc12",
            "PIVOTA_ENV": "staging",
        },
        "cloud_run",
        True,
        False,
    ),
    # ---- already-safe-by-accident rows, now safe on purpose --------------
    "live_prod_job_transcript": (_LIVE_PROD_JOB, "cloud_run", True, True),
    "prod_job": (
        {
            "CLOUD_RUN_JOB": "derive-offer-market-currency",
            "CLOUD_RUN_EXECUTION": "derive-offer-market-currency-abc12",
            "PIVOTA_ENV": "production",
        },
        "cloud_run",
        True,
        True,
    ),
    "development_job": (
        {"CLOUD_RUN_JOB": "agent-pdp-orphan-reaper", "PIVOTA_ENV": "development"},
        "cloud_run",
        True,
        False,
    ),
    # Either marker alone is sufficient; neither is load-bearing on the other.
    "job_marker_only": (
        {"CLOUD_RUN_JOB": "reviews-invitation-send", "PIVOTA_ENV": "staging"},
        "cloud_run",
        True,
        False,
    ),
    "execution_marker_only": (
        {"CLOUD_RUN_EXECUTION": "reviews-invitation-send-abc12", "PIVOTA_ENV": "staging"},
        "cloud_run",
        True,
        False,
    ),
    # A job deployed without PIVOTA_ENV: demonstrably managed, environment
    # unknowable -> FAIL CLOSED to production. Before the fix this answered
    # ("local", False, False) and every guard on the revision unarmed at once.
    "job_without_pivota_env_fails_closed": (
        {"CLOUD_RUN_JOB": "audit-domainless-offer-currency"},
        "cloud_run",
        True,
        True,
    ),
    # ---- the service family must not regress ----------------------------
    "service_k_service": (
        {"K_SERVICE": "pivota-backend-staging", "PIVOTA_ENV": "staging"},
        "cloud_run",
        True,
        False,
    ),
    # ---- deliberate exclusions ------------------------------------------
    # The TASK_* trio describes the shard/retry of the WORK, not the host.
    # Honouring it would let a local fan-out harness ("task 3 of 5") answer
    # "am I deployed" — the exact failure _RAILWAY_DEPLOYMENT_KEYS exists to
    # prevent. Cloud Run always sets CLOUD_RUN_JOB alongside these, so nothing
    # genuinely deployed is lost.
    "task_vars_alone_are_not_a_deployment": (
        {
            "CLOUD_RUN_TASK_INDEX": "0",
            "CLOUD_RUN_TASK_COUNT": "1",
            "CLOUD_RUN_TASK_ATTEMPT": "0",
            "PIVOTA_ENV": "staging",
        },
        "local",
        False,
        False,
    ),
    "task_index_nonzero_is_still_not_a_deployment": (
        {"CLOUD_RUN_TASK_INDEX": "3", "CLOUD_RUN_TASK_COUNT": "5", "PIVOTA_ENV": "staging"},
        "local",
        False,
        False,
    ),
    # Absent and empty are the same thing, here as everywhere else.
    "empty_job_marker_is_not_a_signal": (
        {"CLOUD_RUN_JOB": "", "CLOUD_RUN_EXECUTION": "", "PIVOTA_ENV": "staging"},
        "local",
        False,
        False,
    ),
    # Nothing named CLOUD_RUN_* is honoured by prefix: this is an allowlist.
    "unknown_cloud_run_variable_is_not_a_signal": (
        {"CLOUD_RUN_REGION": "us-west1", "PIVOTA_ENV": "staging"},
        "local",
        False,
        False,
    ),
    # ---- precedence -----------------------------------------------------
    "railway_still_wins_over_a_job_marker": (
        {"RAILWAY_ENVIRONMENT": "production", "CLOUD_RUN_JOB": "relgraph-sync"},
        "railway",
        True,
        True,
    ),
    "pivota_platform_override_still_wins": (
        {"PIVOTA_PLATFORM": "local", "CLOUD_RUN_JOB": "relgraph-sync", "PIVOTA_ENV": "staging"},
        "local",
        False,
        False,
    ),
}


def _apply(monkeypatch, env: dict) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize(
    "env,expected_platform,expected_deployed,expected_production",
    [pytest.param(*row, id=name) for name, row in _ROWS.items()],
)
def test_platform_resolution(
    monkeypatch, env, expected_platform, expected_deployed, expected_production
):
    _apply(monkeypatch, env)
    assert P.platform_name() == expected_platform
    assert P.is_deployed() is expected_deployed
    assert P.is_production() is expected_production


def test_the_reported_reproduction(monkeypatch):
    """The exact repro from the bug report, asserted end to end.

    Before the fix this printed ``local False False``.
    """
    _apply(
        monkeypatch,
        {"CLOUD_RUN_JOB": "x", "CLOUD_RUN_EXECUTION": "y", "PIVOTA_ENV": "staging"},
    )
    assert (P.platform_name(), P.is_deployed(), P.is_production()) == (
        "cloud_run",
        True,
        False,
    )


def test_a_staging_job_can_no_longer_use_a_test_credential(monkeypatch):
    """The guard that actually moves for a staging Job.

    ``pytest_bypass_allowed`` is ``not (is_deployed() or is_production())``.
    A staging job answered False to both, so a leaked ``PYTEST_CURRENT_TEST``
    there handed out ``role=admin`` on ``test-token`` and an unbounded agent
    context on ``test-agent-key``. Reverting either new marker re-arms this.
    """
    _apply(monkeypatch, _ROWS["staging_job"][0])
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y (call)")
    assert P.is_deployed() is True
    assert P.is_production() is False
    assert P.pytest_bypass_allowed() is False


def test_require_platform_env_raises_on_a_job_without_pivota_env(monkeypatch):
    """A job that named no environment is a managed host we cannot resolve.

    Before this change it resolved to ``development`` and booted happily.
    """
    _apply(monkeypatch, {"CLOUD_RUN_JOB": "audit-domainless-offer-currency"})
    with pytest.raises(RuntimeError, match="cannot resolve the deployment environment") as excinfo:
        P.require_platform_env()
    # The refusal must name the marker that proved this is a managed host, or
    # the operator cannot tell why their container refused to boot. This
    # message is the twin of the _resolve_env warning and had the same Cloud
    # Run blindness: it named only RAILWAY_ENVIRONMENT.
    assert "CLOUD_RUN_JOB=audit-domainless-offer-currency" in str(excinfo.value)


def test_require_platform_env_accepts_a_job_with_pivota_env(monkeypatch):
    """Positive control ONLY — it does not exercise platform detection.

    ``PIVOTA_ENV`` is set, so resolution short-circuits before the Cloud Run
    markers are ever consulted: this passes identically with the whole change
    reverted, and it kills no mutant. Its sibling above is the row that pins
    detection. Kept because a guard that refuses a correctly-configured job
    would be worse than the bug, but do not read it as coverage.
    """
    _apply(monkeypatch, _LIVE_PROD_JOB)
    assert P.require_platform_env() == "production"


def test_fail_closed_log_names_the_job_marker_not_k_service(monkeypatch, caplog):
    """The diagnostic must identify the host it is diagnosing.

    The message used to print ``K_SERVICE=%r``, which is ``None`` inside every
    Cloud Run Job — i.e. it was blank on exactly the hosts that most needed it.
    """
    _apply(monkeypatch, {"CLOUD_RUN_JOB": "audit-domainless-offer-currency"})
    with caplog.at_level("ERROR"):
        assert P.platform_env() == "production"
    messages = [rec.getMessage() for rec in caplog.records if "FAILING CLOSED" in rec.getMessage()]
    assert messages, "expected the fail-closed line"
    # KEY=value, not a bare value: "audit-domainless-offer-currency" alone does
    # not say whether a Service or a Job produced it.
    assert "CLOUD_RUN_JOB=audit-domainless-offer-currency" in messages[0]


def test_fail_closed_log_still_names_k_service_for_a_service(monkeypatch, caplog):
    """The Jobs fix must not cost the Services diagnostic.

    Teaching the marker helper about Jobs makes it easy to narrow it to the Jobs
    family alone — a mutant that does exactly that survives every other test in
    this file, because nothing else asserts the SERVICE side of this log line.
    This is that assertion.
    """
    _apply(monkeypatch, {"K_SERVICE": "pivota-web", "K_REVISION": "pivota-web-00042-abc"})
    with caplog.at_level("ERROR"):
        assert P.platform_env() == "production"
    messages = [rec.getMessage() for rec in caplog.records if "FAILING CLOSED" in rec.getMessage()]
    assert messages, "expected the fail-closed line"
    assert "K_SERVICE=pivota-web" in messages[0]


def test_job_metadata_falls_back_to_the_job_and_execution_names(monkeypatch):
    """Read as VALUES, not as host proof — the same split as RAILWAY_GIT_*.

    Every job we deploy sets ``PIVOTA_SERVICE_NAME``, so this fallback is inert
    in every live job today — no job entrypoint reaches ``service_name()`` at
    all. It decides only what a FUTURE caller, or a job created without
    ``PIVOTA_SERVICE_NAME``, reports: the job name beats ``None``.
    """
    _apply(
        monkeypatch,
        {"CLOUD_RUN_JOB": "relgraph-sync", "CLOUD_RUN_EXECUTION": "relgraph-sync-abc12", "PIVOTA_ENV": "production"},
    )
    assert P.service_name() == "relgraph-sync"
    assert P.deployment_id() == "relgraph-sync-abc12"
    # …and an explicit PIVOTA_* still outranks them.
    monkeypatch.setenv("PIVOTA_SERVICE_NAME", "explicit")
    monkeypatch.setenv("PIVOTA_DEPLOYMENT_ID", "explicit-id")
    assert P.service_name() == "explicit"
    assert P.deployment_id() == "explicit-id"


def test_platform_metadata_reports_a_job_as_deployed(monkeypatch):
    _apply(monkeypatch, _LIVE_PROD_JOB)
    meta = P.platform_metadata()
    assert meta["platform"] == "cloud_run"
    assert meta["deployed"] is True
    assert meta["environment"] == "production"
    assert meta["environment_source"] == "pivota_env"


def test_the_suite_itself_is_not_a_cloud_run_job():
    """If CI ever starts exporting CLOUD_RUN_JOB, every `test-token` test dies.

    This fails first and says why.

    ⚠️ It reads ``_REAL_ENV_AT_IMPORT``, NOT ``os.environ``. The autouse
    ``_clean_env`` fixture strips these keys before every test body, so the
    obvious spelling —

        assert not os.environ.get("CLOUD_RUN_JOB")

    — is an assertion that CANNOT FAIL: it passes under
    ``CLOUD_RUN_JOB=evil pytest``, which is precisely the condition it claims
    to detect. ``test_ci_and_local_are_unmanaged_so_the_suite_is_unaffected``
    in ``tests/test_platform_pytest_bypass_allowed.py`` has that defect for the
    K_* markers; do not copy it here.
    """
    for key in ("CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION", "K_SERVICE", "RAILWAY_ENVIRONMENT"):
        assert not (_REAL_ENV_AT_IMPORT.get(key) or "").strip(), (
            f"{key} is set in the real test environment. Every test that "
            f"authenticates with test-token / test-agent-key is now refused by "
            f"config.platform.pytest_bypass_allowed()."
        )
    # And the shim agrees, given that environment. PYTEST_CURRENT_TEST is
    # overlaid because pytest sets it per-test, i.e. after this snapshot was
    # taken — the gate at a real call site always sees it set.
    assert P.platform_name(_REAL_ENV_AT_IMPORT) == "local"
    assert P.is_deployed(_REAL_ENV_AT_IMPORT) is False
    live = dict(_REAL_ENV_AT_IMPORT)
    live["PYTEST_CURRENT_TEST"] = "tests/test_x.py::test_y (call)"
    assert P.pytest_bypass_allowed(live) is True

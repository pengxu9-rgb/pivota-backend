"""Platform abstraction: ONE place that knows which host we are deployed on.

WHY THIS EXISTS
---------------
Production has no ``NODE_ENV`` and no ``ENV``.  For most of this codebase's
life the de-facto "am I in production" signal has been Railway's injected
``RAILWAY_ENVIRONMENT`` (and its siblings ``RAILWAY_SERVICE_NAME`` /
``RAILWAY_GIT_COMMIT_SHA`` / ``RAILWAY_DEPLOYMENT_ID``).  Those variables gate
Sentry release tagging, the settlement transfer money path, the shakeout debug
endpoints, the shared-queue scheduler worker, the startup-mode DDL skip, the
webhook livemode guards and the service-token strength check.

We are moving to Google Cloud Run (us-west1).  On Cloud Run **every** RAILWAY_*
variable is unset.  A guard written as ``os.getenv("RAILWAY_ENVIRONMENT") ==
"production"`` therefore evaluates to *False* on the new platform, which means
the "not production" branch — demo data, unsigned webhooks, insecure fallbacks,
debug endpoints — would run live.  That is the failure mode this module exists
to make impossible: read the platform through here, and the migration is a
config change instead of a silent security regression.

CONTRACT
--------
* Nothing is cached at import time.  Every accessor reads the environment on
  each call, so ``monkeypatch.setenv`` in tests works exactly as it always did.
* Every accessor takes an optional ``env`` mapping, so callers that already
  accept an injected environment (``utils.startup_mode``) keep their signature.
* Precedence:  explicit ``PIVOTA_*``  →  Railway  →  Cloud Run  →  local.
* FAIL CLOSED: if we are demonstrably on a managed platform (``K_SERVICE`` set,
  or a Railway *deployment* marker set — see ``_RAILWAY_DEPLOYMENT_KEYS``, which
  deliberately excludes the CLI's auth variables) but the environment cannot be resolved to
  production/staging, :func:`platform_env` returns ``"production"`` and logs
  loudly.  The safest wrong answer is "this is production": demo/fabrication/
  insecure paths stay OFF and prod-only money/queue paths stay ON.
* :func:`require_platform_env` is called at app startup and *raises* in that
  same situation, so a misconfigured Cloud Run revision dies at boot rather
  than serving half-guarded traffic.  Local dev and tests (no ``K_SERVICE``, no
  ``RAILWAY_*``) resolve to ``"development"`` and never raise.

Cloud Run does NOT inject a git commit sha.  The image stamps it at BUILD time
into ``/app/.image_commit_sha`` (see ``infra/gcp/Dockerfile``) and that wins,
because it is a property of the code rather than a claim made about it at deploy
time.  ``PIVOTA_COMMIT_SHA`` (and ``COMMIT_SHA`` / ``SOURCE_VERSION``, which
Cloud Build and buildpacks set) remain the fallback for images built without the
build arg, and for local runs.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Mapping, Optional

try:  # pragma: no cover - typing_extensions is not a hard dependency
    from typing import Literal

    PlatformEnv = Literal["production", "staging", "development", "test"]
    PlatformName = Literal["railway", "cloud_run", "local"]
except ImportError:  # pragma: no cover
    PlatformEnv = str  # type: ignore[misc,assignment]
    PlatformName = str  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "PRODUCTION",
    "STAGING",
    "DEVELOPMENT",
    "TEST",
    "platform_env",
    "platform_name",
    "is_production",
    "is_staging",
    "is_development",
    "is_deployed",
    "service_name",
    "service_id",
    "project_id",
    "commit_sha",
    "deployment_id",
    "git_branch",
    "raw_environment_label",
    "platform_metadata",
    "require_platform_env",
    "reset_platform_state",
]

PRODUCTION = "production"
STAGING = "staging"
DEVELOPMENT = "development"
TEST = "test"

# Normalisation of whatever string the platform hands us.  Railway's
# `RAILWAY_ENVIRONMENT` is "production"/"staging" in this account; PR
# environments report "preview". Cloud Run has no equivalent at all, which is
# exactly why PIVOTA_ENV must be set on every revision.
_ENV_ALIASES: Dict[str, str] = {
    "production": PRODUCTION,
    "prod": PRODUCTION,
    "live": PRODUCTION,
    "staging": STAGING,
    "stage": STAGING,
    "preview": STAGING,
    "development": DEVELOPMENT,
    "dev": DEVELOPMENT,
    "local": DEVELOPMENT,
    "test": TEST,
    "testing": TEST,
    "ci": TEST,
}

# Any of these being non-empty proves Railway INJECTED them into a running
# deployment — i.e. we are executing inside a Railway container.
#
# ⚠️ THIS IS AN ALLOWLIST, NOT A `RAILWAY_` PREFIX SCAN, AND THAT IS THE WHOLE
# POINT. The first version of this module scanned the prefix, reasoning that a
# new Railway variable should count immediately. It counted too much: the
# Railway CLI's own credentials are named `RAILWAY_TOKEN` / `RAILWAY_API_TOKEN`,
# and this team's documented ops workflow is "reach the prod DB through the
# Railway proxy", so those are exported in developer shell profiles. With the
# prefix scan, a laptop with a token exported resolved platform_name=railway,
# fail-closed to platform_env=production, is_production()=True — which ARMED THE
# REAL SETTLEMENT TRANSFER PATH (services.settlement_file_service has inverted
# polarity: production ENABLES payouts) and made require_platform_env() kill
# local boot. A developer's shell must never be able to answer "am I in
# production".
#
# Excluded on purpose:
#   RAILWAY_TOKEN, RAILWAY_API_TOKEN — CLI auth credentials, developer-exported.
#   RAILWAY_PRIVATE_DOMAIN          — a routing TARGET, not a statement about
#       the host we run on; it is routinely copied into local .env files and
#       docker-compose to address a Railway-side service from outside.
#   RAILWAY_GIT_* (COMMIT_SHA, BRANCH, AUTHOR, …) — build metadata about a
#       commit, not proof that Railway is executing this process. They are
#       copied into local .env files and CI matrices for /version parity, and
#       `commit_sha()` / `git_branch()` below still read them as VALUES. Reading
#       a value is not the same question as "which host am I on".
#
# The cost of the allowlist is that a brand-new Railway deployment variable must
# be added here by hand. That is the correct trade: this list decides whether
# money moves, and the risk of missing an addition (Railway always injects
# RAILWAY_ENVIRONMENT and RAILWAY_SERVICE_ID on every deployment, both listed)
# is far smaller than the risk of a shell export deciding it.
_RAILWAY_DEPLOYMENT_KEYS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_SERVICE_NAME",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_DEPLOYMENT_ID",
)
# Cloud Run injects K_SERVICE / K_REVISION / K_CONFIGURATION on every revision.
_CLOUD_RUN_KEYS = ("K_SERVICE", "K_REVISION", "K_CONFIGURATION")

# Loud-but-not-per-request: the fail-closed path logs once per distinct
# signature.  This memoises LOGGING ONLY — the resolved value is always
# recomputed from the live environment.
_WARNED_SIGNATURES: set = set()


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _mapping(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def _get(env: Optional[Mapping[str, str]], key: str) -> str:
    """Read one variable, stripped.  Absent and empty are the same thing."""
    return (_mapping(env).get(key) or "").strip()


#: Written by ``infra/gcp/Dockerfile`` at build time.  Module-level so tests can point it
#: somewhere writable.
_IMAGE_COMMIT_SHA_FILE = "/app/.image_commit_sha"
_UNREAD = object()
_image_commit_sha_cache: Any = _UNREAD


def _baked_commit_sha() -> Optional[str]:
    """The commit stamped into the image, or ``None`` outside a stamped image.

    Cached: this is read on every ``/health``, and the file cannot change without the
    process being replaced along with it.  Tests reset ``_image_commit_sha_cache``.
    """
    global _image_commit_sha_cache
    if _image_commit_sha_cache is _UNREAD:
        try:
            with open(_IMAGE_COMMIT_SHA_FILE, "r", encoding="utf-8") as handle:
                _image_commit_sha_cache = handle.read().strip() or None
        except OSError:
            # Not running from a stamped image - local dev, tests, or an image built
            # without the build arg. The env-var fallback covers those.
            _image_commit_sha_cache = None
    return _image_commit_sha_cache


def _first(env: Optional[Mapping[str, str]], *keys: str) -> Optional[str]:
    for key in keys:
        value = _get(env, key)
        if value:
            return value
    return None


def _on_railway(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when Railway injected a DEPLOYMENT marker into this process.

    Explicit allowlist, not a ``RAILWAY_`` prefix scan — see the comment on
    :data:`_RAILWAY_DEPLOYMENT_KEYS`. ``RAILWAY_TOKEN`` in a developer's shell
    profile must resolve to ``local``, not to production.
    """
    return any(_get(env, key) for key in _RAILWAY_DEPLOYMENT_KEYS)


def _on_cloud_run(env: Optional[Mapping[str, str]] = None) -> bool:
    return any(_get(env, key) for key in _CLOUD_RUN_KEYS)


def _normalize(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _ENV_ALIASES.get(raw.strip().lower())


def _warn_once(signature: str, message: str, *args: Any) -> None:
    if signature in _WARNED_SIGNATURES:
        return
    _WARNED_SIGNATURES.add(signature)
    logger.error(message, *args)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def reset_platform_state() -> None:
    """Clear the once-only warning memo.

    There is no value cache to clear — accessors always read the live
    environment.  Tests that assert on the fail-closed log call this between
    cases so the second case still emits.
    """
    _WARNED_SIGNATURES.clear()


def platform_name(env: Optional[Mapping[str, str]] = None) -> PlatformName:
    """Which host are we running on: ``railway``, ``cloud_run`` or ``local``.

    ``PIVOTA_PLATFORM`` overrides detection (useful for a cutover dry-run where
    both variable families are present).
    """
    override = _get(env, "PIVOTA_PLATFORM").lower()
    if override in ("railway", "cloud_run", "cloudrun", "local"):
        return "cloud_run" if override == "cloudrun" else override  # type: ignore[return-value]
    if _on_railway(env):
        return "railway"
    if _on_cloud_run(env):
        return "cloud_run"
    return "local"


def is_deployed(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when this process runs on a managed platform rather than a laptop.

    This is the honest replacement for the old ``bool(os.getenv(
    "RAILWAY_GIT_COMMIT_SHA"))`` idiom, which meant "Railway injected build
    metadata, so I am deployed" and silently became False on Cloud Run.
    """
    return platform_name(env) != "local"


def _resolve_env(env: Optional[Mapping[str, str]] = None) -> tuple:
    """Return ``(resolved, source, raw)`` — the whole decision, for logging.

    ``source`` is one of ``pivota_env`` / ``railway`` / ``cloud_run`` /
    ``fail_closed`` / ``local_default``.
    """
    explicit_raw = _first(env, "PIVOTA_ENV")
    explicit = _normalize(explicit_raw)
    if explicit:
        return explicit, "pivota_env", explicit_raw

    railway_raw = _first(env, "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME")
    railway = _normalize(railway_raw)
    if railway:
        return railway, "railway", railway_raw

    # Cloud Run has no environment variable of its own.  A revision that names
    # its environment in K_CONFIGURATION (e.g. "pivota-backend-staging") is a
    # convention, not a contract, so it is NOT parsed here — guessing prod vs
    # staging from a string is precisely the kind of reasoning this module
    # replaces with a required, explicit setting.

    if _on_cloud_run(env) or _on_railway(env) or explicit_raw:
        # We are demonstrably deployed (or someone set PIVOTA_ENV to a value we
        # do not recognise) and cannot tell which environment this is.
        # FAIL CLOSED to production: guards stay armed, demo paths stay off.
        _warn_once(
            "unresolved:%s:%s:%s" % (explicit_raw, railway_raw, platform_name(env)),
            (
                "platform: environment could NOT be resolved on a managed host "
                "(platform=%s PIVOTA_ENV=%r RAILWAY_ENVIRONMENT=%r K_SERVICE=%r). "
                "FAILING CLOSED to 'production' so guards stay armed. Set "
                "PIVOTA_ENV=production|staging on this revision."
            ),
            platform_name(env),
            explicit_raw,
            railway_raw,
            _get(env, "K_SERVICE") or None,
        )
        return PRODUCTION, "fail_closed", explicit_raw or railway_raw

    return DEVELOPMENT, "local_default", None


def platform_env(env: Optional[Mapping[str, str]] = None) -> PlatformEnv:
    """The resolved environment: production / staging / development / test."""
    return _resolve_env(env)[0]  # type: ignore[return-value]


def is_production(env: Optional[Mapping[str, str]] = None) -> bool:
    return platform_env(env) == PRODUCTION


def pytest_bypass_allowed(
    env: Optional[Mapping[str, str]] = None, *, bypass_name: str = "a test-only bypass"
) -> bool:
    """True only inside an actual pytest run outside production.

    Several call sites short-circuit auth/durability checks whenever
    ``PYTEST_CURRENT_TEST`` is set, to let the unit-test suite run without
    real credentials or a real database. ``PYTEST_CURRENT_TEST`` is only ever
    set by pytest itself, but it is just an environment variable — a debug
    image built from a test stage, a copied ``.env``, or a misconfigured
    smoke harness could still leak it into a real server process. A bypass
    gated on this variable alone would then be live in production.

    Centralizing the check here means the ``not is_production()`` conjunct
    can't be forgotten at a new call site the way it was for the bypasses
    fixed in PR #1893 (see also the demo login lanes fixed in PR #1889).
    """
    if not _get(env, "PYTEST_CURRENT_TEST"):
        return False
    if is_production(env):
        logger.warning(
            "[Platform] PYTEST_CURRENT_TEST is set but the environment "
            "resolves to production; %s stays disabled",
            bypass_name,
        )
        return False
    return True


def is_staging(env: Optional[Mapping[str, str]] = None) -> bool:
    return platform_env(env) == STAGING


def is_development(env: Optional[Mapping[str, str]] = None) -> bool:
    return platform_env(env) in (DEVELOPMENT, TEST)


def raw_environment_label(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The environment string the platform itself published, un-normalised.

    Health/build payloads and persisted audit metadata publish this rather than
    :func:`platform_env` so their bytes are unchanged by this refactor.  Returns
    ``None`` when the platform published nothing (local dev).
    """
    return _first(
        env,
        "PIVOTA_ENV",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_ENVIRONMENT_NAME",
    )


def service_name(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    return _first(env, "PIVOTA_SERVICE_NAME", "RAILWAY_SERVICE_NAME", "K_SERVICE")


def service_id(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    return _first(env, "PIVOTA_SERVICE_ID", "RAILWAY_SERVICE_ID", "K_CONFIGURATION")


def project_id(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    return _first(
        env, "PIVOTA_PROJECT_ID", "RAILWAY_PROJECT_ID", "GOOGLE_CLOUD_PROJECT"
    )


def commit_sha(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Build commit.

    Prefers the sha stamped into the image at build time.  It outranks every env
    var deliberately: the env var is set by whoever deploys, so a deploy that
    forgets it reports the PREVIOUS commit while running the new code — which is
    exactly what happened on 2026-08-23, and it fed the prod-drift alarm a commit
    that was not the one serving traffic.  The stamped file cannot disagree with
    the code it ships beside.

    Cloud Run injects nothing here, so images built without the build arg fall
    back to ``PIVOTA_COMMIT_SHA``.  ``COMMIT_SHA`` (Cloud Build substitution) and
    ``SOURCE_VERSION`` (buildpacks) are accepted as conventional aliases.
    ``GIT_COMMIT_SHA`` / ``VERCEL_GIT_COMMIT_SHA`` are pre-existing fallbacks
    kept so ``/version`` behaves exactly as before.
    """
    baked = _baked_commit_sha()
    if baked:
        return baked
    return _first(
        env,
        "PIVOTA_COMMIT_SHA",
        "RAILWAY_GIT_COMMIT_SHA",
        "COMMIT_SHA",
        "SOURCE_VERSION",
        "GIT_COMMIT_SHA",
        "VERCEL_GIT_COMMIT_SHA",
    )


def deployment_id(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Unique id for this rollout: Railway deployment id, Cloud Run revision."""
    return _first(
        env, "PIVOTA_DEPLOYMENT_ID", "RAILWAY_DEPLOYMENT_ID", "K_REVISION"
    )


def git_branch(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    return _first(env, "PIVOTA_GIT_BRANCH", "RAILWAY_GIT_BRANCH", "GIT_BRANCH")


def platform_metadata(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Everything the shim knows, for health endpoints and startup logs."""
    resolved, source, raw = _resolve_env(env)
    return {
        "platform": platform_name(env),
        "environment": resolved,
        "environment_source": source,
        "environment_raw": raw,
        "deployed": is_deployed(env),
        "service_name": service_name(env),
        "service_id": service_id(env),
        "deployment_id": deployment_id(env),
        "commit_sha": commit_sha(env),
        "git_branch": git_branch(env),
    }


def require_platform_env(env: Optional[Mapping[str, str]] = None) -> PlatformEnv:
    """Assert the environment is knowable.  Call once, at app startup.

    Raises ``RuntimeError`` when we are on a managed platform whose environment
    cannot be resolved — a Cloud Run revision deployed without ``PIVOTA_ENV``.
    Dying at boot is strictly better than serving traffic with every
    prod-vs-staging guard resolved by a fail-closed guess.

    Local development and the test suite (no ``K_SERVICE``, no ``RAILWAY_*``)
    resolve to ``development`` and never raise.
    """
    resolved, source, raw = _resolve_env(env)
    if source == "fail_closed":
        raise RuntimeError(
            "platform: cannot resolve the deployment environment on a managed "
            "host (platform=%s PIVOTA_ENV=%r RAILWAY_ENVIRONMENT=%r). Set "
            "PIVOTA_ENV=production|staging|development on this revision. "
            "Refusing to boot: every prod guard would otherwise run on a "
            "fail-closed guess."
            % (platform_name(env), raw, _get(env, "RAILWAY_ENVIRONMENT") or None)
        )
    return resolved  # type: ignore[return-value]

"""Single transport seam for backend Gemini calls.

Eleven modules build their own Gemini request bodies and POST them with httpx.
Only two things differ between the AI Studio endpoint and Vertex AI: the URL,
and how the request authenticates. This module owns exactly those two things.
Request bodies stay with their callers — several of them encode hard-won
behaviour (see the thinkingConfig comment in ``services/llm_synthesis.py``,
which exists because prod run 370dde30 truncated its output to 9 characters),
and that logic is not this module's to rewrite.

Cutover is a single env flag, not a deploy:

    VERTEX_AI_ENABLED=false   AI Studio endpoint + GEMINI_API_KEY   (default)
    VERTEX_AI_ENABLED=true    Vertex AI endpoint + ADC bearer token

Both paths stay live so the flag can be flipped per environment and rolled back
without a revert — merges to main auto-deploy prod, so eleven LLM call sites
must not hard-cut at merge time.

Credentials come from ``google.auth.default()``, which resolves, in order: an
explicit service-account key, a Workload Identity Federation credential
config, the GCE/Cloud Run metadata server, or a local ``gcloud auth
application-default login`` session. Because the resolution is the library's,
this module does not care which mechanism an environment uses, and moving from
one to another is an env change with no code change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Dict, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

AI_STUDIO_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Vertex requires an OAuth2 access token with cloud-platform scope; unlike the
# AI Studio endpoint it does not accept an API key in a header.
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_credentials = None
_credentials_failed = False
_credentials_lock = threading.Lock()


class VertexAuthError(RuntimeError):
    """No usable Google credentials could be resolved."""


def vertex_enabled() -> bool:
    return bool(getattr(settings, "vertex_ai_enabled", False))


def generate_content_url(model: str, *, base_url: Optional[str] = None) -> str:
    """Endpoint for ``:generateContent`` on ``model``.

    ``base_url`` is honoured only on the AI Studio path, where a handful of
    callers accept an override for tests; on Vertex the URL is fully derived
    from project and location, so an override would be a footgun.
    """
    if not vertex_enabled():
        return f"{base_url or AI_STUDIO_BASE_URL}/models/{model}:generateContent"

    project = settings.vertex_project_id
    location = settings.vertex_location
    if not project:
        raise VertexAuthError(
            "VERTEX_AI_ENABLED=true but GOOGLE_CLOUD_PROJECT is unset"
        )

    # The global endpoint is not regional-prefixed, and generally carries a
    # higher quota than any single region.
    host = (
        "aiplatform.googleapis.com"
        if location == "global"
        else f"{location}-aiplatform.googleapis.com"
    )
    return (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:generateContent"
    )


def _load_credentials():
    """Resolve ADC once per process. Blocking — call via ``_bearer_token``."""
    global _credentials, _credentials_failed
    with _credentials_lock:
        if _credentials is not None:
            return _credentials
        if _credentials_failed:
            raise VertexAuthError("google credentials previously failed to resolve")
        try:
            # Railway (and anything not on GCP) can only hand us env vars, and
            # google.auth.default() reads GOOGLE_APPLICATION_CREDENTIALS as a
            # FILE PATH. Accept the key material inline so a deployment does not
            # need a bootstrap step that writes it to disk.
            raw_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
            if raw_json:
                from google.oauth2 import service_account

                info = json.loads(raw_json)
                creds = service_account.Credentials.from_service_account_info(
                    info, scopes=_SCOPES
                )
                resolved_project = info.get("project_id")
            else:
                import google.auth

                creds, resolved_project = google.auth.default(scopes=_SCOPES)
        except Exception as exc:  # noqa: BLE001 - surfaced as VertexAuthError
            _credentials_failed = True
            raise VertexAuthError(f"could not resolve ADC: {exc}") from exc
        if resolved_project and resolved_project != settings.vertex_project_id:
            # Billing lands on the credential's project, not ours — the whole
            # point of the migration is which project gets charged.
            logger.warning(
                "vertex_gemini: ADC project %r != configured %r; requests bill "
                "the configured project but quota is checked against the "
                "credential's",
                resolved_project,
                settings.vertex_project_id,
            )
        _credentials = creds
        return _credentials


def _bearer_token_blocking() -> str:
    import google.auth.transport.requests

    creds = _load_credentials()
    if not creds.valid:
        try:
            creds.refresh(google.auth.transport.requests.Request())
        except Exception as exc:  # noqa: BLE001
            raise VertexAuthError(f"token refresh failed: {exc}") from exc
    if not creds.token:
        raise VertexAuthError("credentials resolved but carry no access token")
    return creds.token


async def auth_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    """Headers for a Gemini ``:generateContent`` POST.

    ``api_key`` is the caller's own resolved key and is used verbatim on the AI
    Studio path — several callers take it as a function argument rather than
    reading settings, and tests inject it that way, so swallowing it here would
    change behaviour well beyond transport. On the Vertex path it is ignored:
    that endpoint has no API-key auth.

    Raises ``VertexAuthError`` rather than returning unauthenticated headers —
    a 401 from Vertex is far harder to read than a named failure here. Callers
    that degrade gracefully when the LLM is unavailable should gate on
    ``credentials_available()`` first, as they already did on the API key.
    """
    if not vertex_enabled():
        resolved = api_key or settings.gemini_api_key
        if not resolved:
            raise VertexAuthError("GEMINI_API_KEY is not configured")
        return {"Content-Type": "application/json", "x-goog-api-key": resolved}

    # google-auth is synchronous and a refresh is a network round trip, so keep
    # it off the event loop.
    token = await asyncio.get_running_loop().run_in_executor(
        None, _bearer_token_blocking
    )
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def auth_headers_sync(api_key: Optional[str] = None) -> Dict[str, str]:
    """Blocking twin of :func:`auth_headers` for synchronous callers.

    ``services/identity_tier3_judge.py`` posts with the sync httpx client, and
    authenticated on the AI Studio path via ``params={"key": ...}``. Vertex has
    no query-param auth, so that caller moves to headers like everyone else.
    """
    if not vertex_enabled():
        resolved = api_key or settings.gemini_api_key
        if not resolved:
            raise VertexAuthError("GEMINI_API_KEY is not configured")
        return {"Content-Type": "application/json", "x-goog-api-key": resolved}
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_bearer_token_blocking()}",
    }


def credentials_available(api_key: Optional[str] = None) -> bool:
    """Whether a call would authenticate, without making one.

    Replaces the ``if not api_key: return None`` guard the callers already had,
    so their degradation paths keep working once the flag flips and
    GEMINI_API_KEY is no longer the thing that matters.
    """
    if not vertex_enabled():
        return bool(api_key or settings.gemini_api_key)
    try:
        _load_credentials()
        return True
    except VertexAuthError:
        return False


def reset_credentials_cache() -> None:
    """Test hook — drops the memoised credential and its failure latch."""
    global _credentials, _credentials_failed
    with _credentials_lock:
        _credentials = None
        _credentials_failed = False

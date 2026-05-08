"""
Phase D — Google Search Console auto-submit.

Wire-up complete. Module reads gsc_oauth_tokens for per-merchant
credentials, refreshes expired access tokens via Google's token
endpoint, and calls the Indexing API + URL Inspection API for the
two action surfaces:

  - submit_url_to_gsc: POSTs to indexing.googleapis.com/v3/urlNotifications:publish
  - get_index_status:  POSTs to searchconsole.googleapis.com/v1/urlInspection/index:inspect

Both upsert per-URL state into gsc_url_submissions so the audit's
merchant_view.tracking.gsc_submission_status surfaces accurate
counts ({submitted, indexed, pending, errors}).

Disabled by default (settings.gsc_integration_enabled). When the
flag is off, the API functions raise GscNotConfiguredError so
callers (audit pipeline, background jobs) degrade gracefully
without producing fake submission state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
INDEXING_API_PUBLISH_URL = (
    "https://indexing.googleapis.com/v3/urlNotifications:publish"
)
URL_INSPECTION_URL = (
    "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
)


async def is_gsc_integrated(merchant_id: str) -> bool:
    """Returns True iff the merchant has a non-revoked OAuth token
    on file. The audit's integration-state check uses this to decide
    whether to surface the GSC integration action."""
    if not merchant_id or not str(merchant_id).strip():
        return False
    try:
        from db.database import database
        row = await database.fetch_one(
            """
            SELECT 1 FROM gsc_oauth_tokens
             WHERE merchant_id = :merchant_id
               AND revoked_at IS NULL
             LIMIT 1
            """,
            {"merchant_id": merchant_id},
        )
        return row is not None
    except Exception as exc:  # noqa: BLE001
        # Best-effort: if the table doesn't exist yet (migration not
        # applied) or the DB is unreachable, treat as un-integrated.
        # Audit pipeline correctly surfaces "Grant GSC access" instead
        # of a misleading "you're done" state.
        logger.warning(
            "gsc_integration is_gsc_integrated check failed for %s: %s",
            merchant_id, exc,
        )
        return False


async def get_gsc_submission_state(merchant_id: str) -> Dict[str, Any]:
    """Aggregate per-URL submission state for the
    merchant_view.tracking.gsc_submission_status block.

    Returns:
      {
        submitted: int,    # rows with last_status='submitted' or 'pending'
        indexed:   int,    # rows with last_status='indexed'
        pending:   int,    # subset of submitted, still awaiting GSC index
        errors:    int,    # rows with last_status='error'
        last_submission_at: ISO8601 | None,
        last_indexed_at:    ISO8601 | None,
      }
    """
    empty: Dict[str, Any] = {
        "submitted": 0,
        "indexed": 0,
        "pending": 0,
        "errors": 0,
        "last_submission_at": None,
        "last_indexed_at": None,
    }
    if not merchant_id or not str(merchant_id).strip():
        return empty
    try:
        from db.database import database
        rows = await database.fetch_all(
            """
            SELECT last_status, submitted_at, indexed_at
              FROM gsc_url_submissions
             WHERE merchant_id = :merchant_id
            """,
            {"merchant_id": merchant_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "gsc_integration submission_state lookup failed for %s: %s",
            merchant_id, exc,
        )
        return empty

    if not rows:
        return empty

    out = dict(empty)
    submitted_ts: Optional[str] = None
    indexed_ts: Optional[str] = None
    for r in rows:
        status = (r["last_status"] or "").strip().lower()
        if status == "indexed":
            out["indexed"] += 1
        elif status in ("submitted", "pending"):
            out["submitted"] += 1
            if status == "pending":
                out["pending"] += 1
        elif status == "error":
            out["errors"] += 1
        # submission timestamps
        sub_at = r["submitted_at"]
        if sub_at and (submitted_ts is None or str(sub_at) > submitted_ts):
            submitted_ts = str(sub_at)
        idx_at = r["indexed_at"]
        if idx_at and (indexed_ts is None or str(idx_at) > indexed_ts):
            indexed_ts = str(idx_at)
    out["last_submission_at"] = submitted_ts
    out["last_indexed_at"] = indexed_ts
    return out


# ---------------------------------------------------------------
# STUB FUNCTIONS — return "not_configured" until OAuth wire-up lands
# ---------------------------------------------------------------


class GscNotConfiguredError(RuntimeError):
    """Raised when an action requires real GSC API access but the
    OAuth client credentials / token exchange isn't wired up yet.
    Callers should catch + degrade gracefully (e.g., by NOT recording
    a submission attempt that will never succeed)."""


async def submit_url_to_gsc(
    merchant_id: str,
    url: str,
    *,
    audit_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Submit a URL to Google's Indexing API for the merchant's
    authorized site. Upserts gsc_url_submissions with the response.

    Returns: {status: 'submitted' | 'error', message, last_status, last_status_at}
    """
    _require_enabled()
    access_token = await _get_valid_access_token(merchant_id)
    if access_token is None:
        raise GscNotConfiguredError(
            f"No active GSC OAuth token for merchant_id={merchant_id}. "
            f"Merchant must complete /api/gsc/oauth/start first."
        )
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                INDEXING_API_PUBLISH_URL,
                json={"url": url, "type": "URL_UPDATED"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        await _record_url_submission_error(
            merchant_id, url, str(exc), audit_run_id,
        )
        return {
            "status": "error",
            "message": f"Network error: {exc}",
            "last_status": "error",
            "last_status_at": datetime.now(timezone.utc).isoformat(),
        }

    if resp.status_code != 200:
        body = resp.text[:300]
        logger.error(
            "GSC indexing publish failed: merchant=%s url=%s status=%d body=%s",
            merchant_id, url, resp.status_code, body,
        )
        await _record_url_submission_error(
            merchant_id, url, f"http_{resp.status_code}: {body}", audit_run_id,
        )
        return {
            "status": "error",
            "message": f"Google returned {resp.status_code}",
            "last_status": "error",
            "last_status_at": datetime.now(timezone.utc).isoformat(),
        }

    submitted_at = datetime.now(timezone.utc)
    await _upsert_url_submission(
        merchant_id=merchant_id,
        url=url,
        last_status="submitted",
        submitted_at=submitted_at,
        indexed_at=None,
        error_message=None,
        audit_run_id=audit_run_id,
    )
    return {
        "status": "submitted",
        "message": "Submitted to Google Indexing API",
        "last_status": "submitted",
        "last_status_at": submitted_at.isoformat(),
    }


async def get_index_status(
    merchant_id: str,
    url: str,
) -> Dict[str, Any]:
    """Poll URL Inspection API. Updates gsc_url_submissions with the
    indexed-or-not status. Returns {status, last_status, last_status_at}.
    """
    _require_enabled()
    access_token = await _get_valid_access_token(merchant_id)
    if access_token is None:
        raise GscNotConfiguredError(
            f"No active GSC OAuth token for merchant_id={merchant_id}."
        )
    site_url = await _get_authorized_site_url(merchant_id)
    if not site_url:
        raise GscNotConfiguredError(
            "No authorized_site_url on the merchant's OAuth row."
        )
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                URL_INSPECTION_URL,
                json={"inspectionUrl": url, "siteUrl": site_url},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        return {
            "status": "error",
            "last_status": "error",
            "last_status_at": datetime.now(timezone.utc).isoformat(),
            "message": f"Network error: {exc}",
        }
    if resp.status_code != 200:
        return {
            "status": "error",
            "last_status": "error",
            "last_status_at": datetime.now(timezone.utc).isoformat(),
            "message": f"Google returned {resp.status_code}",
        }
    body = resp.json() or {}
    inspection = (body.get("inspectionResult") or {}).get("indexStatusResult") or {}
    verdict = (inspection.get("verdict") or "").upper()
    coverage_state = inspection.get("coverageState") or ""
    # Verdict: PASS = indexed, NEUTRAL/FAIL = not indexed.
    # coverageState examples: "Submitted and indexed", "Discovered – not indexed",
    # "Submitted, currently not indexed".
    indexed = verdict == "PASS"
    indexed_at = datetime.now(timezone.utc) if indexed else None
    last_status = "indexed" if indexed else "pending"
    await _upsert_url_submission(
        merchant_id=merchant_id,
        url=url,
        last_status=last_status,
        submitted_at=None,  # don't overwrite
        indexed_at=indexed_at,
        error_message=None,
        audit_run_id=None,
    )
    return {
        "status": last_status,
        "last_status": last_status,
        "last_status_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "coverage_state": coverage_state,
    }


# Internal helpers — token refresh + DB upserts


def _require_enabled() -> None:
    """Raise GscNotConfiguredError if the feature flag is off OR the
    OAuth client credentials aren't set. Safer than silently hitting
    Google with empty creds."""
    from config.settings import settings
    if not settings.gsc_integration_enabled:
        raise GscNotConfiguredError(
            "GSC integration is feature-flagged off "
            "(GSC_INTEGRATION_ENABLED=false)."
        )
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise GscNotConfiguredError(
            "GOOGLE_OAUTH_CLIENT_ID / CLIENT_SECRET not configured."
        )


async def _get_valid_access_token(merchant_id: str) -> Optional[str]:
    """Fetch the merchant's OAuth tokens; refresh access_token if
    expired. Returns the live access_token, or None if the merchant
    hasn't granted access (or tokens are corrupted)."""
    from db.gsc_tokens import get_oauth_tokens, update_access_token, record_refresh_error
    tokens = await get_oauth_tokens(merchant_id)
    if not tokens or not tokens.get("refresh_token"):
        return None

    expires_at = tokens.get("access_token_expires_at")
    access = tokens.get("access_token")
    now = datetime.now(timezone.utc)
    # Refresh when no access_token, or it expires within 60 seconds.
    if access and expires_at:
        # Normalize naive datetimes to UTC.
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at - timedelta(seconds=60) > now:
            return access

    new_access, new_expires_at, err = await _refresh_access_token(
        tokens["refresh_token"],
    )
    if err is not None:
        await record_refresh_error(merchant_id, err)
        return None
    await update_access_token(
        merchant_id=merchant_id,
        access_token=new_access,
        access_token_expires_at=new_expires_at,
    )
    return new_access


async def _refresh_access_token(
    refresh_token: str,
) -> tuple[Optional[str], Optional[datetime], Optional[str]]:
    """Exchange refresh_token for a fresh access_token via Google's
    token endpoint. Returns (access_token, expires_at, error)."""
    from config.settings import settings
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": settings.google_oauth_client_id,
                    "client_secret": settings.google_oauth_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
    except httpx.HTTPError as exc:
        return (None, None, f"Network error during refresh: {exc}")
    if resp.status_code != 200:
        return (None, None, f"http_{resp.status_code}: {resp.text[:300]}")
    body = resp.json() or {}
    access = body.get("access_token")
    expires_in = int(body.get("expires_in") or 3600)
    if not access:
        return (None, None, "No access_token in refresh response")
    return (
        access,
        datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        None,
    )


async def _get_authorized_site_url(merchant_id: str) -> Optional[str]:
    from db.gsc_tokens import get_oauth_tokens
    tokens = await get_oauth_tokens(merchant_id)
    return (tokens or {}).get("authorized_site_url")


async def _upsert_url_submission(
    *,
    merchant_id: str,
    url: str,
    last_status: str,
    submitted_at: Optional[datetime],
    indexed_at: Optional[datetime],
    error_message: Optional[str],
    audit_run_id: Optional[str],
) -> None:
    from db.database import database
    await database.execute(
        """
        INSERT INTO gsc_url_submissions (
          merchant_id, url, last_status, last_status_at,
          submitted_at, indexed_at, error_message,
          source_audit_run_id
        )
        VALUES (
          :merchant_id, :url, :last_status, NOW(),
          :submitted_at, :indexed_at, :error_message,
          :audit_run_id
        )
        ON CONFLICT (merchant_id, url) DO UPDATE SET
          last_status = EXCLUDED.last_status,
          last_status_at = NOW(),
          submitted_at = COALESCE(EXCLUDED.submitted_at, gsc_url_submissions.submitted_at),
          indexed_at = COALESCE(EXCLUDED.indexed_at, gsc_url_submissions.indexed_at),
          error_message = EXCLUDED.error_message
        """,
        {
            "merchant_id": merchant_id,
            "url": url,
            "last_status": last_status,
            "submitted_at": submitted_at,
            "indexed_at": indexed_at,
            "error_message": error_message,
            "audit_run_id": audit_run_id,
        },
    )


async def _record_url_submission_error(
    merchant_id: str,
    url: str,
    error: str,
    audit_run_id: Optional[str],
) -> None:
    await _upsert_url_submission(
        merchant_id=merchant_id,
        url=url,
        last_status="error",
        submitted_at=None,
        indexed_at=None,
        error_message=error,
        audit_run_id=audit_run_id,
    )


async def submit_audit_canonical_urls(
    *,
    merchant_id: str,
    brand_report: Dict[str, Any],
    audit_run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Walk a brand_report's per_product reports and submit each
    Pivota canonical PDP URL (url_source='pivota_canonical_pdp') to
    GSC's Indexing API. Submissions run in PARALLEL via
    asyncio.gather so total wall time is bounded by the slowest
    Google call (~2s) regardless of how many products.

    Best-effort: any failure is caught + recorded in gsc_url_submissions
    with last_status='error'. Never raises — the audit response
    succeeds even when submissions fail. The audit's
    merchant_view.tracking.gsc_submission_status surfaces the
    aggregate state on the next audit.

    Returns the list of per-URL results for diagnostics + telemetry.
    Empty list when:
      - GSC integration disabled (feature flag off)
      - Merchant has no OAuth tokens (no grant yet)
      - No products audited via Pivota canonical PDP
    """
    import asyncio

    from config.settings import settings
    if not settings.gsc_integration_enabled:
        return []

    # Walk per-product reports, collect canonical URLs.
    urls_to_submit: List[str] = []
    for report in (brand_report or {}).get("per_product") or []:
        if not isinstance(report, dict):
            continue
        url_source = (
            (report.get("merchant_view") or {})
            .get("headline", {})
            .get("url_source")
        )
        if url_source != "pivota_canonical_pdp":
            continue
        url = report.get("merchant_pdp_url")
        if url and isinstance(url, str):
            urls_to_submit.append(url)
    # Dedupe while preserving order
    seen: set = set()
    unique_urls: List[str] = []
    for u in urls_to_submit:
        if u in seen:
            continue
        seen.add(u)
        unique_urls.append(u)
    if not unique_urls:
        return []

    # Verify the merchant has tokens before firing submissions; saves
    # one round-trip per URL when un-integrated.
    if not await is_gsc_integrated(merchant_id):
        return []

    async def _safe_submit(url: str) -> Dict[str, Any]:
        try:
            return await submit_url_to_gsc(
                merchant_id, url, audit_run_id=audit_run_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gsc auto-submit failed for merchant=%s url=%s: %s",
                merchant_id, url, exc,
            )
            return {
                "status": "error",
                "url": url,
                "message": str(exc),
            }

    results = await asyncio.gather(*[_safe_submit(u) for u in unique_urls])
    # Annotate with the URL each result came from for diagnostics.
    for url, result in zip(unique_urls, results):
        if "url" not in result:
            result["url"] = url
    return list(results)


def build_gsc_integration_action(
    *,
    onboarding_url: str = "/onboarding/gsc",
) -> Dict[str, Any]:
    """Build the playbook action surfaced when the merchant hasn't
    granted GSC access yet. Designed to slot in as a SECONDARY
    integration action — emitted only when Phase 0's
    `complete_pivota_onboarding` is already satisfied (store + PSP
    integrated). Otherwise we'd be asking merchants to hand over GSC
    access before they've completed the basic onboarding.

    `lever="gsc_integration"` shares the PITCH_TOKENS carve-out with
    `lever="pivota_integration"` — these are the only actions whose
    body legitimately mentions Pivota's value prop (canonical PDP
    indexing + "we'll auto-submit").
    """
    return {
        "severity": "high",  # not critical; store+PSP onboarding is critical-tier
        "lever": "gsc_integration",
        "title": "Grant Pivota access to your Google Search Console",
        "body": (
            "Pivota canonical PDPs need to land in Google's index "
            "before AI agents can ground answers in them. The fastest "
            "path is to grant Pivota Search Console access — we'll "
            "auto-submit your canonical URLs + monitor indexing "
            "status, so 'submit your sitemap' becomes a one-click "
            "step rather than a recurring task you manage."
        ),
        "concrete_next_step": "Open Search Console authorization",
        "cta_url": onboarding_url,
        "cta_label": "Grant GSC access",
        "evidence": {
            "gsc_integrated": False,
        },
    }

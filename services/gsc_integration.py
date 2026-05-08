"""
Phase D scaffolding — Google Search Console auto-submit.

CURRENT STATUS: scaffolding only. The Google API wire-up (real
google-api-python-client OAuth flow + URL Inspection API calls)
lands in a follow-up PR once OAuth client credentials are configured
in Google Cloud Console + the project has GSC API quota approved.

What this module DOES today:
  - Detect whether a merchant has granted GSC access (presence of
    a gsc_oauth_tokens row)
  - Aggregate per-URL submission state from gsc_url_submissions
  - Stub `submit_url_to_gsc` / `get_index_status` that return a
    "not_configured" sentinel — callers must handle this gracefully

What this module does NOT do yet (follow-up):
  - OAuth flow (consent screen, token exchange, refresh)
  - Real GSC URL Inspection API calls
  - Background job that polls index status periodically

Why scaffold now: the merchant audit pipeline can surface the
"Grant Pivota GSC access" action + render `tracking.gsc_submission_status`
the moment merchants have tokens. Wiring up the actual API calls is
additive — no schema or surface changes needed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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
    """Submit a URL to GSC's URL Inspection API for the merchant's
    authorized site. Stub for now.

    Once wire-up lands:
      1. Look up OAuth refresh token from gsc_oauth_tokens
      2. Exchange for access token (cache until expiry)
      3. POST to GSC URL Inspection API
      4. Upsert gsc_url_submissions with the response
      5. Return {status, message, last_status, last_status_at}
    """
    raise GscNotConfiguredError(
        "GSC submission API is not yet wired up. Phase D scaffolding "
        "covers the data model + integration state + action surface; "
        "the actual google-api-python-client wire-up + OAuth flow "
        "lands in a follow-up PR once Google Cloud Console "
        "credentials are configured."
    )


async def get_index_status(
    merchant_id: str,
    url: str,
) -> Dict[str, Any]:
    """Poll GSC's URL Inspection API for current index status. Stub."""
    raise GscNotConfiguredError(
        "GSC index status API is not yet wired up. See submit_url_to_gsc "
        "for the wire-up plan."
    )


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

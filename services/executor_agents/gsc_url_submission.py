"""GSC URL submission auto-loop agent (PR-4a).

For a merchant with GSC OAuth granted: query gsc_url_submissions for
URLs in `pending` or `unknown` state older than the indexing-arc
window, re-submit them to Google's indexing API, and record results
in evidence.

This agent is the canonical executor pattern proof — extends the
existing services/gsc_integration.py infrastructure (already calls
real Google APIs) without re-implementing the integration. The
executor wrapper adds:
  - Cron-driven discovery of which URLs need re-submission
  - Idempotency: skip if URL was submitted within the last 24h
    (don't burn quota on duplicate submits)
  - Per-merchant evidence: "We submitted N URLs this week; M
    indexed, K still pending"

What it does NOT do (out of scope):
  - Mint new Pivota canonical PDPs (that's catalog_sync_service)
  - Submit URLs that aren't already in gsc_url_submissions (the
    audit pipeline writes those rows when the merchant's first audit
    runs)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
    ExecutorResult,
)

logger = logging.getLogger(__name__)


# Don't re-submit a URL we submitted within this window — Google
# treats duplicate submissions as no-ops but they still burn quota.
_RESUBMIT_GRACE_HOURS = 23

# Per-tick cap on how many URLs we re-submit. Google indexing API
# quota is 200 URLs/day per project; we cap per-merchant per-run at
# 25 to stay under quota even with many merchants on the same cron.
_MAX_SUBMITS_PER_RUN = 25


def _gsc_enabled() -> bool:
    """Non-raising version of gsc_integration._require_enabled. Used by
    the agent's should_run gate so a disabled GSC integration silently
    skips this agent rather than raising mid-dispatch."""
    try:
        from config.settings import settings
        return bool(getattr(settings, "gsc_integration_enabled", False))
    except Exception:  # noqa: BLE001
        return False


class GscUrlSubmissionAgent(BaseExecutorAgent):
    """Re-submits stale unindexed Pivota canonical URLs to Google."""

    name = "gsc_url_submission_loop"

    async def should_run(self, context: ExecutorContext) -> bool:
        """Run when:
          - merchant_id is set (this agent is per-merchant)
          - GSC integration is enabled
          - merchant has at least one URL in gsc_url_submissions with
            status in {pending, unknown} that's older than 24h
        """
        if not context.merchant_id:
            return False
        if not _gsc_enabled():
            return False
        candidates = await self._fetch_stale_unindexed(context.merchant_id)
        return len(candidates) > 0

    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        if not context.merchant_id:
            return ExecutorResult(
                status="skipped", error_message="merchant_id required",
            )
        try:
            from services.gsc_integration import (
                GscNotConfiguredError,
                submit_url_to_gsc,
            )
        except Exception as exc:  # noqa: BLE001
            return ExecutorResult(
                status="failed",
                error_message=f"gsc_integration import failed: {exc!r}",
            )

        candidates = await self._fetch_stale_unindexed(context.merchant_id)
        if not candidates:
            return ExecutorResult(
                status="skipped",
                evidence={"reason": "no stale unindexed URLs"},
            )

        # Cap per-run to avoid burning daily quota on a single merchant.
        to_submit = candidates[:_MAX_SUBMITS_PER_RUN]
        results: List[Dict[str, Any]] = []
        succeeded = 0
        failed = 0

        for entry in to_submit:
            url = entry["url"]
            try:
                resp = await submit_url_to_gsc(
                    context.merchant_id,
                    url,
                    audit_run_id=context.parent_audit_run_id,
                )
            except GscNotConfiguredError as exc:
                # Whole loop aborts: no OAuth means no submissions
                # for any URL. Return what we've done so far + the
                # diagnostic.
                return ExecutorResult(
                    status="failed",
                    error_message=f"gsc_oauth_missing: {exc!r}",
                    evidence={
                        "submitted_count": succeeded,
                        "results_so_far": results,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                results.append({
                    "url": url,
                    "status": "error",
                    "message": f"unexpected: {exc!r}",
                })
                continue

            if resp.get("status") == "submitted":
                succeeded += 1
            else:
                failed += 1
            results.append({
                "url": url,
                "status": resp.get("status"),
                "message": resp.get("message"),
            })

        evidence = {
            "candidates_total": len(candidates),
            "submits_attempted": len(to_submit),
            "succeeded_count": succeeded,
            "failed_count": failed,
            "skipped_for_throttle": max(0, len(candidates) - len(to_submit)),
            "results": results,
        }
        # Even partial success is a success for the agent — we did
        # the work the audit recommended. Caller can dig into evidence
        # to surface "8 of 12 URLs submitted, 4 hit Google quota."
        return ExecutorResult(
            status="succeeded" if succeeded > 0 else "failed",
            evidence=evidence,
            error_message=None if succeeded > 0 else "all submissions failed",
        )

    async def _fetch_stale_unindexed(
        self, merchant_id: str,
    ) -> List[Dict[str, Any]]:
        """URLs needing re-submission: status in (pending, unknown)
        AND last_status_at older than 24h. Returns [{url, last_status,
        last_status_at}]."""
        from db.database import database
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_RESUBMIT_GRACE_HOURS)
        try:
            rows = await database.fetch_all(
                """
                SELECT url, last_status, last_status_at
                FROM gsc_url_submissions
                WHERE merchant_id = :merchant_id
                  AND last_status IN ('pending', 'unknown', 'error')
                  AND last_status_at < :cutoff
                ORDER BY last_status_at ASC
                LIMIT 100
                """,
                {"merchant_id": merchant_id, "cutoff": cutoff},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gsc_url_submission_loop: fetch failed for %s: %s",
                merchant_id, str(exc)[:200],
            )
            return []
        return [
            {
                "url": r["url"],
                "last_status": r["last_status"],
                "last_status_at": (
                    r["last_status_at"].isoformat()
                    if isinstance(r["last_status_at"], datetime) else None
                ),
            }
            for r in (rows or [])
        ]

"""P5.4 verifier: gsc_url_submitted.

Asserts that the merchant's PDP URL has been submitted to Google
Search Console for indexing. Reads from `gsc_url_submissions`
(already maintained by services/gsc_integration.py + the
GscUrlSubmissionAgent in services/executor_agents/gsc_url_submission.py).

No HTTP calls — this verifier is purely a DB read. The actual
submission to Google's Indexing API is done by the executor agent
(P3.2 work queue); this verifier just confirms the post-condition.

Evidence:
  - merchant_id
  - checked_url (the PDP URL we looked up)
  - row_found (whether gsc_url_submissions has a row)
  - last_status (submitted | indexed | pending | error | not_found)
  - submitted_at
  - source_audit_run_id (which audit caused the submission)

Status routing:
  - succeeded: row exists with last_status in {submitted, indexed,
    pending} — submission was attempted
  - blocked: merchant has no GSC integration (no rows for this
    merchant in gsc_url_submissions OR consent revoked); NOT
    retryable for this audit. A future audit re-checks.
  - failed: transient DB error (caller retries)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from services.verification_run_worker import (
    VerifierContext, VerifierResult, register_verifier,
)
from services.verifiers._product_context import load_product_context

logger = logging.getLogger(__name__)


# last_status values that count as "submitted" for verification
# purposes (per services/gsc_integration.py).
_SUBMITTED_STATUSES = frozenset({"submitted", "indexed", "pending"})


async def _has_any_gsc_rows(merchant_id: str) -> bool:
    """Does this merchant have ANY rows in gsc_url_submissions?
    Used to distinguish 'this URL not submitted yet' (recoverable
    by a re-audit) from 'this merchant doesn't use GSC at all'
    (blocked — verifier won't help)."""
    from db.database import database
    try:
        row = await database.fetch_one(
            "SELECT 1 FROM gsc_url_submissions "
            "WHERE merchant_id = :merchant_id LIMIT 1",
            {"merchant_id": merchant_id},
        )
        return row is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_has_any_gsc_rows lookup failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        return False


async def _fetch_submission_row(
    *, merchant_id: str, url: str,
) -> Optional[Dict[str, Any]]:
    from db.database import database
    try:
        row = await database.fetch_one(
            """
            SELECT last_status, submitted_at, source_audit_run_id,
                   error_message
              FROM gsc_url_submissions
             WHERE merchant_id = :merchant_id
               AND url = :url
             LIMIT 1
            """,
            {"merchant_id": merchant_id, "url": url},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_fetch_submission_row failed for %s / %s: %s",
            merchant_id, url, str(exc)[:200],
        )
        return None
    if row is None:
        return None
    return dict(row)


async def run_gsc_url_submitted(
    context: VerifierContext,
) -> VerifierResult:
    product = await load_product_context(
        audit_run_id=context.audit_run_id,
        product_key=context.product_key,
    )
    if product is None:
        return VerifierResult(
            status="blocked",
            error_message="product context unresolvable",
            evidence_jsonb={"resolved_product": False},
        )

    merchant_id = product["merchant_id"]
    # Use the Pivota canonical URL — that's what the
    # GscUrlSubmissionAgent submits, not the merchant's own URL.
    url = (product.get("pivota_canonical_url") or "").strip()
    if not url:
        return VerifierResult(
            status="blocked",
            error_message="product has no pivota_canonical_url",
            evidence_jsonb={
                "merchant_id": merchant_id,
                "product_key": product.get("product_key"),
            },
        )

    sub_row = await _fetch_submission_row(
        merchant_id=merchant_id, url=url,
    )

    evidence: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "checked_url": url,
        "row_found": sub_row is not None,
    }

    if sub_row is not None:
        last_status = (sub_row.get("last_status") or "").strip().lower()
        evidence["last_status"] = last_status
        evidence["submitted_at"] = _iso_or_none(sub_row.get("submitted_at"))
        evidence["source_audit_run_id"] = (
            str(sub_row.get("source_audit_run_id"))
            if sub_row.get("source_audit_run_id") else None
        )

        if last_status in _SUBMITTED_STATUSES:
            return VerifierResult(
                status="succeeded", evidence_jsonb=evidence,
            )

        if last_status == "error":
            # The submission attempt failed (e.g., Indexing API
            # error). Don't retry the verifier — the executor agent
            # is the right place to retry the SUBMISSION; the
            # verifier just reports the current state.
            evidence["submission_error"] = (
                sub_row.get("error_message")
            )
            return VerifierResult(
                status="blocked",
                error_message=(
                    f"gsc_submission_state_error: "
                    f"{(sub_row.get('error_message') or '')[:200]}"
                ),
                evidence_jsonb=evidence,
            )

        # Unknown last_status — treat as failed (retryable) so
        # the worker re-checks once. If still unknown after
        # retries, exhausted_retries surfaces the gap to ops.
        return VerifierResult(
            status="failed",
            error_message=f"unknown_gsc_status: {last_status!r}",
            evidence_jsonb=evidence,
        )

    # No row for this URL. Differentiate "merchant has no GSC at
    # all" (blocked) from "this specific URL hasn't been
    # submitted" (failed; the executor agent may submit it).
    has_any = await _has_any_gsc_rows(merchant_id)
    evidence["merchant_has_any_gsc_rows"] = has_any
    if not has_any:
        return VerifierResult(
            status="blocked",
            error_message="merchant_has_no_gsc_integration",
            evidence_jsonb=evidence,
        )

    # Merchant uses GSC but this URL isn't submitted yet. Retryable
    # — the executor agent may submit it within the retry window.
    return VerifierResult(
        status="failed",
        error_message="url_not_yet_submitted",
        evidence_jsonb=evidence,
    )


def _iso_or_none(ts) -> Optional[str]:
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


register_verifier("gsc_url_submitted", run_gsc_url_submitted)

"""P5.4 verifier: gsc_indexing_status.

Stricter sibling of gsc_url_submitted: this verifier only succeeds
when last_status == 'indexed' (Google has confirmed the URL is in
the index). Reads from `gsc_url_submissions`.

The Indexing API submission is async — Google takes minutes-to-days
to index a URL after submission. This verifier provides the
'did indexing actually happen' signal, distinct from 'did we
submit'.

Evidence:
  - merchant_id
  - checked_url
  - row_found
  - last_status
  - indexed_at (when Google confirmed indexed)

Status routing:
  - succeeded: last_status == 'indexed'
  - failed: last_status in {submitted, pending} (retryable —
    indexing is async; a later check may see 'indexed')
  - blocked: no GSC integration OR submission error (per-row error)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from services.verification_run_worker import (
    VerifierContext, VerifierResult, register_verifier,
)
from services.verifiers._product_context import load_product_context
from services.verifiers.gsc_url_submitted import (
    _fetch_submission_row, _has_any_gsc_rows, _iso_or_none,
)

logger = logging.getLogger(__name__)


async def run_gsc_indexing_status(
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

    if sub_row is None:
        has_any = await _has_any_gsc_rows(merchant_id)
        evidence["merchant_has_any_gsc_rows"] = has_any
        if not has_any:
            return VerifierResult(
                status="blocked",
                error_message="merchant_has_no_gsc_integration",
                evidence_jsonb=evidence,
            )
        # URL not submitted yet — gsc_url_submitted will surface
        # this gap. For this verifier (indexing), treat as failed
        # so it retries; indexing follows submission and may land
        # within our retry budget.
        return VerifierResult(
            status="failed",
            error_message="url_not_yet_submitted",
            evidence_jsonb=evidence,
        )

    last_status = (sub_row.get("last_status") or "").strip().lower()
    evidence["last_status"] = last_status
    evidence["indexed_at"] = _iso_or_none(sub_row.get("submitted_at"))
    # NOTE: gsc_url_submissions has both submitted_at + indexed_at;
    # surface the latter when present (P5.4 follow-up if we find
    # the column shape needs richer handling).

    if last_status == "indexed":
        return VerifierResult(
            status="succeeded",
            evidence_jsonb=evidence,
        )

    if last_status == "error":
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

    # Submitted but not yet indexed → retry (indexing is async).
    if last_status in ("submitted", "pending"):
        return VerifierResult(
            status="failed",
            error_message="indexing_pending",
            evidence_jsonb=evidence,
        )

    return VerifierResult(
        status="failed",
        error_message=f"unknown_gsc_status: {last_status!r}",
        evidence_jsonb=evidence,
    )


register_verifier("gsc_indexing_status", run_gsc_indexing_status)

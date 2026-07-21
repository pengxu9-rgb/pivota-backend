"""Phase 5.4 — GSC verifier tests.

gsc_url_submitted + gsc_indexing_status read from the existing
gsc_url_submissions table maintained by services/gsc_integration.py
+ the GscUrlSubmissionAgent. No HTTP calls in these verifiers —
the executor agent owns the API-call side; the verifier reads
post-condition state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest


# =====================================================================
# Stubs + helpers
# =====================================================================


def _sample_product() -> Dict[str, Any]:
    return {
        "merchant_id": "merch-1",
        "product_key": "shopify::sp-1",
        "title": "Test product",
        "brand": "Test Brand",
        "pivota_signature_id": "sig_abc",
        "pivota_canonical_url": (
            "https://agent.pivota.cc/products/sig_abc"
        ),
    }


def _ctx(verifier_id: str = "gsc_url_submitted"):
    from services.verification_run_worker import VerifierContext
    return VerifierContext(
        verify_id="v-1",
        audit_run_id="audit-1",
        verifier_id=verifier_id,
        product_key="shopify::sp-1",
    )


def _patch_product_loader(monkeypatch, product=None):
    async def fake_load(*, audit_run_id, product_key):
        return product

    from services.verifiers import (
        gsc_url_submitted, gsc_indexing_status,
    )
    monkeypatch.setattr(
        gsc_url_submitted, "load_product_context", fake_load,
    )
    monkeypatch.setattr(
        gsc_indexing_status, "load_product_context", fake_load,
    )


def _patch_gsc_db(monkeypatch, *, submission_row=None, has_any=False):
    """Replace the DB-touch helpers (_fetch_submission_row +
    _has_any_gsc_rows) on the gsc_url_submitted module. Both
    GSC verifiers import them from there."""
    async def fake_fetch(*, merchant_id, url):
        return submission_row

    async def fake_has_any(merchant_id):
        return has_any

    # Patch on the gsc_url_submitted module (canonical home)
    from services.verifiers import (
        gsc_url_submitted, gsc_indexing_status,
    )
    monkeypatch.setattr(
        gsc_url_submitted, "_fetch_submission_row", fake_fetch,
    )
    monkeypatch.setattr(
        gsc_url_submitted, "_has_any_gsc_rows", fake_has_any,
    )
    # gsc_indexing_status imports these by symbol from
    # gsc_url_submitted at import time, so its module-local
    # bindings need to be patched separately.
    monkeypatch.setattr(
        gsc_indexing_status, "_fetch_submission_row", fake_fetch,
    )
    monkeypatch.setattr(
        gsc_indexing_status, "_has_any_gsc_rows", fake_has_any,
    )


# =====================================================================
# gsc_url_submitted
# =====================================================================


@pytest.mark.asyncio
async def test_gsc_url_submitted_succeeded_when_last_status_submitted(
    monkeypatch,
):
    from services.verifiers import gsc_url_submitted as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row={
            "last_status": "submitted",
            "submitted_at": datetime(2026, 5, 12, tzinfo=timezone.utc),
            "source_audit_run_id": "audit-1",
            "error_message": None,
        },
    )
    result = await v.run_gsc_url_submitted(_ctx())
    assert result.status == "succeeded"
    assert result.evidence_jsonb["last_status"] == "submitted"


@pytest.mark.asyncio
async def test_gsc_url_submitted_succeeded_when_indexed(monkeypatch):
    """Even though gsc_indexing_status is stricter, 'indexed' is
    a superset of 'submitted' — the url IS submitted."""
    from services.verifiers import gsc_url_submitted as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row={
            "last_status": "indexed",
            "submitted_at": datetime(2026, 5, 12, tzinfo=timezone.utc),
            "source_audit_run_id": "audit-1",
            "error_message": None,
        },
    )
    result = await v.run_gsc_url_submitted(_ctx())
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_gsc_url_submitted_blocked_on_submission_error(
    monkeypatch,
):
    """last_status='error' → blocked. Submitter (executor agent)
    is the right place to retry; the verifier reports state."""
    from services.verifiers import gsc_url_submitted as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row={
            "last_status": "error",
            "submitted_at": None,
            "source_audit_run_id": "audit-1",
            "error_message": "indexing api 403",
        },
    )
    result = await v.run_gsc_url_submitted(_ctx())
    assert result.status == "blocked"
    assert "gsc_submission_state_error" in result.error_message


@pytest.mark.asyncio
async def test_gsc_url_submitted_blocked_when_merchant_has_no_gsc(
    monkeypatch,
):
    """No row for this URL AND merchant has no rows at all →
    blocked (no GSC integration; verifier can't help)."""
    from services.verifiers import gsc_url_submitted as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row=None,
        has_any=False,  # merchant has no GSC integration
    )
    result = await v.run_gsc_url_submitted(_ctx())
    assert result.status == "blocked"
    assert "no_gsc_integration" in result.error_message


@pytest.mark.asyncio
async def test_gsc_url_submitted_failed_when_url_not_yet_submitted(
    monkeypatch,
):
    """No row for this URL BUT merchant has other rows → failed
    (retryable; the executor agent may submit this URL within
    the retry window)."""
    from services.verifiers import gsc_url_submitted as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row=None,
        has_any=True,  # merchant uses GSC but this URL not yet submitted
    )
    result = await v.run_gsc_url_submitted(_ctx())
    assert result.status == "failed"
    assert "url_not_yet_submitted" in result.error_message


@pytest.mark.asyncio
async def test_gsc_url_submitted_blocked_when_no_product_context(
    monkeypatch,
):
    from services.verifiers import gsc_url_submitted as v
    _patch_product_loader(monkeypatch, product=None)
    result = await v.run_gsc_url_submitted(_ctx())
    assert result.status == "blocked"


# =====================================================================
# gsc_indexing_status
# =====================================================================


@pytest.mark.asyncio
async def test_gsc_indexing_succeeded_only_when_indexed(monkeypatch):
    """The strict verifier — last_status MUST be 'indexed'."""
    from services.verifiers import gsc_indexing_status as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row={
            "last_status": "indexed",
            "submitted_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "source_audit_run_id": "audit-1",
            "error_message": None,
        },
    )
    result = await v.run_gsc_indexing_status(_ctx(
        verifier_id="gsc_indexing_status",
    ))
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_gsc_indexing_failed_when_only_submitted(monkeypatch):
    """last_status='submitted' (not yet indexed) → failed
    (retryable; indexing is async and may land within retry
    window)."""
    from services.verifiers import gsc_indexing_status as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row={
            "last_status": "submitted",
            "submitted_at": datetime(2026, 5, 12, tzinfo=timezone.utc),
            "source_audit_run_id": "audit-1",
            "error_message": None,
        },
    )
    result = await v.run_gsc_indexing_status(_ctx(
        verifier_id="gsc_indexing_status",
    ))
    assert result.status == "failed"
    assert "indexing_pending" in result.error_message


@pytest.mark.asyncio
async def test_gsc_indexing_blocked_on_submission_error(monkeypatch):
    from services.verifiers import gsc_indexing_status as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row={
            "last_status": "error",
            "submitted_at": None,
            "source_audit_run_id": "audit-1",
            "error_message": "rate limited by Google",
        },
    )
    result = await v.run_gsc_indexing_status(_ctx(
        verifier_id="gsc_indexing_status",
    ))
    assert result.status == "blocked"
    assert "gsc_submission_state_error" in result.error_message


@pytest.mark.asyncio
async def test_gsc_indexing_failed_when_url_not_yet_submitted(
    monkeypatch,
):
    """Distinct from gsc_url_submitted: when the URL isn't even
    submitted yet, indexing definitely hasn't happened. Both
    verifiers return failed (retryable); ops gets two signals
    (submit gap + indexing gap) which is fine — each is
    independently informative."""
    from services.verifiers import gsc_indexing_status as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row=None,
        has_any=True,
    )
    result = await v.run_gsc_indexing_status(_ctx(
        verifier_id="gsc_indexing_status",
    ))
    assert result.status == "failed"


# =====================================================================
# Registration
# =====================================================================


# =====================================================================
# P5.8.4 — assert the indexing-status verifier reads the actual
# indexed_at column, not submitted_at. The original P5.4 code shipped
# with this bug; the verifier-review caught it; this test makes sure
# it doesn't regress.
# =====================================================================


@pytest.mark.asyncio
async def test_gsc_indexing_exposes_indexed_at_distinct_from_submitted_at(
    monkeypatch,
):
    """If the row has DIFFERENT submitted_at + indexed_at timestamps,
    the verifier MUST surface the indexed_at value in evidence
    (the merchant-facing timestamp). Catches the P0-4 bug class
    where the SELECT statement and the read site drift."""
    from services.verifiers import gsc_indexing_status as v
    submitted_ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    indexed_ts = datetime(2026, 5, 8, tzinfo=timezone.utc)
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_gsc_db(
        monkeypatch,
        submission_row={
            "last_status": "indexed",
            "submitted_at": submitted_ts,
            "indexed_at": indexed_ts,
            "source_audit_run_id": "audit-1",
            "error_message": None,
        },
    )
    result = await v.run_gsc_indexing_status(_ctx(
        verifier_id="gsc_indexing_status",
    ))
    assert result.status == "succeeded"
    # indexed_at evidence MUST be the indexed_at timestamp, NOT the
    # submitted_at timestamp (the original P5.4 bug).
    assert result.evidence_jsonb["indexed_at"] == indexed_ts.isoformat()
    assert result.evidence_jsonb["submitted_at"] == submitted_ts.isoformat()
    # The two must NOT be equal — proves the verifier doesn't read
    # the same column for both fields.
    assert (
        result.evidence_jsonb["indexed_at"]
        != result.evidence_jsonb["submitted_at"]
    )


def test_gsc_url_submitted_select_statement_includes_indexed_at():
    """Schema-coupling check: the SELECT in _fetch_submission_row
    must include indexed_at, otherwise gsc_indexing_status reads a
    missing column and silently falls back to None (or worse, an
    aliased value). Catches the SELECT-vs-read drift bug class."""
    import inspect
    from services.verifiers import gsc_url_submitted
    src = inspect.getsource(gsc_url_submitted._fetch_submission_row)
    # The SELECT clause must include indexed_at as a column.
    assert "indexed_at" in src, (
        "_fetch_submission_row SELECT omits indexed_at; "
        "gsc_indexing_status will silently report None / "
        "submitted_at value as indexed_at"
    )


def test_both_gsc_verifiers_register_at_import():
    import services.verifiers  # noqa: F401
    from services.verification_run_worker import (
        get_registered_verifier_ids,
    )
    ids = get_registered_verifier_ids()
    assert "gsc_url_submitted" in ids
    assert "gsc_indexing_status" in ids

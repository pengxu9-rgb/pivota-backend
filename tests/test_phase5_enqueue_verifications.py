"""Phase 5.7 — verification-enqueue tests.

Validates enqueue_verifications_for_completed_audit: correct row
count per (product × verifier), correct not_before scheduling
for citation_movement, idempotency dedupe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest


# =====================================================================
# Stubs
# =====================================================================


class _EnqueueAccessors:
    def __init__(self):
        self.enqueued: List[Dict[str, Any]] = []
        # Per-idempotency-key → returns this run_id (simulating
        # existing in-flight row); default None
        self.idem_returns: Dict[str, Optional[str]] = {}
        # Configurable: which verifier_ids fail enqueue
        self.fail_for: set = set()

    async def find_in_flight_by_idem(self, *, idempotency_key):
        return self.idem_returns.get(idempotency_key)

    async def enqueue(self, **kwargs):
        if kwargs.get("verifier_id") in self.fail_for:
            return None
        self.enqueued.append(kwargs)
        return f"v-{len(self.enqueued)}"


def _patch(monkeypatch):
    """Patch the audit_evidence accessors. Returns the accessors
    stub for assertion."""
    accessors = _EnqueueAccessors()
    import db.audit_evidence as ae
    monkeypatch.setattr(
        ae, "find_in_flight_verification_by_idempotency",
        accessors.find_in_flight_by_idem,
    )
    monkeypatch.setattr(
        ae, "enqueue_verification_run", accessors.enqueue,
    )
    return accessors


# =====================================================================
# Tests
# =====================================================================


@pytest.mark.asyncio
async def test_enqueues_seven_rows_per_product(monkeypatch):
    """Each product gets one row per verifier. With 2 products
    and 7 verifiers, expect 14 rows."""
    from services.audit_verification_enqueuer import (
        enqueue_verifications_for_completed_audit,
    )
    accessors = _patch(monkeypatch)
    completed_at = datetime(2026, 5, 12, tzinfo=timezone.utc)
    summary = await enqueue_verifications_for_completed_audit(
        audit_run_id="audit-1",
        merchant_id="merch-1",
        product_keys=["pk-1", "pk-2"],
        completed_at=completed_at,
    )
    assert summary["enqueued"] == 14  # 7 verifiers × 2 products
    assert summary["deduped"] == 0
    assert summary["failed"] == 0
    # Verifier set
    by_verifier_id = {}
    for row in accessors.enqueued:
        by_verifier_id.setdefault(row["verifier_id"], []).append(row)
    assert sorted(by_verifier_id.keys()) == [
        "frontend_agent_cite", "gsc_indexing_status",
        "gsc_url_submitted", "pdp_in_sitemap", "pdp_renders",
        "pivota_internal_retrieval", "public_llm_citation_movement",
    ]
    # Each verifier got 2 rows (one per product)
    for v, rows in by_verifier_id.items():
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_citation_movement_gets_30day_not_before(monkeypatch):
    """The public_llm_citation_movement verifier is the ONLY one
    that should be scheduled with not_before set."""
    from services.audit_verification_enqueuer import (
        enqueue_verifications_for_completed_audit,
    )
    accessors = _patch(monkeypatch)
    completed_at = datetime(2026, 5, 12, tzinfo=timezone.utc)
    await enqueue_verifications_for_completed_audit(
        audit_run_id="audit-1",
        merchant_id="merch-1",
        product_keys=["pk-1"],
        completed_at=completed_at,
    )
    # Each row: confirm not_before is None for everyone except
    # public_llm_citation_movement, which gets +30 days
    for row in accessors.enqueued:
        if row["verifier_id"] == "public_llm_citation_movement":
            expected = completed_at + timedelta(days=30)
            assert row["not_before"] == expected
        else:
            assert row["not_before"] is None


@pytest.mark.asyncio
async def test_idempotency_dedupes_existing_in_flight(monkeypatch):
    """If an idempotency_key already maps to an in-flight row,
    that verifier+product is counted as deduped, not enqueued."""
    from services.audit_verification_enqueuer import (
        enqueue_verifications_for_completed_audit,
    )
    from db.audit_evidence import compute_verification_idempotency_key
    accessors = _patch(monkeypatch)
    # Pre-seed: pdp_renders for pk-1 already in-flight
    key = compute_verification_idempotency_key(
        audit_run_id="audit-1",
        verifier_id="pdp_renders",
        product_key="pk-1",
    )
    accessors.idem_returns[key] = "existing-v-1"

    summary = await enqueue_verifications_for_completed_audit(
        audit_run_id="audit-1",
        merchant_id="merch-1",
        product_keys=["pk-1"],
        completed_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    # 7 verifiers × 1 product = 7 expected enqueues. One is
    # deduped → 6 enqueued + 1 deduped.
    assert summary["enqueued"] == 6
    assert summary["deduped"] == 1
    assert summary["failed"] == 0
    # Confirm the deduped one wasn't passed to enqueue
    verifier_ids = [r["verifier_id"] for r in accessors.enqueued]
    assert verifier_ids.count("pdp_renders") == 0


@pytest.mark.asyncio
async def test_enqueue_failure_counted_per_verifier(monkeypatch):
    """If enqueue_verification_run returns None for one verifier,
    that's counted in failed; rest proceed."""
    from services.audit_verification_enqueuer import (
        enqueue_verifications_for_completed_audit,
    )
    accessors = _patch(monkeypatch)
    accessors.fail_for = {"gsc_url_submitted"}
    summary = await enqueue_verifications_for_completed_audit(
        audit_run_id="audit-1",
        merchant_id="merch-1",
        product_keys=["pk-1"],
        completed_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    assert summary["enqueued"] == 6
    assert summary["failed"] == 1


@pytest.mark.asyncio
async def test_short_circuit_on_missing_audit_run_id(monkeypatch):
    """Missing audit_run_id → returns error stub without enqueueing."""
    from services.audit_verification_enqueuer import (
        enqueue_verifications_for_completed_audit,
    )
    accessors = _patch(monkeypatch)
    summary = await enqueue_verifications_for_completed_audit(
        audit_run_id="",
        merchant_id="merch-1",
        product_keys=["pk-1"],
    )
    assert summary["enqueued"] == 0
    assert summary["failed"] == 0
    assert "error" in summary
    assert accessors.enqueued == []


@pytest.mark.asyncio
async def test_empty_product_keys_produces_no_enqueues(monkeypatch):
    """All current verifiers are per_product. Empty product_keys
    → no enqueues + summary all zeros (not an error)."""
    from services.audit_verification_enqueuer import (
        enqueue_verifications_for_completed_audit,
    )
    accessors = _patch(monkeypatch)
    summary = await enqueue_verifications_for_completed_audit(
        audit_run_id="audit-1",
        merchant_id="merch-1",
        product_keys=[],
        completed_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    assert summary["enqueued"] == 0
    assert summary["deduped"] == 0
    assert summary["failed"] == 0
    assert accessors.enqueued == []


@pytest.mark.asyncio
async def test_idempotency_key_distinguishes_product(monkeypatch):
    """The idempotency key includes product_key — re-enqueueing for
    DIFFERENT products of the same audit should NOT dedupe."""
    from services.audit_verification_enqueuer import (
        enqueue_verifications_for_completed_audit,
    )
    from db.audit_evidence import compute_verification_idempotency_key
    accessors = _patch(monkeypatch)
    # Pre-seed only pk-1's pdp_renders
    key1 = compute_verification_idempotency_key(
        audit_run_id="audit-1",
        verifier_id="pdp_renders",
        product_key="pk-1",
    )
    accessors.idem_returns[key1] = "existing-pk1-v-1"

    summary = await enqueue_verifications_for_completed_audit(
        audit_run_id="audit-1",
        merchant_id="merch-1",
        product_keys=["pk-1", "pk-2"],
        completed_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    # 7 verifiers × 2 products = 14 expected enqueues. ONE
    # deduped (pdp_renders × pk-1). So 13 enqueued + 1 deduped.
    assert summary["enqueued"] == 13
    assert summary["deduped"] == 1
    # Confirm pdp_renders for pk-2 still enqueued
    pdp_renders_rows = [
        r for r in accessors.enqueued
        if r["verifier_id"] == "pdp_renders"
    ]
    assert len(pdp_renders_rows) == 1
    assert pdp_renders_rows[0]["product_key"] == "pk-2"


def test_verifier_specs_count_matches_registered_count():
    """The 7 verifier_ids in VERIFIER_SPECS must match the 7
    registered verifiers. Drift between these lists would mean
    we enqueue verifier_ids the worker can't run, OR fail to
    enqueue ones that exist."""
    import services.verifiers  # noqa: F401 — register side-effect
    from services.audit_verification_enqueuer import VERIFIER_SPECS
    from services.verification_run_worker import (
        get_registered_verifier_ids,
    )
    spec_ids = {s["id"] for s in VERIFIER_SPECS}
    registered = get_registered_verifier_ids()
    assert spec_ids == registered, (
        f"drift: in specs but not registered={spec_ids - registered} ; "
        f"registered but not in specs={registered - spec_ids}"
    )

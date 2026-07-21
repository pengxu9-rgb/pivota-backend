"""Persistent-workspace reconciliation (page-usability Step 1).

When a new audit completes, the action plan should reflect CURRENT priorities:
prior-run pending tasks the latest audit no longer surfaces get closed — but
SCOPE-AWARE, so an audit of SKU-B never closes SKU-A's still-valid tasks, and
in-progress / standing tasks are never touched.
"""

import pytest

from services import task_queue_service as tqs


def test_covered_product_keys_reads_both_report_shapes():
    # per-SKU shape
    assert tqs._covered_product_keys(
        {"per_sku_reports": [{"product_key": "PK-A"}, {"sku_key": "SK-B"}]}
    ) == {"pk-a", "sk-b"}
    # legacy brand shape
    assert tqs._covered_product_keys(
        {"brand_report": {"per_product": [{"product_key": "PK-C"}]}}
    ) == {"pk-c"}
    # empty / brand-only audit
    assert tqs._covered_product_keys({}) == set()


@pytest.mark.asyncio
async def test_reconcile_is_scope_aware_and_exempts_other_skus(monkeypatch):
    """Brand-level + covered-product tasks close; an uncovered SKU's task is
    left alone (no false-close); standing/in-progress never reach this fetch."""
    stale = [
        {"task_id": "brand-1", "evidence": {}},                       # brand-level -> close
        {"task_id": "covered-sku", "evidence": {"product_key": "PK-A"}},  # covered -> close
        {"task_id": "other-sku", "evidence": {"product_key": "PK-Z"}},    # NOT covered -> keep
    ]
    closed_ids = []

    async def _fake_list(**kwargs):
        assert kwargs["exclude_audit_run_id"] == "run-new"
        return stale

    async def _fake_supersede(*, task_id, superseded_by_task_id=None):
        closed_ids.append(task_id)
        return True

    monkeypatch.setattr(
        "db.merchant_tasks.list_pending_audit_tasks_excluding_run", _fake_list
    )
    monkeypatch.setattr(
        "db.merchant_tasks.mark_task_superseded", _fake_supersede
    )

    closed = await tqs._reconcile_dropped_pending_tasks(
        merchant_id="m1",
        audit_run_id="run-new",
        covered_product_keys={"pk-a"},
    )

    assert closed == 2
    assert set(closed_ids) == {"brand-1", "covered-sku"}
    assert "other-sku" not in closed_ids  # the false-close guard


@pytest.mark.asyncio
async def test_reconcile_no_stale_tasks_is_noop(monkeypatch):
    async def _fake_list(**kwargs):
        return []

    monkeypatch.setattr(
        "db.merchant_tasks.list_pending_audit_tasks_excluding_run", _fake_list
    )
    closed = await tqs._reconcile_dropped_pending_tasks(
        merchant_id="m1", audit_run_id="run-new", covered_product_keys=set()
    )
    assert closed == 0

"""Regression cover for the paid-order recovery query.

`reconcile_paid_orders_missing_merchant_order` builds its merchant filter
conditionally but used to bind `:merchant_id` unconditionally. The only caller
(`run_agentic_commerce_reconciliation_once`) passes merchant_id=None and the CLI
exposes no flag to change that, so every real invocation hit:

    sqlalchemy.exc.ArgumentError: This text() construct doesn't define
    a bound parameter named 'merchant_id'

raised inside databases.Connection._build_query before a single row was read.
These tests drive the real build step rather than a stand-in, so a query whose
declared parameters and supplied values disagree fails here the way it fails in
production.
"""

import pytest

from databases.core import Connection


class _BindCheckingDB:
    """Runs the exact step `databases` takes before reaching the driver."""

    def __init__(self):
        self.query = None
        self.values = None

    async def fetch_all(self, query, values=None):
        Connection._build_query(query, values)
        self.query = query
        self.values = values
        return [{"order_id": "ORD_1"}, {"order_id": "ORD_2"}]


@pytest.mark.asyncio
async def test_unfiltered_reconcile_binds_only_declared_params(monkeypatch):
    """The unfiltered call is the only one the scheduled job can make."""
    from jobs import agentic_commerce_reconciliation as job

    fake = _BindCheckingDB()
    monkeypatch.setattr(job, "database", fake)

    result = await job.reconcile_paid_orders_missing_merchant_order(
        merchant_id=None,
        limit=50,
        min_age_seconds=120,
        dry_run=True,
    )

    assert result == {"dry_run": True, "candidates": ["ORD_1", "ORD_2"], "count": 2}
    assert ":merchant_id" not in fake.query
    assert "merchant_id" not in fake.values


@pytest.mark.asyncio
async def test_merchant_filtered_reconcile_declares_and_binds_merchant_id(monkeypatch):
    """Positive counterpart: when the clause IS emitted, the value must be bound."""
    from jobs import agentic_commerce_reconciliation as job

    fake = _BindCheckingDB()
    monkeypatch.setattr(job, "database", fake)

    result = await job.reconcile_paid_orders_missing_merchant_order(
        merchant_id="merchant_abc",
        limit=10,
        min_age_seconds=60,
        dry_run=True,
    )

    assert result["count"] == 2
    assert ":merchant_id" in fake.query
    assert fake.values["merchant_id"] == "merchant_abc"

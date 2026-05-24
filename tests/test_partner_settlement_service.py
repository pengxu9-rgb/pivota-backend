from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from services import partner_settlement_service as service


class _FakeSettlementDatabase:
    def __init__(self) -> None:
        self.fetch_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.partner_attribution = [
            {"merchant_id": "merch_1", "channel_partner_id": 7},
        ]
        self.invoices = [
            {
                "merchant_id": "merch_1",
                "billing_period_start": date(2026, 4, 1),
                "stripe_invoice_id": "in_subscription",
                "total_cents": 10000,
                "status": "paid",
                "billing_run_id": None,
            },
            {
                "merchant_id": "merch_1",
                "billing_period_start": date(2026, 4, 1),
                "stripe_invoice_id": "in_gmv_current",
                "total_cents": 2000,
                "status": "paid",
                "billing_run_id": 101,
            },
            {
                "merchant_id": "merch_1",
                "billing_period_start": date(2026, 4, 1),
                "stripe_invoice_id": "in_gmv_legacy",
                "total_cents": 300,
                "status": "paid",
                "billing_run_id": None,
            },
        ]
        self.billing_run_items = [
            {
                "stripe_invoice_id": "in_gmv_current",
                "source_type": "gmv_rollup",
            },
            {
                "stripe_invoice_id": "in_gmv_legacy",
                "source_type": "gmv_rollup",
            },
        ]
        self.monthly_brand_statements = [
            {
                "merchant_id": "merch_1",
                "calendar_month": date(2026, 4, 1),
                "overage_revenue_usd_cents": 1950,
                "status": "frozen",
            },
            {
                "merchant_id": "merch_1",
                "calendar_month": date(2026, 4, 1),
                "overage_revenue_usd_cents": 500,
                "status": "open",
            },
            {
                "merchant_id": "merch_1",
                "calendar_month": date(2026, 3, 1),
                "overage_revenue_usd_cents": 700,
                "status": "invoiced",
            },
        ]

    async def fetch_all(self, query: str, values: dict[str, Any] | None = None):
        params = dict(values or {})
        sql = _normalize_sql(query)
        self.fetch_all_calls.append((sql, params))

        if "from invoices i" not in sql or "join partner_attribution pa" not in sql:
            raise AssertionError(f"Unhandled fetch_all query: {query}")

        totals: dict[str, int] = {}
        for invoice in self.invoices:
            merchant_id = str(invoice["merchant_id"])
            if invoice["status"] != "paid":
                continue
            if invoice["billing_period_start"] != params["period_start"]:
                continue
            if not self._partner_matches(merchant_id, int(params["channel_partner_id"])):
                continue
            if "i.billing_run_id is null" in sql and invoice["billing_run_id"] is not None:
                continue
            if "from billing_run_items" in sql and self._has_billing_run_item(invoice):
                continue
            totals[merchant_id] = totals.get(merchant_id, 0) + int(invoice["total_cents"])

        return [
            {"merchant_id": merchant_id, "revenue_cents": revenue_cents}
            for merchant_id, revenue_cents in sorted(totals.items())
        ]

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        params = dict(values or {})
        sql = _normalize_sql(query)
        if "from monthly_brand_statements mbs" not in sql:
            raise AssertionError(f"Unhandled fetch_one query: {query}")

        total = 0
        for statement in self.monthly_brand_statements:
            merchant_id = str(statement["merchant_id"])
            if not self._partner_matches(merchant_id, int(params["channel_partner_id"])):
                continue
            if statement["status"] not in {"frozen", "invoiced"}:
                continue
            if not (params["period_start"] <= statement["calendar_month"] < params["period_end"]):
                continue
            total += int(statement["overage_revenue_usd_cents"])
        return {"revenue_cents": total}

    def _partner_matches(self, merchant_id: str, channel_partner_id: int) -> bool:
        return any(
            row["merchant_id"] == merchant_id
            and row["channel_partner_id"] == channel_partner_id
            for row in self.partner_attribution
        )

    def _has_billing_run_item(self, invoice: dict[str, Any]) -> bool:
        stripe_invoice_id = invoice.get("stripe_invoice_id")
        return any(
            item["stripe_invoice_id"] == stripe_invoice_id
            for item in self.billing_run_items
        )


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()


@pytest.mark.asyncio
async def test_subscription_revenue_excludes_gmv_take_invoices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeSettlementDatabase()
    monkeypatch.setattr(service, "database", db)

    revenue = await service._subscription_revenue_by_merchant(
        7,
        date(2026, 4, 1),
    )

    assert revenue == {"merch_1": 10000}
    sql, _ = db.fetch_all_calls[0]
    assert "i.billing_run_id is null" in sql
    assert "from billing_run_items" in sql


@pytest.mark.asyncio
async def test_credit_overage_for_partner_reads_frozen_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeSettlementDatabase()
    monkeypatch.setattr(service, "database", db)

    revenue = await service._credit_overage_for_partner(
        7,
        date(2026, 4, 1),
        date(2026, 5, 1),
    )

    assert revenue == 1950


@pytest.mark.asyncio
async def test_run_settlement_skips_legacy_payout_path_when_v2_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex post-merge review of PR #641 caught a real double-pay risk:

    PR #8 (#640) added settlement_file_generate + settlement_file_transfer
    crons as a NEW payment pipeline. The OLD path in run_settlement still
    calls credit_partner_balance + create_payout. Both pipelines consume the
    same settlement_snapshots — without this skip, partners would be paid
    twice (once via the legacy agent_payouts flow, once via Stripe Connect
    transfer).

    When PARTNER_REV_SHARE_USE_V2=true, run_settlement should write the
    snapshot (so the new pipeline has data) and then SKIP the legacy
    credit/debit/payout steps.
    """

    calls = {
        "compute_v2": 0,
        "compute_v1": 0,
        "snapshot_writes": 0,
        "credit": 0,
        "debit": 0,
        "create_payout": 0,
    }

    async def fake_fetch_run(billing_run_id):
        return {"period_start": date(2026, 4, 1), "period_end": date(2026, 5, 1)}

    async def fake_fetch_all(query, params=None):
        return [{"channel_partner_id": 7}]

    fake_db = type("FakeDB", (), {"fetch_all": staticmethod(fake_fetch_all)})

    async def fake_compute_v2(*a, **kw):
        calls["compute_v2"] += 1
        return {
            "net_comp_cents": 5000,
            "clawbacks": [{"amount_cents": 1000, "merchant_id": "m1", "reason": "x"}],
        }

    async def fake_compute_v1(*a, **kw):
        calls["compute_v1"] += 1
        return {
            "net_comp_cents": 5000,
            "clawbacks": [{"amount_cents": 1000, "merchant_id": "m1", "reason": "x"}],
        }

    async def fake_write_snapshot(billing_run_id, partner_id, comp):
        calls["snapshot_writes"] += 1
        return 999

    async def fake_credit(*a, **kw):
        calls["credit"] += 1

    async def fake_debit(*a, **kw):
        calls["debit"] += 1

    async def fake_create_payout(*a, **kw):
        calls["create_payout"] += 1
        return 1

    monkeypatch.setattr(service, "_fetch_billing_run", fake_fetch_run)
    monkeypatch.setattr(service, "database", fake_db)
    monkeypatch.setattr(
        service.partner_rev_share_engine_v2,
        "compute_partner_comp_v2",
        fake_compute_v2,
    )
    monkeypatch.setattr(service, "compute_partner_comp", fake_compute_v1)
    monkeypatch.setattr(service, "write_settlement_snapshot", fake_write_snapshot)
    monkeypatch.setattr(service, "credit_partner_balance", fake_credit)
    monkeypatch.setattr(service, "debit_partner_balance", fake_debit)
    monkeypatch.setattr(service, "create_payout", fake_create_payout)

    # Flag ON: v2 compute fires, snapshot written, legacy payout path SKIPPED
    monkeypatch.setattr(service.settings, "partner_rev_share_use_v2", True)
    await service.run_settlement(101)
    assert calls == {
        "compute_v2": 1,
        "compute_v1": 0,
        "snapshot_writes": 1,
        "credit": 0,
        "debit": 0,
        "create_payout": 0,
    }, f"v2 path leaked into legacy payout calls: {calls}"

    # Reset + flag OFF: v1.3 path runs unchanged
    for k in calls:
        calls[k] = 0
    monkeypatch.setattr(service.settings, "partner_rev_share_use_v2", False)
    await service.run_settlement(101)
    assert calls == {
        "compute_v2": 0,
        "compute_v1": 1,
        "snapshot_writes": 1,
        "credit": 1,
        "debit": 1,
        "create_payout": 1,
    }, f"legacy path regressed: {calls}"

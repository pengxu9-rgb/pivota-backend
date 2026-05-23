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

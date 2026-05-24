from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any

import pytest

from services import activation_event_service as service


pytestmark = pytest.mark.asyncio


class _FakeActivationDatabase:
    def __init__(self) -> None:
        self.invoices: list[dict[str, Any]] = []
        self.partner_attribution: list[dict[str, Any]] = []
        self.update_count = 0
        self._next_invoice_id = 1
        self._next_attribution_id = 1

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "sum(total_cents - refunded_cents)" in sql:
            activation_date = params["activation_date"]
            net_cash_received_cents = 0
            for invoice in self._eligible_invoices(params["merchant_id"]):
                invoice_timestamp = invoice.get("paid_at") or invoice.get(
                    "finalized_at"
                )
                if invoice_timestamp is None:
                    continue
                invoice_date = _as_date(invoice_timestamp)
                if invoice_date <= activation_date:
                    # RAW - RAW: eligible invoice retained cash after refunds.
                    net_cash_received_cents += (
                        int(invoice["total_cents"]) - int(invoice["refunded_cents"])
                    )
            return {"net_cash_received_cents": net_cash_received_cents}

        if "from invoices" in sql:
            rows = self._eligible_invoices(params["merchant_id"])
            return rows[0] if rows else None

        if "from partner_attribution" in sql and "for update" in sql:
            return next(
                (
                    row
                    for row in self.partner_attribution
                    if row["merchant_id"] == params["merchant_id"]
                    and int(row["channel_partner_id"])
                    == int(params["channel_partner_id"])
                ),
                None,
            )

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def execute(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("update partner_attribution"):
            for row in self.partner_attribution:
                if int(row["id"]) == int(params["attribution_id"]):
                    if row.get("activated_at") is None:
                        row["activated_at"] = params["activated_at"]
                        self.update_count += 1
                    return None
            return None

        raise AssertionError(f"Unhandled execute query: {query}")

    def add_invoice(
        self,
        *,
        merchant_id: str,
        status: str = "paid",
        total_cents: int = 9900,
        refunded_cents: int = 0,
        paid_at: datetime | None = None,
        finalized_at: datetime | None = None,
        created_at: datetime | None = None,
        stripe_invoice_id: str | None = None,
    ) -> int:
        invoice_id = self._next_invoice_id
        self._next_invoice_id += 1
        created_at = created_at or paid_at or finalized_at or datetime(
            2025,
            1,
            1,
            tzinfo=timezone.utc,
        )
        self.invoices.append(
            {
                "id": invoice_id,
                "merchant_id": merchant_id,
                "stripe_invoice_id": stripe_invoice_id or f"in_{invoice_id}",
                "status": status,
                "total_cents": total_cents,
                "refunded_cents": refunded_cents,
                "paid_at": paid_at,
                "finalized_at": finalized_at,
                "created_at": created_at,
            }
        )
        return invoice_id

    def add_attribution(
        self,
        *,
        merchant_id: str,
        channel_partner_id: int,
        activated_at: date | None = None,
    ) -> int:
        attribution_id = self._next_attribution_id
        self._next_attribution_id += 1
        self.partner_attribution.append(
            {
                "id": attribution_id,
                "merchant_id": merchant_id,
                "channel_partner_id": channel_partner_id,
                "activated_at": activated_at,
            }
        )
        return attribution_id

    def _eligible_invoices(self, merchant_id: str) -> list[dict[str, Any]]:
        rows = [
            invoice
            for invoice in self.invoices
            if invoice["merchant_id"] == merchant_id
            and invoice["status"] == "paid"
            and int(invoice["total_cents"]) > 0
            and int(invoice["total_cents"]) - int(invoice["refunded_cents"]) > 0
        ]
        return sorted(
            rows,
            key=lambda invoice: invoice.get("paid_at")
            or invoice.get("finalized_at")
            or invoice.get("created_at"),
        )


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeActivationDatabase:
    db = _FakeActivationDatabase()
    monkeypatch.setattr(service, "database", db)
    return db


async def test_activates_on_first_paid_positive_invoice(
    fake_db: _FakeActivationDatabase,
) -> None:
    merchant_id = "merch_activation_happy"
    fake_db.add_invoice(
        merchant_id=merchant_id,
        paid_at=datetime(2025, 6, 15, tzinfo=timezone.utc),
    )
    first_invoice_id = fake_db.add_invoice(
        merchant_id=merchant_id,
        paid_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )

    decision = await service.evaluate_activation(merchant_id=merchant_id)

    assert decision.eligible is True
    assert decision.activation_date == date(2025, 6, 1)
    assert decision.qualifying_invoice_id == first_invoice_id
    assert decision.net_cash_received_cents == 9900


async def test_does_not_activate_on_zero_dollar_invoice(
    fake_db: _FakeActivationDatabase,
) -> None:
    fake_db.add_invoice(
        merchant_id="merch_zero",
        total_cents=0,
        paid_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )

    decision = await service.evaluate_activation(merchant_id="merch_zero")

    assert decision.eligible is False
    assert decision.activation_date is None
    assert decision.qualifying_invoice_id is None


async def test_does_not_activate_on_fully_refunded_invoice(
    fake_db: _FakeActivationDatabase,
) -> None:
    fake_db.add_invoice(
        merchant_id="merch_refunded",
        total_cents=9900,
        refunded_cents=9900,
        paid_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )

    decision = await service.evaluate_activation(merchant_id="merch_refunded")

    assert decision.eligible is False
    assert decision.net_cash_received_cents == 0


async def test_activates_on_partial_refund_keeps_net_positive(
    fake_db: _FakeActivationDatabase,
) -> None:
    invoice_id = fake_db.add_invoice(
        merchant_id="merch_partial_refund",
        total_cents=9900,
        refunded_cents=2000,
        paid_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )

    decision = await service.evaluate_activation(merchant_id="merch_partial_refund")

    assert decision.eligible is True
    assert decision.activation_date == date(2025, 6, 1)
    assert decision.qualifying_invoice_id == invoice_id
    assert decision.net_cash_received_cents == 7900


async def test_does_not_activate_when_no_paid_invoices(
    fake_db: _FakeActivationDatabase,
) -> None:
    fake_db.add_invoice(
        merchant_id="merch_no_paid",
        status="draft",
        paid_at=None,
        finalized_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    fake_db.add_invoice(
        merchant_id="merch_no_paid",
        status="failed",
        paid_at=None,
        finalized_at=datetime(2025, 6, 2, tzinfo=timezone.utc),
    )

    decision = await service.evaluate_activation(merchant_id="merch_no_paid")

    assert decision.eligible is False
    assert decision.qualifying_invoice_id is None


async def test_try_activate_brand_is_write_once(
    fake_db: _FakeActivationDatabase,
) -> None:
    merchant_id = "merch_write_once"
    partner_id = 7
    fake_db.add_attribution(merchant_id=merchant_id, channel_partner_id=partner_id)
    fake_db.add_invoice(
        merchant_id=merchant_id,
        paid_at=datetime(2025, 6, 10, tzinfo=timezone.utc),
    )

    first_decision = await service.try_activate_brand(
        merchant_id=merchant_id,
        channel_partner_id=partner_id,
    )
    fake_db.add_invoice(
        merchant_id=merchant_id,
        paid_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    second_decision = await service.try_activate_brand(
        merchant_id=merchant_id,
        channel_partner_id=partner_id,
    )

    attribution = fake_db.partner_attribution[0]
    assert first_decision.activation_date == date(2025, 6, 10)
    assert second_decision.activation_date == date(2025, 6, 1)
    # The UPDATE now stores the full TIMESTAMPTZ from the qualifying invoice's
    # paid_at (per the post-merge fix for the activated_at >= signed_at CHECK
    # constraint on partner_attribution from migration 111). The DATE portion
    # must still match the first eligible invoice's paid_at date.
    assert attribution["activated_at"] == datetime(2025, 6, 10, tzinfo=timezone.utc)
    assert first_decision.activation_at == datetime(2025, 6, 10, tzinfo=timezone.utc)
    assert fake_db.update_count == 1


async def test_try_activate_brand_no_op_when_no_attribution_row(
    fake_db: _FakeActivationDatabase,
) -> None:
    fake_db.add_invoice(
        merchant_id="merch_no_attribution",
        paid_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )

    decision = await service.try_activate_brand(
        merchant_id="merch_no_attribution",
        channel_partner_id=7,
    )

    assert decision.eligible is True
    assert fake_db.update_count == 0
    assert fake_db.partner_attribution == []


async def test_try_activate_brand_no_op_when_activated_at_already_set(
    fake_db: _FakeActivationDatabase,
) -> None:
    merchant_id = "merch_already_active"
    partner_id = 7
    fake_db.add_attribution(
        merchant_id=merchant_id,
        channel_partner_id=partner_id,
        activated_at=date(2025, 5, 1),
    )
    fake_db.add_invoice(
        merchant_id=merchant_id,
        paid_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )

    decision = await service.try_activate_brand(
        merchant_id=merchant_id,
        channel_partner_id=partner_id,
    )

    assert decision.eligible is True
    assert decision.activation_date == date(2025, 6, 1)
    assert fake_db.partner_attribution[0]["activated_at"] == date(2025, 5, 1)
    assert fake_db.update_count == 0


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any

import pytest

from core.billing_constants import overage_revenue_cents
from services.billing import credit_overage_billing
from services.billing import monthly_brand_statements_service as statements


pytestmark = pytest.mark.asyncio


class _FakeStatementsDatabase:
    def __init__(self) -> None:
        self.subscription_plans: list[dict[str, Any]] = []
        self.merchants: list[dict[str, Any]] = []
        self.user_subscriptions: list[dict[str, Any]] = []
        self.usage_events: list[dict[str, Any]] = []
        self.commerce_attribution_edges: list[dict[str, Any]] = []
        self.monthly_brand_statements: list[dict[str, Any]] = []
        self.invoices: list[dict[str, Any]] = []
        self.billing_run_items: list[dict[str, Any]] = []
        self._next_plan_id = 1
        self._next_subscription_id = 1
        self._next_usage_event_id = 1
        self._next_statement_id = 1

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch_all(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})
        if "from agent_center_usage_events" in sql:
            merchant_id = params["merchant_id"]
            period_start = params["period_start"]
            period_end = params["period_end"]
            debit_types = {
                "credit_debit_audit",
                "credit_debit_prompt",
                "credit_debit_execution",
            }
            grant_types = {
                "credit_grant_audit",
                "credit_grant_prompt",
                "credit_grant_execution",
            }
            rows = []
            for ev in self.usage_events:
                if ev["merchant_id"] != merchant_id:
                    continue
                if not (period_start <= _as_date(ev["created_at"]) < period_end):
                    continue
                if ev["billing_mode"] == "debit" and ev["event_type"] in debit_types:
                    delta = -int(ev["quantity"])
                elif ev["billing_mode"] == "credit" and ev["event_type"] in grant_types:
                    delta = int(ev["quantity"])
                else:
                    continue
                rows.append({"id": ev["id"], "credits_delta": delta})
            return sorted(rows, key=lambda row: row["id"])
        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "from monthly_brand_statements" in sql and "where merchant_id" in sql:
            return self._statement_for_month(params["merchant_id"], params["calendar_month"])

        if "from user_subscriptions us" in sql:
            return self._latest_subscription(
                params["merchant_id"],
                params["period_start"],
                params["period_end"],
            )

        if "from commerce_attribution_edges" in sql:
            return self._gmv_aggregation(params["merchant_id"], params["period_start"], params["period_end"])

        if sql.startswith("insert into monthly_brand_statements"):
            statement = {
                "id": self._next_statement_id,
                "merchant_id": params["merchant_id"],
                "calendar_month": params["calendar_month"],
                "subscription_plan_id": params.get("subscription_plan_id"),
                "tier_name": params.get("tier_name"),
                "subscription_revenue_usd_cents": params["subscription_revenue_usd_cents"],
                "credits_consumed": params["credits_consumed"],
                "bundled_credits_consumed": params["bundled_credits_consumed"],
                "overage_credits": params["overage_credits"],
                "overage_revenue_usd_cents": params["overage_revenue_usd_cents"],
                "gmv_usd_cents": params["gmv_usd_cents"],
                "gmv_personal_usd_cents": params["gmv_personal_usd_cents"],
                "gmv_third_party_usd_cents": params["gmv_third_party_usd_cents"],
                "pivota_gmv_take_usd_cents": params["pivota_gmv_take_usd_cents"],
                "total_revenue_usd_cents": params["total_revenue_usd_cents"],
                "total_cogs_usd_cents": params["total_cogs_usd_cents"],
                "pivota_gross_margin_usd_cents": params["pivota_gross_margin_usd_cents"],
                "status": "open",
                "frozen_at": None,
                "invoiced_at": None,
                "overage_invoice_id": None,
                "metadata": json.loads(params["metadata_json"]),
            }
            self._next_statement_id += 1
            self.monthly_brand_statements.append(statement)
            return {"id": statement["id"]}

        if sql.startswith("update monthly_brand_statements") and "subscription_plan_id =" in sql:
            statement = self._statement_by_id(params["statement_id"])
            if statement["status"] != "open":
                return None
            statement.update(
                {
                    "subscription_plan_id": params.get("subscription_plan_id"),
                    "tier_name": params.get("tier_name"),
                    "subscription_revenue_usd_cents": params["subscription_revenue_usd_cents"],
                    "credits_consumed": params["credits_consumed"],
                    "bundled_credits_consumed": params["bundled_credits_consumed"],
                    "overage_credits": params["overage_credits"],
                    "overage_revenue_usd_cents": params["overage_revenue_usd_cents"],
                    "gmv_usd_cents": params["gmv_usd_cents"],
                    "gmv_personal_usd_cents": params["gmv_personal_usd_cents"],
                    "gmv_third_party_usd_cents": params["gmv_third_party_usd_cents"],
                    "pivota_gmv_take_usd_cents": params["pivota_gmv_take_usd_cents"],
                    "total_revenue_usd_cents": params["total_revenue_usd_cents"],
                    "total_cogs_usd_cents": params["total_cogs_usd_cents"],
                    "pivota_gross_margin_usd_cents": params["pivota_gross_margin_usd_cents"],
                    "metadata": json.loads(params["metadata_json"]),
                }
            )
            return {"id": statement["id"]}

        if sql.startswith("update monthly_brand_statements") and "set status = 'frozen'" in sql:
            statement = self._statement_by_id(params["statement_id"])
            if statement["status"] != "open":
                return None
            statement["status"] = "frozen"
            statement["frozen_at"] = datetime.now(timezone.utc)
            return {"id": statement["id"]}

        if sql.startswith("update monthly_brand_statements") and "set status = 'invoiced'" in sql:
            statement = self._statement_by_id(params["statement_id"])
            if statement["status"] != "frozen":
                return None
            if "overage_credits = 0" in sql and statement["overage_credits"] != 0:
                return None
            if "overage_invoice_id is null" in sql and statement["overage_invoice_id"] is not None:
                return None
            statement["status"] = "invoiced"
            statement["invoiced_at"] = datetime.now(timezone.utc)
            statement["overage_invoice_id"] = params.get("overage_invoice_id")
            return {"id": statement["id"]}

        if sql.startswith("select id from invoices where id"):
            return next((row for row in self.invoices if row["id"] == params["invoice_id"]), None)

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def execute(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})
        if sql.startswith("update monthly_brand_statements") and "set overage_credits" in sql:
            statement = self._statement_by_id(params["statement_id"])
            if statement["status"] == "frozen":
                raise RuntimeError(
                    "monthly_brand_statements frozen row does not allow computed field mutation"
                )
            statement["overage_credits"] = params["overage_credits"]
            return None
        raise AssertionError(f"Unhandled execute query: {query}")

    def add_plan(self, *, name: str = "starter", price_cents: int = 9900, allowance: int = 4000) -> int:
        plan_id = self._next_plan_id
        self._next_plan_id += 1
        self.subscription_plans.append(
            {
                "id": plan_id,
                "name": name,
                "price_cents": price_cents,
                "monthly_credit_allowance": allowance,
            }
        )
        return plan_id

    def add_merchant(self, merchant_id: str) -> None:
        self.merchants.append({"merchant_id": merchant_id})

    def add_subscription(self, *, merchant_id: str, plan_id: int, month: date) -> int:
        subscription_id = self._next_subscription_id
        self._next_subscription_id += 1
        self.user_subscriptions.append(
            {
                "id": subscription_id,
                "merchant_id": merchant_id,
                "plan_id": plan_id,
                "status": "active",
                "current_period_start": month,
                "current_period_end": _next_month(month),
                "started_at": month,
                "updated_at": month,
            }
        )
        return subscription_id

    def consume_credits(self, *, merchant_id: str, credits: int, occurred_at: datetime) -> str:
        # Consumption is metered to agent_center_usage_events by the wired debit
        # path, which is what assemble_for_month now reads.
        event_id = f"mcb_{self._next_usage_event_id:09d}"
        self._next_usage_event_id += 1
        self.usage_events.append(
            {
                "id": event_id,
                "merchant_id": merchant_id,
                "billing_mode": "debit",
                "event_type": "credit_debit_audit",
                "quantity": int(credits),
                "created_at": occurred_at,
            }
        )
        return event_id

    def add_gmv_edge(
        self,
        *,
        merchant_id: str,
        net_attributed_gmv_cents: int,
        gmv_channel: str | None,
        created_at: datetime,
        edge_id: str | None = None,
        third_party_platform: str | None = None,
        third_party_platform_fee_pct: float | None = None,
    ) -> str:
        edge_id = edge_id or f"edge_{len(self.commerce_attribution_edges) + 1}"
        self.commerce_attribution_edges.append(
            {
                "edge_id": edge_id,
                "merchant_id": merchant_id,
                "net_attributed_gmv_cents": net_attributed_gmv_cents,
                "gmv_channel": gmv_channel,
                "third_party_platform": third_party_platform,
                "third_party_platform_fee_pct": third_party_platform_fee_pct,
                "created_at": created_at,
            }
        )
        return edge_id

    def statement(self, statement_id: int) -> dict[str, Any]:
        return dict(self._statement_by_id(statement_id))

    def _statement_by_id(self, statement_id: int) -> dict[str, Any]:
        for row in self.monthly_brand_statements:
            if row["id"] == statement_id:
                return row
        raise AssertionError(f"Statement not found: {statement_id}")

    def _statement_for_month(self, merchant_id: str, calendar_month: date) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.monthly_brand_statements
                if row["merchant_id"] == merchant_id and row["calendar_month"] == calendar_month
            ),
            None,
        )

    def _latest_subscription(
        self,
        merchant_id: str,
        period_start: date,
        period_end: date,
    ) -> dict[str, Any] | None:
        candidates = [
            row
            for row in self.user_subscriptions
            if row["merchant_id"] == merchant_id
            and row["status"] in {"active", "trialing", "past_due"}
            and (row["current_period_start"] is None or row["current_period_start"] < period_end)
            and (row["current_period_end"] is None or row["current_period_end"] > period_start)
        ]
        if not candidates:
            return None
        subscription = sorted(
            candidates,
            key=lambda row: (row["current_period_start"] or date.min, row["id"]),
        )[-1]
        plan = next(row for row in self.subscription_plans if row["id"] == subscription["plan_id"])
        return {
            "subscription_plan_id": plan["id"],
            "tier_name": plan["name"],
            "monthly_credit_allowance": plan["monthly_credit_allowance"],
            "subscription_revenue_usd_cents": plan["price_cents"],
        }

    def _gmv_aggregation(self, merchant_id: str, period_start: date, period_end: date) -> dict[str, Any]:
        rows = [
            row
            for row in self.commerce_attribution_edges
            if row["merchant_id"] == merchant_id
            and period_start <= _as_date(row["created_at"]) < period_end
            and int(row.get("net_attributed_gmv_cents") or 0) > 0
        ]
        return {
            "gmv_raw_total_cents": sum(int(row["net_attributed_gmv_cents"]) for row in rows),
            "gmv_personal_cents": sum(
                int(row["net_attributed_gmv_cents"]) for row in rows if row["gmv_channel"] == "personal_agent"
            ),
            "gmv_third_party_cents": sum(
                int(row["net_attributed_gmv_cents"])
                for row in rows
                if row["gmv_channel"] == "third_party_agent"
            ),
            "unclassified_edge_count": sum(1 for row in rows if row["gmv_channel"] is None),
        }


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeStatementsDatabase:
    db = _FakeStatementsDatabase()
    monkeypatch.setattr(statements, "database", db)
    monkeypatch.setattr(statements, "IS_POSTGRES", False)
    monkeypatch.setattr(credit_overage_billing, "database", db)
    return db


async def test_assemble_and_freeze_report_only_freezes_without_invoicing(
    fake_db: _FakeStatementsDatabase,
) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_report_only"
    plan_id = fake_db.add_plan(name="starter", price_cents=9900, allowance=4000)
    fake_db.add_merchant(merchant_id)
    fake_db.add_subscription(merchant_id=merchant_id, plan_id=plan_id, month=month)
    fake_db.consume_credits(
        merchant_id=merchant_id,
        credits=5500,
        occurred_at=datetime(2025, 6, 15, tzinfo=timezone.utc),
    )

    statement_id = await statements.assemble_and_freeze_report_only(merchant_id, month)

    row = fake_db.statement(statement_id)
    # Consumption mirrored from the balance system, and the row is frozen (so it
    # surfaces in the merchant statements list) but NOT invoiced — overage is
    # already charged inline by the debit path; this path must never bill.
    assert row["credits_consumed"] == 5500
    assert row["overage_credits"] == 1500
    assert row["overage_revenue_usd_cents"] == 1950
    assert row["status"] == "frozen"
    assert row["invoiced_at"] is None

    # Idempotent: re-running returns the same id and leaves it frozen.
    again = await statements.assemble_and_freeze_report_only(merchant_id, month)
    assert again == statement_id
    assert fake_db.statement(statement_id)["status"] == "frozen"


async def test_starter_brand_normal_usage_brief_9_1(fake_db: _FakeStatementsDatabase) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_starter"
    plan_id = fake_db.add_plan(name="starter", price_cents=9900, allowance=4000)
    fake_db.add_merchant(merchant_id)
    fake_db.add_subscription(merchant_id=merchant_id, plan_id=plan_id, month=month)
    fake_db.consume_credits(
        merchant_id=merchant_id,
        credits=5500,
        occurred_at=datetime(2025, 6, 15, tzinfo=timezone.utc),
    )
    fake_db.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=150000,
        gmv_channel="personal_agent",
        created_at=datetime(2025, 6, 16, tzinfo=timezone.utc),
    )
    fake_db.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=350000,
        gmv_channel="third_party_agent",
        third_party_platform="openai",
        third_party_platform_fee_pct=0.65,
        created_at=datetime(2025, 6, 17, tzinfo=timezone.utc),
    )

    statement_id = await statements.assemble_for_month(merchant_id, month)

    row = fake_db.statement(statement_id)
    assert row["subscription_revenue_usd_cents"] == 9900
    assert row["credits_consumed"] == 5500
    assert row["bundled_credits_consumed"] == 4000
    assert row["overage_credits"] == 1500
    assert row["overage_revenue_usd_cents"] == 1950
    assert row["gmv_usd_cents"] == 500000
    assert row["gmv_personal_usd_cents"] == 150000
    assert row["gmv_third_party_usd_cents"] == 350000
    assert row["pivota_gmv_take_usd_cents"] == 50000
    assert row["total_revenue_usd_cents"] == 61850
    assert row["total_cogs_usd_cents"] == 5060
    # PR #4 records raw GMV take as revenue, but PR #5 owns channel-tiered GMV COGS.
    # Until then, gross margin is total_revenue - credit/SaaS COGS only.
    assert row["pivota_gross_margin_usd_cents"] == 56790
    assert row["metadata"]["gmv_unclassified_edge_count"] == 0
    assert row["metadata"]["gmv_take_rate_bp_applied"] == 1000
    assert row["metadata"]["gmv_take_cogs_pending_pr5"] is True


async def test_gmv_unclassified_edges_are_excluded_from_statement_total(
    fake_db: _FakeStatementsDatabase,
) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_unclassified"
    plan_id = fake_db.add_plan(name="starter", price_cents=9900, allowance=4000)
    fake_db.add_merchant(merchant_id)
    fake_db.add_subscription(merchant_id=merchant_id, plan_id=plan_id, month=month)
    fake_db.consume_credits(
        merchant_id=merchant_id,
        credits=5500,
        occurred_at=datetime(2025, 6, 15, tzinfo=timezone.utc),
    )
    fake_db.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=150000,
        gmv_channel="personal_agent",
        created_at=datetime(2025, 6, 16, tzinfo=timezone.utc),
    )
    fake_db.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=350000,
        gmv_channel="third_party_agent",
        third_party_platform="openai",
        third_party_platform_fee_pct=0.65,
        created_at=datetime(2025, 6, 17, tzinfo=timezone.utc),
    )
    fake_db.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=20000,
        gmv_channel=None,
        created_at=datetime(2025, 6, 18, tzinfo=timezone.utc),
    )

    statement_id = await statements.assemble_for_month(merchant_id, month)

    row = fake_db.statement(statement_id)
    assert row["gmv_usd_cents"] == 500000
    assert row["gmv_personal_usd_cents"] == 150000
    assert row["gmv_third_party_usd_cents"] == 350000
    assert row["pivota_gmv_take_usd_cents"] == 50000
    assert row["total_revenue_usd_cents"] == 61850
    assert row["metadata"]["gmv_unclassified_edge_count"] == 1


async def test_no_double_charge_within_allowance_brief_9_3(fake_db: _FakeStatementsDatabase) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_allowance"
    plan_id = fake_db.add_plan(name="starter", price_cents=9900, allowance=4000)
    fake_db.add_merchant(merchant_id)
    fake_db.add_subscription(merchant_id=merchant_id, plan_id=plan_id, month=month)
    fake_db.consume_credits(
        merchant_id=merchant_id,
        credits=4000,
        occurred_at=datetime(2025, 6, 20, tzinfo=timezone.utc),
    )

    statement_id = await statements.assemble_for_month(merchant_id, month)
    await statements.freeze(statement_id)
    await credit_overage_billing.finalize_statement_no_overage(statement_id)

    row = fake_db.statement(statement_id)
    assert row["overage_credits"] == 0
    assert row["overage_revenue_usd_cents"] == 0
    assert row["status"] == "invoiced"
    assert row["overage_invoice_id"] is None
    assert fake_db.invoices == []
    assert fake_db.billing_run_items == []


async def test_assemble_is_idempotent_for_open_statement(fake_db: _FakeStatementsDatabase) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_idempotent"
    plan_id = fake_db.add_plan(name="starter", price_cents=9900, allowance=4000)
    fake_db.add_merchant(merchant_id)
    fake_db.add_subscription(merchant_id=merchant_id, plan_id=plan_id, month=month)
    fake_db.consume_credits(
        merchant_id=merchant_id,
        credits=5500,
        occurred_at=datetime(2025, 6, 7, tzinfo=timezone.utc),
    )
    fake_db.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=100000,
        gmv_channel="personal_agent",
        created_at=datetime(2025, 6, 7, 12, tzinfo=timezone.utc),
    )

    first_id = await statements.assemble_for_month(merchant_id, month)
    first_row = fake_db.statement(first_id)
    second_id = await statements.assemble_for_month(merchant_id, month)
    second_row = fake_db.statement(second_id)

    assert second_id == first_id
    assert len(fake_db.monthly_brand_statements) == 1
    assert second_row == first_row
    assert second_row["gmv_usd_cents"] == 100000
    assert second_row["gmv_personal_usd_cents"] == 100000
    assert second_row["gmv_third_party_usd_cents"] == 0
    assert second_row["pivota_gmv_take_usd_cents"] == 10000


async def test_frozen_statement_blocks_computed_field_mutation(fake_db: _FakeStatementsDatabase) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_frozen"
    plan_id = fake_db.add_plan(name="starter", price_cents=9900, allowance=4000)
    fake_db.add_merchant(merchant_id)
    fake_db.add_subscription(merchant_id=merchant_id, plan_id=plan_id, month=month)
    fake_db.consume_credits(
        merchant_id=merchant_id,
        credits=5500,
        occurred_at=datetime(2025, 6, 7, tzinfo=timezone.utc),
    )
    statement_id = await statements.assemble_for_month(merchant_id, month)
    await statements.freeze(statement_id)

    with pytest.raises(RuntimeError, match="frozen row"):
        await fake_db.execute(
            """
            UPDATE monthly_brand_statements
            SET overage_credits = :overage_credits
            WHERE id = :statement_id
            """,
            {"statement_id": statement_id, "overage_credits": 999},
        )


async def test_no_subscription_consumption_is_all_overage(fake_db: _FakeStatementsDatabase) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_no_subscription"
    fake_db.add_merchant(merchant_id)
    fake_db.consume_credits(
        merchant_id=merchant_id,
        credits=1000,
        occurred_at=datetime(2025, 6, 12, tzinfo=timezone.utc),
    )

    statement_id = await statements.assemble_for_month(merchant_id, month)

    row = fake_db.statement(statement_id)
    assert row["subscription_plan_id"] is None
    assert row["tier_name"] is None
    assert row["subscription_revenue_usd_cents"] == 0
    assert row["credits_consumed"] == 1000
    assert row["bundled_credits_consumed"] == 0
    assert row["overage_credits"] == 1000
    assert row["overage_revenue_usd_cents"] == overage_revenue_cents(1000)
    assert row["metadata"]["no_subscription_for_month"] is True


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)

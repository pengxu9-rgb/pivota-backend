"""End-to-end acceptance tests for the channel-partner program.

Other section 9 scenarios are covered unit-style by PR #3-8 test files; this
module re-asserts the section 9.1 full pipeline plus the section 9.2 static
merchant-surface dollar-leak sweep.
"""

from __future__ import annotations

import ast
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from services import partner_rev_share_engine_v2 as engine
from services import partner_settlement_service
from services import settlement_file_service
from services.billing import credit_overage_billing
from services.billing import monthly_brand_statements_service as statements


pytestmark = pytest.mark.asyncio


_DOLLAR_CREDIT_RE = re.compile(
    r"\$\d+(\.\d{2})?\s*(of|in|worth)?\s*credits?",
    re.IGNORECASE,
)
_BANNED_FIELD_RE = re.compile(
    r"credit_(value|price|dollar|usd)|(bundle|credits?)_(value|usd|dollar)",
    re.IGNORECASE,
)


class _FakeFullDatabase:
    def __init__(self) -> None:
        self.subscription_plans: list[dict[str, Any]] = []
        self.merchants: list[dict[str, Any]] = []
        self.user_subscriptions: list[dict[str, Any]] = []
        self.usage_events: list[dict[str, Any]] = []
        self.commerce_attribution_edges: list[dict[str, Any]] = []
        self.monthly_brand_statements: list[dict[str, Any]] = []
        self.billing_runs: list[dict[str, Any]] = []
        self.invoices: list[dict[str, Any]] = []
        self.billing_run_items: list[dict[str, Any]] = []
        self.channel_partners: list[dict[str, Any]] = []
        self.partner_attribution: list[dict[str, Any]] = []
        self.partner_rate_schedules: list[dict[str, Any]] = []
        self.settlement_snapshots: list[dict[str, Any]] = []
        self.settlement_files: list[dict[str, Any]] = []
        self._next_plan_id = 1
        self._next_subscription_id = 1
        self._next_usage_event_id = 1
        self._next_statement_id = 1
        self._next_billing_run_id = 1
        self._next_invoice_id = 1
        self._next_partner_id = 1
        self._next_rate_id = 1
        self._next_snapshot_id = 1
        self._next_file_id = 1
        self._transaction_depth = 0

    @property
    def in_transaction(self) -> bool:
        return self._transaction_depth > 0

    @asynccontextmanager
    async def transaction(self):
        self._transaction_depth += 1
        try:
            yield
        finally:
            self._transaction_depth -= 1

    async def fetch_all(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("select ss.id, ss.snapshot_payload_jsonb"):
            partner_id = int(params["channel_partner_id"])
            calendar_month = params["calendar_month"]
            period_end = params["period_end"]
            rows = []
            for snapshot in self.settlement_snapshots:
                if int(snapshot["channel_partner_id"]) != partner_id:
                    continue
                if snapshot.get("settled_at") is not None:
                    continue
                billing_run = self._billing_run_by_id(snapshot["billing_run_id"])
                if (
                    billing_run["period_start"] <= calendar_month
                    and billing_run["period_end"] >= period_end
                ):
                    rows.append(
                        {
                            "id": snapshot["id"],
                            "snapshot_payload_jsonb": snapshot[
                                "snapshot_payload_jsonb"
                            ],
                        }
                    )
            return sorted(rows, key=lambda row: row["id"])

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

        if "from partner_attribution" in sql:
            partner_id = int(params["channel_partner_id"])
            rows = [
                {
                    "merchant_id": row["merchant_id"],
                    "activated_at": row.get("activated_at"),
                }
                for row in self.partner_attribution
                if int(row["channel_partner_id"]) == partner_id
                and row["status"] in {"registered", "signed", "active"}
            ]
            return sorted(rows, key=lambda row: row["merchant_id"])

        if sql.startswith("select snapshot_payload_jsonb from settlement_snapshots"):
            partner_id = int(params["channel_partner_id"])
            rows = [
                {"snapshot_payload_jsonb": row["snapshot_payload_jsonb"]}
                for row in self.settlement_snapshots
                if int(row["channel_partner_id"]) == partner_id
            ]
            return sorted(rows, key=lambda row: row.get("created_at") or date.min)

        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("select id, active_rate_scope"):
            return self._partner_by_id(int(params["channel_partner_id"]))

        if sql.startswith("select 1 from invoices"):
            return self._overdue_unpaid_invoice(
                params["merchant_id"],
                int(params["nonpayment_days"]),
            )

        if sql.startswith("select us.id, us.status, us.canceled_at"):
            return self._latest_subscription_without_active_replacement(
                params["merchant_id"]
            )

        if (
            sql.startswith("select id, status from monthly_brand_statements")
            and "where merchant_id" in sql
        ):
            statement = self._statement_for_month(
                params["merchant_id"],
                params["calendar_month"],
            )
            return (
                {"id": statement["id"], "status": statement["status"]}
                if statement
                else None
            )

        if (
            sql.startswith("select id, merchant_id, calendar_month")
            and "from monthly_brand_statements" in sql
        ):
            return self._statement_by_id(int(params["statement_id"]))

        if "from monthly_brand_statements" in sql and "where merchant_id" in sql:
            statement = self._statement_for_month(
                params["merchant_id"],
                params["calendar_month"],
            )
            if "status in ('frozen', 'invoiced')" in sql:
                if statement and statement["status"] in {"frozen", "invoiced"}:
                    return statement
                return None
            return statement

        if "from user_subscriptions us" in sql:
            return self._latest_subscription(
                params["merchant_id"],
                params["period_start"],
                params["period_end"],
            )

        if "from commerce_attribution_edges" in sql:
            return self._gmv_aggregation(
                params["merchant_id"],
                params["period_start"],
                params["period_end"],
            )

        if sql.startswith("insert into monthly_brand_statements"):
            statement = {
                "id": self._next_statement_id,
                "merchant_id": params["merchant_id"],
                "calendar_month": params["calendar_month"],
                "subscription_plan_id": params.get("subscription_plan_id"),
                "tier_name": params.get("tier_name"),
                "subscription_revenue_usd_cents": params[
                    "subscription_revenue_usd_cents"
                ],
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
                "pivota_gross_margin_usd_cents": params[
                    "pivota_gross_margin_usd_cents"
                ],
                "status": "open",
                "frozen_at": None,
                "invoiced_at": None,
                "overage_invoice_id": None,
                "metadata": json.loads(params["metadata_json"]),
            }
            self._next_statement_id += 1
            self.monthly_brand_statements.append(statement)
            return {"id": statement["id"]}

        if (
            sql.startswith("update monthly_brand_statements")
            and "subscription_plan_id =" in sql
        ):
            statement = self._statement_by_id(params["statement_id"])
            if statement["status"] != "open":
                return None
            statement.update(
                {
                    "subscription_plan_id": params.get("subscription_plan_id"),
                    "tier_name": params.get("tier_name"),
                    "subscription_revenue_usd_cents": params[
                        "subscription_revenue_usd_cents"
                    ],
                    "credits_consumed": params["credits_consumed"],
                    "bundled_credits_consumed": params["bundled_credits_consumed"],
                    "overage_credits": params["overage_credits"],
                    "overage_revenue_usd_cents": params[
                        "overage_revenue_usd_cents"
                    ],
                    "gmv_usd_cents": params["gmv_usd_cents"],
                    "gmv_personal_usd_cents": params["gmv_personal_usd_cents"],
                    "gmv_third_party_usd_cents": params[
                        "gmv_third_party_usd_cents"
                    ],
                    "pivota_gmv_take_usd_cents": params[
                        "pivota_gmv_take_usd_cents"
                    ],
                    "total_revenue_usd_cents": params["total_revenue_usd_cents"],
                    "total_cogs_usd_cents": params["total_cogs_usd_cents"],
                    "pivota_gross_margin_usd_cents": params[
                        "pivota_gross_margin_usd_cents"
                    ],
                    "metadata": json.loads(params["metadata_json"]),
                }
            )
            return {"id": statement["id"]}

        if (
            sql.startswith("update monthly_brand_statements")
            and "set status = 'frozen'" in sql
        ):
            statement = self._statement_by_id(params["statement_id"])
            if statement["status"] != "open":
                return None
            statement["status"] = "frozen"
            statement["frozen_at"] = _now()
            return {"id": statement["id"]}

        if (
            sql.startswith("update monthly_brand_statements")
            and "set status = 'invoiced'" in sql
        ):
            statement = self._statement_by_id(params["statement_id"])
            if statement["status"] != "frozen":
                return None
            statement["status"] = "invoiced"
            statement["invoiced_at"] = _now()
            statement["overage_invoice_id"] = params.get("overage_invoice_id")
            return {"id": statement["id"]}

        if sql.startswith("select stripe_customer_id from merchants"):
            row = self._merchant_by_id(params["merchant_id"])
            return {"stripe_customer_id": row.get("stripe_customer_id")} if row else None

        if sql.startswith("insert into billing_runs"):
            existing = self._billing_run_by_key(params["idempotency_key"])
            if existing:
                return None
            row = {
                "id": self._next_billing_run_id,
                "period_start": params["period_start"],
                "period_end": params["period_end"],
                "idempotency_key": params["idempotency_key"],
                "status": "completed",
            }
            self._next_billing_run_id += 1
            self.billing_runs.append(row)
            return {"id": row["id"]}

        if sql.startswith("select id from billing_runs"):
            row = self._billing_run_by_key(params["idempotency_key"])
            return {"id": row["id"]} if row else None

        if sql.startswith("insert into invoices"):
            existing = self._invoice_by_stripe_id(params["stripe_invoice_id"])
            if existing:
                return None
            invoice = {
                "id": self._next_invoice_id,
                "merchant_id": params["merchant_id"],
                "billing_period_start": params["calendar_month"],
                "billing_period_end": params["period_end"],
                "billing_run_id": params.get("billing_run_id"),
                "stripe_invoice_id": params["stripe_invoice_id"],
                "stripe_customer_id": params["stripe_customer_id"],
                "total_cents": params["total_cents"],
                "status": "draft",
                "due_date": params.get("due_date"),
                "created_at": _now(),
            }
            self._next_invoice_id += 1
            self.invoices.append(invoice)
            return {"id": invoice["id"]}

        if sql.startswith("select id from invoices where stripe_invoice_id"):
            row = self._invoice_by_stripe_id(params["stripe_invoice_id"])
            return {"id": row["id"]} if row else None

        if sql.startswith("select id from invoices where id"):
            row = self._invoice_by_id(params["invoice_id"])
            return {"id": row["id"]} if row else None

        if sql.startswith("select rate_bp, id"):
            return self._rate_for_lookup(params)

        if sql.startswith("select id from settlement_snapshots"):
            row = self._existing_snapshot(
                int(params["billing_run_id"]),
                int(params["channel_partner_id"]),
            )
            return {"id": row["id"]} if row else None

        if sql.startswith("insert into settlement_snapshots"):
            payload = json.loads(params["snapshot_payload_json"])
            snapshot = {
                "id": self._next_snapshot_id,
                "billing_run_id": int(params["billing_run_id"]),
                "channel_partner_id": int(params["channel_partner_id"]),
                "snapshot_payload_jsonb": payload,
                "computed_comp_cents": int(params["computed_comp_cents"]),
                "subsidy_cap_remaining_at_snapshot": params[
                    "subsidy_cap_remaining_cents"
                ],
                "created_at": _now(),
                "settled_at": None,
                "settled_via_file_id": None,
            }
            self._next_snapshot_id += 1
            self.settlement_snapshots.append(snapshot)
            return {"id": snapshot["id"]}

        if sql.startswith("select id from settlement_files where channel_partner_id"):
            row = self._file_by_partner_month(
                int(params["channel_partner_id"]),
                params["calendar_month"],
            )
            return {"id": row["id"]} if row else None

        if sql.startswith("select coalesce(carryover_forward_cents"):
            row = self._file_by_partner_month(
                int(params["channel_partner_id"]),
                params["prior_month"],
            )
            return (
                {"carryover_applied_cents": row["carryover_forward_cents"]}
                if row
                else None
            )

        if sql.startswith("insert into settlement_files"):
            existing = self._file_by_partner_month(
                int(params["channel_partner_id"]),
                params["calendar_month"],
            )
            if existing:
                return None
            row = {
                "id": self._next_file_id,
                "channel_partner_id": int(params["channel_partner_id"]),
                "calendar_month": params["calendar_month"],
                "subscription_share_cents": int(params["subscription_share_cents"]),
                "credit_overage_share_cents": int(
                    params["credit_overage_share_cents"]
                ),
                "gmv_share_cents": int(params["gmv_share_cents"]),
                "clawback_cents": int(params["clawback_cents"]),
                "net_before_carryover_cents": int(
                    params["net_before_carryover_cents"]
                ),
                "carryover_applied_cents": int(params["carryover_applied_cents"]),
                "transfer_amount_cents": int(params["transfer_amount_cents"]),
                "carryover_forward_cents": int(params["carryover_forward_cents"]),
                "source_snapshot_ids_jsonb": json.loads(
                    params["source_snapshot_ids_json"]
                ),
                "transfer_status": params["transfer_status"],
                "stripe_transfer_id": None,
                "stripe_transfer_error": None,
                "transferred_at": None,
                "metadata": json.loads(params["metadata_json"]),
            }
            self._next_file_id += 1
            self.settlement_files.append(row)
            return {"id": row["id"]}

        if sql.startswith("select sf.*, cp.stripe_connect_account_id"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            if not file_row:
                return None
            partner = self._partner_by_id(file_row["channel_partner_id"])
            return {
                **file_row,
                "stripe_connect_account_id": partner.get(
                    "stripe_connect_account_id"
                ),
            }

        if sql.startswith("select source_snapshot_ids_jsonb from settlement_files"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            return (
                {"source_snapshot_ids_jsonb": file_row["source_snapshot_ids_jsonb"]}
                if file_row
                else None
            )

        if sql.startswith("select transfer_status from settlement_files"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            return {"transfer_status": file_row["transfer_status"]} if file_row else None

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def execute(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("insert into billing_run_items"):
            self.billing_run_items.append(
                {
                    "billing_run_id": params["billing_run_id"],
                    "merchant_id": params["merchant_id"],
                    "source_type": "credit_overage",
                    "source_id": params["statement_id"],
                    "stripe_invoice_item_id": params["stripe_invoice_item_id"],
                    "stripe_invoice_id": params["stripe_invoice_id"],
                    "amount_cents": params["amount_cents"],
                    "description": params["description"],
                }
            )
            return None

        if sql.startswith("update settlement_files set transfer_status = 'transferring'"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            if file_row and file_row["transfer_status"] == "pending":
                file_row["transfer_status"] = "transferring"
                file_row["stripe_transfer_error"] = None
            return None

        if sql.startswith("update settlement_files set transfer_status = 'transferred'"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            file_row["transfer_status"] = "transferred"
            file_row["stripe_transfer_id"] = params.get("stripe_transfer_id")
            file_row["stripe_transfer_error"] = None
            file_row["transferred_at"] = file_row["transferred_at"] or _now()
            return None

        if sql.startswith("update settlement_files set transfer_status = 'skipped_negative_net'"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            file_row["transfer_status"] = "skipped_negative_net"
            file_row["stripe_transfer_error"] = None
            if "metadata_json" in params:
                file_row["metadata"] = json.loads(params["metadata_json"])
            return None

        if sql.startswith("update settlement_files set transfer_status = 'failed'"):
            file_row = self._file_by_id(int(params["settlement_file_id"]))
            file_row["transfer_status"] = "failed"
            file_row["stripe_transfer_error"] = params["stripe_transfer_error"]
            return None

        if sql.startswith("update settlement_snapshots"):
            snapshot = self._snapshot_by_id(int(params["snapshot_id"]))
            snapshot["settled_at"] = snapshot["settled_at"] or _now()
            snapshot["settled_via_file_id"] = int(params["settlement_file_id"])
            return None

        raise AssertionError(f"Unhandled execute query: {query}")

    def add_partner(
        self,
        *,
        active_rate_scope: str = "B",
        gmv_take_definition: str = "net",
        per_brand_tail_months: int = 36,
        churn_clawback_days: int = 90,
        nonpayment_clawback_days: int = 60,
        stripe_connect_account_id: str | None = "acct_partner",
        status: str = "active",
    ) -> int:
        partner_id = self._next_partner_id
        self._next_partner_id += 1
        self.channel_partners.append(
            {
                "id": partner_id,
                "active_rate_scope": active_rate_scope,
                "gmv_take_definition": gmv_take_definition,
                "per_brand_tail_months": per_brand_tail_months,
                "churn_clawback_days": churn_clawback_days,
                "nonpayment_clawback_days": nonpayment_clawback_days,
                "stripe_connect_account_id": stripe_connect_account_id,
                "status": status,
            }
        )
        return partner_id

    def add_attribution(
        self,
        *,
        merchant_id: str,
        partner_id: int,
        activated_at: datetime | None,
        status: str = "active",
    ) -> None:
        self.partner_attribution.append(
            {
                "merchant_id": merchant_id,
                "channel_partner_id": partner_id,
                "activated_at": activated_at,
                "status": status,
            }
        )

    def seed_default_scope_b_rates(self, partner_id: int) -> None:
        for brand_year, subscription_bp, credit_bp, gmv_bp in (
            (1, 2700, 1700, 3000),
            (2, 1700, 1200, 2200),
            (3, 700, 700, 1200),
        ):
            self.add_rate(
                partner_id=partner_id,
                stream="subscription",
                brand_year=brand_year,
                rate_bp=subscription_bp,
            )
            self.add_rate(
                partner_id=partner_id,
                stream="credit_overage",
                brand_year=brand_year,
                rate_bp=credit_bp,
            )
            self.add_rate(
                partner_id=partner_id,
                stream="gmv_take",
                brand_year=brand_year,
                rate_bp=gmv_bp,
            )

    def add_rate(
        self,
        *,
        partner_id: int,
        stream: str,
        brand_year: int,
        rate_bp: int,
        scope: str = "B",
        effective_from: date = date(2025, 1, 1),
        effective_to: date | None = None,
    ) -> int:
        rate_id = self._next_rate_id
        self._next_rate_id += 1
        self.partner_rate_schedules.append(
            {
                "id": rate_id,
                "channel_partner_id": partner_id,
                "scope": scope,
                "stream": stream,
                "brand_year": brand_year,
                "rate_bp": rate_bp,
                "effective_from": effective_from,
                "effective_to": effective_to,
            }
        )
        return rate_id

    def add_plan(
        self,
        *,
        name: str = "starter",
        price_cents: int = 9900,
        allowance: int = 4000,
    ) -> int:
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

    def add_merchant(
        self,
        merchant_id: str,
        *,
        stripe_customer_id: str | None = "cus_test_123",
    ) -> None:
        self.merchants.append(
            {"merchant_id": merchant_id, "stripe_customer_id": stripe_customer_id}
        )

    def add_subscription(
        self,
        *,
        merchant_id: str,
        plan_id: int,
        month: date,
        status: str = "active",
    ) -> int:
        subscription_id = self._next_subscription_id
        self._next_subscription_id += 1
        self.user_subscriptions.append(
            {
                "id": subscription_id,
                "merchant_id": merchant_id,
                "plan_id": plan_id,
                "status": status,
                "current_period_start": month,
                "current_period_end": _next_month(month),
                "started_at": month,
                "canceled_at": None,
                "created_at": month,
                "updated_at": month,
            }
        )
        return subscription_id

    def consume_credits(
        self,
        *,
        merchant_id: str,
        credits: int,
        occurred_at: datetime,
    ) -> str:
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
        gmv_channel: str,
        created_at: datetime,
        third_party_platform: str | None = None,
        third_party_platform_fee_pct: float | None = None,
    ) -> None:
        self.commerce_attribution_edges.append(
            {
                "edge_id": f"edge_{len(self.commerce_attribution_edges) + 1}",
                "merchant_id": merchant_id,
                "net_attributed_gmv_cents": net_attributed_gmv_cents,
                "gmv_channel": gmv_channel,
                "third_party_platform": third_party_platform,
                "third_party_platform_fee_pct": third_party_platform_fee_pct,
                "created_at": created_at,
            }
        )

    def statement(self, statement_id: int) -> dict[str, Any]:
        return dict(self._statement_by_id(statement_id))

    def invoice(self, invoice_id: int) -> dict[str, Any]:
        return dict(self._invoice_by_id(invoice_id))

    def mark_invoice_paid(self, invoice_id: int) -> None:
        """Simulate the Stripe invoice.paid webhook for an existing invoice row."""

        invoice = self._invoice_by_id(invoice_id)
        invoice["status"] = "paid"
        invoice["paid_at"] = datetime.now(timezone.utc)

    def file(self, file_id: int) -> dict[str, Any]:
        return dict(self._file_by_id(file_id))

    def snapshot(self, snapshot_id: int) -> dict[str, Any]:
        return dict(self._snapshot_by_id(snapshot_id))

    def _partner_by_id(self, partner_id: int) -> dict[str, Any]:
        for row in self.channel_partners:
            if int(row["id"]) == partner_id:
                return row
        raise AssertionError(f"Partner not found: {partner_id}")

    def _merchant_by_id(self, merchant_id: str) -> dict[str, Any] | None:
        return next(
            (row for row in self.merchants if row["merchant_id"] == merchant_id),
            None,
        )

    def _statement_by_id(self, statement_id: int) -> dict[str, Any]:
        for row in self.monthly_brand_statements:
            if int(row["id"]) == int(statement_id):
                return row
        raise AssertionError(f"Statement not found: {statement_id}")

    def _statement_for_month(
        self,
        merchant_id: str,
        calendar_month: date,
    ) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.monthly_brand_statements
                if row["merchant_id"] == merchant_id
                and row["calendar_month"] == calendar_month
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
        plan = next(
            row for row in self.subscription_plans if row["id"] == subscription["plan_id"]
        )
        return {
            "subscription_plan_id": plan["id"],
            "tier_name": plan["name"],
            "monthly_credit_allowance": plan["monthly_credit_allowance"],
            "subscription_revenue_usd_cents": plan["price_cents"],
        }

    def _latest_subscription_without_active_replacement(
        self,
        merchant_id: str,
    ) -> dict[str, Any] | None:
        if any(
            row["merchant_id"] == merchant_id
            and row["status"] in {"active", "trialing", "past_due"}
            for row in self.user_subscriptions
        ):
            return None
        return next(
            (row for row in self.user_subscriptions if row["merchant_id"] == merchant_id),
            None,
        )

    def _overdue_unpaid_invoice(
        self,
        merchant_id: str,
        nonpayment_days: int,
    ) -> dict[str, int] | None:
        threshold = datetime.now(timezone.utc).date() - timedelta(
            days=nonpayment_days
        )
        unpaid_statuses = {
            "draft",
            "finalizing",
            "finalized",
            "failed",
            "payment_failed",
            "uncollectible",
        }
        for invoice in self.invoices:
            if invoice["merchant_id"] != merchant_id:
                continue
            if invoice["status"] not in unpaid_statuses:
                continue
            invoice_anchor = invoice.get("due_date") or invoice.get("created_at")
            if _as_date(invoice_anchor) < threshold:
                return {"?column?": 1}
        return None

    def _gmv_aggregation(
        self,
        merchant_id: str,
        period_start: date,
        period_end: date,
    ) -> dict[str, Any]:
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
                int(row["net_attributed_gmv_cents"])
                for row in rows
                if row["gmv_channel"] == "personal_agent"
            ),
            "gmv_third_party_cents": sum(
                int(row["net_attributed_gmv_cents"])
                for row in rows
                if row["gmv_channel"] == "third_party_agent"
            ),
            "unclassified_edge_count": sum(1 for row in rows if row["gmv_channel"] is None),
        }

    def _rate_for_lookup(self, params: dict[str, Any]) -> dict[str, Any] | None:
        calendar_month = params["calendar_month"]
        candidates = [
            row
            for row in self.partner_rate_schedules
            if int(row["channel_partner_id"]) == int(params["partner_id"])
            and row["scope"] == params["scope"]
            and row["stream"] == params["stream"]
            and int(row["brand_year"]) == int(params["brand_year"])
            and row["effective_from"] <= calendar_month
            and (row["effective_to"] is None or row["effective_to"] > calendar_month)
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda row: (row["effective_from"], row["id"]))[-1]

    def _existing_snapshot(
        self,
        billing_run_id: int,
        channel_partner_id: int,
    ) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.settlement_snapshots
                if row["billing_run_id"] == billing_run_id
                and row["channel_partner_id"] == channel_partner_id
            ),
            None,
        )

    def _billing_run_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.billing_runs
                if row["idempotency_key"] == idempotency_key
            ),
            None,
        )

    def _billing_run_by_id(self, billing_run_id: int) -> dict[str, Any]:
        return next(row for row in self.billing_runs if row["id"] == billing_run_id)

    def _invoice_by_id(self, invoice_id: int) -> dict[str, Any]:
        return next(row for row in self.invoices if row["id"] == int(invoice_id))

    def _invoice_by_stripe_id(self, stripe_invoice_id: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.invoices
                if row["stripe_invoice_id"] == stripe_invoice_id
            ),
            None,
        )

    def _snapshot_by_id(self, snapshot_id: int) -> dict[str, Any]:
        return next(row for row in self.settlement_snapshots if row["id"] == snapshot_id)

    def _file_by_id(self, file_id: int) -> dict[str, Any]:
        return next(row for row in self.settlement_files if row["id"] == file_id)

    def _file_by_partner_month(
        self,
        partner_id: int,
        calendar_month: date,
    ) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.settlement_files
                if row["channel_partner_id"] == partner_id
                and row["calendar_month"] == calendar_month
            ),
            None,
        )


class _FakeStripeClient:
    def __init__(self) -> None:
        self.invoice_create_params: list[dict[str, Any]] = []
        self.invoice_item_create_params: list[dict[str, Any]] = []
        self.v1 = _FakeStripeV1(self)


class _FakeStripeV1:
    def __init__(self, parent: _FakeStripeClient) -> None:
        self.invoices = _FakeStripeInvoices(parent)
        self.invoice_items = _FakeStripeInvoiceItems(parent)


class _FakeStripeInvoices:
    def __init__(self, parent: _FakeStripeClient) -> None:
        self._parent = parent

    def create(self, *, params: dict[str, Any], options: dict[str, Any]) -> dict[str, str]:
        self._parent.invoice_create_params.append({"params": params, "options": options})
        return {"id": f"in_test_{len(self._parent.invoice_create_params)}"}


class _FakeStripeInvoiceItems:
    def __init__(self, parent: _FakeStripeClient) -> None:
        self._parent = parent

    def create(self, *, params: dict[str, Any], options: dict[str, Any]) -> dict[str, str]:
        self._parent.invoice_item_create_params.append(
            {"params": params, "options": options}
        )
        return {"id": f"ii_test_{len(self._parent.invoice_item_create_params)}"}


@pytest.fixture
def fake_db_full(monkeypatch: pytest.MonkeyPatch) -> _FakeFullDatabase:
    db = _FakeFullDatabase()
    fake_stripe = _FakeStripeClient()
    monkeypatch.setattr(engine, "database", db)
    monkeypatch.setattr(statements, "database", db)
    monkeypatch.setattr(statements, "IS_POSTGRES", False)
    monkeypatch.setattr(credit_overage_billing, "database", db)
    monkeypatch.setattr(credit_overage_billing, "stripe_client", fake_stripe)
    monkeypatch.setattr(partner_settlement_service, "database", db)
    monkeypatch.setattr(partner_settlement_service, "IS_POSTGRES", False)
    monkeypatch.setattr(settlement_file_service, "database", db)
    monkeypatch.setattr(settlement_file_service, "IS_POSTGRES", False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.delenv("SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING", raising=False)
    partner_settlement_service._TABLE_COLUMN_CACHE.clear()
    return db


async def test_brief_9_1_end_to_end_starter_brand_round_trip(
    fake_db_full: _FakeFullDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build brief section 9.1 worked example through the production service path."""

    month = date(2025, 6, 1)
    merchant_id = "merch_starter_e2e"
    plan_id = fake_db_full.add_plan(
        name="starter",
        price_cents=9900,
        allowance=4000,
    )
    partner_id = fake_db_full.add_partner(gmv_take_definition="net")
    fake_db_full.seed_default_scope_b_rates(partner_id)
    fake_db_full.add_merchant(merchant_id, stripe_customer_id="cus_starter_e2e")
    fake_db_full.add_subscription(merchant_id=merchant_id, plan_id=plan_id, month=month)
    fake_db_full.add_attribution(
        merchant_id=merchant_id,
        partner_id=partner_id,
        activated_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    fake_db_full.consume_credits(
        merchant_id=merchant_id,
        credits=5500,
        occurred_at=datetime(2025, 6, 15, tzinfo=timezone.utc),
    )
    fake_db_full.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=150000,
        gmv_channel="personal_agent",
        created_at=datetime(2025, 6, 16, tzinfo=timezone.utc),
    )
    fake_db_full.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=350000,
        gmv_channel="third_party_agent",
        third_party_platform="openai",
        third_party_platform_fee_pct=0.65,
        created_at=datetime(2025, 6, 17, tzinfo=timezone.utc),
    )

    statement_id = await statements.assemble_for_month(merchant_id, month)
    statement_row = fake_db_full.statement(statement_id)
    assert statement_row["status"] == "open"
    assert statement_row["subscription_revenue_usd_cents"] == 9900
    assert statement_row["bundled_credits_consumed"] == 4000
    assert statement_row["overage_credits"] == 1500
    assert statement_row["overage_revenue_usd_cents"] == 1950
    assert statement_row["gmv_usd_cents"] == 500000
    assert statement_row["gmv_personal_usd_cents"] == 150000
    assert statement_row["gmv_third_party_usd_cents"] == 350000
    assert statement_row["pivota_gmv_take_usd_cents"] == 50000

    await statements.freeze(statement_id)
    assert fake_db_full.statement(statement_id)["status"] == "frozen"

    invoice_id = await credit_overage_billing.create_overage_invoice(statement_id)
    invoice = fake_db_full.invoice(invoice_id)
    assert invoice["total_cents"] == 1950
    assert fake_db_full.statement(statement_id)["status"] == "invoiced"
    assert fake_db_full.billing_run_items[0]["description"] == (
        "1,500 credits overage \u2014 $19.50"
    )

    # Simulate the Stripe `invoice.paid` webhook firing after Stripe collects
    # the customer. In production the settlement compute on day 10 happens
    # AFTER Stripe payment, so the overage invoice is 'paid' by then. Without
    # this mark, the v2 engine's nonpayment-suspension check (PR #6) sees a
    # stalled draft invoice and zeros the brand's accrual. Known limitation
    # of the CURRENT_DATE-based threshold in PR #6 \u2014 flagged for follow-up.
    fake_db_full.mark_invoice_paid(invoice_id)

    comp = await engine.compute_partner_comp_v2(partner_id, month, date(2025, 7, 1))
    accrual = comp["merchant_accruals"][merchant_id]
    assert accrual["brand_year"] == 1
    assert accrual["subscription_share_cents"] == 2673
    assert accrual["credit_overage_share_cents"] == 332
    assert accrual["gmv_share_cents"] == 8040
    assert accrual["gross_comp_cents"] == 11045
    assert comp["net_comp_cents"] == 11045

    billing_run_id = int(invoice["billing_run_id"])
    snapshot_id = await partner_settlement_service.write_settlement_snapshot(
        billing_run_id,
        partner_id,
        comp,
    )

    file_id = await settlement_file_service.generate(
        channel_partner_id=partner_id,
        calendar_month=month,
    )
    file_row = fake_db_full.file(file_id)
    assert file_row["transfer_amount_cents"] == 11045
    assert file_row["source_snapshot_ids_jsonb"] == [snapshot_id]

    transfer_calls: list[dict[str, Any]] = []

    def fake_transfer_create(**kwargs: Any) -> dict[str, str]:
        assert not fake_db_full.in_transaction
        transfer_calls.append(kwargs)
        return {"id": "tr_test_123"}

    monkeypatch.setattr(
        settlement_file_service.stripe.Transfer,
        "create",
        fake_transfer_create,
    )

    await settlement_file_service.transfer(settlement_file_id=file_id)

    transferred_file = fake_db_full.file(file_id)
    settled_snapshot = fake_db_full.snapshot(snapshot_id)
    assert transfer_calls[0]["amount"] == 11045
    assert transfer_calls[0]["destination"] == "acct_partner"
    assert transfer_calls[0]["idempotency_key"] == "settlement:partner_1:month_2025-06"
    assert transferred_file["transfer_status"] == "transferred"
    assert transferred_file["stripe_transfer_id"] == "tr_test_123"
    assert settled_snapshot["settled_at"] is not None
    assert settled_snapshot["settled_via_file_id"] == file_id


async def test_brief_9_2_no_dollar_for_credits_regex_sweep() -> None:
    """Build brief section 9.2 static sweep over non-admin route modules."""

    violations = _collect_route_credit_dollar_violations()
    description = credit_overage_billing._overage_line_description(
        overage_credits=1500,
        overage_revenue_usd_cents=1950,
    )
    assert description == "1,500 credits overage \u2014 $19.50"
    forbidden_description_terms = ("per credit", "at $", "rate", "premium")
    assert not any(term in description.lower() for term in forbidden_description_terms)

    assert violations == [], "\n".join(violations)


def _collect_route_credit_dollar_violations() -> list[str]:
    routes_dir = Path(__file__).resolve().parents[1] / "routes"
    allow_list: set[str] = set()
    violations: list[str] = []
    for path in sorted(routes_dir.glob("*.py")):
        if path.name.startswith("admin_") or path.name in allow_list:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        model_fields = _model_fields_by_class(tree)
        route_functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _has_route_decorator(node)
        ]
        for function in sorted(route_functions, key=lambda node: node.lineno):
            for field_name, lineno in _response_model_field_names(function, model_fields):
                if _BANNED_FIELD_RE.search(field_name):
                    violations.append(
                        f"{path}:{lineno}: banned response field name {field_name!r}"
                    )
            for field_name, lineno in _dict_key_field_names(function):
                if _BANNED_FIELD_RE.search(field_name):
                    violations.append(
                        f"{path}:{lineno}: banned response key {field_name!r}"
                    )
            for value, lineno in _string_literals(function):
                if _DOLLAR_CREDIT_RE.search(value):
                    violations.append(
                        f"{path}:{lineno}: dollar-credit string {value!r}"
                    )
                if _BANNED_FIELD_RE.search(value):
                    violations.append(
                        f"{path}:{lineno}: banned field-name string {value!r}"
                    )
    return violations


def _model_fields_by_class(tree: ast.AST) -> dict[str, list[tuple[str, int]]]:
    fields_by_class: dict[str, list[tuple[str, int]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        fields: list[tuple[str, int]] = []
        for child in node.body:
            if isinstance(child, ast.AnnAssign):
                name = _target_name(child.target)
                if name:
                    fields.append((name, child.lineno))
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    name = _target_name(target)
                    if name:
                        fields.append((name, child.lineno))
        fields_by_class[node.name] = fields
    return fields_by_class


def _response_model_field_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    model_fields: dict[str, list[tuple[str, int]]],
) -> list[tuple[str, int]]:
    fields: list[tuple[str, int]] = []
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or not _is_route_call(decorator):
            continue
        for keyword in decorator.keywords:
            if keyword.arg != "response_model":
                continue
            for model_name in _model_names(keyword.value):
                fields.extend(model_fields.get(model_name, []))
    return fields


def _dict_key_field_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, int]]:
    keys: list[tuple[str, int]] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.append((key.value, key.lineno))
    return keys


def _string_literals(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, int]]:
    strings: list[tuple[str, int]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append((node.value, node.lineno))
    return strings


def _has_route_decorator(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Call) and _is_route_call(decorator)
        for decorator in function.decorator_list
    )


def _is_route_call(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    return call.func.attr in {"get", "post", "put", "patch", "delete", "options"}


def _model_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Subscript):
        return _model_names(node.value) + _model_names(node.slice)
    if isinstance(node, ast.Tuple):
        names: list[str] = []
        for elt in node.elts:
            names.extend(_model_names(elt))
        return names
    if isinstance(node, ast.List):
        names = []
        for elt in node.elts:
            names.extend(_model_names(elt))
        return names
    return []


def _target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


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


def _now() -> datetime:
    return datetime.now(timezone.utc)

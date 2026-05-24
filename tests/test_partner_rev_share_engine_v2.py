from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any

import pytest

from services import partner_rev_share_engine_v2 as engine
from services import partner_settlement_service
from services.billing import monthly_brand_statements_service as statements


pytestmark = pytest.mark.asyncio


class _FakeV2Database:
    def __init__(self) -> None:
        self.subscription_plans: list[dict[str, Any]] = []
        self.merchants: list[dict[str, Any]] = []
        self.user_subscriptions: list[dict[str, Any]] = []
        self.credit_ledger: list[dict[str, Any]] = []
        self.commerce_attribution_edges: list[dict[str, Any]] = []
        self.monthly_brand_statements: list[dict[str, Any]] = []
        self.invoices: list[dict[str, Any]] = []
        self.channel_partners: list[dict[str, Any]] = []
        self.partner_attribution: list[dict[str, Any]] = []
        self.partner_rate_schedules: list[dict[str, Any]] = []
        self.settlement_snapshots: list[dict[str, Any]] = []
        self._next_plan_id = 1
        self._next_subscription_id = 1
        self._next_credit_ledger_id = 1
        self._next_statement_id = 1
        self._next_partner_id = 1
        self._next_rate_id = 1
        self._next_snapshot_id = 1

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch_all(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "from credit_ledger" in sql:
            merchant_id = params["merchant_id"]
            period_start = params["period_start"]
            period_end = params["period_end"]
            rows = [
                row
                for row in self.credit_ledger
                if row["merchant_id"] == merchant_id
                and row["credits_delta"] < 0
                and period_start <= _as_date(row["occurred_at"]) < period_end
            ]
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

        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if sql.startswith("select id, active_rate_scope"):
            return self._partner_by_id(int(params["channel_partner_id"]))

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

        if sql.startswith("update monthly_brand_statements") and "subscription_plan_id =" in sql:
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
            statement["status"] = "invoiced"
            statement["invoiced_at"] = datetime.now(timezone.utc)
            statement["overage_invoice_id"] = params.get("overage_invoice_id")
            return {"id": statement["id"]}

        if sql.startswith("select id from invoices where id"):
            return next((row for row in self.invoices if row["id"] == params["invoice_id"]), None)

        if sql.startswith("select rate_bp, id"):
            return self._rate_for_lookup(params)

        if sql.startswith("select id from settlement_snapshots"):
            return self._existing_snapshot(
                int(params["billing_run_id"]),
                int(params["channel_partner_id"]),
            )

        if sql.startswith("insert into settlement_snapshots"):
            payload = json.loads(params["snapshot_payload_json"])
            snapshot = {
                "id": self._next_snapshot_id,
                "billing_run_id": int(params["billing_run_id"]),
                "channel_partner_id": int(params["channel_partner_id"]),
                "snapshot_payload_jsonb": payload,
                "computed_comp_cents": int(params["computed_comp_cents"]),
                "subsidy_cap_remaining_cents": params[
                    "subsidy_cap_remaining_cents"
                ],
            }
            self._next_snapshot_id += 1
            self.settlement_snapshots.append(snapshot)
            return {"id": snapshot["id"]}

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    def add_partner(
        self,
        *,
        active_rate_scope: str = "B",
        gmv_take_definition: str = "net",
        per_brand_tail_months: int = 36,
    ) -> int:
        partner_id = self._next_partner_id
        self._next_partner_id += 1
        self.channel_partners.append(
            {
                "id": partner_id,
                "active_rate_scope": active_rate_scope,
                "gmv_take_definition": gmv_take_definition,
                "per_brand_tail_months": per_brand_tail_months,
            }
        )
        return partner_id

    def update_partner(self, partner_id: int, **updates: Any) -> None:
        self._partner_by_id(partner_id).update(updates)

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

    def seed_channel_tiered_gmv_rates(self, partner_id: int) -> None:
        self.add_rate(
            partner_id=partner_id,
            stream="gmv_take_personal",
            brand_year=1,
            rate_bp=3000,
        )
        self.add_rate(
            partner_id=partner_id,
            stream="gmv_take_third_party",
            brand_year=1,
            rate_bp=1200,
        )

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

    def add_merchant(self, merchant_id: str) -> None:
        if not any(row["merchant_id"] == merchant_id for row in self.merchants):
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

    def consume_credits(
        self,
        *,
        merchant_id: str,
        credits: int,
        occurred_at: datetime,
    ) -> int:
        ledger_id = self._next_credit_ledger_id
        self._next_credit_ledger_id += 1
        self.credit_ledger.append(
            {
                "id": ledger_id,
                "merchant_id": merchant_id,
                "operation_type": "operation_commit",
                "operation_id": f"op_{ledger_id}",
                "credits_delta": -credits,
                "balance_after": 0,
                "occurred_at": occurred_at,
                "source_type": "operation_commit",
                "metadata": {},
            }
        )
        return ledger_id

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

    def _partner_by_id(self, partner_id: int) -> dict[str, Any]:
        for row in self.channel_partners:
            if int(row["id"]) == partner_id:
                return row
        raise AssertionError(f"Partner not found: {partner_id}")

    def _statement_by_id(self, statement_id: int) -> dict[str, Any]:
        for row in self.monthly_brand_statements:
            if int(row["id"]) == statement_id:
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
        plan = next(row for row in self.subscription_plans if row["id"] == subscription["plan_id"])
        return {
            "subscription_plan_id": plan["id"],
            "tier_name": plan["name"],
            "monthly_credit_allowance": plan["monthly_credit_allowance"],
            "subscription_revenue_usd_cents": plan["price_cents"],
        }

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


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeV2Database:
    db = _FakeV2Database()
    monkeypatch.setattr(engine, "database", db)
    monkeypatch.setattr(statements, "database", db)
    monkeypatch.setattr(statements, "IS_POSTGRES", False)
    monkeypatch.setattr(partner_settlement_service, "database", db)
    monkeypatch.setattr(partner_settlement_service, "IS_POSTGRES", False)
    partner_settlement_service._TABLE_COLUMN_CACHE.clear()
    return db


async def test_brief_9_1_starter_y1_scope_b_net_definition(
    fake_db: _FakeV2Database,
) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_starter"
    partner_id = fake_db.add_partner(gmv_take_definition="net")
    fake_db.add_attribution(
        merchant_id=merchant_id,
        partner_id=partner_id,
        activated_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    fake_db.seed_default_scope_b_rates(partner_id)
    await _assemble_and_freeze_statement(fake_db, merchant_id, month)

    comp = await engine.compute_partner_comp_v2(partner_id, month, date(2025, 7, 1))
    accrual = comp["merchant_accruals"][merchant_id]

    assert accrual["brand_year"] == 1
    assert accrual["subscription_share_cents"] == 2673
    assert accrual["credit_overage_share_cents"] == 332
    assert accrual["gmv_share_cents"] == 8040
    assert accrual["gross_comp_cents"] == 11045
    assert comp["subscription_rev_cents"] == 2673
    assert comp["credit_overage_rev_cents"] == 332
    assert comp["gmv_take_rev_cents"] == 8040
    assert comp["net_comp_cents"] == 11045


async def test_tail_boundary_month_36_is_paid_month_37_is_zero(
    fake_db: _FakeV2Database,
) -> None:
    merchant_id = "merch_tail"
    partner_id = fake_db.add_partner(gmv_take_definition="net")
    fake_db.add_attribution(
        merchant_id=merchant_id,
        partner_id=partner_id,
        activated_at=datetime(2025, 4, 1, tzinfo=timezone.utc),
    )
    fake_db.seed_default_scope_b_rates(partner_id)
    await _assemble_and_freeze_statement(fake_db, merchant_id, date(2028, 4, 1))
    await _assemble_and_freeze_statement(fake_db, merchant_id, date(2028, 5, 1))

    paid_comp = await engine.compute_partner_comp_v2(
        partner_id,
        date(2028, 4, 1),
        date(2028, 5, 1),
    )
    exhausted_comp = await engine.compute_partner_comp_v2(
        partner_id,
        date(2028, 5, 1),
        date(2028, 6, 1),
    )

    paid_accrual = paid_comp["merchant_accruals"][merchant_id]
    exhausted_accrual = exhausted_comp["merchant_accruals"][merchant_id]
    assert paid_accrual["brand_year"] == 3
    assert paid_accrual["tail_exhausted"] is False
    assert paid_accrual["gross_comp_cents"] > 0
    assert exhausted_accrual["tail_exhausted"] is True
    assert exhausted_accrual["gross_comp_cents"] == 0
    assert exhausted_comp["net_comp_cents"] == 0
    assert exhausted_comp["v2_metadata"]["brand_count_skipped_tail_exhausted"] == 1


async def test_non_retroactive_snapshot_keeps_pre_flag_rates(
    fake_db: _FakeV2Database,
) -> None:
    month = date(2025, 5, 1)
    merchant_id = "merch_snapshot"
    partner_id = fake_db.add_partner(gmv_take_definition="gross")
    fake_db.add_attribution(
        merchant_id=merchant_id,
        partner_id=partner_id,
        activated_at=datetime(2025, 5, 1, tzinfo=timezone.utc),
    )
    fake_db.seed_default_scope_b_rates(partner_id)
    await _assemble_and_freeze_statement(fake_db, merchant_id, month)

    comp = await engine.compute_partner_comp_v2(partner_id, month, date(2025, 6, 1))
    await partner_settlement_service.write_settlement_snapshot(101, partner_id, comp)
    snapshot_payload = fake_db.settlement_snapshots[0]["snapshot_payload_jsonb"]
    snapshot_accrual = snapshot_payload["merchant_accruals"][merchant_id]

    fake_db.update_partner(partner_id, gmv_take_definition="net")
    persisted_payload = fake_db.settlement_snapshots[0]["snapshot_payload_jsonb"]
    persisted_accrual = persisted_payload["merchant_accruals"][merchant_id]

    assert snapshot_payload["v2_metadata"]["gmv_take_definition"] == "gross"
    assert snapshot_accrual["gmv_share_cents"] == 15000
    assert _rate_for_stream(snapshot_accrual, "gmv_take")["rate_bp"] == 3000
    assert persisted_payload["v2_metadata"]["gmv_take_definition"] == "gross"
    assert persisted_accrual["gmv_share_cents"] == 15000
    assert _rate_for_stream(persisted_accrual, "gmv_take")["rate_bp"] == 3000


async def test_brand_with_no_activation_yields_zero(
    fake_db: _FakeV2Database,
) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_no_activation"
    partner_id = fake_db.add_partner(gmv_take_definition="net")
    fake_db.add_attribution(
        merchant_id=merchant_id,
        partner_id=partner_id,
        activated_at=None,
    )
    fake_db.seed_default_scope_b_rates(partner_id)
    await _assemble_and_freeze_statement(fake_db, merchant_id, month)

    comp = await engine.compute_partner_comp_v2(partner_id, month, date(2025, 7, 1))
    accrual = comp["merchant_accruals"][merchant_id]

    assert accrual["brand_year"] == 0
    assert accrual["gross_comp_cents"] == 0
    assert accrual["resolved_rates"] == []
    assert comp["v2_metadata"]["brand_count_skipped_no_activation"] == 1
    assert comp["net_comp_cents"] == 0


async def test_brand_with_no_statement_yields_zero(
    fake_db: _FakeV2Database,
) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_no_statement"
    partner_id = fake_db.add_partner(gmv_take_definition="net")
    fake_db.add_attribution(
        merchant_id=merchant_id,
        partner_id=partner_id,
        activated_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    fake_db.seed_default_scope_b_rates(partner_id)

    comp = await engine.compute_partner_comp_v2(partner_id, month, date(2025, 7, 1))
    accrual = comp["merchant_accruals"][merchant_id]

    assert accrual["brand_year"] == 1
    assert accrual["tail_exhausted"] is False
    assert accrual["gross_comp_cents"] == 0
    assert comp["v2_metadata"]["brand_count_computed"] == 0


async def test_gmv_take_definition_gross_simple_path(
    fake_db: _FakeV2Database,
) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_gross"
    partner_id = fake_db.add_partner(gmv_take_definition="gross")
    fake_db.add_attribution(
        merchant_id=merchant_id,
        partner_id=partner_id,
        activated_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    fake_db.seed_default_scope_b_rates(partner_id)
    await _assemble_and_freeze_statement(fake_db, merchant_id, month)

    comp = await engine.compute_partner_comp_v2(partner_id, month, date(2025, 7, 1))
    accrual = comp["merchant_accruals"][merchant_id]

    assert accrual["gmv_share_definition_applied"] == "gross"
    assert accrual["gmv_share_cents"] == 15000
    assert accrual["gross_comp_cents"] == 18005


async def test_gmv_take_definition_channel_tiered(
    fake_db: _FakeV2Database,
) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_tiered"
    partner_id = fake_db.add_partner(gmv_take_definition="channel_tiered")
    fake_db.add_attribution(
        merchant_id=merchant_id,
        partner_id=partner_id,
        activated_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    fake_db.seed_default_scope_b_rates(partner_id)
    fake_db.seed_channel_tiered_gmv_rates(partner_id)
    await _assemble_and_freeze_statement(fake_db, merchant_id, month)

    comp = await engine.compute_partner_comp_v2(partner_id, month, date(2025, 7, 1))
    accrual = comp["merchant_accruals"][merchant_id]

    assert accrual["gmv_share_definition_applied"] == "channel_tiered"
    assert accrual["gmv_share_cents"] == 8700
    assert accrual["gross_comp_cents"] == 11705
    resolved_streams = {row["stream"] for row in accrual["resolved_rates"]}
    assert "gmv_take_personal" in resolved_streams
    assert "gmv_take_third_party" in resolved_streams
    assert "gmv_take" not in resolved_streams


async def test_rate_schedule_effective_from_window(
    fake_db: _FakeV2Database,
) -> None:
    month = date(2025, 6, 1)
    merchant_id = "merch_rate_window"
    partner_id = fake_db.add_partner(gmv_take_definition="gross")
    fake_db.add_attribution(
        merchant_id=merchant_id,
        partner_id=partner_id,
        activated_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    old_rate_id = fake_db.add_rate(
        partner_id=partner_id,
        stream="subscription",
        brand_year=1,
        rate_bp=1000,
        effective_from=date(2025, 1, 1),
    )
    new_rate_id = fake_db.add_rate(
        partner_id=partner_id,
        stream="subscription",
        brand_year=1,
        rate_bp=2700,
        effective_from=date(2025, 6, 1),
    )
    await _assemble_and_freeze_statement(fake_db, merchant_id, month)

    comp = await engine.compute_partner_comp_v2(partner_id, month, date(2025, 7, 1))
    accrual = comp["merchant_accruals"][merchant_id]
    subscription_rate = _rate_for_stream(accrual, "subscription")

    assert old_rate_id != new_rate_id
    assert accrual["subscription_share_cents"] == 2673
    assert subscription_rate["schedule_row_id"] == new_rate_id
    assert accrual["credit_overage_share_cents"] == 0
    assert accrual["gmv_share_cents"] == 0


async def _assemble_and_freeze_statement(
    fake_db: _FakeV2Database,
    merchant_id: str,
    month: date,
) -> int:
    plan_id = fake_db.add_plan(name="starter", price_cents=9900, allowance=4000)
    fake_db.add_merchant(merchant_id)
    fake_db.add_subscription(merchant_id=merchant_id, plan_id=plan_id, month=month)
    fake_db.consume_credits(
        merchant_id=merchant_id,
        credits=5500,
        occurred_at=datetime(month.year, month.month, 15, tzinfo=timezone.utc),
    )
    fake_db.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=150000,
        gmv_channel="personal_agent",
        created_at=datetime(month.year, month.month, 16, tzinfo=timezone.utc),
    )
    fake_db.add_gmv_edge(
        merchant_id=merchant_id,
        net_attributed_gmv_cents=350000,
        gmv_channel="third_party_agent",
        third_party_platform="openai",
        third_party_platform_fee_pct=0.65,
        created_at=datetime(month.year, month.month, 17, tzinfo=timezone.utc),
    )
    statement_id = await statements.assemble_for_month(merchant_id, month)
    await statements.freeze(statement_id)
    return statement_id


def _rate_for_stream(accrual: dict[str, Any], stream: str) -> dict[str, Any]:
    for row in accrual["resolved_rates"]:
        if row["stream"] == stream:
            return row
    raise AssertionError(f"Missing resolved rate for stream {stream}")


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

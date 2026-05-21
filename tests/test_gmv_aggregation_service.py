from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pytest

import services.gmv_aggregation_service as service


class FakeTransaction:
    def __init__(self, db: "FakeDB") -> None:
        self.db = db

    async def __aenter__(self) -> "FakeTransaction":
        self.db.transaction_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeDB:
    def __init__(
        self,
        *,
        edges: list[dict[str, Any]],
        promo_until_by_merchant: Optional[dict[str, datetime | None]] = None,
    ) -> None:
        self.edges = edges
        self.promo_until_by_merchant = promo_until_by_merchant or {}
        self.daily: dict[tuple[Any, str, str, int], dict[str, Any]] = {}
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.fetch_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.fetch_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.transaction_count = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def fetch_all(self, query: str, values: Optional[dict[str, Any]] = None):
        params = dict(values or {})
        self.fetch_all_calls.append((str(query), params))
        if "FROM commerce_attribution_edges e" not in str(query):
            raise AssertionError(f"Unexpected fetch_all query: {query}")
        return self._rollup(params["date"], params.get("merchant_id"))

    async def fetch_one(self, query: str, values: Optional[dict[str, Any]] = None):
        params = dict(values or {})
        self.fetch_one_calls.append((str(query), params))
        sql = str(query)
        if "FOR UPDATE" in sql:
            edge_id = params["edge_id"]
            for edge in self.edges:
                if edge["edge_id"] == edge_id:
                    return {
                        "edge_id": edge["edge_id"],
                        "merchant_id": edge["merchant_id"],
                        "created_at": edge["created_at"],
                    }
            return None

        if "promo_period_until" in sql:
            merchant_id = params["merchant_id"]
            if merchant_id not in self.promo_until_by_merchant:
                return None
            return {"promo_period_until": self.promo_until_by_merchant[merchant_id]}

        raise AssertionError(f"Unexpected fetch_one query: {query}")

    async def execute(self, query: str, values: Optional[dict[str, Any]] = None):
        params = dict(values or {})
        sql = str(query)
        self.executed.append((sql, params))

        if "INSERT INTO gmv_attribution_daily" in sql:
            key = (
                params["date"],
                params["merchant_id"],
                params.get("agent_id") or "",
                params.get("channel_partner_id") if params.get("channel_partner_id") is not None else -1,
            )
            self.daily[key] = dict(params)
            return None

        if "UPDATE commerce_attribution_edges" in sql:
            for edge in self.edges:
                if edge["edge_id"] == params["edge_id"]:
                    edge["refund_amount_cents"] = int(edge.get("refund_amount_cents") or 0) + int(
                        params["refund_amount_cents"]
                    )
                    edge["refunded_at"] = edge.get("refunded_at") or datetime.now(timezone.utc)
                    return None
            raise AssertionError("Refund update missed edge")

        raise AssertionError(f"Unexpected execute query: {query}")

    def _rollup(self, target_date: date, merchant_id: Optional[str]):
        groups: dict[tuple[Any, str, Any, Any], dict[str, Any]] = {}
        for edge in self.edges:
            created_at = edge["created_at"]
            edge_date = created_at.date() if isinstance(created_at, datetime) else created_at
            if edge_date != target_date:
                continue
            if merchant_id is not None and edge["merchant_id"] != merchant_id:
                continue
            if edge.get("gross_attributed_gmv_cents") is None:
                continue

            key = (
                edge_date,
                edge["merchant_id"],
                edge.get("agent_id"),
                edge.get("channel_partner_id"),
            )
            row = groups.setdefault(
                key,
                {
                    "date": edge_date,
                    "merchant_id": edge["merchant_id"],
                    "agent_id": edge.get("agent_id"),
                    "channel_partner_id": edge.get("channel_partner_id"),
                    "gross_sum": 0,
                    "refund_sum": 0,
                },
            )
            row["gross_sum"] += int(edge.get("gross_attributed_gmv_cents") or 0)
            row["refund_sum"] += int(edge.get("refund_amount_cents") or 0)
        return list(groups.values())


@pytest.fixture(autouse=True)
def clear_promo_cache() -> None:
    service._promo_cache.clear()


def _edge(
    *,
    gross: int,
    refund: int = 0,
    edge_id: str = "edge_1",
    merchant_id: str = "merch_1",
    agent_id: Optional[str] = "agent_1",
    channel_partner_id: Optional[int] = None,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "edge_id": edge_id,
        "merchant_id": merchant_id,
        "agent_id": agent_id,
        "channel_partner_id": channel_partner_id,
        "gross_attributed_gmv_cents": gross,
        "refund_amount_cents": refund,
        "created_at": created_at,
        "refunded_at": None,
    }


def _only_daily_row(fake_db: FakeDB) -> dict[str, Any]:
    assert len(fake_db.daily) == 1
    return next(iter(fake_db.daily.values()))


@pytest.mark.asyncio
async def test_aggregate_daily_simple_one_edge_no_refunds(monkeypatch: pytest.MonkeyPatch) -> None:
    target_date = date(2026, 5, 19)
    future_promo = datetime.now(timezone.utc) + timedelta(days=1)
    fake_db = FakeDB(
        edges=[_edge(gross=10_000, created_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc))],
        promo_until_by_merchant={"merch_1": future_promo},
    )
    monkeypatch.setattr(service, "database", fake_db)

    row_count = await service.aggregate_daily(target_date)

    assert row_count == 1
    row = _only_daily_row(fake_db)
    assert row["gross_attributed_gmv_cents"] == 10_000
    assert row["refund_amount_cents"] == 0
    assert row["net_attributed_gmv_cents"] == 10_000
    assert row["take_rate_bp"] == 500
    assert row["take_amount_cents"] == 500
    assert "orders.total" not in service._ROLLUP_QUERY
    assert "shipping_fee" not in service._ROLLUP_QUERY

    upsert_sql = next(sql for sql, _ in fake_db.executed if "INSERT INTO gmv_attribution_daily" in sql)
    assert (
        "ON CONFLICT (date, merchant_id, COALESCE(agent_id, ''), "
        "COALESCE(channel_partner_id, -1))"
    ) in upsert_sql


@pytest.mark.asyncio
async def test_aggregate_daily_partial_refund_reduces_net(monkeypatch: pytest.MonkeyPatch) -> None:
    target_date = date(2026, 5, 19)
    fake_db = FakeDB(
        edges=[
            _edge(
                gross=10_000,
                refund=2_500,
                created_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc),
            )
        ]
    )
    monkeypatch.setattr(service, "database", fake_db)

    assert await service.aggregate_daily(target_date) == 1

    row = _only_daily_row(fake_db)
    assert row["gross_attributed_gmv_cents"] == 10_000
    assert row["refund_amount_cents"] == 2_500
    assert row["net_attributed_gmv_cents"] == 7_500
    assert row["take_rate_bp"] == 1000
    assert row["take_amount_cents"] == 750


@pytest.mark.asyncio
async def test_aggregate_daily_full_refund_sets_net_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    target_date = date(2026, 5, 19)
    fake_db = FakeDB(
        edges=[
            _edge(
                gross=10_000,
                refund=10_000,
                created_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc),
            )
        ]
    )
    monkeypatch.setattr(service, "database", fake_db)

    assert await service.aggregate_daily(target_date) == 1

    row = _only_daily_row(fake_db)
    assert row["net_attributed_gmv_cents"] == 0
    assert row["take_amount_cents"] == 0


@pytest.mark.asyncio
async def test_aggregate_daily_refund_larger_than_gross_keeps_net_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    target_date = date(2026, 5, 19)
    fake_db = FakeDB(
        edges=[
            _edge(
                gross=10_000,
                refund=12_500,
                created_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc),
            )
        ]
    )
    monkeypatch.setattr(service, "database", fake_db)

    assert await service.aggregate_daily(target_date) == 1

    row = _only_daily_row(fake_db)
    assert row["refund_amount_cents"] == 12_500
    assert row["net_attributed_gmv_cents"] == 0
    assert row["take_amount_cents"] == 0


@pytest.mark.asyncio
async def test_promo_period_in_past_uses_standard_take_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    target_date = date(2026, 5, 19)
    past_promo = datetime.now(timezone.utc) - timedelta(days=1)
    fake_db = FakeDB(
        edges=[_edge(gross=10_000, created_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc))],
        promo_until_by_merchant={"merch_1": past_promo},
    )
    monkeypatch.setattr(service, "database", fake_db)

    assert await service.aggregate_daily(target_date) == 1

    row = _only_daily_row(fake_db)
    assert row["take_rate_bp"] == 1000
    assert row["take_amount_cents"] == 1_000


@pytest.mark.asyncio
async def test_aggregate_daily_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    target_date = date(2026, 5, 19)
    fake_db = FakeDB(
        edges=[
            _edge(
                gross=10_000,
                refund=1_000,
                agent_id=None,
                channel_partner_id=None,
                created_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc),
            )
        ]
    )
    monkeypatch.setattr(service, "database", fake_db)

    first_count = await service.aggregate_daily(target_date)
    first_row = dict(_only_daily_row(fake_db))
    second_count = await service.aggregate_daily(target_date)
    second_row = dict(_only_daily_row(fake_db))

    assert first_count == 1
    assert second_count == 1
    assert len(fake_db.daily) == 1
    assert first_row == second_row


@pytest.mark.asyncio
async def test_apply_refund_recomputes_daily_rollup(monkeypatch: pytest.MonkeyPatch) -> None:
    target_date = date(2026, 5, 19)
    fake_db = FakeDB(
        edges=[_edge(gross=10_000, refund=0, created_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc))]
    )
    monkeypatch.setattr(service, "database", fake_db)
    await service.aggregate_daily(target_date)

    recompute_calls: list[tuple[date, str]] = []
    original_recompute = service.recompute_for_date

    async def spy_recompute_for_date(date: date, merchant_id: str) -> None:
        recompute_calls.append((date, merchant_id))
        await original_recompute(date, merchant_id)

    monkeypatch.setattr(service, "recompute_for_date", spy_recompute_for_date)

    await service.apply_refund("edge_1", 2_500)

    assert recompute_calls == [(target_date, "merch_1")]
    assert fake_db.edges[0]["refund_amount_cents"] == 2_500
    row = _only_daily_row(fake_db)
    assert row["gross_attributed_gmv_cents"] == 10_000
    assert row["refund_amount_cents"] == 2_500
    assert row["net_attributed_gmv_cents"] == 7_500
    assert row["take_amount_cents"] == 750

    edge_update_sql = next(sql for sql, _ in fake_db.executed if "UPDATE commerce_attribution_edges" in sql)
    assert "net_attributed_gmv_cents" not in edge_update_sql


def test_rollup_query_casts_merchant_id_to_text() -> None:
    """Regression: bug C surfaced in Step 6 staging run 2026-05-21.

    asyncpg cannot infer the type of `:merchant_id` when used in
    `:merchant_id IS NULL` — the query fails with a parameter-typing error
    against a real Postgres connection. The cron path (aggregate_daily
    with no merchant filter) was broken because of this.

    The fix is `CAST(:merchant_id AS TEXT) IS NULL`. Guard that the query
    keeps using the CAST form so we don't quietly regress.
    """
    assert "CAST(:merchant_id AS TEXT) IS NULL" in service._ROLLUP_QUERY
    assert "CAST(:merchant_id AS TEXT)" in service._ROLLUP_QUERY


def test_subscription_period_helper_reads_items_data_first() -> None:
    """Regression: bug B surfaced in Step 6 staging run 2026-05-21.

    Post-2025 Stripe API moved `current_period_start/end` from the Subscription
    object to its per-item rows (`subscription.items.data[0]`). Old code paths
    that read `subscription.current_period_start` get NULL on modern accounts.
    The fix uses an items-first / top-level-fallback helper.

    Imported lazily because billing_routes pulls in FastAPI + database + stripe.
    """
    from routes.billing_routes import _extract_subscription_period

    # Modern API shape — fields on subscription.items.data[0]
    modern = {
        "items": {
            "data": [
                {"current_period_start": 1700000000, "current_period_end": 1702592000},
            ],
        },
        # Top-level fields absent (or stale).
    }
    assert _extract_subscription_period(modern) == (1700000000, 1702592000)

    # Legacy API shape — fields on the subscription itself
    legacy = {"current_period_start": 1500000000, "current_period_end": 1502592000, "items": {"data": []}}
    assert _extract_subscription_period(legacy) == (1500000000, 1502592000)

    # Mixed shape — items.data wins
    mixed = {
        "current_period_start": 1500000000,
        "current_period_end": 1502592000,
        "items": {
            "data": [
                {"current_period_start": 1700000000, "current_period_end": 1702592000},
            ],
        },
    }
    assert _extract_subscription_period(mixed) == (1700000000, 1702592000)

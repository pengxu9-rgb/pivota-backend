from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

import pytest

import services.psp_payment_finalizer as finalizer


class FakeAttributionDatabase:
    def __init__(self, edges: list[dict[str, Any]]) -> None:
        self.edges = edges
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, query: str, values: Optional[dict[str, Any]] = None) -> int:
        params = dict(values or {})
        sql = str(query)
        self.executed.append((sql, params))

        updated = 0
        for edge in self.edges:
            if (
                edge["order_id"] == params["order_id"]
                and edge.get("gross_attributed_gmv_cents") is None
            ):
                edge["gross_attributed_gmv_cents"] = params["gross"]
                updated += 1
        return updated


def _edge(
    order_id: str = "ORD_ATTR_1",
    *,
    edge_id: str = "edge_1",
    gross_attributed_gmv_cents: Optional[int] = None,
) -> dict[str, Any]:
    return {
        "edge_id": edge_id,
        "order_id": order_id,
        "gross_attributed_gmv_cents": gross_attributed_gmv_cents,
    }


async def _stamp(
    monkeypatch: pytest.MonkeyPatch,
    fake_db: FakeAttributionDatabase,
    *,
    order_id: str = "ORD_ATTR_1",
    subtotal: Any = Decimal("100.00"),
    discount_total: Any = Decimal("0.00"),
) -> Optional[int]:
    monkeypatch.setattr(finalizer, "database", fake_db)
    return await finalizer.stamp_gross_attributed_gmv(
        order_id,
        subtotal=subtotal,
        discount_total=discount_total,
    )


def _assert_stamp_sql(fake_db: FakeAttributionDatabase) -> dict[str, Any]:
    assert len(fake_db.executed) == 1
    sql, params = fake_db.executed[0]
    assert sql.strip() == finalizer._STAMP_GROSS_ATTRIBUTED_GMV_QUERY.strip()
    assert "gross_attributed_gmv_cents IS NULL" in sql
    return params


@pytest.mark.asyncio
async def test_stamp_gross_attributed_gmv_standard_order_one_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = FakeAttributionDatabase([_edge()])

    updated = await _stamp(
        monkeypatch,
        fake_db,
        subtotal=Decimal("125.99"),
        discount_total=Decimal("25.99"),
    )

    assert updated == 1
    assert fake_db.edges[0]["gross_attributed_gmv_cents"] == 10_000
    assert _assert_stamp_sql(fake_db) == {"order_id": "ORD_ATTR_1", "gross": 10_000}


@pytest.mark.asyncio
async def test_stamp_gross_attributed_gmv_multiple_edges_for_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = FakeAttributionDatabase(
        [
            _edge(edge_id="edge_1"),
            _edge(edge_id="edge_2"),
            _edge(order_id="ORD_OTHER", edge_id="edge_other"),
        ]
    )

    updated = await _stamp(
        monkeypatch,
        fake_db,
        subtotal=Decimal("80.00"),
        discount_total=Decimal("5.00"),
    )

    assert updated == 2
    assert [edge["gross_attributed_gmv_cents"] for edge in fake_db.edges] == [
        7_500,
        7_500,
        None,
    ]
    _assert_stamp_sql(fake_db)


@pytest.mark.asyncio
async def test_stamp_gross_attributed_gmv_no_edges_noops_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_db = FakeAttributionDatabase([])

    with caplog.at_level(logging.INFO, logger=finalizer.__name__):
        updated = await _stamp(monkeypatch, fake_db)

    assert updated == 0
    assert "No unstamped commerce attribution edges found for paid order ORD_ATTR_1" in caplog.text
    _assert_stamp_sql(fake_db)


@pytest.mark.asyncio
async def test_stamp_gross_attributed_gmv_retry_skips_already_stamped_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = FakeAttributionDatabase([_edge(gross_attributed_gmv_cents=12_345)])

    updated = await _stamp(
        monkeypatch,
        fake_db,
        subtotal=Decimal("80.00"),
        discount_total=Decimal("5.00"),
    )

    assert updated == 0
    assert fake_db.edges[0]["gross_attributed_gmv_cents"] == 12_345
    assert _assert_stamp_sql(fake_db) == {"order_id": "ORD_ATTR_1", "gross": 7_500}


@pytest.mark.asyncio
async def test_stamp_gross_attributed_gmv_zero_subtotal_stamps_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = FakeAttributionDatabase([_edge()])

    updated = await _stamp(
        monkeypatch,
        fake_db,
        subtotal=Decimal("0.00"),
        discount_total=Decimal("0.00"),
    )

    assert updated == 1
    assert fake_db.edges[0]["gross_attributed_gmv_cents"] == 0
    assert _assert_stamp_sql(fake_db) == {"order_id": "ORD_ATTR_1", "gross": 0}


@pytest.mark.asyncio
async def test_stamp_gross_attributed_gmv_discount_greater_than_subtotal_clamps_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = FakeAttributionDatabase([_edge()])

    updated = await _stamp(
        monkeypatch,
        fake_db,
        subtotal=Decimal("25.00"),
        discount_total=Decimal("30.00"),
    )

    assert updated == 1
    assert fake_db.edges[0]["gross_attributed_gmv_cents"] == 0
    assert _assert_stamp_sql(fake_db) == {"order_id": "ORD_ATTR_1", "gross": 0}


@pytest.mark.asyncio
async def test_finalize_payment_success_stamps_after_marking_order_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    async def fake_mark_order_paid(order_id: str) -> bool:
        calls.append(("mark_paid", order_id))
        return True

    async def fake_stamp(order_id: str, *, subtotal: Any, discount_total: Any = None) -> int:
        calls.append(("stamp", order_id, subtotal, discount_total))
        return 1

    async def fake_log_order_event(**kwargs: Any) -> None:
        calls.append(("log_event", kwargs["order_id"]))

    monkeypatch.setattr(finalizer, "stamp_gross_attributed_gmv", fake_stamp)

    result = await finalizer.finalize_payment_success(
        {
            "order_id": "ORD_ATTR_HOOK",
            "merchant_id": "merch_1",
            "status": "pending",
            "payment_status": "awaiting_payment",
            "payment_intent_id": "pi_hook",
            "subtotal": Decimal("40.00"),
            "discount_total": Decimal("4.00"),
            "shipping_fee": Decimal("20.00"),
            "tax": Decimal("3.00"),
            "metadata": {},
        },
        psp="stripe",
        payment_reference="pi_hook",
        mark_order_paid_fn=fake_mark_order_paid,
        log_order_event_fn=fake_log_order_event,
    )

    assert result["applied"] is True
    assert calls == [
        ("mark_paid", "ORD_ATTR_HOOK"),
        ("stamp", "ORD_ATTR_HOOK", Decimal("40.00"), Decimal("4.00")),
        ("log_event", "ORD_ATTR_HOOK"),
    ]

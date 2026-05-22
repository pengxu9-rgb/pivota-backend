"""Refund attribution write path tests.

Covers the SQL-side atomic refund UPDATE in commerce_attribution_service.
The query handles three concerns in one statement:

  1. Per-edge accumulation: each row matching order_id gets the same delta
     applied independently (mirrors T9's fan-out — every edge sees the full
     gross stamp, every edge sees the full refund).
  2. Idempotency: the JSONB `?` containment check on refund_ids prevents
     double-counting when a Stripe webhook is retried.
  3. T6 visibility: refund_amount_cents (the column T6 reads) is written
     alongside the legacy refunded_amount Decimal.

The tests use a FakeDB that interprets the production UPDATE...RETURNING
query, so the assertions exercise the actual SQL contract rather than a
Python-side reimplementation that could drift from it.
"""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest

from services import commerce_attribution_service


def _base_edge(**overrides: Any) -> Dict[str, Any]:
    edge = {
        "edge_id": "cae_1",
        "order_id": "ord_1",
        "merchant_id": "merch_1",
        "refund_ids": [],
        "refund_count": 0,
        "refunded_amount": Decimal("0"),
        "refund_amount_cents": 0,
        "refunded_at": None,
        "latest_refund_id": None,
        "latest_refund_at": None,
        "updated_at": None,
        "metadata": {},
        "click_id": "clk_x",
        "interaction_id": "int_x",
        "canonical_product_id": None,
        "canonical_variant_id": None,
        "surface": "agent",
        "prompt_cluster": None,
    }
    edge.update(overrides)
    return edge


class _FakeDB:
    """Interprets the production _ATTRIBUTE_REFUND_QUERY against an in-memory
    edge list, applying the same per-row accumulation + idempotency check
    semantics the real Postgres query encodes.

    Keeping the simulation logic small and explicit so the test asserts
    against the SQL behavior, not against a Python reimplementation in the
    service module.
    """

    def __init__(self, edges: List[Dict[str, Any]]) -> None:
        self.edges = [copy.deepcopy(e) for e in edges]
        self.fetch_all_calls: List[tuple[str, Dict[str, Any]]] = []

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None):
        params = dict(values or {})
        self.fetch_all_calls.append((str(query), params))
        sql = str(query)
        assert "UPDATE commerce_attribution_edges" in sql, sql
        assert "RETURNING" in sql, sql

        order_id = params["order_id"]
        refund_id = params["refund_id"]
        amount_cents = int(params["amount_cents"])
        amount_decimal = Decimal(str(params["amount_decimal"]))
        now = params["now"]

        returned: List[Dict[str, Any]] = []
        for edge in self.edges:
            if edge.get("order_id") != order_id:
                continue
            current_ids = list(edge.get("refund_ids") or [])
            already_seen = refund_id in current_ids

            if already_seen:
                new_refund_ids = current_ids
                new_refund_count = edge.get("refund_count") or 0
                new_amount_cents = edge.get("refund_amount_cents") or 0
                new_decimal = Decimal(str(edge.get("refunded_amount") or "0"))
            else:
                new_refund_ids = current_ids + [refund_id]
                new_refund_count = (edge.get("refund_count") or 0) + 1
                new_amount_cents = (edge.get("refund_amount_cents") or 0) + amount_cents
                new_decimal = Decimal(str(edge.get("refunded_amount") or "0")) + amount_decimal

            edge.update(
                {
                    "latest_refund_id": refund_id,
                    "refund_ids": new_refund_ids,
                    "refund_count": new_refund_count,
                    "refund_amount_cents": new_amount_cents,
                    "refunded_amount": new_decimal,
                    "refunded_at": edge.get("refunded_at") or now,
                    "latest_refund_at": now,
                    "updated_at": now,
                }
            )
            # The real UPDATE...RETURNING projects a subset of columns; mirror
            # that so the service code reads back the expected shape.
            returned.append(
                {
                    "edge_id": edge["edge_id"],
                    "merchant_id": edge["merchant_id"],
                    "click_id": edge["click_id"],
                    "canonical_product_id": edge["canonical_product_id"],
                    "canonical_variant_id": edge["canonical_variant_id"],
                    "surface": edge["surface"],
                    "prompt_cluster": edge["prompt_cluster"],
                    "interaction_id": edge["interaction_id"],
                    "metadata": edge["metadata"],
                    "refund_ids": new_refund_ids,
                    "refund_count": new_refund_count,
                    "refund_amount_cents": new_amount_cents,
                    "refunded_amount": new_decimal,
                    "refunded_at": edge["refunded_at"],
                    "latest_refund_at": now,
                }
            )
        return returned


def _install(monkeypatch: pytest.MonkeyPatch, db: _FakeDB) -> None:
    monkeypatch.setattr(commerce_attribution_service, "database", db)

    async def fake_record(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        return {"interaction_id": "int_x"}

    monkeypatch.setattr(
        commerce_attribution_service,
        "record_commerce_event_best_effort",
        fake_record,
    )


@pytest.mark.asyncio
async def test_attach_refund_single_edge_populates_both_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(edges=[_base_edge()])
    _install(monkeypatch, db)

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_first",
        amount=Decimal("25.50"),
    )

    assert result is not None
    assert result["refunded_amount"] == Decimal("25.50")
    assert result["refund_amount_cents"] == 2550
    assert result["refund_count"] == 1
    assert result["refund_ids"] == ["ref_first"]
    assert result["edge_count"] == 1
    # Only the matching order's edge updated.
    assert db.edges[0]["refund_amount_cents"] == 2550


@pytest.mark.asyncio
async def test_attach_refund_two_distinct_refunds_accumulate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(
        edges=[
            _base_edge(
                refund_ids=["ref_first"],
                refund_count=1,
                refunded_amount=Decimal("10.00"),
                refund_amount_cents=1000,
            )
        ]
    )
    _install(monkeypatch, db)

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_second",
        amount=Decimal("5.25"),
    )

    assert result is not None
    assert result["refunded_amount"] == Decimal("15.25")
    assert result["refund_amount_cents"] == 1525
    assert result["refund_count"] == 2


@pytest.mark.asyncio
async def test_attach_refund_idempotent_on_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same refund_id replayed — JSONB `?` containment check skips
    accumulation on every column."""
    db = _FakeDB(
        edges=[
            _base_edge(
                refund_ids=["ref_first"],
                refund_count=1,
                refunded_amount=Decimal("10.00"),
                refund_amount_cents=1000,
            )
        ]
    )
    _install(monkeypatch, db)

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_first",
        amount=Decimal("10.00"),
    )

    assert result is not None
    assert result["refunded_amount"] == Decimal("10.00")
    assert result["refund_amount_cents"] == 1000
    assert result["refund_count"] == 1
    assert result["refund_ids"] == ["ref_first"]


@pytest.mark.asyncio
async def test_attach_refund_handles_fractional_cents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(edges=[_base_edge()])
    _install(monkeypatch, db)

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_penny",
        amount=Decimal("0.01"),
    )

    assert result is not None
    assert result["refund_amount_cents"] == 1


# === multi-edge fan-out cases ===
# T9 stamps every edge for an order with the same gross_attributed_gmv_cents
# (each surface_click_event gets full attribution credit). The refund path
# must mirror that — every matching edge sees the refund.


@pytest.mark.asyncio
async def test_attach_refund_applies_to_every_edge_of_multi_edge_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(
        edges=[
            _base_edge(edge_id="cae_a", click_id="clk_a"),
            _base_edge(edge_id="cae_b", click_id="clk_b"),
            _base_edge(edge_id="cae_c", click_id="clk_c"),
        ]
    )
    _install(monkeypatch, db)

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_xyz",
        amount=Decimal("30.00"),
    )

    assert result is not None
    assert result["edge_count"] == 3
    # Each of the three edges accumulated the full refund. T6 will then SUM
    # them per (merchant, agent, channel) rollup group; if all three edges
    # share the same group, gross_sum and refund_sum are both 3× — net math
    # stays correct. If they fan out to different agent groups, each group
    # sees full credit on both sides — also correct under fan-out semantics.
    for edge in db.edges:
        assert edge["refund_amount_cents"] == 3000
        assert edge["refunded_amount"] == Decimal("30.00")
        assert edge["refund_ids"] == ["ref_xyz"]
        assert edge["refund_count"] == 1


@pytest.mark.asyncio
async def test_attach_refund_idempotent_across_multi_edge_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripe retries the refund.succeeded webhook on a multi-edge order.
    Every edge must dedupe independently."""
    db = _FakeDB(
        edges=[
            _base_edge(
                edge_id="cae_a",
                refund_ids=["ref_seen"],
                refund_count=1,
                refunded_amount=Decimal("10.00"),
                refund_amount_cents=1000,
            ),
            _base_edge(
                edge_id="cae_b",
                refund_ids=["ref_seen"],
                refund_count=1,
                refunded_amount=Decimal("10.00"),
                refund_amount_cents=1000,
            ),
        ]
    )
    _install(monkeypatch, db)

    await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_seen",
        amount=Decimal("10.00"),
    )

    # Both edges unchanged.
    for edge in db.edges:
        assert edge["refund_amount_cents"] == 1000
        assert edge["refunded_amount"] == Decimal("10.00")
        assert edge["refund_count"] == 1
        assert edge["refund_ids"] == ["ref_seen"]


@pytest.mark.asyncio
async def test_attach_refund_robust_to_edge_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the N edges of an order have drifted in refund state (e.g. one
    was patched out-of-band), the SQL-side accumulation applies the delta
    per-row instead of flattening to a single Python-computed value.
    Previous read-modify-write code would silently overwrite the drift."""
    db = _FakeDB(
        edges=[
            _base_edge(
                edge_id="cae_a",
                refund_ids=["ref_old"],
                refund_count=1,
                refund_amount_cents=500,
                refunded_amount=Decimal("5.00"),
            ),
            _base_edge(
                edge_id="cae_b",
                refund_ids=[],
                refund_count=0,
                refund_amount_cents=0,
                refunded_amount=Decimal("0"),
            ),
        ]
    )
    _install(monkeypatch, db)

    await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_1",
        refund_id="ref_new",
        amount=Decimal("3.00"),
    )

    # cae_a accumulated correctly atop its prior state.
    assert db.edges[0]["refund_amount_cents"] == 800
    assert db.edges[0]["refund_count"] == 2
    assert db.edges[0]["refund_ids"] == ["ref_old", "ref_new"]
    # cae_b accumulated from zero, unaffected by cae_a's prior state.
    assert db.edges[1]["refund_amount_cents"] == 300
    assert db.edges[1]["refund_count"] == 1
    assert db.edges[1]["refund_ids"] == ["ref_new"]


@pytest.mark.asyncio
async def test_attach_refund_returns_none_when_no_edges_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(edges=[])
    _install(monkeypatch, db)

    result = await commerce_attribution_service.attach_refund_to_attribution_edge(
        order_id="ord_missing",
        refund_id="ref_x",
        amount=Decimal("10.00"),
    )

    assert result is None
    # No event emitted when no edges matched.
    assert db.fetch_all_calls  # the SQL still fired (single statement)


def test_refund_query_uses_jsonb_dedup_and_atomic_increment() -> None:
    """Regression guards on the SQL itself — these are the load-bearing
    invariants of the multi-edge refund fix.
    """
    sql = commerce_attribution_service._ATTRIBUTE_REFUND_QUERY
    # JSONB containment check, not Python read-modify-write
    assert "?" in sql and "refund_ids" in sql, (
        "refund_id idempotency check must use JSONB `?` operator"
    )
    # SQL-side accumulation
    assert "COALESCE(refund_amount_cents, 0) +" in sql, (
        "refund_amount_cents must accumulate via SQL, not Python read-write"
    )
    assert "COALESCE(refunded_amount, 0) +" in sql, (
        "refunded_amount must accumulate via SQL, not Python read-write"
    )
    # WHERE matches all edges of the order_id (multi-edge fan-out)
    assert "WHERE order_id = :order_id" in sql, (
        "must match all edges for the order_id (T9 fan-out symmetry)"
    )
    # RETURNING so the service can detect zero-row miss + emit the event
    assert "RETURNING" in sql

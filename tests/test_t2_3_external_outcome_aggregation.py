"""T2-3 — outcome aggregation counts EXTERNAL converted edges.

T2-2 records a purchase completed on an un-integrated merchant's OWN checkout as a
self-contained `converted` attribution edge: state='converted', GMV + currency ON the
edge, and NO Pivota `orders` row. Before T2-3 the aggregator INNER-JOINed `orders`, so
those conversions never reached `aggregated_outcomes` (the moat signal that feeds
seller_trust). T2-3 makes them countable. See tier2_v1_build_plan §T2-3 (gaps #4/#6).

INTEGRITY (non-negotiable): only external edges with `metadata.click_matched = true`
count. A false value is a forgeable, merchant-supplied click id (Shopify
note_attributes) and must never inflate the outcome/trust signal.

Harness note: the unit-test DB is SQLite; the production SQL is Postgres-specific
(FILTER, ANY(:param), ->> / ::boolean, interval). So — following the T2-2 convention
of a FakeDB that emulates just enough Postgres — these tests pair (a) STRUCTURAL
assertions on the real SQL strings (the integrity gate + LEFT JOIN + UNION are
literally present) with (b) a PgLikeDB that reproduces the JOIN / FILTER / grouping
semantics over in-memory fixture rows, exercising the real _build_row → UPSERT path.
Live-Postgres execution is validated end-to-end in T2-5.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import outcome_aggregation_service as svc  # noqa: E402


# =============================================================================
# (a) STRUCTURAL — the integrity gate + external arm are literally in the SQL
# =============================================================================

GATE = "(cae.metadata->>'click_matched')::boolean IS TRUE"


def test_product_sql_has_click_matched_gate_and_left_join() -> None:
    sql = svc._product_sql("all_time")
    assert GATE in sql, "product SQL must gate external edges on click_matched=true"
    assert "LEFT JOIN orders o ON o.order_id = cae.order_id" in sql, "must LEFT (not INNER) JOIN orders"
    assert "cae.state = 'converted'" in sql
    # GMV always from the edge (works with or without an orders row).
    assert "SUM(cae.gross_attributed_gmv_cents)" in sql


def test_merchant_sql_unions_external_arm_with_gate() -> None:
    sql = svc._merchant_sql("all_time")
    assert "UNION ALL" in sql, "merchant SQL must UNION the external (no-orders-row) arm"
    assert GATE in sql, "merchant external arm must gate on click_matched=true"
    assert "cae.gross_attributed_gmv_cents::numeric / 100" in sql, "external GMV read from the edge"
    # Internal arm preserved verbatim (byte-identical intent).
    assert "(payment_status = ANY(:transacted)) AS is_transacted" in sql
    assert "(total)::numeric                    AS gmv_units" in sql


def test_referred_state_is_never_counted_by_construction() -> None:
    # Only state='converted' is admitted on the external arm; 'referred' can't match.
    for sql in (svc._product_sql("all_time"), svc._merchant_sql("all_time")):
        assert "'referred'" not in sql


# =============================================================================
# (b) SEMANTIC — a PgLikeDB that reproduces the SQL over raw fixture rows
# =============================================================================

_TRANSACTED = set(svc.TRANSACTED_STATUSES)
_REFUNDED = set(svc.REFUNDED_STATUSES)


def _order(order_id: str, merchant_id: str, payment_status: str, total: str, currency: str = "USD") -> Dict[str, Any]:
    return {
        "order_id": order_id, "merchant_id": merchant_id,
        "payment_status": payment_status, "total": Decimal(total), "currency": currency,
    }


def _edge(
    *, product: Optional[str], merchant_id: str, order_id: str,
    state: Optional[str] = None, click_matched: Optional[bool] = None,
    gmv_cents: Optional[int] = None, currency: Optional[str] = None,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    if click_matched is not None:
        metadata["click_matched"] = click_matched
    return {
        "canonical_product_id": product, "merchant_id": merchant_id, "order_id": order_id,
        "state": state, "metadata": metadata,
        "gross_attributed_gmv_cents": gmv_cents, "currency": currency,
    }


class PgLikeDB:
    """Emulates the product/merchant aggregation SQL semantics over in-memory rows.

    Mirrors, in Python, exactly what the Postgres queries compute for the `all_time`
    window (window filtering is orthogonal and skipped here): the product LEFT JOIN
    with the (internal-transacted OR external-converted-and-click_matched) predicate,
    and the merchant UNION of internal orders + gated external edges.
    """

    def __init__(self, orders: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> None:
        self.orders = orders
        self.edges = edges
        self.upserts: List[Dict[str, Any]] = []

    @staticmethod
    def _ext_converted(edge: Dict[str, Any]) -> bool:
        # (cae.state='converted' AND (metadata->>'click_matched')::boolean IS TRUE)
        return edge.get("state") == "converted" and edge.get("metadata", {}).get("click_matched") is True

    def _merchant_rows(self) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}

        def bump(mid: str, *, transacted: bool, paid: bool, refunded: bool, gmv_units: Decimal, currency):
            g = groups.setdefault(mid, {"t": 0, "p": 0, "r": 0, "gmv": Decimal(0), "cur": None})
            if transacted:
                g["t"] += 1
                g["gmv"] += gmv_units
            if paid:
                g["p"] += 1
            if refunded:
                g["r"] += 1
            if currency and (g["cur"] is None or currency > g["cur"]):  # MAX(currency)
                g["cur"] = currency

        for o in self.orders:
            if not o.get("merchant_id"):
                continue
            bump(
                o["merchant_id"],
                transacted=o["payment_status"] in _TRANSACTED,
                paid=o["payment_status"] == "paid",
                refunded=o["payment_status"] in _REFUNDED,
                gmv_units=Decimal(o["total"]),
                currency=o.get("currency"),
            )
        for e in self.edges:
            if not e.get("merchant_id") or not self._ext_converted(e):
                continue
            bump(
                e["merchant_id"], transacted=True, paid=True, refunded=False,
                gmv_units=Decimal(e["gross_attributed_gmv_cents"]) / 100, currency=e.get("currency"),
            )
        return [
            {"subject_key": mid, "transacted_count": g["t"], "paid_count": g["p"],
             "refunded_count": g["r"], "gmv_units": g["gmv"], "currency": g["cur"]}
            for mid, g in groups.items() if g["t"] > 0  # HAVING transacted > 0
        ]

    def _product_rows(self) -> List[Dict[str, Any]]:
        orders_by_id = {o["order_id"]: o for o in self.orders}
        groups: Dict[str, Dict[str, Any]] = {}
        for e in self.edges:
            pid = e.get("canonical_product_id")
            if not pid:  # WHERE cae.canonical_product_id IS NOT NULL
                continue
            o = orders_by_id.get(e["order_id"])  # LEFT JOIN
            int_transacted = o is not None and o["payment_status"] in _TRANSACTED
            ext_converted = self._ext_converted(e)
            transacted = int_transacted or ext_converted
            paid = (o is not None and o["payment_status"] == "paid") or ext_converted
            refunded = o is not None and o["payment_status"] in _REFUNDED
            currency = (o.get("currency") if o else None) or e.get("currency")
            g = groups.setdefault(pid, {"t": 0, "p": 0, "r": 0, "gmv": 0, "cur": None})
            if transacted:
                g["t"] += 1
                g["gmv"] += int(e.get("gross_attributed_gmv_cents") or 0)
            if paid:
                g["p"] += 1
            if refunded:
                g["r"] += 1
            if currency and (g["cur"] is None or currency > g["cur"]):
                g["cur"] = currency
        return [
            {"subject_key": pid, "transacted_count": g["t"], "paid_count": g["p"],
             "refunded_count": g["r"], "gmv_cents": g["gmv"], "currency": g["cur"]}
            for pid, g in groups.items() if g["t"] > 0
        ]

    async def fetch_all(self, sql: str, params: Any = None) -> List[Dict[str, Any]]:
        if "UNION ALL" in sql:            # merchant SQL
            return self._merchant_rows()
        if "LEFT JOIN orders o" in sql:   # product SQL
            return self._product_rows()
        return []

    async def execute(self, sql: str, params: Any = None) -> None:
        if isinstance(params, dict) and "subject_type" in params:
            self.upserts.append(params)

    async def fetch_one(self, sql: str, params: Any = None):
        return None


async def _run(orders, edges) -> Dict[tuple, Dict[str, Any]]:
    fake = PgLikeDB(orders, edges)
    svc.database = fake  # module-level singleton the service reads
    svc._DDL_READY = True
    await svc.refresh_all_outcomes()
    # index the all_time upserts by (subject_type, subject_key)
    return {
        (u["subject_type"], u["subject_key"]): u
        for u in fake.upserts if u["window_key"] == "all_time"
    }


@pytest.fixture(autouse=True)
def _restore_db():
    saved = svc.database
    saved_ddl = svc._DDL_READY
    yield
    svc.database = saved
    svc._DDL_READY = saved_ddl


# --- external converted edge (click_matched=true, NO orders row) → counted --------

@pytest.mark.asyncio
async def test_external_converted_matched_edge_is_counted() -> None:
    edges = [_edge(
        product="merch_x|shopify|1", merchant_id="merch_x", order_id="ext_abc",
        state="converted", click_matched=True, gmv_cents=4999, currency="USD",
    )]
    out = await _run(orders=[], edges=edges)  # NOTE: zero orders rows
    prod = out[("product", "merch_x|shopify|1")]
    assert prod["transacted_count"] == 1
    assert prod["paid_count"] == 1
    assert prod["refunded_count"] == 0
    assert prod["gmv_cents"] == 4999      # from the edge itself
    assert prod["currency"] == "USD"
    merch = out[("merchant", "merch_x")]
    assert merch["transacted_count"] == 1
    assert merch["gmv_cents"] == 4999     # cents ÷ 100 → units → ×100 round-trips exactly


# --- INTEGRITY: click_matched=false external edge → excluded ----------------------

@pytest.mark.asyncio
async def test_external_converted_unmatched_edge_is_excluded() -> None:
    edges = [_edge(
        product="merch_x|shopify|1", merchant_id="merch_x", order_id="ext_forge",
        state="converted", click_matched=False, gmv_cents=999999, currency="USD",
    )]
    out = await _run(orders=[], edges=edges)
    # A forgeable (unmatched) click id must NOT create any outcome row.
    assert ("product", "merch_x|shopify|1") not in out
    assert ("merchant", "merch_x") not in out


# --- referred (non-converted) edge → not counted ---------------------------------

@pytest.mark.asyncio
async def test_referred_edge_is_not_counted() -> None:
    edges = [_edge(
        product="merch_x|shopify|1", merchant_id="merch_x", order_id="ext_ref",
        state="referred", click_matched=True, gmv_cents=5000, currency="USD",
    )]
    out = await _run(orders=[], edges=edges)
    assert out == {}  # a click that never converted is not an outcome


# --- mixed internal + external → both counted, GMV summed, no double-count --------

@pytest.mark.asyncio
async def test_mixed_internal_and_external_sum_without_double_count() -> None:
    orders = [_order("ord_1", "merch_x", "paid", "100.00", "USD")]
    edges = [
        # internal edge: real orders row, no state (legacy/internal path) — GMV on edge
        _edge(product="merch_x|shopify|1", merchant_id="merch_x", order_id="ord_1",
              state=None, click_matched=None, gmv_cents=10000, currency=None),
        # external converted edge for the SAME product, different (no) order
        _edge(product="merch_x|shopify|1", merchant_id="merch_x", order_id="ext_zzz",
              state="converted", click_matched=True, gmv_cents=4999, currency="USD"),
    ]
    out = await _run(orders, edges)
    prod = out[("product", "merch_x|shopify|1")]
    assert prod["transacted_count"] == 2            # internal + external, counted once each
    assert prod["gmv_cents"] == 10000 + 4999        # 14999, summed, not doubled
    merch = out[("merchant", "merch_x")]
    assert merch["transacted_count"] == 2
    assert merch["gmv_cents"] == 10000 + 4999       # 100.00 order + 49.99 external


# --- REGRESSION: internal-only fixture → aggregates unchanged (byte-identical) ----

@pytest.mark.asyncio
async def test_internal_only_aggregates_are_unchanged() -> None:
    # No external edges anywhere → the external arm/predicate must be a strict no-op.
    orders = [
        _order("ord_1", "merch_i", "paid", "20.00", "USD"),
        _order("ord_2", "merch_i", "refunded", "30.00", "USD"),
        _order("ord_3", "merch_i", "pending", "99.00", "USD"),   # not transacted
    ]
    edges = [
        _edge(product="merch_i|shopify|1", merchant_id="merch_i", order_id="ord_1",
              state=None, gmv_cents=2000, currency=None),
        _edge(product="merch_i|shopify|1", merchant_id="merch_i", order_id="ord_2",
              state=None, gmv_cents=3000, currency=None),
        _edge(product="merch_i|shopify|1", merchant_id="merch_i", order_id="ord_3",
              state=None, gmv_cents=9900, currency=None),  # pending → excluded
    ]
    out = await _run(orders, edges)

    # Exact values the PRE-T2-3 (INNER JOIN / orders-only) pipeline produced:
    merch = out[("merchant", "merch_i")]
    assert merch["transacted_count"] == 2           # paid + refunded (pending excluded)
    assert merch["paid_count"] == 1
    assert merch["refunded_count"] == 1
    assert merch["gmv_cents"] == 5000               # 20.00 + 30.00 (pending excluded)
    assert merch["currency"] == "USD"

    prod = out[("product", "merch_i|shopify|1")]
    assert prod["transacted_count"] == 2
    assert prod["paid_count"] == 1
    assert prod["refunded_count"] == 1
    assert prod["gmv_cents"] == 5000                # edge GMV for the 2 transacted only
    assert prod["currency"] == "USD"


# --- currency: external GMV carries the edge's currency when no order exists -------

@pytest.mark.asyncio
async def test_external_edge_currency_comes_from_edge() -> None:
    edges = [_edge(
        product="merch_e|shopify|9", merchant_id="merch_e", order_id="ext_eur",
        state="converted", click_matched=True, gmv_cents=8000, currency="EUR",
    )]
    out = await _run(orders=[], edges=edges)
    assert out[("product", "merch_e|shopify|9")]["currency"] == "EUR"
    assert out[("merchant", "merch_e")]["currency"] == "EUR"

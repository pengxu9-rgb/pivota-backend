"""Revenue aggregates over `orders` must name a column the table actually has.

WHAT WAS WRONG. Four statements summed `orders.amount`. There is no such column
and there never has been in this repo's lineage: `db/orders.py` carries it only
as `# Column("amount", ...) # REMOVED - use "total" instead`, commented out in
the initial restructure commit, and no migration, no `schema_guard` ALTER and no
startup DDL creates it. Every one of these statements was dead on every call.

WHY NOTHING NOTICED. Each sits inside a bare `except Exception`, and two of the
three handlers answer `"status": "success"` from the except arm:

    get_finance_overview      -> prints the error, returns gross_revenue 0,
                                 net_revenue 0, avg 0, monthly_breakdown []
                                 under "status": "success"
    get_onboarding_details    -> prints the error, leaves stats at
                                 {"total_transactions": 0, "total_revenue": 0}
    get_payment_analytics     -> at least returns "status": "error"

So the employee finance dashboard and the merchant detail page have been
rendering a confident, well-formed $0 rather than failing. That is the failure
mode this file exists to make impossible to reintroduce quietly.

WHY IT DRIVES THE ROUTES RATHER THAN THE SQL. The statements are inline literals
inside the handlers. Re-asserting a copy of the SQL here would prove only that
the copy works — the delivering line would stay untested and could drift away
from it. These tests call the real handler functions, so the statement under
test is the statement that ships.

WHY POSTGRES. `tests/test_repo_sql_prepare_postgres.py` PREPAREs statements, and
PREPARE validates TYPES, never VALUES — a green sweep says "Postgres would plan
this", not "this returns the right number". These aggregates are exactly the case
where the difference matters: the fix swaps which column is summed, and only
executing it over real rows can show the sum is the order total. SQLite would not
catch the original defect at all, since the repo's SQLite fixtures build `orders`
from the same model and would also lack `amount` — but nothing in the sweep ever
executed these handlers on any engine.

Named `test_*_postgres.py`, so `.github/workflows/postgres-dialect-gate.yml`
auto-discovers it and its existing `tests/test_*_postgres.py` path filter already
matches. No ride-along entry is needed: unlike the quality-backfill file, these
statements are not a dialect split, so there is no SQLite twin to keep honest.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_orders_revenue_columns_postgres.py

Never point this at prod — the teardown deletes by merchant_id, but it writes.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

MERCHANT = f"revcol_{uuid.uuid4().hex[:8]}"
# A second merchant, so `COUNT(DISTINCT merchant_id)` and the merchant-scoped
# statement have something to tell apart.
OTHER_MERCHANT = f"revcol_{uuid.uuid4().hex[:8]}"
_EMPLOYEE = {"role": "employee", "email": "gate@example.com"}


@pytest.fixture(autouse=True)
async def _db():
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import postgresql

    from db.database import database
    from db.merchant_onboarding import merchant_onboarding
    from db.orders import orders

    await database.connect()
    # The repo's own schema objects, compiled for Postgres, so this fixture can
    # never drift from the tables the handlers query. Note what that buys here
    # specifically: `orders` compiled from db/orders.py has NO `amount` column,
    # because the model is where the column was removed. A hand-written fixture
    # that happened to include `amount` would make every test below pass against
    # the unfixed code.
    for table in (orders, merchant_onboarding):
        try:
            await database.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
        except Exception:
            # Another gate file in this process already created it; same
            # metadata, so the shape is identical. Never DROP — this DB is
            # shared with every other test_*_postgres.py file.
            pass
    try:
        yield database
    finally:
        for merchant in (MERCHANT, OTHER_MERCHANT):
            await database.execute(
                "DELETE FROM orders WHERE merchant_id = :m", {"m": merchant}
            )
            await database.execute(
                "DELETE FROM merchant_onboarding WHERE merchant_id = :m", {"m": merchant}
            )
        await database.disconnect()


async def _order(order_id: str, *, total: str, status: str, merchant: str = MERCHANT,
                 days_ago: int = 1, email: str = "buyer@example.com",
                 is_deleted: bool = False) -> None:
    """One row, every NOT NULL column without a default sourced from the model.

    `subtotal` is deliberately NOT equal to `total`. The first version of this
    fixture set both to the same value, and that made the whole file blind to the
    defect it exists to catch: `SUM(total)` could be changed to `SUM(subtotal)` —
    a wrong but perfectly plannable column — and all four tests stayed green.
    These tests are supposed to prove the aggregate names the RIGHT column, not
    merely a real one, and identical fixture values reduce them to the weaker
    claim. Held at total - 5.00 so every expected figure below is unambiguous.
    """
    from db.database import database
    from db.orders import orders

    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await database.execute(
        orders.insert().values(
            order_id=order_id,
            merchant_id=merchant,
            customer_email=email,
            shipping_address={},
            items=[],
            subtotal=Decimal(total) - Decimal("5.00"),
            total=Decimal(total),
            currency="USD",
            status=status,
            created_at=created,
            updated_at=created,
            is_deleted=is_deleted,
        )
    )


async def _merchant_row(merchant: str) -> None:
    from db.database import database
    from db.merchant_onboarding import merchant_onboarding

    await database.execute(
        merchant_onboarding.insert().values(
            merchant_id=merchant,
            business_name="Gate Fixture Co",
            contact_email="ops@example.com",
            status="approved",
            # Explicit because `databases` compiles the INSERT without running
            # SQLAlchemy's Python-side column defaults, so a NOT NULL column
            # carrying only `default=False` is sent as NULL.
            apm_enabled=False,
        )
    )


# ---------------------------------------------------------------------------
# The three handlers, driven for real.
# ---------------------------------------------------------------------------
# WHY THE NEXT TWO ASSERT DELTAS. Both of these handlers aggregate over EVERY
# row in `orders` — neither is merchant-scoped — and this database is shared with
# the other forty-odd test_*_postgres.py gate files, several of which write
# orders of their own. Absolute figures would be a test that passes or fails on
# execution order. Measuring the same call before and after this test's own
# inserts is exact regardless of what else is in the table, and it still kills
# the defect: the unfixed handler answers zero both times, so the delta is 0.


async def test_finance_overview_sums_the_order_total_rather_than_answering_zero():
    """routes/employee_dashboard_routes.py — BOTH of its statements.

    The `except Exception` arm of this handler returns "status": "success" with
    every figure zeroed, so asserting the numbers is the only thing that can
    tell a working query from the swallowed failure. Asserting `status ==
    "success"` alone passes on the unfixed code.
    """
    from routes.employee_dashboard_routes import get_finance_overview

    before = (await get_finance_overview(current_user=_EMPLOYEE))["data"]

    # `delivered` and `paid` are real values of models/order.py::OrderStatus.
    # NOTE the handler's net_revenue predicate is `status IN ('completed',
    # 'delivered')` and OrderStatus defines NO `completed` — so net_revenue is
    # delivered-only. That is pre-existing and NOT changed here; it is pinned
    # below so the next person meets it as a fact rather than a surprise.
    await _order("revcol-fin-1", total="100.00", status="delivered")
    await _order("revcol-fin-2", total="40.00", status="paid")
    await _order("revcol-fin-3", total="10.00", status="delivered",
                 merchant=OTHER_MERCHANT, email="other@example.com")
    # Soft-deleted: must not reach any figure.
    await _order("revcol-fin-deleted", total="777.00", status="delivered",
                 is_deleted=True)

    after = (await get_finance_overview(current_user=_EMPLOYEE))["data"]

    def delta(key: str) -> float:
        return float(after[key]) - float(before[key])

    # gross = every live order added in the window; net = the delivered ones.
    # Both are the SUM(total) the fix introduced — and `total` is 5.00 above
    # `subtotal` on every row, so these figures also prove it is not summing
    # the neighbouring column.
    assert delta("gross_revenue") == 150.0
    assert delta("net_revenue") == 110.0
    assert delta("total_transactions") == 3
    assert delta("active_merchants") == 2

    # The second statement in the same handler, which the assertions above do
    # not reach: the monthly breakdown is a separate query and carried the same
    # defect. An empty list is precisely what the swallowed failure returns.
    def by_month(data) -> dict:
        return {row["month"]: float(row["revenue"]) for row in data["monthly_breakdown"]}

    monthly_before, monthly_after = by_month(before), by_month(after)
    assert monthly_after, "monthly_breakdown is empty — the second query failed"
    moved = sum(
        monthly_after[m] - monthly_before.get(m, 0.0) for m in monthly_after
    )
    assert round(moved, 2) == 150.0


async def test_payment_analytics_reports_the_amount_per_status():
    """routes/employee_missing_endpoints.py."""
    from routes.employee_missing_endpoints import get_payment_analytics

    async def amounts() -> dict:
        body = await get_payment_analytics(current_user=_EMPLOYEE)
        assert body["status"] == "success", body.get("message")
        return {
            row["status"]: (float(row["amount"]), row["count"])
            for row in body["analytics"]["status_breakdown"]
        }

    before = await amounts()

    await _order("revcol-an-1", total="25.50", status="delivered")
    await _order("revcol-an-2", total="74.50", status="delivered")
    await _order("revcol-an-3", total="5.00", status="cancelled")
    await _order("revcol-an-4", total="900.00", status="delivered", is_deleted=True)

    after = await amounts()

    def delta(status: str) -> tuple:
        was_amount, was_count = before.get(status, (0.0, 0))
        now_amount, now_count = after[status]
        return round(now_amount - was_amount, 2), now_count - was_count

    assert delta("delivered") == (100.0, 2)
    assert delta("cancelled") == (5.0, 1)


async def test_onboarding_details_reports_revenue_for_that_merchant_only():
    """routes/merchant_onboarding_routes.py.

    This handler leaves `stats` at its zeroed default inside `except Exception:
    print(...)`, so the defect was a merchant detail page that showed a real
    business name beside $0 of revenue.
    """
    from routes.merchant_onboarding_routes import get_onboarding_details

    await _merchant_row(MERCHANT)
    await _order("revcol-onb-1", total="60.00", status="delivered")
    await _order("revcol-onb-2", total="15.25", status="pending")
    await _order("revcol-onb-deleted", total="500.00", status="delivered",
                 is_deleted=True)
    # Another merchant's order must not leak into this merchant's total.
    await _order("revcol-onb-3", total="999.00", status="delivered",
                 merchant=OTHER_MERCHANT, email="other@example.com")

    body = await get_onboarding_details(MERCHANT, current_user=_EMPLOYEE)
    stats = body["merchant"]["stats"]

    assert stats["total_transactions"] == 2
    assert float(stats["total_revenue"]) == 75.25
    assert stats["unique_customers"] == 1


# ---------------------------------------------------------------------------
# ...and the reason all three of the above are worth having.
# ---------------------------------------------------------------------------
async def test_a_soft_deleted_order_reaches_none_of_the_three_figures():
    """The guard added alongside the column fix.

    Every OTHER `SUM(total)` over `orders` in these same files carries
    `(is_deleted IS NULL OR is_deleted = FALSE)` — employee_dashboard_routes.py
    lines 156, 216, 234 and 1360. These three did not, which did not matter while
    they threw and answered $0 from an except arm. It matters now that they
    return a number: without the guard the fix would trade a visibly-wrong zero
    for an invisibly-overstated total, which is the worse failure.

    Each test above already seeds a large soft-deleted row; this asserts the
    property directly, at one place, so it cannot be lost by editing one of them.
    """
    from routes.employee_dashboard_routes import get_finance_overview

    before = (await get_finance_overview(current_user=_EMPLOYEE))["data"]
    await _order("revcol-del-1", total="10000.00", status="delivered", is_deleted=True)
    after = (await get_finance_overview(current_user=_EMPLOYEE))["data"]

    assert float(after["gross_revenue"]) - float(before["gross_revenue"]) == 0.0
    assert float(after["net_revenue"]) - float(before["net_revenue"]) == 0.0
    assert after["total_transactions"] - before["total_transactions"] == 0


async def test_net_revenue_is_delivered_only_because_completed_is_not_a_status():
    """A KNOWN, UNFIXED mismatch, pinned so it stays visible rather than folklore.

    `get_finance_overview` computes net_revenue with
    `status IN ('completed', 'delivered')`, and models/order.py::OrderStatus
    defines pending, payment_processing, payment_failed, paid, processing,
    shipped, delivered, cancelled, refunded — there is NO `completed`. So a paid,
    shipped, not-yet-delivered order contributes zero to "net revenue", and
    `processing_fees` (2.9% of it) inherits that.

    NOT changed here: this commit is about which COLUMN is summed, and redefining
    what the business means by net revenue is a different decision with a
    different owner. Pinned instead, so the day someone fixes the predicate this
    test fails and they must decide deliberately. The repo already disagrees with
    itself about it — employee_finance.py uses payment_status IN ('paid',
    'captured', 'succeeded', 'completed', 'fulfilled') for the same idea.
    """
    from models.order import OrderStatus
    from routes.employee_dashboard_routes import get_finance_overview

    defined = {v for k, v in vars(OrderStatus).items() if not k.startswith("_")}
    assert "completed" not in defined, (
        "OrderStatus now defines 'completed' — the net_revenue predicate in "
        "get_finance_overview may no longer be delivered-only; re-check it."
    )

    before = (await get_finance_overview(current_user=_EMPLOYEE))["data"]
    await _order("revcol-paid-1", total="50.00", status="paid")
    after = (await get_finance_overview(current_user=_EMPLOYEE))["data"]

    # It reaches gross...
    assert float(after["gross_revenue"]) - float(before["gross_revenue"]) == 50.0
    # ...and not net, because `paid` is neither 'completed' nor 'delivered'.
    assert float(after["net_revenue"]) - float(before["net_revenue"]) == 0.0


async def test_the_orders_table_really_has_no_amount_column():
    """The premise, asserted rather than assumed.

    Every test above is only meaningful if `amount` is genuinely absent — if the
    column existed, the old SQL would have worked and these would be testing
    nothing. This pins the premise to the schema the handlers actually run
    against, so the day someone adds an `amount` column back this test fails and
    whoever is holding it can decide what the aggregates should mean.
    """
    from db.database import database

    columns = {
        row["column_name"]
        for row in await database.fetch_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'orders'"
        )
    }
    assert "total" in columns, "fixture did not build orders from the model"
    assert "amount" not in columns

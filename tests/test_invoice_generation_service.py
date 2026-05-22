from __future__ import annotations

import copy
import logging
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

import services.invoice_generation_service as service


class _FakeTransaction:
    def __init__(self, db: "_FakeDatabase") -> None:
        self.db = db
        self.snapshot: dict[str, Any] = {}

    async def __aenter__(self) -> "_FakeTransaction":
        self.snapshot = {
            "billing_run_items": copy.deepcopy(self.db.billing_run_items),
            "billing_items_by_id": copy.deepcopy(self.db.billing_items_by_id),
            "invoices": copy.deepcopy(self.db.invoices),
            "invoices_by_id": copy.deepcopy(self.db.invoices_by_id),
            "invoice_disputes": copy.deepcopy(self.db.invoice_disputes),
        }
        self.db.transaction_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.db.billing_run_items = self.snapshot["billing_run_items"]
            self.db.billing_items_by_id = self.snapshot["billing_items_by_id"]
            self.db.invoices = self.snapshot["invoices"]
            self.db.invoices_by_id = self.snapshot["invoices_by_id"]
            self.db.invoice_disputes = self.snapshot["invoice_disputes"]
        return False


class _FakeDatabase:
    def __init__(
        self,
        *,
        billing_run_id: int = 101,
        merchants_to_bill: list[str] | None = None,
        merchant_customers: dict[str, str | None] | None = None,
        gmv_rows_by_merchant: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.billing_run_id = billing_run_id
        self.billing_run_inserted = False
        self.merchants_to_bill = merchants_to_bill or []
        self.merchant_customers = merchant_customers or {}
        self.gmv_rows_by_merchant = gmv_rows_by_merchant or {}
        self.billing_run_items: list[dict[str, Any]] = []
        self.billing_items_by_id: dict[int, dict[str, Any]] = {}
        self.invoices: list[dict[str, Any]] = []
        self.invoices_by_id: dict[int, dict[str, Any]] = {}
        self.invoice_disputes: dict[int, dict[str, Any]] = {}
        self.completed_billing_runs: list[int] = []
        # Tracks current status per billing_run_id. UPDATE billing_runs handler
        # below transitions this; the new run_billing_cycle resume logic reads
        # it via SELECT id, status FROM billing_runs WHERE id = ...
        self.billing_run_statuses: dict[int, str] = {}
        # Tracks the most recent error string written to billing_runs.error
        # when transitioning to partial_failed. Useful for assertions.
        self.billing_run_errors: dict[int, str | None] = {}
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.fetch_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.fetch_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.transaction_count = 0
        self.distinct_merchant_fetches = 0

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        params = dict(values or {})
        q = _normalize_sql(query)
        self.fetch_one_calls.append((q, params))

        if q.startswith("insert into billing_runs"):
            if self.billing_run_inserted:
                return None
            self.billing_run_inserted = True
            self.billing_run_statuses[self.billing_run_id] = "running"
            return {"id": self.billing_run_id}

        if "from billing_runs" in q and "where idempotency_key" in q:
            return {"id": self.billing_run_id}

        # New for PR F: resume logic queries the current status of the
        # billing_run row to decide whether to short-circuit (completed) or
        # resume (any other status).
        if q.startswith("select id, status from billing_runs"):
            run_id = int(params["billing_run_id"])
            status = self.billing_run_statuses.get(run_id, "running")
            return {"id": run_id, "status": status}

        if "from merchants m join user_subscriptions us" in q:
            customer_id = self.merchant_customers.get(params["merchant_id"])
            return {"stripe_customer_id": customer_id} if customer_id else None

        if "from invoice_disputes" in q and "where id = :invoice_dispute_id" in q:
            return self.invoice_disputes.get(int(params["invoice_dispute_id"]))

        if "from invoices" in q and "where id = :invoice_id" in q:
            return self.invoices_by_id.get(int(params["invoice_id"]))

        if "from billing_run_items" in q and "where id = :billing_run_item_id" in q:
            return self.billing_items_by_id.get(int(params["billing_run_item_id"]))

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def fetch_all(self, query: str, values: dict[str, Any] | None = None):
        params = dict(values or {})
        q = _normalize_sql(query)
        self.fetch_all_calls.append((q, params))

        if q.startswith("select distinct merchant_id from gmv_attribution_daily"):
            self.distinct_merchant_fetches += 1
            return [{"merchant_id": merchant_id} for merchant_id in self.merchants_to_bill]

        if "from gmv_attribution_daily" in q:
            return list(self.gmv_rows_by_merchant.get(params["merchant_id"], []))

        # New for PR F: resume-path query — merchants that already have an
        # invoices row for this billing_run_id (so we don't re-bill them).
        if q.startswith("select distinct merchant_id from invoices"):
            run_id = int(params["billing_run_id"])
            seen: set[str] = set()
            for inv in self.invoices:
                if int(inv.get("billing_run_id", -1)) == run_id:
                    mid = inv.get("merchant_id")
                    if mid:
                        seen.add(str(mid))
            return [{"merchant_id": mid} for mid in sorted(seen)]

        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def execute(self, query: str, values: dict[str, Any] | None = None):
        params = dict(values or {})
        q = _normalize_sql(query)
        self.executed.append((q, params))

        if q.startswith("alter table"):
            return None

        if q.startswith("update billing_runs"):
            run_id = int(params["billing_run_id"])
            # Tell the two transition queries apart by their SET clause.
            if "status = 'completed'" in q:
                self.completed_billing_runs.append(run_id)
                self.billing_run_statuses[run_id] = "completed"
                self.billing_run_errors[run_id] = None
            elif "status = 'partial_failed'" in q:
                self.billing_run_statuses[run_id] = "partial_failed"
                self.billing_run_errors[run_id] = params.get("error")
            return None

        if q.startswith("insert into billing_run_items"):
            row = dict(params)
            row.setdefault("id", len(self.billing_run_items) + 1_000)
            self.billing_run_items.append(row)
            return None

        if q.startswith("insert into invoices"):
            self.invoices.append(dict(params))
            return None

        if q.startswith("update billing_run_items set voided_at"):
            item = self.billing_items_by_id[int(params["billing_run_item_id"])]
            item["voided_at"] = True
            return None

        if q.startswith("update invoice_disputes set status = 'applied'"):
            dispute = self.invoice_disputes[int(params["invoice_dispute_id"])]
            dispute["status"] = "applied"
            dispute["resolved_at"] = True
            return None

        if q.startswith("update invoices set status = 'finalizing'"):
            return None

        raise AssertionError(f"Unhandled execute query: {query}")


class _FakeInvoices:
    def __init__(self, client: "_FakeStripeClient") -> None:
        self.client = client

    def create(self, *args, **kwargs):
        params = _extract_stripe_params(args, kwargs)
        self.client.calls.append(("invoice.create", params))
        invoice_id = f"in_{len(self.client.invoice_create_params) + 1}"
        self.client.invoice_create_params.append(params)
        return SimpleNamespace(id=invoice_id)

    def finalize_invoice(self, stripe_invoice_id: str, *args, **kwargs):
        params = _extract_stripe_params(args, kwargs)
        self.client.calls.append(("invoice.finalize", stripe_invoice_id, params))
        self.client.finalize_calls.append((stripe_invoice_id, params))
        return SimpleNamespace(id=stripe_invoice_id)


class _FakeInvoiceItems:
    def __init__(self, client: "_FakeStripeClient") -> None:
        self.client = client

    def create(self, *args, **kwargs):
        params = _extract_stripe_params(args, kwargs)
        self.client.calls.append(("invoice_item.create", params))
        self.client.invoice_item_create_attempts += 1
        if self.client.fail_item_create_at == self.client.invoice_item_create_attempts:
            raise RuntimeError("stripe item failure")

        item_id = f"ii_{len(self.client.invoice_item_create_params) + 1}"
        self.client.invoice_item_create_params.append(params)
        return SimpleNamespace(id=item_id)

    def delete(self, stripe_invoice_item_id: str, *args, **kwargs):
        self.client.calls.append(("invoice_item.delete", stripe_invoice_item_id))
        self.client.deleted_invoice_items.append(stripe_invoice_item_id)
        return SimpleNamespace(id=stripe_invoice_item_id, deleted=True)


class _FakeStripeClient:
    def __init__(self, *, fail_item_create_at: int | None = None) -> None:
        self.calls: list[Any] = []
        self.invoice_create_params: list[dict[str, Any]] = []
        self.invoice_item_create_params: list[dict[str, Any]] = []
        self.invoice_item_create_attempts = 0
        self.fail_item_create_at = fail_item_create_at
        self.deleted_invoice_items: list[str] = []
        self.finalize_calls: list[tuple[str, dict[str, Any]]] = []
        self.v1 = SimpleNamespace(
            invoices=_FakeInvoices(self),
            invoice_items=_FakeInvoiceItems(self),
        )


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()


def _extract_stripe_params(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    if "params" in kwargs:
        return dict(kwargs["params"])
    if args and isinstance(args[0], dict):
        return dict(args[0])
    return {}


def _gmv_row(
    row_id: int,
    *,
    amount: int,
    merchant_id: str = "merch_1",
    agent_id: str | None = "agent_1",
    row_date: date = date(2026, 4, 1),
) -> dict[str, Any]:
    return {
        "id": row_id,
        "date": row_date,
        "merchant_id": merchant_id,
        "agent_id": agent_id,
        "take_amount_cents": amount,
    }


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    db: _FakeDatabase,
    stripe_client: _FakeStripeClient | None = None,
) -> _FakeStripeClient:
    fake_stripe = stripe_client or _FakeStripeClient()
    monkeypatch.setattr(service, "database", db)
    monkeypatch.setattr(service, "stripe_client", fake_stripe)
    monkeypatch.setattr(service, "_SCHEMA_GUARD_ATTEMPTED", False)
    return fake_stripe


@pytest.mark.asyncio
async def test_run_billing_cycle_happy_path_one_merchant_multiple_rollups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period_start = date(2026, 4, 1)
    period_end = date(2026, 4, 30)
    db = _FakeDatabase(
        merchants_to_bill=["merch_1"],
        merchant_customers={"merch_1": "cus_1"},
        gmv_rows_by_merchant={
            "merch_1": [
                _gmv_row(1, amount=125),
                _gmv_row(2, amount=275, agent_id=None, row_date=date(2026, 4, 2)),
            ]
        },
    )
    stripe_client = _install_fakes(monkeypatch, db)

    billing_run_id = await service.run_billing_cycle(period_start, period_end)

    assert billing_run_id == 101
    assert stripe_client.calls[0][0] == "invoice.create"
    # Step 5 (commit df79065) flipped Invoice.create auto_advance from False to
    # True. With True, Stripe auto-finalizes empty drafts on its internal timer
    # if our explicit finalize_invoice() never fires (and finalize_invoice now
    # tolerates the resulting "already finalized" race via commit 462d2ce).
    assert stripe_client.invoice_create_params[0]["auto_advance"] is True
    assert stripe_client.invoice_create_params[0]["collection_method"] == "charge_automatically"
    assert len(stripe_client.invoice_item_create_params) == 2
    assert all(params["invoice"] == "in_1" for params in stripe_client.invoice_item_create_params)
    assert all("invoice" in params for params in stripe_client.invoice_item_create_params)
    assert [row["source_type"] for row in db.billing_run_items] == ["gmv_rollup", "gmv_rollup"]
    assert [row["source_id"] for row in db.billing_run_items] == [1, 2]
    assert db.invoices[0]["stripe_invoice_id"] == "in_1"
    assert db.invoices[0]["stripe_customer_id"] == "cus_1"
    assert db.invoices[0]["total_cents"] == 400
    assert db.completed_billing_runs == [101]


@pytest.mark.asyncio
async def test_run_billing_cycle_idempotent_rerun_returns_existing_without_duplicate_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period_start = date(2026, 4, 1)
    period_end = date(2026, 4, 30)
    db = _FakeDatabase(
        merchants_to_bill=["merch_1"],
        merchant_customers={"merch_1": "cus_1"},
        gmv_rows_by_merchant={"merch_1": [_gmv_row(1, amount=125)]},
    )
    stripe_client = _install_fakes(monkeypatch, db)

    first_run_id = await service.run_billing_cycle(period_start, period_end)
    first_call_count = len(stripe_client.calls)
    second_run_id = await service.run_billing_cycle(period_start, period_end)

    assert first_run_id == second_run_id == 101
    assert len(stripe_client.calls) == first_call_count
    assert len(stripe_client.invoice_create_params) == 1
    assert len(stripe_client.invoice_item_create_params) == 1
    assert db.distinct_merchant_fetches == 1


@pytest.mark.asyncio
async def test_handle_dispute_replaces_draft_invoice_line_item(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDatabase()
    db.invoice_disputes[77] = {
        "id": 77,
        "invoice_id": 88,
        "merchant_id": "merch_1",
        "disputed_line_items_jsonb": [{"billing_run_item_id": 10, "adjusted_amount_cents": 275}],
        "status": "open",
    }
    db.invoices_by_id[88] = {
        "id": 88,
        "merchant_id": "merch_1",
        "billing_run_id": 101,
        "stripe_invoice_id": "in_1",
        "stripe_customer_id": "cus_1",
        "status": "draft",
    }
    db.billing_items_by_id[10] = {
        "id": 10,
        "billing_run_id": 101,
        "merchant_id": "merch_1",
        "source_type": "gmv_rollup",
        "source_id": 1,
        "stripe_invoice_item_id": "ii_old",
        "stripe_invoice_id": "in_1",
        "amount_cents": 500,
        "description": "GMV Take Rate - Agent agent_1 - 2026-04-01",
    }
    stripe_client = _install_fakes(monkeypatch, db)

    await service.handle_dispute(77)

    assert stripe_client.calls[0] == ("invoice_item.delete", "ii_old")
    replacement_params = stripe_client.invoice_item_create_params[0]
    assert replacement_params["customer"] == "cus_1"
    assert replacement_params["invoice"] == "in_1"
    assert replacement_params["amount"] == 275
    assert db.billing_items_by_id[10]["voided_at"] is True
    assert db.billing_run_items[-1]["source_type"] == "dispute_adj"
    assert db.billing_run_items[-1]["source_id"] == 77
    assert db.invoice_disputes[77]["status"] == "applied"


@pytest.mark.asyncio
async def test_generate_merchant_invoice_zero_gmv_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDatabase(
        merchant_customers={"merch_1": "cus_1"},
        gmv_rows_by_merchant={"merch_1": []},
    )
    stripe_client = _install_fakes(monkeypatch, db)

    result = await service.generate_merchant_invoice(101, "merch_1", date(2026, 4, 1), date(2026, 4, 30))

    assert result is None
    assert stripe_client.calls == []
    assert db.billing_run_items == []
    assert db.invoices == []


@pytest.mark.asyncio
async def test_generate_merchant_invoice_without_stripe_customer_logs_warning_and_skips(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = _FakeDatabase(merchant_customers={"merch_1": None})
    stripe_client = _install_fakes(monkeypatch, db)
    caplog.set_level(logging.WARNING, logger=service.__name__)

    result = await service.generate_merchant_invoice(101, "merch_1", date(2026, 4, 1), date(2026, 4, 30))

    assert result is None
    assert stripe_client.calls == []
    assert "stripe_customer_id is missing" in caplog.text


@pytest.mark.asyncio
async def test_generate_merchant_invoice_stripe_failure_mid_loop_logs_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = _FakeDatabase(
        merchant_customers={"merch_1": "cus_1"},
        gmv_rows_by_merchant={
            "merch_1": [
                _gmv_row(1, amount=125),
                _gmv_row(2, amount=275),
            ]
        },
    )
    stripe_client = _install_fakes(monkeypatch, db, _FakeStripeClient(fail_item_create_at=2))
    caplog.set_level(logging.ERROR, logger=service.__name__)

    with pytest.raises(RuntimeError, match="stripe item failure"):
        await service.generate_merchant_invoice(101, "merch_1", date(2026, 4, 1), date(2026, 4, 30))

    assert stripe_client.calls[0][0] == "invoice.create"
    assert len(stripe_client.invoice_item_create_params) == 1
    assert db.billing_run_items == []
    assert db.invoices == []
    assert "Stripe invoice generation failed" in caplog.text
    assert "stripe_invoice_id=in_1" in caplog.text
    assert "ii_1" in caplog.text


# === PR F: partial billing-run recovery (codex finding #7) ===
# run_billing_cycle previously marked the run 'completed' even when some
# per-merchant invoices failed — the same idempotency_key then prevented a
# clean retry of the failed merchants. The fix:
#   - track per-merchant success/failure during the loop
#   - mark 'partial_failed' if any failed (new status, migration 122)
#   - on resume, skip merchants that already have an invoices row.


class _MerchantFailingStripeClient(_FakeStripeClient):
    """Variant that fails invoice.create only for a named merchant."""

    def __init__(self, fail_for_merchant_id: str) -> None:
        super().__init__()
        self.fail_for_merchant_id = fail_for_merchant_id
        self._orig_invoices_create = self.v1.invoices.create

        def conditional_create(*args, **kwargs):
            params = _extract_stripe_params(args, kwargs)
            metadata = params.get("metadata") or {}
            if metadata.get("merchant_id") == self.fail_for_merchant_id:
                raise RuntimeError(f"stripe invoice.create failure for {self.fail_for_merchant_id}")
            return self._orig_invoices_create(*args, **kwargs)

        self.v1.invoices = SimpleNamespace(create=conditional_create)


@pytest.mark.asyncio
async def test_run_billing_cycle_partial_failure_marks_run_partial_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-merchant failure must surface in billing_runs.status; the previous
    code marked everything 'completed' and the idempotency key then locked
    the failed merchants out of retry."""
    period_start = date(2026, 4, 1)
    period_end = date(2026, 4, 30)
    db = _FakeDatabase(
        merchants_to_bill=["merch_ok", "merch_fail"],
        merchant_customers={"merch_ok": "cus_ok", "merch_fail": "cus_fail"},
        gmv_rows_by_merchant={
            "merch_ok": [_gmv_row(1, amount=100, merchant_id="merch_ok")],
            "merch_fail": [_gmv_row(2, amount=200, merchant_id="merch_fail")],
        },
    )
    stripe = _MerchantFailingStripeClient(fail_for_merchant_id="merch_fail")
    _install_fakes(monkeypatch, db, stripe)

    billing_run_id = await service.run_billing_cycle(period_start, period_end)

    assert billing_run_id == 101
    # Exactly one merchant got an invoices row.
    assert len(db.invoices) == 1
    assert db.invoices[0]["merchant_id"] == "merch_ok"
    # The run is NOT completed — it's partial_failed.
    assert db.completed_billing_runs == []
    assert db.billing_run_statuses[101] == "partial_failed"
    # Error column carries the failing merchant id.
    assert "merch_fail" in (db.billing_run_errors[101] or "")


@pytest.mark.asyncio
async def test_run_billing_cycle_resume_only_processes_missing_merchants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a partial_failed run, calling again with the same idempotency_key
    must resume only the merchants without an invoices row, leaving the
    already-billed ones untouched (PR E idempotency keys protect Stripe-side
    duplicates as a belt-and-suspenders backstop)."""
    period_start = date(2026, 4, 1)
    period_end = date(2026, 4, 30)
    db = _FakeDatabase(
        merchants_to_bill=["merch_ok", "merch_fail"],
        merchant_customers={"merch_ok": "cus_ok", "merch_fail": "cus_fail"},
        gmv_rows_by_merchant={
            "merch_ok": [_gmv_row(1, amount=100, merchant_id="merch_ok")],
            "merch_fail": [_gmv_row(2, amount=200, merchant_id="merch_fail")],
        },
    )

    # First attempt: merch_fail fails.
    stripe = _MerchantFailingStripeClient(fail_for_merchant_id="merch_fail")
    _install_fakes(monkeypatch, db, stripe)
    await service.run_billing_cycle(period_start, period_end)

    assert db.billing_run_statuses[101] == "partial_failed"
    assert len(db.invoices) == 1
    first_call_count = len(stripe.calls)

    # Second attempt: now merch_fail's stripe is healthy. Install a fresh
    # client that never fails so the resume actually completes.
    healthy_stripe = _install_fakes(monkeypatch, db)
    billing_run_id = await service.run_billing_cycle(period_start, period_end)

    assert billing_run_id == 101
    assert db.billing_run_statuses[101] == "completed"
    assert len(db.invoices) == 2
    # The healthy_stripe is a fresh client — only the missing merchant should
    # have hit it. invoice.create called exactly once (for merch_fail).
    invoice_create_calls = [c for c in healthy_stripe.calls if c[0] == "invoice.create"]
    assert len(invoice_create_calls) == 1, (
        f"resume should re-create exactly one invoice; got {invoice_create_calls}"
    )


@pytest.mark.asyncio
async def test_run_billing_cycle_completed_short_circuits_on_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call against an already-completed billing_run must not
    re-query merchants or hit Stripe at all — that's the original
    idempotency contract preserved by the new resume logic."""
    period_start = date(2026, 4, 1)
    period_end = date(2026, 4, 30)
    db = _FakeDatabase(
        merchants_to_bill=["merch_1"],
        merchant_customers={"merch_1": "cus_1"},
        gmv_rows_by_merchant={"merch_1": [_gmv_row(1, amount=125)]},
    )
    stripe_client = _install_fakes(monkeypatch, db)

    first_id = await service.run_billing_cycle(period_start, period_end)
    first_call_count = len(stripe_client.calls)
    assert db.billing_run_statuses[101] == "completed"

    second_id = await service.run_billing_cycle(period_start, period_end)

    assert first_id == second_id == 101
    # No new Stripe calls. No re-query of merchants.
    assert len(stripe_client.calls) == first_call_count
    assert db.distinct_merchant_fetches == 1


def test_partial_failed_status_value_is_what_migration_expects() -> None:
    """Migration 122 extends ck_billing_runs_status to include 'partial_failed'.
    The string MUST match between the migration's CHECK constraint and the
    service's UPDATE — a typo silently fails the DB write at runtime, and
    the FakeDB pattern won't catch it.
    """
    sql = service._PARTIAL_FAILED_BILLING_RUN_QUERY
    assert "status = 'partial_failed'" in sql, (
        "service writes 'partial_failed' — migration 122's CHECK constraint "
        "must include this exact string"
    )


def test_completed_query_clears_error_column() -> None:
    """A successful retry must clear the partial_failed error message so
    ops doesn't see a stale failure summary on a row that's now actually
    done.
    """
    sql = service._COMPLETE_BILLING_RUN_QUERY
    assert "error = NULL" in sql, (
        "completed transition must clear the partial_failed error summary"
    )

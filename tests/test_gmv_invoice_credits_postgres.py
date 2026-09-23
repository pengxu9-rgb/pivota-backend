"""GMV invoice credits on real Postgres, against the REAL billing schema (migrations 110, 112-115,
118-122), agent share (235) and the credit table (238). Stripe is a recording fake.

A refund on a day already invoiced leaves the rollup as billed and computes a PENDING credit for
what it took off; an admin's approval issues a Stripe credit note (open invoice: amount due; paid
invoice: customer balance; never cash); the agent's and the channel partner's shares follow.

    DATABASE_URL=postgresql://postgres@localhost:5432/pivota_refund_rollup_test \\
        .venv/bin/python -m pytest tests/test_gmv_invoice_credits_postgres.py
"""

import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("gmv_invoice_credits", "partner_settlement_completions", "agent_share_ledger", "agent_share_rates",
           "partner_balance_ledger",
           "partner_balance", "settlement_snapshots", "billing_run_items", "invoice_disputes", "invoices",
           "billing_runs", "gmv_attribution_daily", "commerce_attribution_edges")
_MIG = Path(__file__).resolve().parent.parent / "db/migrations"
_BILLING = ("113_billing_core.sql", "118_invoice_payment_failed_status.sql", "119_invoice_finalizing_status.sql",
            "120_invoices_billing_period_to_date.sql", "121_billing_runs_period_to_date.sql",
            "122_billing_runs_partial_failed_status.sql")
_PARTNER = ("112_partner_balance.sql", "114_settlement_snapshots.sql", "115_partner_balance_ledger.sql")

MERCHANT = "m_credit"
AGENT = "agent_1"
BILLED_AT = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
DAY = BILLED_AT.date()
PERIOD = (date(2026, 8, 1), date(2026, 8, 31))


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


def _no_channel_partner_fk(ddl: str) -> str:
    # channel_partners is not what is under test
    for clause in ("REFERENCES channel_partners(id) ON DELETE SET NULL",
                   "REFERENCES channel_partners(id) ON DELETE CASCADE",
                   "REFERENCES channel_partners(id) ON DELETE RESTRICT"):
        ddl = ddl.replace(clause, "")
    return ddl


async def _drop_all(database):
    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


async def _build_schema(database):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.commerce_attribution import commerce_attribution_edges
    from db.sql_migrations import split_statements

    await _drop_all(database)
    await database.execute(str(CreateTable(commerce_attribution_edges).compile(dialect=postgresql.dialect())))
    await database.execute(
        "ALTER TABLE commerce_attribution_edges "
        "ADD COLUMN channel_partner_id BIGINT, ADD COLUMN take_rate_applied_bp SMALLINT, "
        "ADD COLUMN refund_amount_cents BIGINT NOT NULL DEFAULT 0, ADD COLUMN refunded_at TIMESTAMPTZ"
    )
    names = ("110_gmv_attribution_daily.sql",) + _BILLING + _PARTNER + (
        "235_agent_share_accrual.sql", "236_agent_share_after_partner.sql", "238_gmv_invoice_credits.sql")
    for name in names:
        for stmt in split_statements(_no_channel_partner_fk((_MIG / name).read_text(encoding="utf-8"))):
            await database.execute(stmt)


class _FakeStripe:
    """invoices.retrieve / credit_notes.list / credit_notes.create, recording every create."""

    def __init__(self) -> None:
        self.invoice_status = "paid"
        self.notes: List[Dict[str, Any]] = []
        self.created: List[Dict[str, Any]] = []
        self.fail_create: Exception | None = None
        self.list_calls = 0
        outer = self

        class _Invoices:
            def retrieve(self, invoice, params=None, options=None):
                return {"id": invoice, "status": outer.invoice_status}

        class _Notes:
            def list(self, params=None, options=None):
                params = params or {}
                mine = [n for n in outer.notes if n["invoice"] == params.get("invoice")]
                if params.get("starting_after"):
                    ids = [n["id"] for n in mine]
                    mine = mine[ids.index(params["starting_after"]) + 1:]
                limit = int(params.get("limit") or 10)
                outer.list_calls += 1
                return {"data": mine[:limit], "has_more": len(mine) > limit}

            def create(self, params=None, options=None):
                if outer.fail_create is not None:
                    raise outer.fail_create
                note = {"id": f"cn_{len(outer.notes) + 1}", "status": "issued", "invoice": params["invoice"],
                        "metadata": params.get("metadata") or {},
                        # as Stripe: a note credited to the customer balance carries its transaction
                        "customer_balance_transaction": "cbtxn_1" if params.get("credit_amount") else None}
                outer.created.append({"params": params, "options": options})
                outer.notes.append(note)
                return note

        self.v1 = type("V1", (), {"invoices": _Invoices(), "credit_notes": _Notes()})()


@pytest.fixture(autouse=True)
async def db(monkeypatch):
    from db.database import database
    from services import commerce_attribution_service as cas
    from services import gmv_aggregation_service as gmv

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    async def _no_event(*a, **k):
        return {"interaction_id": "int_stub"}

    async def _flat_rate(merchant_id):
        return 1000  # 10%

    monkeypatch.setattr(cas, "record_commerce_event_best_effort", _no_event)
    monkeypatch.setattr(gmv, "_take_rate_bp_for_merchant", _flat_rate)
    monkeypatch.delenv("AGENT_SHARE_ACCRUAL_ENABLED", raising=False)
    await _build_schema(database)
    try:
        yield database
    finally:
        try:
            await _drop_all(database)  # DROP, not DELETE: leave nothing reduced for later gate files
        except Exception:  # noqa: BLE001
            pass
        if not was_connected and database.is_connected:
            await database.disconnect()


@pytest.fixture
def fake_stripe(monkeypatch):
    from services import gmv_invoice_credits as svc

    fake = _FakeStripe()
    monkeypatch.setattr(svc, "stripe_client", fake)
    return fake


async def _edge(db, *, edge_id="cae_1", order_id="ord_1", gross=10_000, channel_partner=None, created_at=BILLED_AT):
    await db.execute(
        "INSERT INTO commerce_attribution_edges (edge_id, merchant_id, order_id, agent_id, channel_partner_id, "
        " gross_attributed_gmv_cents, currency, refund_ids, refund_count, refunded_amount, created_at, updated_at) "
        "VALUES (:e, :m, :o, :a, :cp, :g, 'USD', '[]'::jsonb, 0, 0, :c, :c)",
        {"e": edge_id, "m": MERCHANT, "o": order_id, "a": AGENT, "cp": channel_partner, "g": gross, "c": created_at})


async def _billing_run(db, status="completed", tag="aug"):
    return await db.fetch_val(
        "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
        "VALUES (:s, :e, :k, :st) RETURNING id",
        {"s": PERIOD[0], "e": PERIOD[1], "k": f"{PERIOD[0]}-billing-{tag}", "st": status})


async def _invoice_the_day(db, run_id, *, invoice_status="paid", tag="1"):
    """What generate_merchant_invoice writes: one line per rollup row at its take, one invoice."""
    rollups = [dict(r) for r in await db.fetch_all(
        "SELECT id, take_amount_cents FROM gmv_attribution_daily WHERE date = :d AND merchant_id = :m ORDER BY id",
        {"d": DAY, "m": MERCHANT})]
    lines = []
    for i, r in enumerate(rollups):
        lines.append(await db.fetch_val(
            "INSERT INTO billing_run_items (billing_run_id, merchant_id, source_type, source_id, "
            "stripe_invoice_item_id, stripe_invoice_id, amount_cents) "
            "VALUES (:r, :m, 'gmv_rollup', :g, :ii, :inv, :amt) RETURNING id",
            {"r": run_id, "m": MERCHANT, "g": r["id"], "ii": f"ii_{tag}_{i}", "inv": f"in_{tag}",
             "amt": r["take_amount_cents"]}))
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, stripe_invoice_id, "
        "total_cents, status, billing_run_id, paid_at) VALUES (:m, :s, :e, :inv, :t, :st, :r, :paid)",
        {"m": MERCHANT, "s": PERIOD[0], "e": PERIOD[1], "inv": f"in_{tag}",
         "t": sum(r["take_amount_cents"] for r in rollups), "st": invoice_status, "r": run_id,
         "paid": datetime.now(timezone.utc) if invoice_status == "paid" else None})
    return lines


async def _billed(db, *, invoice_status="paid", channel_partner=None):
    """An edge whose day was rolled up (take 1000) and invoiced. Returns (line_id, rollup_id, run_id)."""
    from services.gmv_aggregation_service import aggregate_daily

    await _edge(db, channel_partner=channel_partner)
    await aggregate_daily(DAY)
    run_id = await _billing_run(db)
    (line,) = await _invoice_the_day(db, run_id, invoice_status=invoice_status)
    rollup_id = await db.fetch_val("SELECT source_id FROM billing_run_items WHERE id = :l", {"l": line})
    return line, rollup_id, run_id


async def _refund(amount: str, refund_id: str, order_id: str = "ord_1"):
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    return await attach_refund_to_attribution_edge(order_id=order_id, refund_id=refund_id, amount=Decimal(amount))


async def _credits(db, line=None):
    sql = "SELECT * FROM gmv_invoice_credits" + (" WHERE billing_run_item_id = :l" if line else "") + " ORDER BY id"
    return [dict(r) for r in await db.fetch_all(sql, {"l": line} if line else {})]


async def _rollup_take(db):
    return await db.fetch_val(
        "SELECT take_amount_cents FROM gmv_attribution_daily WHERE date = :d AND merchant_id = :m", {"d": DAY, "m": MERCHANT})


# ── computing what is owed ──────────────────────────────────────────────────────────────────────


async def test_a_refund_on_an_invoiced_day_computes_a_pending_credit_and_sends_nothing(db, fake_stripe):
    line, rollup_id, run_id = await _billed(db)

    await _refund("25.00", "re_1")

    assert await _rollup_take(db) == 1_000  # left exactly as billed
    (credit,) = await _credits(db, line)
    assert (credit["status"], credit["amount_cents"], credit["billed_cents"], credit["correct_take_cents"]) == (
        "pending", 250, 1_000, 750)
    assert (credit["gross_cents"], credit["refund_cents"], credit["take_rate_bp"]) == (10_000, 2_500, 1_000)
    assert (credit["rollup_id"], credit["billing_run_id"], credit["billed_day"]) == (rollup_id, run_id, DAY)
    assert fake_stripe.created == []  # nothing reaches Stripe without an approval


async def test_recompute_is_idempotent_and_a_second_refund_grows_the_one_open_credit(db, fake_stripe):
    from services.gmv_invoice_credits import compute_credit_for_line

    line, _, _ = await _billed(db)
    await _refund("25.00", "re_1")
    assert (await compute_credit_for_line(line)).status == "unchanged"
    await _refund("25.00", "re_1")  # redelivered: the edge does not move, nor does the credit
    await _refund("15.00", "re_2")

    (credit,) = await _credits(db, line)
    assert (credit["status"], credit["amount_cents"]) == ("pending", 400)


async def test_a_credit_is_only_ever_a_credit_never_a_charge(db, fake_stripe):
    from services.gmv_invoice_credits import compute_credit_for_line

    line, _, _ = await _billed(db)
    # Gross went UP after invoicing (a late gross stamp): the line is not billed upward. A gross that
    # moved for any reason other than a refund is left for a human, not recomputed.
    await db.execute("UPDATE commerce_attribution_edges SET gross_attributed_gmv_cents = 20000")
    assert (await compute_credit_for_line(line)).reason == "group_gross_changed"
    assert await _credits(db, line) == []


async def test_a_pending_credit_is_superseded_when_nothing_more_is_owed(db, fake_stripe):
    from services.gmv_invoice_credits import compute_credit_for_line

    line, _, _ = await _billed(db)
    await _refund("25.00", "re_1")
    await db.execute("UPDATE commerce_attribution_edges SET refund_amount_cents = 0")  # e.g. corrected

    assert (await compute_credit_for_line(line)).status == "superseded"
    (credit,) = await _credits(db, line)
    assert (credit["status"], credit["cancelled_by"]) == ("cancelled", "system")
    # superseded does not count as decided: a real refund later is still owed in full
    await db.execute("UPDATE commerce_attribution_edges SET refund_amount_cents = 2500")
    assert (await compute_credit_for_line(line)).status == "created"


async def test_an_admin_cancelled_credit_is_not_brought_back_by_the_daily_job(db, fake_stripe):
    from services.gmv_invoice_credits import cancel, run_daily

    line, _, _ = await _billed(db)
    await _refund("25.00", "re_1")
    (credit,) = await _credits(db, line)
    assert await cancel(credit["id"], by="ops@pivota", reason="merchant agreed to net it themselves") == "cancelled"

    await run_daily()
    assert [c["status"] for c in await _credits(db, line)] == ["cancelled"]
    # a further refund owes only its own delta
    await _refund("10.00", "re_2")
    assert [(c["status"], c["amount_cents"]) for c in await _credits(db, line)] == [
        ("cancelled", 250), ("pending", 100)]


async def test_the_daily_job_credits_a_day_invoiced_after_its_refund_was_frozen(db, fake_stripe):
    """An unfinished billing run freezes the re-roll; the resumed run then bills the pre-refund
    rollup. The daily job finds the invoiced line and computes what the refund is owed."""
    from services.gmv_aggregation_service import aggregate_daily
    from services.gmv_invoice_credits import run_daily

    await _edge(db)
    await aggregate_daily(DAY)
    run_id = await _billing_run(db, status="running")
    await _refund("25.00", "re_1")  # frozen: the run has not invoiced the merchant yet
    assert await _rollup_take(db) == 1_000 and await _credits(db) == []
    (line,) = await _invoice_the_day(db, run_id)

    summary = await run_daily()
    assert summary["computed"]["created"] == 1
    (credit,) = await _credits(db, line)
    assert (credit["status"], credit["amount_cents"]) == ("pending", 250)


async def test_a_voided_line_is_not_credited(db, fake_stripe):
    from services.gmv_invoice_credits import compute_credit_for_line

    line, _, _ = await _billed(db)
    await db.execute("UPDATE billing_run_items SET voided_at = NOW() WHERE id = :l", {"l": line})
    await db.execute("UPDATE commerce_attribution_edges SET refund_amount_cents = 2500")
    assert (await compute_credit_for_line(line)).reason == "line_voided"


# ── issuing ────────────────────────────────────────────────────────────────────────────────────


async def _pending_credit(db, *, invoice_status="paid", channel_partner=None, refund="25.00"):
    line, rollup_id, run_id = await _billed(db, invoice_status=invoice_status, channel_partner=channel_partner)
    await _refund(refund, "re_1")
    (credit,) = await _credits(db, line)
    return credit, line, rollup_id, run_id


async def test_approving_a_paid_invoices_credit_issues_it_to_the_customer_balance(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    credit, _, _, _ = await _pending_credit(db)
    assert await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    result = await issue(credit["id"], by="ops@pivota")

    assert (result.status, result.kind, result.stripe_credit_note_id) == ("issued", "customer_balance", "cn_1")
    (call,) = fake_stripe.created
    params = call["params"]
    assert params["invoice"] == "in_1" and params["credit_amount"] == 250
    assert "refund_amount" not in params and "out_of_band_amount" not in params  # never cash
    assert params["lines"] == [{"type": "custom_line_item", "description": params["lines"][0]["description"],
                                "quantity": 1, "unit_amount": 250}]
    assert params["metadata"]["gmv_invoice_credit_id"] == str(credit["id"])
    assert call["options"]["idempotency_key"] == f"gmv_invoice_credit:{credit['id']}:customer_balance:1"
    (row,) = await _credits(db)
    assert (row["status"], row["stripe_credit_note_id"], row["approved_by"], row["issued_by"]) == (
        "issued", "cn_1", "ops@pivota", "ops@pivota")


async def test_an_open_invoice_is_credited_against_its_amount_due(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    fake_stripe.invoice_status = "open"
    credit, _, _, _ = await _pending_credit(db, invoice_status="finalized")
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])

    assert (await issue(credit["id"], by="ops@pivota")).kind == "amount_due"
    params = fake_stripe.created[0]["params"]
    assert "credit_amount" not in params and "refund_amount" not in params


async def test_a_pending_credit_cannot_be_issued_without_approval(db, fake_stripe):
    from services.gmv_invoice_credits import issue

    credit, _, _, _ = await _pending_credit(db)
    assert (await issue(credit["id"], by="ops@pivota")).status == "not_issuable"
    assert fake_stripe.created == []


async def test_after_an_issued_credit_a_further_refund_owes_only_its_delta(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    credit, line, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    await issue(credit["id"], by="ops@pivota")
    await _refund("15.00", "re_2")

    assert [(c["status"], c["amount_cents"]) for c in await _credits(db, line)] == [("issued", 250), ("pending", 150)]


async def test_a_draft_invoice_fails_the_issue_and_a_retry_succeeds(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    fake_stripe.invoice_status = "draft"
    credit, _, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    failed = await issue(credit["id"], by="ops@pivota")
    assert failed.status == "failed" and "invoice_draft" in failed.error
    (row,) = await _credits(db)
    assert row["status"] == "failed" and row["stripe_credit_note_id"] is None

    fake_stripe.invoice_status = "paid"
    assert (await issue(credit["id"], by="ops@pivota")).status == "issued"
    assert len(fake_stripe.created) == 1


async def test_a_stripe_error_leaves_the_credit_failed_not_issued(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    credit, _, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    fake_stripe.fail_create = RuntimeError("card_error: nope")
    result = await issue(credit["id"], by="ops@pivota")

    assert result.status == "failed" and "nope" in result.error
    (row,) = await _credits(db)
    assert (row["status"], row["issue_attempts"]) == ("failed", 1)


async def test_a_retry_after_a_stripe_error_uses_a_fresh_idempotency_key(db, fake_stripe):
    """Stripe replays a key's first result, errors included, for 24h: a retry on the same key would
    get the same failure back."""
    from services.gmv_invoice_credits import approve, issue

    credit, _, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    keys = []
    real_create = fake_stripe.v1.credit_notes.create

    def _recording_create(params=None, options=None):
        keys.append(options["idempotency_key"])
        if len(keys) == 1:
            raise RuntimeError("api_error: try again")
        return real_create(params=params, options=options)

    fake_stripe.v1.credit_notes.create = _recording_create
    assert (await issue(credit["id"], by="ops@pivota")).status == "failed"
    assert (await issue(credit["id"], by="ops@later")).status == "issued"
    assert len(set(keys)) == 2
    (row,) = await _credits(db)
    assert (row["issue_attempts"], row["issued_by"], row["approved_by"]) == (2, "ops@later", "ops@pivota")


async def test_adoption_finds_the_credits_note_beyond_the_first_page(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    credit, _, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    for i in range(150):  # other credit notes on the same invoice
        fake_stripe.notes.append({"id": f"cn_other_{i:03d}", "status": "issued", "invoice": "in_1", "metadata": {}})
    fake_stripe.notes.append({"id": "cn_ours", "status": "issued", "invoice": "in_1",
                              "metadata": {"gmv_invoice_credit_id": str(credit["id"])}})
    await db.execute("UPDATE gmv_invoice_credits SET status = 'issuing', updated_at = NOW() - INTERVAL '1 hour' "
                     "WHERE id = :i", {"i": credit["id"]})

    result = await issue(credit["id"], by="ops@pivota")
    assert (result.status, result.stripe_credit_note_id) == ("adopted", "cn_ours")
    assert fake_stripe.created == [] and fake_stripe.list_calls == 2


async def test_a_void_invoice_cancels_the_credit(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    fake_stripe.invoice_status = "void"
    credit, _, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])

    assert (await issue(credit["id"], by="ops@pivota")).status == "cancelled"
    (row,) = await _credits(db)
    assert (row["status"], row["status_reason"]) == ("cancelled", "invoice_void")
    assert fake_stripe.created == []


async def test_a_crash_after_stripe_is_recovered_by_adopting_the_note_never_a_second_one(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    credit, _, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    # Stripe created the note; the process died before our write, leaving the credit `issuing`.
    fake_stripe.notes.append({"id": "cn_earlier", "status": "issued", "invoice": "in_1",
                              "metadata": {"gmv_invoice_credit_id": str(credit["id"])}})
    await db.execute("UPDATE gmv_invoice_credits SET status = 'issuing', updated_at = NOW() - INTERVAL '1 hour' "
                     "WHERE id = :i", {"i": credit["id"]})

    result = await issue(credit["id"], by="ops@pivota")
    assert (result.status, result.stripe_credit_note_id) == ("adopted", "cn_earlier")
    assert fake_stripe.created == []


async def test_a_fresh_issuing_credit_is_not_reissued(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    credit, _, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    await db.execute("UPDATE gmv_invoice_credits SET status = 'issuing', updated_at = NOW() WHERE id = :i",
                     {"i": credit["id"]})
    assert (await issue(credit["id"], by="ops@pivota")).status == "not_issuable"


# ── agent share ────────────────────────────────────────────────────────────────────────────────


async def _agent_rate(bp=2500):
    from services.agent_share_accrual import set_agent_share_rate

    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    await set_agent_share_rate(agent_id=AGENT, share_bp=bp, effective_from=now, created_by="t", now=now)


async def _settlement_completed(db, run_id, partner_ids=()):
    await db.execute("INSERT INTO partner_settlement_completions (billing_run_id, partner_ids, engine) "
                     "VALUES (:r, CAST(:p AS jsonb), 'v1')", {"r": run_id, "p": json.dumps(list(partner_ids))})


async def _agent_total(db, line):
    return await db.fetch_val("SELECT COALESCE(SUM(amount_minor), 0) FROM agent_share_ledger "
                              "WHERE billing_run_item_id = :l", {"l": line})


async def test_the_agents_share_follows_the_credit(db, fake_stripe, monkeypatch):
    from services.agent_share_accrual import accrue_for_line
    from services.gmv_invoice_credits import approve, issue

    monkeypatch.setenv("AGENT_SHARE_ACCRUAL_ENABLED", "true")
    await _agent_rate()
    credit, line, _, run_id = await _pending_credit(db)
    await _settlement_completed(db, run_id)  # no partner on this merchant
    assert (await accrue_for_line(line)).target_minor == 250  # 25% of the 1000 invoiced
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    await issue(credit["id"], by="ops@pivota")  # re-accrues the line after issuing

    assert await _agent_total(db, line) == 187  # 25% of the 750 Pivota kept


async def test_a_credit_after_settlement_moves_the_partner_and_the_agent_together(db, fake_stripe, monkeypatch):
    """Partner paid 20% of the take (200 of 1000); the agent gets 25% of what is left after the
    partner. A 250 credit claws 50 back from the partner, so the agent is priced against the 750
    Pivota kept and the 150 the partner kept: 25% x (750 - 150) = 150. Neither side double-counts."""
    from services.agent_share_accrual import accrue_for_line
    from services.gmv_invoice_credits import approve, issue

    monkeypatch.setenv("AGENT_SHARE_ACCRUAL_ENABLED", "true")
    await _agent_rate()
    credit, line, rollup_id, run_id = await _pending_credit(db, channel_partner=7)
    await _snapshot(db, run_id, counted=[rollup_id], bp=2000, paid_on_gmv=200)
    assert (await accrue_for_line(line)).target_minor == 200  # 25% x (1000 - 200)

    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    await issue(credit["id"], by="ops@pivota")

    assert [e["amount_cents"] for e in await _partner_ledger(db)] == [-50]
    assert await _agent_total(db, line) == 150


# ── channel partner share (settlement v1) ────────────────────────────────────────────────────────


async def _issued_partner_credit(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    credit, line, rollup_id, run_id = await _pending_credit(db, channel_partner=7)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    assert (await issue(credit["id"], by="ops@pivota")).status == "issued"
    return credit["id"], rollup_id, run_id


async def _snapshot(db, run_id, *, counted, netted=(), bp=2000, paid_on_gmv=200, engine="v1", complete=True):
    payload = {"gmv_rollup_ids_counted": list(counted), "netted_invoice_credit_ids": list(netted),
               "commission_config": {"gmv_take_share_bp": bp},
               "merchant_accruals": {MERCHANT: {"gmv_take_rev_cents": paid_on_gmv, "credited_comp_cents": paid_on_gmv}}}
    if engine == "v2":  # what partner_rev_share_engine_v2 writes: its own shape, no v1 share bp
        payload = {"commission_config": {"source": "structured_contract_columns"},
                   "merchant_accruals": {MERCHANT: {"gmv_share_cents": paid_on_gmv, "credited_comp_cents": paid_on_gmv}},
                   "v2_metadata": {"channel_partner_id": 7}}
    snapshot_id = await db.fetch_val(
        "INSERT INTO settlement_snapshots (billing_run_id, channel_partner_id, snapshot_payload_jsonb, "
        "computed_comp_cents) VALUES (:r, 7, CAST(:p AS jsonb), :c) RETURNING id",
        {"r": run_id, "p": json.dumps(payload), "c": paid_on_gmv})
    if complete:
        await _settlement_completed(db, run_id, partner_ids=[7])
    return snapshot_id


async def _partner_ledger(db):
    return [dict(r) for r in await db.fetch_all(
        "SELECT event_type, amount_cents, metadata FROM partner_balance_ledger ORDER BY id")]


async def test_settlement_nets_an_issued_credit_out_of_the_partners_take_and_records_it(db, fake_stripe):
    from services.partner_settlement_service import _gmv_take_by_merchant_from_rows, _gmv_take_rows

    credit_id, rollup_id, _ = await _issued_partner_credit(db, fake_stripe)
    rows = await _gmv_take_rows(7, *PERIOD)

    assert [(r["rollup_id"], r["take_amount_cents"], r["credited_cents"], list(r["credit_ids"])) for r in rows] == [
        (rollup_id, 1_000, 250, [credit_id])]
    assert _gmv_take_by_merchant_from_rows(rows) == {MERCHANT: 750}


async def test_compute_partner_comp_writes_what_reconcile_reads(db, monkeypatch):
    from services import partner_settlement_service as settlement

    async def _one(*a, **k):
        return {"id": 7, "commission_config_json": {"gmv_take_share_bp": 2000}}

    async def _rows(*a, **k):
        return [{"rollup_id": 11, "merchant_id": MERCHANT, "take_amount_cents": 1000, "credited_cents": 250,
                 "credit_ids": [5, 6]}]

    monkeypatch.setattr(settlement.database, "fetch_one", _one)
    monkeypatch.setattr(settlement, "_gmv_take_rows", _rows)
    monkeypatch.setattr(settlement, "_subscription_revenue_by_merchant", lambda *a, **k: _async({}))
    monkeypatch.setattr(settlement, "_attributed_merchants", lambda *a, **k: _async([]))
    monkeypatch.setattr(settlement, "_compute_churn_clawbacks", lambda *a, **k: _async([]))

    comp = await settlement.compute_partner_comp(7, *PERIOD)
    assert comp["gmv_rollup_ids_counted"] == [11] and comp["netted_invoice_credit_ids"] == [5, 6]
    assert comp["gmv_take_rev_cents"] == 150  # 20% of the 750 kept, not of the 1000 invoiced


async def _async(value):
    return value


async def test_a_credit_issued_after_settlement_is_clawed_back_once_pro_rata(db, fake_stripe):
    from services.gmv_invoice_credits import reconcile_partner, reconcile_partners

    credit_id, rollup_id, run_id = await _issued_partner_credit(db, fake_stripe)
    # _after_issue ran with no snapshot: awaiting settlement, nothing decided
    assert (await _credits(db))[0]["partner_status"] is None
    snapshot_id = await _snapshot(db, run_id, counted=[rollup_id])

    assert (await reconcile_partners())["clawed_back"] == 1
    (entry,) = await _partner_ledger(db)
    assert (entry["event_type"], entry["amount_cents"]) == ("clawback", -50)  # 20% of 250
    meta = entry["metadata"] if isinstance(entry["metadata"], dict) else json.loads(entry["metadata"])
    assert (meta["gmv_invoice_credit_id"], meta["source_snapshot_id"]) == (credit_id, snapshot_id)
    assert await db.fetch_val("SELECT balance_cents FROM partner_balance WHERE channel_partner_id = 7") == -50

    assert await reconcile_partner(credit_id) == "clawed_back"
    assert len(await _partner_ledger(db)) == 1  # once


async def test_a_clawback_never_exceeds_what_the_settlement_paid_on_that_merchants_take(db, fake_stripe):
    from services.gmv_invoice_credits import reconcile_partner

    credit_id, rollup_id, run_id = await _issued_partner_credit(db, fake_stripe)
    await _snapshot(db, run_id, counted=[rollup_id], bp=2000, paid_on_gmv=30)  # a subsidy cap bound it

    assert await reconcile_partner(credit_id) == "clawed_back"
    assert [e["amount_cents"] for e in await _partner_ledger(db)] == [-30]


async def test_a_credit_the_settlement_already_netted_is_not_clawed_back(db, fake_stripe):
    from services.gmv_invoice_credits import reconcile_partner

    credit_id, rollup_id, run_id = await _issued_partner_credit(db, fake_stripe)
    await _snapshot(db, run_id, counted=[rollup_id], netted=[credit_id])

    assert await reconcile_partner(credit_id) == "netted"
    assert await _partner_ledger(db) == []


async def test_a_row_the_settlement_did_not_pay_on_is_not_clawed_back(db, fake_stripe):
    from services.gmv_invoice_credits import reconcile_partner

    credit_id, _, run_id = await _issued_partner_credit(db, fake_stripe)
    await _snapshot(db, run_id, counted=[])  # its invoice was not paid when the partner settled

    assert await reconcile_partner(credit_id) == "netted"
    assert await _partner_ledger(db) == []


@pytest.mark.parametrize("flag_now", [False, True])
async def test_the_engine_that_wrote_the_snapshot_decides_not_todays_flag(db, fake_stripe, monkeypatch, flag_now):
    """A v2 settlement is never run through the v1 formula (its payload has no gmv_take_share_bp:
    a silent 0 clawback), whatever PARTNER_REV_SHARE_USE_V2 says today."""
    from config.settings import settings
    from services.gmv_invoice_credits import reconcile_partner

    monkeypatch.setattr(settings, "partner_rev_share_use_v2", flag_now)
    credit_id, _, run_id = await _issued_partner_credit(db, fake_stripe)
    await _snapshot(db, run_id, counted=[], engine="v2")

    assert await reconcile_partner(credit_id) == "v2_unhandled"
    assert await _partner_ledger(db) == []


async def test_a_v1_settlement_is_clawed_back_even_with_the_v2_flag_on_now(db, fake_stripe, monkeypatch):
    from config.settings import settings
    from services.gmv_invoice_credits import reconcile_partner

    credit_id, rollup_id, run_id = await _issued_partner_credit(db, fake_stripe)
    monkeypatch.setattr(settings, "partner_rev_share_use_v2", True)
    await _snapshot(db, run_id, counted=[rollup_id])

    assert await reconcile_partner(credit_id) == "clawed_back"
    assert [e["amount_cents"] for e in await _partner_ledger(db)] == [-50]


async def test_a_credit_with_no_channel_partner_is_not_applicable(db, fake_stripe):
    from services.gmv_invoice_credits import approve, issue

    credit, _, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops@pivota", expected_amount_cents=credit["amount_cents"])
    await issue(credit["id"], by="ops@pivota")
    assert (await _credits(db))[0]["partner_status"] == "not_applicable"


# ── the admin routes ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def admin_client():
    from fastapi import FastAPI

    from routes.admin_gmv_invoice_credits import router
    from utils.auth import require_admin

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: {"email": "ops@pivota", "role": "admin"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_the_admin_routes_review_approve_and_refuse_a_second_approval(db, fake_stripe, admin_client):
    credit, _, _, _ = await _pending_credit(db)
    base = "/admin/billing/invoice-credits"

    listed = (await admin_client.get(base, params={"status": "pending"})).json()
    assert [c["id"] for c in listed["credits"]] == [credit["id"]]
    detail = (await admin_client.get(f"{base}/{credit['id']}")).json()
    assert [e["edge_id"] for e in detail["refunded_edges"]] == ["cae_1"]

    # a refund lands after the review: the reviewed amount no longer matches, nothing is issued
    await _refund("10.00", "re_2")
    stale = await admin_client.post(f"{base}/{credit['id']}/approve", json={"amount_cents": 250})
    assert stale.status_code == 409 and stale.json()["amount_cents"] == 350
    assert fake_stripe.created == []

    approved = await admin_client.post(f"{base}/{credit['id']}/approve", json={"amount_cents": 350})
    assert approved.status_code == 200
    body = approved.json()
    assert (body["approved_by"], body["issue"]["status"]) == ("ops@pivota", "issued")

    again = await admin_client.post(f"{base}/{credit['id']}/approve", json={"amount_cents": 350})
    assert again.status_code == 409
    refused = await admin_client.post(f"{base}/{credit['id']}/cancel", json={"reason": "late"})
    assert refused.status_code == 409  # an issued credit cannot be cancelled here
    summary = (await admin_client.get(f"{base}/summary")).json()["by_status"]
    assert summary["issued"] == {"count": 1, "cents": 350}
    assert (await admin_client.get(f"{base}/999999")).status_code == 404
    assert (await admin_client.get(base, params={"status": "bogus"})).status_code == 422


# ── review of #2286 (the independent reviewer's reproductions, now fixed) ─────────────────────────


async def _void_line_and_bill_dispute(db, line, run_id, amount=750):
    """handle_dispute on a draft: void the line, bill the agreed amount as a dispute_adj replacement."""
    await db.execute("UPDATE billing_run_items SET voided_at = NOW() WHERE id = :l", {"l": line})
    await db.execute(
        "INSERT INTO billing_run_items (billing_run_id, merchant_id, source_type, source_id, stripe_invoice_item_id,"
        " stripe_invoice_id, amount_cents) VALUES (:r, :m, 'dispute_adj', 1, 'ii_adj', 'in_1', :a)",
        {"r": run_id, "m": MERCHANT, "a": amount})


async def test_a_pending_credit_is_cancelled_when_a_dispute_voids_its_line(db, fake_stripe):
    """A refund computed a credit while the invoice was a draft; a dispute then voided the line and
    billed its own adjustment. Crediting on top of that would credit the merchant twice."""
    from services.gmv_invoice_credits import approve, run_daily, CreditActionRefused

    credit, line, _, run_id = await _pending_credit(db, invoice_status="draft")
    await _void_line_and_bill_dispute(db, line, run_id)

    with pytest.raises(CreditActionRefused) as refused:  # approval refuses at once, before any job runs
        await approve(credit["id"], by="ops", expected_amount_cents=250)
    assert refused.value.code == "line_voided"
    await run_daily()
    (row,) = await _credits(db, line)
    assert (row["status"], row["status_reason"], row["cancelled_by"]) == ("cancelled", "line_voided", "system")
    assert fake_stripe.created == []


async def test_an_approved_or_failed_credit_on_a_voided_line_is_never_issued(db, fake_stripe):
    from services.gmv_invoice_credits import approve, compute_credit_for_line, issue

    credit, line, _, run_id = await _pending_credit(db)
    await approve(credit["id"], by="ops", expected_amount_cents=250)
    await _void_line_and_bill_dispute(db, line, run_id)

    assert (await issue(credit["id"], by="ops")).status == "not_issuable"
    assert (await compute_credit_for_line(line)).reason == "line_voided"
    assert [c["status"] for c in await _credits(db, line)] == ["cancelled"]
    assert fake_stripe.created == []


async def test_the_approve_route_refuses_a_voided_line_and_cancels_the_credit(db, fake_stripe, admin_client):
    credit, line, _, run_id = await _pending_credit(db, invoice_status="draft")
    await _void_line_and_bill_dispute(db, line, run_id)
    fake_stripe.invoice_status = "open"

    r = await admin_client.post(f"/admin/billing/invoice-credits/{credit['id']}/approve", json={"amount_cents": 250})
    assert r.status_code == 409 and "voided" in r.json()["detail"]
    assert [c["status"] for c in await _credits(db, line)] == ["cancelled"]
    assert fake_stripe.created == []


def _create_then_timeout(fake_stripe):
    notes = fake_stripe.v1.credit_notes
    real_create = type(notes).create

    def create_then_timeout(params=None, options=None):
        real_create(notes, params=params, options=options)
        raise TimeoutError("read timed out")

    notes.create = create_then_timeout


async def test_cancelling_a_failed_credit_stripe_actually_issued_records_it_as_issued(db, fake_stripe, monkeypatch):
    """Stripe created the note, the response was lost: `failed`. A cancel must not orphan the live
    note; it is recorded as issued, so the agent (and partner) shares net it."""
    from services.agent_share_accrual import accrue_for_line
    from services.gmv_invoice_credits import approve, cancel, compute_credit_for_line, issue

    monkeypatch.setenv("AGENT_SHARE_ACCRUAL_ENABLED", "true")
    await _agent_rate()
    credit, line, _, run_id = await _pending_credit(db)
    await _settlement_completed(db, run_id)
    await accrue_for_line(line)
    _create_then_timeout(fake_stripe)
    await approve(credit["id"], by="ops", expected_amount_cents=250)
    assert (await issue(credit["id"], by="ops")).status == "failed"

    assert await cancel(credit["id"], by="ops@later", reason="stripe error, giving up") == "issued"
    (row,) = await _credits(db, line)
    assert (row["status"], row["stripe_credit_note_id"], row["issued_by"]) == ("issued", "cn_1", "ops@later")
    assert row["stripe_credit_kind"] == "customer_balance"  # read off the note, not guessed
    assert (await compute_credit_for_line(line)).status == "nothing_owed"
    assert await _agent_total(db, line) == 187  # priced on the 750 kept


async def test_a_failed_credit_with_no_note_at_stripe_is_cancelled(db, fake_stripe):
    from services.gmv_invoice_credits import approve, cancel, issue

    credit, line, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops", expected_amount_cents=250)
    fake_stripe.fail_create = RuntimeError("card_error")
    assert (await issue(credit["id"], by="ops")).status == "failed"

    assert await cancel(credit["id"], by="ops", reason="merchant settled it offline") == "cancelled"
    (row,) = await _credits(db, line)
    assert (row["status"], row["cancelled_by"]) == ("cancelled", "ops")


async def test_a_failed_credit_is_not_cancelled_blind_when_stripe_cannot_be_asked(db, fake_stripe, admin_client):
    from services.gmv_invoice_credits import approve, issue

    credit, line, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops", expected_amount_cents=250)
    _create_then_timeout(fake_stripe)
    assert (await issue(credit["id"], by="ops")).status == "failed"

    def _list_down(params=None, options=None):
        raise ConnectionError("stripe unreachable")

    fake_stripe.v1.credit_notes.list = _list_down
    r = await admin_client.post(f"/admin/billing/invoice-credits/{credit['id']}/cancel", json={"reason": "give up"})
    assert r.status_code == 503
    (row,) = await _credits(db, line)
    assert row["status"] == "failed"  # still retryable, still decided
    assert "Stripe check for an issued note failed" in row["last_error"] and "unreachable" in row["last_error"]


async def test_a_partner_clawback_waits_for_the_runs_settlement_to_complete(db, fake_stripe):
    from services.gmv_invoice_credits import reconcile_partner

    credit_id, rollup_id, run_id = await _issued_partner_credit(db, fake_stripe)
    await _snapshot(db, run_id, counted=[rollup_id], complete=False)  # mid-run: snapshot, no completion
    assert await reconcile_partner(credit_id) is None
    assert await _partner_ledger(db) == []

    await _settlement_completed(db, run_id, partner_ids=[7])
    assert await reconcile_partner(credit_id) == "clawed_back"


async def test_a_settled_run_that_paid_this_partner_nothing_needs_no_clawback(db, fake_stripe):
    from services.gmv_invoice_credits import reconcile_partner

    credit_id, _, run_id = await _issued_partner_credit(db, fake_stripe)
    await _settlement_completed(db, run_id)  # completed; no snapshot for partner 7

    assert await reconcile_partner(credit_id) == "netted"
    assert await _partner_ledger(db) == []


async def test_the_model_and_migration_238_build_the_same_table(db):
    """Prod gets the table from the model (metadata.create_all at startup); environments that apply
    numbered migrations get it from 238. They must not drift."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from db.gmv_invoice_credits import gmv_invoice_credits
    from db.sql_migrations import split_statements

    facts = (
        "SELECT 'col '||column_name||' '||data_type||' '||is_nullable||' '||coalesce(column_default,'') "
        "FROM information_schema.columns WHERE table_name='gmv_invoice_credits' "
        "UNION ALL SELECT 'con '||conname||' '||pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid='gmv_invoice_credits'::regclass "
        "UNION ALL SELECT 'idx '||indexname||' '||indexdef FROM pg_indexes WHERE tablename='gmv_invoice_credits'")
    from_migration = sorted(r[0] for r in await db.fetch_all(facts))  # the fixture built it from 238
    await db.execute("DROP TABLE gmv_invoice_credits")
    await db.execute(str(CreateTable(gmv_invoice_credits).compile(dialect=postgresql.dialect())))
    for index in gmv_invoice_credits.indexes:
        await db.execute(str(CreateIndex(index).compile(dialect=postgresql.dialect())))
    from_model = sorted(r[0] for r in await db.fetch_all(facts))
    assert from_model == from_migration and len(from_model) > 40


# ── re-review of #2286 ───────────────────────────────────────────────────────────────────────────


async def test_a_group_whose_gross_moved_without_a_refund_is_left_for_a_human(db, fake_stripe):
    """An edge re-attributed out of an invoiced group takes its gross with it; nothing was refunded.
    Recomputing the group would credit what the move took out. Only refunds are credited."""
    from services.gmv_aggregation_service import aggregate_daily
    from services.gmv_invoice_credits import compute_credit_for_line

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000)
    await _edge(db, edge_id="cae_2", order_id="ord_2", gross=5_000)
    await aggregate_daily(DAY)
    (line,) = await _invoice_the_day(db, await _billing_run(db))  # billed 1500
    await db.execute("UPDATE commerce_attribution_edges SET agent_id = 'agent_2' WHERE edge_id = 'cae_2'")

    result = await compute_credit_for_line(line)
    assert (result.status, result.reason) == ("skipped", "group_gross_changed")
    assert await _credits(db, line) == []


async def test_an_interrupted_cancel_hands_the_credit_back_as_failed(db, fake_stripe, monkeypatch):
    import asyncio

    from services import gmv_invoice_credits as svc

    credit, line, _, _ = await _pending_credit(db)
    await svc.approve(credit["id"], by="ops", expected_amount_cents=250)
    fake_stripe.fail_create = RuntimeError("api_error")
    assert (await svc.issue(credit["id"], by="ops")).status == "failed"

    async def _interrupted(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(svc, "_existing_note", _interrupted)
    with pytest.raises(asyncio.CancelledError):
        await svc.cancel(credit["id"], by="ops", reason="give up")
    (row,) = await _credits(db, line)
    assert row["status"] == "failed"  # not stuck in issuing
    assert row["last_error"].startswith("cancel interrupted before Stripe was checked")


async def test_a_stale_issuing_credit_can_be_cancelled_after_checking_stripe(db, fake_stripe):
    """An issue interrupted mid-way, or a line voided under an issuing credit: cancel is the way out,
    not issue(), which would send the note the admin is cancelling."""
    from services.gmv_invoice_credits import approve, cancel

    credit, line, _, run_id = await _pending_credit(db)
    await approve(credit["id"], by="ops", expected_amount_cents=250)
    await db.execute("UPDATE gmv_invoice_credits SET status = 'issuing', issue_attempts = 1, "
                     "updated_at = NOW() - INTERVAL '1 hour' WHERE id = :i", {"i": credit["id"]})
    await _void_line_and_bill_dispute(db, line, run_id)

    assert await cancel(credit["id"], by="ops", reason="line voided under it") == "cancelled"
    assert [c["status"] for c in await _credits(db, line)] == ["cancelled"]
    assert fake_stripe.created == []


async def test_a_fresh_issuing_credit_cannot_be_cancelled_under_a_running_issue(db, fake_stripe):
    from services.gmv_invoice_credits import approve, cancel

    credit, _, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops", expected_amount_cents=250)
    await db.execute("UPDATE gmv_invoice_credits SET status = 'issuing', updated_at = NOW() WHERE id = :i",
                     {"i": credit["id"]})
    assert await cancel(credit["id"], by="ops", reason="too soon") == "not_cancellable"


async def test_the_cancel_route_reaches_a_stale_issuing_credit(db, fake_stripe, admin_client):
    from services.gmv_invoice_credits import approve

    credit, line, _, _ = await _pending_credit(db)
    await approve(credit["id"], by="ops", expected_amount_cents=250)
    await db.execute("UPDATE gmv_invoice_credits SET status = 'issuing', updated_at = NOW() - INTERVAL '1 hour' "
                     "WHERE id = :i", {"i": credit["id"]})
    r = await admin_client.post(f"/admin/billing/invoice-credits/{credit['id']}/cancel", json={"reason": "stuck"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"

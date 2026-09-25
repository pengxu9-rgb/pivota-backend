"""W6 billing invariant: a merchant is charged (or a free credit consumed)
IF AND ONLY IF a completed report/deliverable was delivered.

Enforced three ways:
  1. Every failure exit in audit_run_worker refunds the launch debit — and a
     source-scan test pins the structure (all FAILED transitions go through
     the single `_fail_run_and_refund` exit, so a future failure branch can't
     silently skip the refund).
  2. The refund is idempotent PER RUN + CREDIT KIND (reason excluded from the
     ledger key), so two failure paths reaching the same run can't double-pay.
  3. /actions/start persists a measured draft, debit and task atomically.
     Failed generation is never charged; task failure rolls back the transaction.

The free-allowance counter (count_runs_for_merchant_by_subject) excludes
failed runs for the same reason — a failed audit must not burn a free credit.
"""
from __future__ import annotations
from contextlib import asynccontextmanager

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

WORKER_SRC = Path("services/audit_run_worker.py").read_text()


# ---- 1. Structural invariant: one failure exit, always refunding ----------

def test_all_failed_transitions_go_through_the_single_refund_exit():
    """`to_stage=mar.STAGE_FAILED` may appear ONLY inside
    _fail_run_and_refund. A new failure branch that transitions directly
    would bypass the refund and break charged-iff-delivered — this test makes
    that a red build instead of a prod ledger bug."""
    occurrences = [
        m.start() for m in re.finditer(
            r"to_stage=mar\.STAGE_FAILED", WORKER_SRC,
        )
    ]
    assert len(occurrences) == 1, (
        f"{len(occurrences)} direct STAGE_FAILED transitions in "
        "audit_run_worker.py — every failure exit must go through "
        "_fail_run_and_refund so the launch debit is refunded."
    )
    # ... and that one occurrence is inside the helper itself.
    helper_start = WORKER_SRC.index("async def _fail_run_and_refund")
    helper_end = WORKER_SRC.index("def _has_recorded_probe_payloads")
    assert helper_start < occurrences[0] < helper_end


def test_every_known_failure_reason_is_wired():
    """The six worker failure paths + the reaper all exit with a refund
    reason. Presence-of-string checks — cheap, but they catch an accidental
    revert of any single site."""
    for reason in (
        "resume_rehydrate_failed",
        "resume_unsupported",
        "probe_infra_failure",
        "upstream_mock_fallback",
        "verifying_post_processing",
        "worker_exception",
        "audit_abandoned_reaped",
    ):
        assert f'"{reason}"' in WORKER_SRC or f"reason={reason!r}" in WORKER_SRC, (
            f"failure reason {reason!r} no longer wired to a refund"
        )


# ---- 2. Refund idempotency: per run+kind, never per reason -----------------

@pytest.mark.asyncio
async def test_refund_key_excludes_reason_so_double_refund_dedupes(monkeypatch):
    from services import audit_run_worker as worker

    calls: List[Dict[str, Any]] = []

    async def fake_credit(merchant_id, kind, amount, *, source_event_id,
                          usd_cogs=0, purchased_credits=None, conn=None):
        calls.append({
            "merchant_id": merchant_id, "kind": kind, "amount": amount,
            "source_event_id": source_event_id,
        })
        return {"ok": True}

    import services.merchant_credit_balance_service as mcb
    monkeypatch.setattr(mcb, "credit", fake_credit)

    launch = {"billing_mode": "credits", "estimated_audit_credits": 7}
    await worker._refund_launch_debits(
        merchant_id="m1", run_id="r1", launch_options=launch,
        reason="worker_exception",
    )
    await worker._refund_launch_debits(
        merchant_id="m1", run_id="r1", launch_options=launch,
        reason="audit_abandoned_reaped",
    )
    assert len(calls) == 2
    assert calls[0]["amount"] == 7
    # Same ledger key regardless of which failure path fired — the ledger's
    # source_event_id dedup makes the second call a no-op.
    assert calls[0]["source_event_id"] == calls[1]["source_event_id"]
    assert calls[0]["source_event_id"] == "refund:audit_run:r1:audit"


@pytest.mark.asyncio
async def test_refund_failure_never_raises(monkeypatch):
    from services import audit_run_worker as worker

    async def exploding_credit(*a, **k):
        raise RuntimeError("ledger down")

    import services.merchant_credit_balance_service as mcb
    monkeypatch.setattr(mcb, "credit", exploding_credit)

    # Must log-and-continue: a refund failure can't mask the failure path.
    await worker._refund_launch_debits(
        merchant_id="m1", run_id="r1",
        launch_options={"estimated_audit_credits": 5},
        reason="worker_exception",
    )


# ---- 3. _fail_run_and_refund: refund iff this worker won the transition ----

@pytest.mark.asyncio
@pytest.mark.parametrize("transition_ok,expect_refund", [(True, True), (False, False)])
async def test_fail_run_refunds_iff_transition_succeeds(
    monkeypatch, transition_ok, expect_refund,
):
    from services import audit_run_worker as worker
    import db.merchant_audit_runs as mar

    async def fake_transition(**kwargs):
        return transition_ok

    refunds: List[str] = []

    async def fake_refund(*, merchant_id, run_id, launch_options, reason):
        refunds.append(run_id)

    monkeypatch.setattr(mar, "transition_stage", fake_transition)
    monkeypatch.setattr(worker, "_refund_launch_debits", fake_refund)

    ok = await worker._fail_run_and_refund(
        run_id="r1", merchant_id="m1",
        launch_options={"estimated_audit_credits": 5},
        from_stage="probing", error_jsonb={"stage": "probing"},
        reason="worker_exception",
    )
    assert ok is transition_ok
    assert (len(refunds) == 1) is expect_refund


@pytest.mark.asyncio
async def test_fail_run_survives_transition_exception(monkeypatch):
    from services import audit_run_worker as worker
    import db.merchant_audit_runs as mar

    async def exploding_transition(**kwargs):
        raise RuntimeError("db down")

    refunds: List[str] = []

    async def fake_refund(**kwargs):
        refunds.append("x")

    monkeypatch.setattr(mar, "transition_stage", exploding_transition)
    monkeypatch.setattr(worker, "_refund_launch_debits", fake_refund)

    ok = await worker._fail_run_and_refund(
        run_id="r1", merchant_id="m1", launch_options={},
        from_stage="probing", error_jsonb={}, reason="worker_exception",
    )
    assert ok is False
    assert refunds == []  # no terminal write -> not this worker's refund


# ---- 4. Abandoned reaper refunds what it reaps ------------------------------

@pytest.mark.asyncio
async def test_reaper_refunds_reaped_runs(monkeypatch):
    from services import audit_run_worker as worker
    import db.merchant_audit_runs as mar

    async def fake_fail_abandoned():
        return [
            {"run_id": "r1", "merchant_id": "m1",
             "launch_options": {"estimated_audit_credits": 7}},
            {"run_id": "r2", "merchant_id": "m2", "launch_options": {}},
        ]

    refunded: List[Dict[str, Any]] = []

    async def fake_refund(*, merchant_id, run_id, launch_options, reason):
        refunded.append({
            "run_id": run_id, "merchant_id": merchant_id,
            "launch_options": launch_options, "reason": reason,
        })

    monkeypatch.setattr(mar, "fail_abandoned_runs", fake_fail_abandoned)
    monkeypatch.setattr(worker, "_refund_launch_debits", fake_refund)

    await worker.run_abandoned_run_reaper_tick()

    assert [r["run_id"] for r in refunded] == ["r1", "r2"]
    assert refunded[0]["reason"] == "audit_abandoned_reaped"
    assert refunded[0]["launch_options"] == {"estimated_audit_credits": 7}


# ---- 5. Free allowance counts delivered runs only ---------------------------

@pytest.fixture
async def sqlite_db(monkeypatch):
    from databases import Database
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(f"sqlite:///{tmp.name}")
    await db.connect()
    await db.execute("""
        CREATE TABLE merchant_audit_runs (
          run_id TEXT PRIMARY KEY,
          merchant_id TEXT NOT NULL,
          subject_type TEXT,
          requested_at TIMESTAMP NOT NULL,
          status TEXT NOT NULL,
          stage TEXT
        )
    """)
    monkeypatch.setattr("db.merchant_audit_runs.database", db)
    import db.merchant_audit_runs as mar
    mar._DDL_READY = True
    yield db
    await db.disconnect()
    mar._DDL_READY = False
    os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_failed_and_cancelled_runs_do_not_consume_free_allowance(sqlite_db):
    from db.merchant_audit_runs import count_runs_for_merchant_by_subject

    now = datetime.now(timezone.utc)
    rows = [
        # counts: delivered + in-flight
        ("done1", "succeeded", "completed"),
        ("running1", "running", "probing"),
        # must NOT count: failed and cancelled (cancelled also carries
        # legacy status='failed' — transition_stage maps every
        # non-completed terminal to 'failed')
        ("failed1", "failed", "failed"),
        ("cancelled1", "failed", "cancelled"),
    ]
    for run_id, status_val, stage in rows:
        await sqlite_db.execute(
            "INSERT INTO merchant_audit_runs "
            "(run_id, merchant_id, subject_type, requested_at, status, stage) "
            "VALUES (:r, 'm1', 'merchant_url', :t, :s, :g)",
            {"r": run_id, "t": now, "s": status_val, "g": stage},
        )

    used = await count_runs_for_merchant_by_subject(
        merchant_id="m1", subject_type="merchant_url",
    )
    assert used == 2


# ---- 6. /actions/start: charge before draft, refund a failed draft ---------

def _action_body(mar_module):
    return mar_module.MerchantAuditActionStartRequest(
        run_id="run-12345678",
        headline="Publish a comparison page",
        first_move="Draft the comparison",
        sku_title="Test SKU",
    )


@pytest.fixture
def actions_env(monkeypatch):
    """Wire /actions/start's dependencies to fakes; returns the shared
    call-order log so tests can assert charge-vs-draft ordering."""
    import routes.merchant_audit_routes as mar_routes
    import db.merchant_tasks as tasks_db

    log: List[str] = []
    env = SimpleNamespace(log=log, draft_result="the draft", consume_raises=None)

    async def fake_fetch(run_id):
        return {"merchant_id": "m1", "report_jsonb": {"per_sku_reports": []}}

    async def fake_candidates(**kwargs):
        return []

    async def fake_record_task(**kwargs):
        log.append("task_created")
        return "task-1"

    async def fake_consume(merchant_id, op, idem, *, probes=None, **kwargs):
        log.append("consume")
        if env.consume_raises:
            raise env.consume_raises
        return {"credits": 5}

    async def fake_refund(merchant_id, op, credits, source_event_id, **kwargs):
        log.append(f"refund:{credits}:{source_event_id}")
        return {"credits": credits}

    async def fake_answer(**kwargs):
        log.append("draft")
        return env.draft_result

    monkeypatch.setattr(mar_routes, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(mar_routes, "consume_credits", fake_consume)
    monkeypatch.setattr(mar_routes, "refund_credits", fake_refund)
    monkeypatch.setattr(mar_routes, "generate_measured_text", fake_answer)
    monkeypatch.setattr(
        mar_routes, "_build_ask_context", lambda report, pk: {"ctx": 1},
    )
    monkeypatch.setattr(
        mar_routes, "_action_product_key_by_title", lambda report, t: None,
    )
    monkeypatch.setattr(
        mar_routes, "estimate_probe_credits", lambda spec: (5, 0),
    )
    monkeypatch.setattr(
        tasks_db, "find_pending_supersede_candidates", fake_candidates,
    )
    monkeypatch.setattr(tasks_db, "record_task_created", fake_record_task)
    @asynccontextmanager
    async def transaction():
        yield
    async def execute(*a, **kw):
        return None
    monkeypatch.setattr(mar_routes, "database", SimpleNamespace(transaction=transaction, execute=execute))
    async def measured(**kw):
        if env.consume_raises:
            raise env.consume_raises
        answer = await kw["generate"]()
        if not answer:
            raise ValueError("No draft")
        log.append("consume")
        return {"answer": answer, "credits_charged": 0.0672}
    monkeypatch.setattr(mar_routes, "run_measured_generation", measured)
    env.routes = mar_routes
    return env


@pytest.mark.asyncio
async def test_action_draft_charges_measured_usage_after_generating(actions_env):
    out = await actions_env.routes.start_merchant_audit_action(
        _action_body(actions_env.routes), merchant_id="m1",
    )
    # Actual usage is available only after generation; transaction tests cover rollback.
    assert actions_env.log.index("draft") < actions_env.log.index("consume")
    assert out["credits_charged"] == 0.0672
    assert out["draft"] == "the draft"


@pytest.mark.asyncio
async def test_action_draft_refunds_when_generation_fails(actions_env):
    actions_env.draft_result = None  # LLM produced nothing
    out = await actions_env.routes.start_merchant_audit_action(
        _action_body(actions_env.routes), merchant_id="m1",
    )
    refunds = [e for e in actions_env.log if e.startswith("refund:5:refund:action_draft:")]
    assert not refunds
    assert "consume" not in actions_env.log
    assert out["credits_charged"] == 0
    assert out["draft"] is None
    assert out["task_id"] == "task-1"  # the tracked task is still created


@pytest.mark.asyncio
async def test_action_insufficient_credits_skips_draft_but_creates_task(actions_env):
    from services.merchant_credit_balance_service import InsufficientCreditsError
    actions_env.consume_raises = InsufficientCreditsError("m1", "prompt", 5, 0)
    out = await actions_env.routes.start_merchant_audit_action(
        _action_body(actions_env.routes), merchant_id="m1",
    )
    assert "draft" not in [e for e in actions_env.log if e == "draft"]
    assert out["credits_charged"] == 0
    assert out["draft"] is None
    assert out["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_action_draft_returns_placement(actions_env, monkeypatch):
    """Founder feedback: a draft with no WHERE ('I have no idea what is our
    target media or where to update') is a confusing artifact. On-page drafts
    carry the SKU's own page; outreach drafts carry the channel host."""
    mar_routes = actions_env.routes

    async def fake_fetch(run_id):
        return {
            "merchant_id": "m1",
            "report_jsonb": {
                "per_sku_reports": [{
                    "sku_key": "sku-1",
                    "sku_title": "Test SKU",
                    "identity": {"anchors": {
                        "canonical_url": "https://mojawa.com/products/purra-swim",
                    }},
                }],
            },
        }

    monkeypatch.setattr(mar_routes, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(
        mar_routes, "_action_product_key_by_title", lambda report, t: "sku-1",
    )
    out = await mar_routes.start_merchant_audit_action(
        _action_body(mar_routes), merchant_id="m1",
    )
    assert out["placement"] == {
        "kind": "own_page",
        "label": "Your product page",
        "url": "https://mojawa.com/products/purra-swim",
        "target_host": None,
    }

    outreach = mar_routes.MerchantAuditActionStartRequest(
        run_id="run-12345678",
        headline="Pitch wired.com",
        first_move="Send the pitch",
        sku_title="Test SKU",
        channel_host="wired.com",
        channel_lever="editorial_outreach",
    )
    out2 = await mar_routes.start_merchant_audit_action(outreach, merchant_id="m1")
    assert out2["placement"]["kind"] == "channel"
    assert out2["placement"]["target_host"] == "wired.com"
    assert "wired.com" in out2["placement"]["label"]

    # Lever-only outreach (no host) stays channel-kind — an outreach draft
    # must never claim "Your product page" as its destination.
    lever_only = mar_routes.MerchantAuditActionStartRequest(
        run_id="run-12345678",
        headline="Pitch the channel",
        first_move="Send the pitch",
        sku_title="Test SKU",
        channel_lever="editorial_outreach",
    )
    out3 = await mar_routes.start_merchant_audit_action(lever_only, merchant_id="m1")
    assert out3["placement"]["kind"] == "channel"
    assert out3["placement"]["target_host"] is None


# ---- 7. /ask: the recovery projection is EXTRA context, not a replacement --


def _ask_report():
    """A retained report with a narrative and two SKUs, plus enough for the
    recovery projection to have something in it."""
    return {
        "merchant_narrative": {
            "headline_story": "AI sends your buyers to retailers.",
            "whats_working": {"summary": "You are named on category questions."},
            "where_youre_losing": {"summary": "No first-party citation."},
            "prioritized_actions": [
                {"headline": "Publish a comparison page",
                 "title": "Publish a comparison page",
                 "severity": "high"},
            ],
            "honest_limits": ["Only 12 queries were probed."],
        },
        "per_sku_reports": [
            {"sku_key": "sku-a", "sku_title": "Alpha Serum",
             "identity": {"name": "Alpha Serum"}, "band": "blocked"},
            {"sku_key": "sku-b", "sku_title": "Beta Cream",
             "identity": {"name": "Beta Cream"}, "band": "partial"},
        ],
    }


@pytest.fixture
def ask_env(monkeypatch):
    import routes.merchant_audit_routes as mar_routes

    log: List[str] = []
    env = SimpleNamespace(log=log, report=_ask_report(), context=None,
                          routes=mar_routes)

    async def fake_fetch(run_id):
        return {"merchant_id": "m1", "subject_type": "merchant_url",
                "report_jsonb": env.report}

    async def fake_answer(*, system_prompt, user_message):
        log.append("answer")
        env.context = json.loads(
            user_message.split("CONTEXT:\n", 1)[1].split("\n\nQUESTION:", 1)[0]
        )
        return "here you go"

    async def fake_consume(merchant_id, op, idem, *, probes=None, **kwargs):
        log.append("consume")
        return {"credits": 1}

    async def fake_paid(merchant_id):
        return True

    monkeypatch.setattr(mar_routes, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(mar_routes, "generate_measured_text", fake_answer)
    monkeypatch.setattr(mar_routes, "consume_credits", fake_consume)
    monkeypatch.setattr(mar_routes, "merchant_is_paid_tier", fake_paid)
    monkeypatch.setattr(mar_routes, "estimate_probe_credits", lambda spec: (1, 0))
    async def measured(**kw):
        answer = await kw["generate"]()
        log.append("consume")
        return {"answer": answer, "credits_charged": 0.0672}
    monkeypatch.setattr(mar_routes, "run_measured_generation", measured)
    return env


def _ask_body(mar_module, **kw):
    return mar_module.MerchantAuditAskRequest(
        run_id="run-12345678", question="Where am I losing?", **kw
    )


@pytest.mark.asyncio
async def test_ask_context_keeps_the_narrative_and_the_product_key_focus(ask_env):
    """recovery_from_report REPLACED _build_ask_context, dropping the overview,
    whats_working, where_youre_losing, honest limits and the per-SKU slice —
    while `product_key` still keyed the idempotent debit. It focuses nothing if
    the context it focuses is gone."""
    await ask_env.routes.answer_merchant_audit_question(
        _ask_body(ask_env.routes, product_key="sku-b"), merchant_id="m1",
    )

    context = ask_env.context
    assert context["overview"]["headline"] == "AI sends your buyers to retailers."
    assert context["overview"]["whats_working"]
    assert context["overview"]["where_youre_losing"]
    assert context["overview"]["honest_limits"]
    # The FOCUS: product_key picked one SKU, and it is the one in the context.
    assert context["product"]["name"] == "Beta Cream"
    assert "products" not in context
    # ...and the recovery projection is merged in beside it, at the top level
    # where the paywall can see stages[].actions.
    assert context["audience"] == "revenue_recovery"
    assert [s["stage"] for s in context["stages"]]


@pytest.mark.asyncio
async def test_ask_without_a_product_key_still_lists_the_skus(ask_env):
    await ask_env.routes.answer_merchant_audit_question(
        _ask_body(ask_env.routes), merchant_id="m1",
    )
    names = [p["name"] for p in ask_env.context["products"]]
    assert names == ["Alpha Serum", "Beta Cream"]


@pytest.mark.asyncio
async def test_ask_409s_on_a_contentless_report_and_never_debits(ask_env):
    """`if not context` went dead when the context became a projection: a
    projection is ALWAYS a populated dict (audience, builder version, three
    stage scaffolds), so an audit that used to 409 for free started charging a
    credit to answer from nothing."""
    ask_env.report = {}
    with pytest.raises(HTTPException) as exc:
        await ask_env.routes.answer_merchant_audit_question(
            _ask_body(ask_env.routes), merchant_id="m1",
        )
    assert exc.value.status_code == 409
    assert ask_env.log == [], "a 409'd ask must neither answer nor debit"


@pytest.mark.asyncio
async def test_ask_charges_when_it_answers(ask_env):
    out = await ask_env.routes.answer_merchant_audit_question(
        _ask_body(ask_env.routes), merchant_id="m1",
    )
    assert ask_env.log == ["answer", "consume"]
    assert out["credits_charged"] == 0.0672


def test_the_rebuilt_recovery_projection_carries_actions_for_the_paywall():
    """recovery_from_report hardcoded actions=[], so every stage's action list
    was empty on every rebuilt projection — the "what to do" layer vanished,
    and _strip_actions_for_free_tier (which counts and empties
    stages[].actions) had nothing to strip and stamped a lock over zero
    counts. extract_actions is the same reader persist_canonical_evidence uses
    to WRITE action_plan_items, so a rebuilt projection now carries what a
    persisted one carries."""
    import routes.merchant_audit_routes as mar_routes
    from services.revenue_recovery_report import recovery_from_report

    report = {
        "per_product": [{
            "product_key": "shopify|sig_abc",
            "merchant_view": {"actions": [
                {"title": "Publish a comparison page", "severity": "high",
                 "lever": "content", "body": "Write it."},
            ]},
        }],
    }
    projection = recovery_from_report(report, run_id="run-12345678")
    actions = [a for s in projection["stages"] for a in s["actions"]]
    assert actions, "no actions reached the projection; the paywall strips nothing"
    assert any(a["title"] == "Publish a comparison page" for a in actions)

    # ...and the paywall can now actually see and strip them.
    locked = mar_routes._strip_actions_for_free_tier(projection)
    assert locked["locked_counts"]["prioritized_actions"] == len(actions)
    assert all(not s["actions"] for s in locked["stages"])

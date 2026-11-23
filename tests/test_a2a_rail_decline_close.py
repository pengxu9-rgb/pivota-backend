"""A human's close of a provider-confirmed decline (services/payment_orchestration/a2a_rail_decline_close.py,
jobs/a2a_rail_operator.py `close-declined`), and the worker's split that writes the decline's own hold code.

Offline: the provider is an httpx MockTransport (tests/test_a2a_rail_checkout.Rail). SQLite here;
tests/test_a2a_rail_decline_close_postgres.py runs the same tests on Postgres.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.a2a_rail_ledger as ledger  # noqa: E402
from services.payment_orchestration import a2a_rail_decline_close as close  # noqa: E402
from services.payment_orchestration import a2a_rail_worker as worker  # noqa: E402
from tests.test_a2a_rail_checkout import (  # noqa: E402
    BASE, ORDER, Rail, _ago, _card, _checkout_result, _client, _dials, _documented_result, _placing, _row, _step,
    _task,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs them from the _postgres file")

_REPO = Path(__file__).resolve().parents[1]
TASK = "ckout_abc123"
FILE_SHA = "a" * 64
OLD = worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 600


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    if not database.is_connected:
        await database.connect()
    tables = ("a2a_rail_manual_resolution_audit", "a2a_rail_purchases", "a2a_rail_buyer_refs",
              "a2a_rail_worker_heartbeat")
    if not IS_POSTGRES:
        tables = ("agent_purchases",) + tables
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    monkeypatch.setenv(close.ENABLED_ENV, "1")
    monkeypatch.setenv("A2A_RAIL_BASE_URL", BASE)
    if not IS_POSTGRES:
        yield
        return
    try:
        yield
    finally:
        await database.disconnect()


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _result_without_order_key(success=False, status="payment-reached"):
    return _task(_card("checkout.result", {"task_id": TASK, "state": "failed", "success": success, "status": status,
                                           "total": "$51.49", "errors": [], "cardLast4": "3478"}))


async def _declined(age=OLD):
    """A real decline: placed, then the provider's answer payment-reached / success false / no order id."""
    pid = await _placing()
    assert await _step(Rail().on("checkout.await#poll", _checkout_result(success=False, order_id=None)), pid) == (
        worker.CHECKOUT_PAYMENT_REACHED_DECLINED)
    await _ago("state_entered_at", age, pid)
    return pid


async def _op(*argv, client=None):
    import jobs.a2a_rail_operator as operator

    out = io.StringIO()
    code = await operator.run(list(argv), out=out, client=client)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()], out.getvalue()


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _evidence_args(*, observed=None, kind="checkout_task_id", ref=TASK, reference="stmt-2026-11-24-001"):
    observed = observed or datetime.now(timezone.utc) - timedelta(minutes=5)
    return ("--evidence-reference", reference, "--evidence-file-sha256", FILE_SHA,
            "--evidence-observed-at", _iso(observed), "--provider-reference-kind", kind,
            "--provider-reference", ref, "--attest-no-authorization", "--attest-no-merchant-order")


async def _fence(pid, *extra, client=None):
    code, [preview], _ = await _op("close-declined", "--purchase-id", pid, *extra, client=client)
    return code, preview


def _expected_args(fence):
    return ("--expected-code", fence["code"], "--expected-state-entered-at", fence["state_entered_at"],
            "--expected-token-digest16", fence["token_digest16"],
            "--expected-pending-write", fence["pending_write"] if fence["pending_write"] else "none")


APPLY = ("--apply", "--operator", "ops-1", "--expect-env", "staging")


async def _apply(pid, *extra, fence=None, evidence=None, client=None):
    if fence is None:
        _, preview = await _fence(pid)
        fence = preview["fence"]
    code, [result], text = await _op("close-declined", "--purchase-id", pid, *(evidence or _evidence_args()),
                                     *APPLY, *_expected_args(fence), *extra, client=client)
    return code, result, text


async def _audit(pid):
    return await ledger.get_manual_resolution(pid)


# ── the worker's split (H1, the raw order id) ────────────────────────────────────────────────


@pytest.mark.parametrize("answer", [
    _checkout_result(success=False, order_id=None),
    _checkout_result(success=False, order_id=""),
    _result_without_order_key(),
], ids=["none", "empty", "absent"])
async def test_a_decline_with_no_order_id_in_the_raw_answer_is_held_as_declined(answer):
    pid = await _placing()
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == worker.CHECKOUT_PAYMENT_REACHED_DECLINED
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["merchant_order_id"], row["terminal_at"]) == (
        "paying", worker.CHECKOUT_PAYMENT_REACHED_DECLINED, None, None)


@pytest.mark.parametrize("success,order_id", [
    (None, None),                 # success missing
    ("false", None),              # success not exactly False
    (0, None),
    (False, 1.5), (False, {"id": "1001"}), (False, ["1001"]), (False, " "), (False, "#"), (False, 0),
    (False, False), (False, "1001"),
], ids=["success_missing", "success_str", "success_zero", "float", "dict", "list", "space", "hash", "zero",
        "false_order", "real_order"])
async def test_anything_but_an_exact_decline_stays_unconfirmed(success, order_id):
    pid = await _placing()
    data = {"task_id": TASK, "state": "failed", "status": "payment-reached", "total": "$51.49", "order_id": order_id}
    if success is not None:
        data["success"] = success
    answer = _task(_card("checkout.result", data))
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED


def test_the_declined_code_is_a_payment_reached_hold_the_worker_never_looks_up():
    code = worker.CHECKOUT_PAYMENT_REACHED_DECLINED
    assert code in worker._CHECKOUT_HOLD_CODES and code in worker._CHECKOUT_PAYMENT_REACHED_CODES
    assert code not in worker._OPERATOR_RESOLVABLE_HOLD_CODES
    assert not worker._lookup_eligible({"state": "paying", "last_error_code": code, "checkout_lookups": 0,
                                        "checkout_task_id": TASK, "ceiling_source": "allowance_v1"})
    assert close.ELIGIBLE_CODES == frozenset({code})


async def test_a_held_decline_row_is_only_released_by_the_worker_never_sent_anything():
    pid = await _declined()
    rail = Rail()
    await _ago("next_poll_at", 1, pid)
    [row] = [r for r in await ledger.claim_due_purchases("w1", limit=50) if r["id"] == pid]
    await worker.step(_client(rail), row, "w1")
    after = await _row(pid)
    assert rail.sent == [] and (after["state"], after["last_error_code"], after["claimed_by"]) == (
        "paying", worker.CHECKOUT_PAYMENT_REACHED_DECLINED, None)


# ── the decline predicate (H2) ───────────────────────────────────────────────────────────────


ROW = {"checkout_task_id": TASK}


@pytest.mark.parametrize("data,expected", [
    ({"task_id": TASK, "success": False, "result_status": "payment-reached"}, True),
    ({"task_id": TASK, "success": False, "status": "declined", "order_id": None}, True),
    ({"task_id": TASK, "success": False, "status": "card_declined", "order_id": ""}, True),
    ({"task_id": "ckout_other", "success": False, "status": "payment-reached"}, False),   # unbound
    ({"task_id": TASK, "success": True, "status": "payment-reached"}, False),
    ({"task_id": TASK, "status": "payment-reached"}, False),                            # success missing
    ({"task_id": TASK, "success": "false", "status": "payment-reached"}, False),
    ({"task_id": TASK, "success": False, "status": "payment-reached", "order_id": "1001"}, False),
    ({"task_id": TASK, "success": False, "status": "payment-reached", "order_id": 0}, False),
    ({"task_id": TASK, "success": False, "status": "paid"}, False),
    ({"task_id": TASK, "success": False, "status": "form-error"}, False),
])
def test_only_a_bound_exact_decline_is_a_confirmed_decline(data, expected):
    assert worker.answer_is_confirmed_decline(data, ROW) is expected
    assert worker.answer_is_confirmed_decline(data, {"checkout_task_id": None}) is False


# ── the close, end to end ────────────────────────────────────────────────────────────────────


async def test_the_preview_prints_the_fence_never_the_token_or_the_task():
    pid = await _declined()
    token = (await _row(pid))["payment_token_id"]
    code, preview = await _fence(pid)
    assert code == 3 and preview["reason"] == "evidence_not_given"
    fence = preview["fence"]
    assert fence["code"] == worker.CHECKOUT_PAYMENT_REACHED_DECLINED and fence["has_task"] is True
    assert fence["token_digest16"] == hashlib.sha256(token.encode()).hexdigest()[:16]
    text = json.dumps(preview)
    assert token not in text and TASK not in text
    code, preview = await _fence(pid, *_evidence_args())
    assert (code, preview["eligible"], preview["reason"]) == (0, True, "eligible")
    assert (await _row(pid))["state"] == "paying"  # a preview writes nothing


async def test_a_confirmed_decline_closes_with_its_audit_row_and_nothing_sent(caplog):
    pid = await _declined()
    token = (await _row(pid))["payment_token_id"]
    rail = Rail()
    with caplog.at_level(logging.DEBUG, logger="services.payment_orchestration"), \
            caplog.at_level(logging.DEBUG, logger="jobs"), caplog.at_level(logging.DEBUG, logger="db.a2a_rail_ledger"):
        code, result, text = await _apply(pid, client=_client(rail))
    assert (code, result["outcome"], result["reason"]) == (0, "resolved", "operator_confirmed_declined")
    assert rail.sent == []  # no lookup asked for: no provider call at all
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"], row["buyer_email"], row["shipping_address"],
            row["claimed_by"], row["merchant_order_id"], row["final_total_minor"]) == (
        "failed", "operator_confirmed_declined", None, None, None, None, None, None)
    assert row["payment_token_id"] == token  # kept: the orphan pass decides, after 72 h
    audit = await _audit(pid)
    assert (audit["from_code"], audit["resolved_state"], audit["resolution_code"], audit["provider_status"],
            audit["operator_ref"], audit["evidence_source"], audit["provider_reference"], bool(audit["lookup_ran"])) == (
        worker.CHECKOUT_PAYMENT_REACHED_DECLINED, "failed", "operator_confirmed_declined", "DECLINED", "ops-1",
        "provider_written_statement", TASK, False)
    assert re.fullmatch(r"[0-9a-f]{64}", audit["evidence_sha256"]) and audit["evidence_sha256"] != FILE_SHA
    logs = "\n".join(r.getMessage() for r in caplog.records
                     if r.name.startswith(("services.", "jobs", "db.a2a_rail_ledger")))
    for needle in (token, FILE_SHA, "stmt-2026-11-24-001", "buyer@example.test", "1 Main St"):
        assert needle not in logs and needle not in text
    assert audit["evidence_sha256"][:12] in logs


async def test_a_replay_with_the_same_evidence_and_operator_is_read_only_anything_else_refuses():
    pid = await _declined()
    _, preview = await _fence(pid)
    fence, evidence = preview["fence"], _evidence_args(observed=datetime.now(timezone.utc) - timedelta(minutes=3))
    assert (await _apply(pid, fence=fence, evidence=evidence))[1]["outcome"] == "resolved"
    code, result, _ = await _apply(pid, fence=fence, evidence=evidence)
    assert (code, result["outcome"]) == (0, "already_resolved")
    code, result, _ = await _apply(pid, fence=fence, evidence=_evidence_args(reference="stmt-other"))
    assert (code, result["reason"]) == (3, "already_closed_with_other_evidence")
    code, [result], _ = await _op("close-declined", "--purchase-id", pid, *evidence, "--apply", "--operator", "ops-2",
                                  "--expect-env", "staging", *_expected_args(fence))
    assert (code, result["reason"]) == (3, "already_closed_with_other_evidence")


async def test_the_close_takes_a_row_a_worker_has_claimed_and_the_workers_release_writes_nothing():
    pid = await _declined()
    _, preview = await _fence(pid)
    await database.execute("UPDATE a2a_rail_purchases SET claimed_by = 'w1', updated_at = CURRENT_TIMESTAMP "
                           "WHERE id = :i", {"i": pid})  # a parked claim (it bumps updated_at)
    assert (await _apply(pid, fence=preview["fence"]))[1]["outcome"] == "resolved"
    # The worker's later release (fenced claimed_by = :worker_id) matches nothing and writes nothing.
    assert await ledger.release_claim(pid, "w1", last_error_code=worker.CHECKOUT_PAYMENT_REACHED_DECLINED) is None
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["claimed_by"]) == ("failed", "operator_confirmed_declined", None)


@pytest.mark.parametrize("change,reason", [
    ("UPDATE a2a_rail_purchases SET last_error_code = 'checkout_payment_no_order' WHERE id = :i", "code_changed"),
    ("UPDATE a2a_rail_purchases SET payment_token_id = 'ptk_other' WHERE id = :i", "token_changed"),
    ("UPDATE a2a_rail_purchases SET pending_write = 'checkout.confirm#confirm' WHERE id = :i", "pending_write_changed"),
    ("UPDATE a2a_rail_purchases SET checkout_task_id = NULL WHERE id = :i", "checkout_task_missing"),
    ("UPDATE a2a_rail_purchases SET state_entered_at = datetime(state_entered_at, '-1 seconds') WHERE id = :i",
     "state_entered_at_changed"),
], ids=["code", "token", "pending", "task", "entered"])
async def test_each_fence_refuses_a_row_that_moved_since_the_preview(change, reason):
    if IS_POSTGRES and "datetime(" in change:
        change = "UPDATE a2a_rail_purchases SET state_entered_at = state_entered_at - INTERVAL '1 second' WHERE id = :i"
    pid = await _declined()
    _, preview = await _fence(pid)
    await database.execute(change, {"i": pid})
    code, result, _ = await _apply(pid, fence=preview["fence"])
    assert (code, result["outcome"], result["reason"]) == (3, "refused", reason)
    assert (await _row(pid))["state"] == "paying" and await _audit(pid) is None


async def test_the_sql_fence_holds_on_its_own_when_the_row_moves_after_the_decision(monkeypatch):
    pid = await _declined()
    _, preview = await _fence(pid)
    real = ledger.close_declined

    async def moved_in_between(*args, **kwargs):
        await database.execute("UPDATE a2a_rail_purchases SET pending_write = 'x' WHERE id = :i", {"i": pid})
        return await real(*args, **kwargs)

    monkeypatch.setattr(ledger, "close_declined", moved_in_between)
    code, result, _ = await _apply(pid, fence=preview["fence"])
    assert (code, result["reason"]) == (3, "fence_lost")
    assert (await _row(pid))["state"] == "paying" and await _audit(pid) is None


async def test_a_row_held_too_recently_refuses():
    pid = await _declined(age=600)
    code, result, _ = await _apply(pid, evidence=_evidence_args())
    assert (code, result["reason"]) == (3, "held_too_recently")  # the age rule speaks first
    assert (await _row(pid))["state"] == "paying"


@pytest.mark.parametrize("observed,reason", [
    (lambda entered: entered + timedelta(seconds=worker.OPERATOR_MIN_HOLD_AGE_SECONDS - 60),
     "evidence_predates_the_checkout_lapse"),
    (lambda entered: datetime.now(timezone.utc) + timedelta(minutes=5), "evidence_in_the_future"),
], ids=["before_lapse", "future"])
async def test_the_statement_must_postdate_the_lapse_and_not_be_in_the_future(observed, reason):
    pid = await _declined()
    entered = (await _row(pid))["state_entered_at"]
    code, result, _ = await _apply(pid, evidence=_evidence_args(observed=observed(entered)))
    assert (code, result["reason"]) == (3, reason)


async def test_a_statement_older_than_seven_days_refuses():
    pid = await _declined(age=8 * 86400)
    old = datetime.now(timezone.utc) - timedelta(days=7, minutes=1)
    code, result, _ = await _apply(pid, evidence=_evidence_args(observed=old))
    assert (code, result["reason"]) == (3, "evidence_too_old")


@pytest.mark.parametrize("kind,ref,ok", [
    ("checkout_task_id", TASK, True), ("external_order_reference", ORDER, True),
    ("checkout_task_id", "ckout_other", False), ("external_order_reference", "ord_other", False),
])
async def test_the_statement_must_name_this_purchase(kind, ref, ok):
    pid = await _declined()
    code, result, _ = await _apply(pid, evidence=_evidence_args(kind=kind, ref=ref))
    assert (result["outcome"] == "resolved") is ok
    if not ok:
        assert (code, result["reason"]) == (3, "provider_reference_does_not_name_this_purchase")


@pytest.mark.parametrize("drop", ["--attest-no-authorization", "--attest-no-merchant-order"])
async def test_both_attestations_are_required(drop):
    pid = await _declined()
    evidence = tuple(a for a in _evidence_args() if a != drop)
    code, result, _ = await _apply(pid, evidence=evidence)
    assert code == 2 and result["outcome"] == "refused" and result["reason"].startswith("attestation_")
    assert (await _row(pid))["state"] == "paying"


@pytest.mark.parametrize("code_", ["checkout_payment_no_order", "checkout_total_over_approved",
                                   "checkout_total_unverified", "checkout_answer_scrubbed",
                                   "checkout_confirm_unresolved"])
async def test_no_other_hold_code_is_ever_closed(code_):
    pid = await _declined()
    await database.execute("UPDATE a2a_rail_purchases SET last_error_code = :c WHERE id = :i", {"c": code_, "i": pid})
    _, preview = await _fence(pid)
    code, result, _ = await _apply(pid, fence=preview["fence"])
    assert (code, result["reason"]) == (3, "not_a_confirmed_decline_code")


async def test_apply_needs_every_guard():
    pid = await _declined()
    _, preview = await _fence(pid)
    expected = _expected_args(preview["fence"])
    for argv in ((*_evidence_args(), "--apply", "--operator", "ops-1", "--expect-env", "staging"),
                 (*_evidence_args(), "--apply", "--expect-env", "staging", *expected),
                 (*_evidence_args(), "--apply", "--operator", "ops-1", "--expect-env", "production", *expected),
                 ("--apply", "--operator", "ops-1", "--expect-env", "staging", *expected)):
        code, _, _ = await _op("close-declined", "--purchase-id", pid, *argv)
        assert code == 2
    code, [result], _ = await _op("close-declined", "--purchase-id", pid, *_evidence_args(), "--apply",
                                  "--operator", "Not A Handle", "--expect-env", "staging", *expected)
    assert (code, result["reason"]) == (3, "operator_handle_invalid")
    assert (await _row(pid))["state"] == "paying"


# ── the optional lookup, and the legacy code ─────────────────────────────────────────────────


DECLINE_LOOKUP = _documented_result(state="failed", success=False, order_id=None, total=None)


async def test_a_lookup_must_agree_when_it_runs():
    pid = await _declined()
    rail = Rail().on("checkout.lookup#submit", DECLINE_LOOKUP)
    code, result, _ = await _apply(pid, "--lookup", client=_client(rail))
    assert (code, result["outcome"]) == (0, "resolved")
    assert [s["key"] for s in rail.sent] == ["checkout.lookup#submit"]
    audit = await _audit(pid)
    assert bool(audit["lookup_ran"]) is True and bool(audit["lookup_agreed"]) is True


@pytest.mark.parametrize("answer", [
    _documented_result(state="failed", success=False, order_id=None, total=None, task="ckout_other"),  # unbound
    _documented_result(success=True, order_id="1001"),
    _documented_result(state="failed", success=False, order_id="1001"),
], ids=["unbound", "success", "order"])
async def test_a_lookup_that_does_not_confirm_the_decline_refuses(answer):
    pid = await _declined()
    code, result, _ = await _apply(pid, "--lookup", client=_client(Rail().on("checkout.lookup#submit", answer)))
    assert (code, result["reason"]) == (3, "lookup_not_a_confirmed_decline")
    assert (await _row(pid))["state"] == "paying"


@pytest.mark.parametrize("lookup,ok", [
    ({"ran": True, "found": True, "bound": True, "confirmed_decline": True}, True),
    ({"ran": True, "found": True, "bound": False, "confirmed_decline": True}, False),
    ({"ran": True, "found": False, "bound": True, "confirmed_decline": True}, False),
    ({"ran": True, "found": True, "bound": True, "confirmed_decline": False}, False),
    ({"ran": True, "found": True, "bound": True, "confirmed_decline": True, "answer_scrubbed": True}, False),
    ({"ran": True, "found": True, "bound": True, "confirmed_decline": True, "money_result_seen": True}, False),
    ({"ran": False, "reason": "the rail client is not configured here"}, False),
    (None, False),
], ids=["agrees", "unbound", "not_found", "not_a_decline", "scrubbed", "money", "not_run", "none"])
def test_a_lookup_agrees_only_when_every_condition_holds(lookup, ok):
    assert close.lookup_agrees(lookup) is ok


async def test_the_close_itself_refuses_while_the_audit_is_not_ready(monkeypatch):
    pid = await _declined()

    async def not_ready():
        return False

    monkeypatch.setattr(ledger, "manual_resolution_audit_ready", not_ready)
    evidence = close.evidence_from(reference="stmt-1", file_sha256=FILE_SHA,
                                   observed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                                   provider_reference_kind="checkout_task_id", provider_reference=TASK,
                                   attests_no_authorization=True, attests_no_merchant_order=True)
    row = await ledger.get_decline_candidate(pid)
    fence = close.fence_view(row)
    expected = close.expected_from(code=fence["code"], state_entered_at=fence["state_entered_at"],
                                   token_digest16=fence["token_digest16"], pending_write=fence["pending_write"])
    result = await close.close(pid, expected=expected, evidence=evidence, operator="ops-1", lookup=None,
                               lookup_required=False, legacy=False)
    assert result == {"outcome": "refused", "reason": "manual_resolution_audit_not_ready"}
    assert (await _row(pid))["state"] == "paying"


async def test_a_lookup_asked_for_that_cannot_run_refuses():
    pid = await _declined()
    code, result, _ = await _apply(pid, "--lookup", client=None)  # no rail client configured
    assert (code, result["reason"]) == (3, "lookup_did_not_run")


async def _legacy():
    pid = await _declined()
    await database.execute("UPDATE a2a_rail_purchases SET last_error_code = :c WHERE id = :i",
                           {"c": worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED, "i": pid})
    return pid


async def test_a_legacy_unconfirmed_row_needs_the_flag_and_a_bound_lookup_that_agrees():
    pid = await _legacy()
    code, result, _ = await _apply(pid)
    assert (code, result["reason"]) == (3, "legacy_code_needs_legacy_flag_and_a_bound_lookup")
    code, result, _ = await _apply(pid, "--legacy-unconfirmed", client=None)
    assert (code, result["reason"]) == (3, "lookup_did_not_run")
    unbound = _documented_result(state="failed", success=False, order_id=None, total=None, task="ckout_other")
    code, result, _ = await _apply(pid, "--legacy-unconfirmed", client=_client(Rail().on("checkout.lookup#submit", unbound)))
    assert (code, result["reason"]) == (3, "lookup_not_a_confirmed_decline")
    code, result, _ = await _apply(pid, "--legacy-unconfirmed",
                                   client=_client(Rail().on("checkout.lookup#submit", DECLINE_LOOKUP)))
    assert (code, result["outcome"]) == (0, "resolved")
    assert (await _audit(pid))["from_code"] == worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED


# ── off by default, readiness, append-only ───────────────────────────────────────────────────


async def test_the_flag_off_refuses_before_any_read(monkeypatch):
    pid = await _declined()
    monkeypatch.delenv(close.ENABLED_ENV)

    async def boom(*_a, **_k):
        raise AssertionError("read with the flag off")

    monkeypatch.setattr(ledger, "get_decline_candidate", boom)
    monkeypatch.setattr(ledger, "manual_resolution_audit_ready", boom)
    code, [result], _ = await _op("close-declined", "--purchase-id", pid)
    assert (code, result["reason"]) == (3, "close_declined_disabled")


async def test_the_close_refuses_while_the_audit_trigger_is_missing():
    pid = await _declined()
    assert await ledger.manual_resolution_audit_ready()
    if IS_POSTGRES:
        await database.execute("DROP TRIGGER a2a_rail_manual_resolution_audit_immutable ON a2a_rail_manual_resolution_audit")
    else:
        await database.execute("DROP TRIGGER a2a_rail_manual_resolution_audit_immutable_no_update")
    code, [result], _ = await _op("close-declined", "--purchase-id", pid)
    assert (code, result["reason"]) == (3, "manual_resolution_audit_not_ready")
    await ledger.ensure_manual_resolution_audit_schema()  # the boot heal puts it back
    assert await ledger.manual_resolution_audit_ready()


async def test_the_audit_is_append_only():
    pid = await _declined()
    assert (await _apply(pid))[1]["outcome"] == "resolved"
    for statement in ("UPDATE a2a_rail_manual_resolution_audit SET operator_ref = 'x'",
                      "DELETE FROM a2a_rail_manual_resolution_audit"):
        with pytest.raises(Exception, match="append-only"):
            await database.execute(statement)
    assert (await _audit(pid))["operator_ref"] == "ops-1"


async def test_the_heal_is_idempotent():
    for _ in range(2):
        await ledger.ensure_manual_resolution_audit_schema()
    assert await ledger.manual_resolution_audit_ready()


# ── the orphan pass waits 72 h after a close ─────────────────────────────────────────────────


async def test_a_closed_decline_stays_off_the_orphan_list_for_72_hours():
    pid = await _declined()
    assert (await _apply(pid))[1]["outcome"] == "resolved"
    assert pid not in {r["id"] for r in await ledger.list_orphan_tokens(limit=50)}
    assert await ledger.count_orphan_tokens() == 1  # still counted
    await _ago("terminal_at", ledger.ORPHAN_DECLINED_HOLD_SECONDS + 60, pid)
    assert pid in {r["id"] for r in await ledger.list_orphan_tokens(limit=50)}


# ── static pins ──────────────────────────────────────────────────────────────────────────────


def test_nothing_outside_jobs_imports_the_close():
    hits = []
    for path in _REPO.rglob("*.py"):
        rel = path.relative_to(_REPO).as_posix()
        if rel.startswith(("tests/", "jobs/", ".venv", "venv/")) or "/site-packages/" in rel:
            continue
        if rel == "services/payment_orchestration/a2a_rail_decline_close.py":
            continue
        text = path.read_text("utf-8", errors="ignore")
        if re.search(r"^\s*(from|import)\s[^\n]*a2a_rail_decline_close", text, re.M):
            hits.append(rel)
    assert hits == []


#: Every UPDATE of a2a_rail_purchases in the ledger, and what it fences on (design §5). A new one must be
#: added here, reviewed: a write that could land on a closed row needs a holder fence or a state fence.
_HOLDER_FENCED = {
    "_TRANSITION_SQL", "_MARK_PENDING_WRITE_SQL", "_CLEAR_PENDING_WRITE_SQL", "_RECORD_QUOTE_SQL",
    "_LOCK_CHECKOUT_ORDER_SQL", "_RECORD_CHECKOUT_TASK_SQL", "_COUNT_CHECKOUT_LOOKUP_SQL", "_NOTE_CHECKOUT_TASK_SQL",
    "_PARK_FOR_SHIPPING_ADDRESS_SQL", "_SET_CHALLENGE_SQL", "_FORGET_TOKEN_SQL", "_RELEASE_SQL",
}
_REVIEWED_UNFENCED = {
    "_OPERATOR_FAIL_HELD_CHECKOUT_SQL": "paying only, never a closed row",
    "_SUBMIT_SHIPPING_ADDRESS_SQL": "opened only",
    "_CLAIM_SQL": "claimed_by IS NULL and a non-terminal state",
    "_REQUEUE_STALE_SQL": "claimed_by IS NOT NULL (a close NULLs it)",
    "_EXPIRE_SQL": "pre-paying states only",
    "_PURGE_CONTACT_SQL": "contact columns only; a close NULLs them",
    "_FORGET_ORPHAN_TOKEN_SQL": "the orphan pass: list_orphan_tokens holds a close back 72 h",
}


def test_every_ledger_update_of_a_purchase_is_fenced_or_reviewed():
    source = (_REPO / "db/a2a_rail_ledger.py").read_text("utf-8")
    found = {}
    for m in re.finditer(r'^(_[A-Z0-9_]+_SQL)\s*=\s*"""(.*?)"""', source, re.S | re.M):
        if re.search(r"\bUPDATE\s+a2a_rail_purchases\b", m.group(2)):
            found[m.group(1)] = m.group(2)
    for name in _HOLDER_FENCED:
        assert re.search(r"claimed_by = :(holder|worker_id)", found[name]), name
    assert set(found) == _HOLDER_FENCED | set(_REVIEWED_UNFENCED)
    assert "UPDATE a2a_rail_purchases" in ledger._CLOSE_DECLINED_SQL  # the close itself, built from parts

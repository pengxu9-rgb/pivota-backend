"""Purchase webhook events (payment orchestration P1), on SQLite.

db/agent_purchase_events.py, services/payment_orchestration/purchase_events.py and
jobs/agent_purchase_events.py. Tables are built by the schema-guard self-heal, as production builds them.
The agent webhook sender is replaced by a recorder: these tests prove what is handed to it (and with
which event id), not how it POSTs (that is tests for services/agent_webhook_service.py).
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
from db.buyer_vault import hash_agent_user_ref  # noqa: E402
import db.agent_purchase_events as events_db  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
import db.reap_agentic_ledger as reap_ledger  # noqa: E402
import jobs.agent_purchase_events as job  # noqa: E402
import services.agent_webhook_service as webhooks  # noqa: E402
from services.payment_orchestration import purchase_events, purchase_view  # noqa: E402

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres is tests/test_agent_purchase_events_postgres.py")

_REPO = Path(__file__).resolve().parents[1]
AGENT = "agent_events"
USER_REF = "user-ada"
OWNER = hash_agent_user_ref(USER_REF)
EMAIL = "ada@example.test"
STREET = "900 Brannan St"
URL = "https://pay.prava.space/approve/abc"
FUTURE = "2999-01-01 00:00:00"


@pytest.fixture
def delivery_on(monkeypatch):
    """Webhook delivery switched on (it is off outside production by default)."""
    from services import webhook_delivery_gate

    monkeypatch.setattr(webhook_delivery_gate, "delivery_enabled", lambda *a, **k: True)


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    for table in ("agent_purchase_events", "agent_purchases", "reap_agentic_purchases", "reap_agentic_enrollments"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    yield


async def _purchase(state="resolving", agent_id=AGENT, **columns) -> str:
    """A Reap purchase in `state`, with its parent row. Returns the pp_ id."""
    rp = (await reap_ledger.create_purchase(
        buyer_ref="bref_ada", agent_id=agent_id, agent_user_ref_hash=OWNER,
        merchant_domain="brand.example", product_key="pk_1", product_name="Glow Serum", quantity=1,
        currency="USD", our_price_minor=4250, buyer_email=EMAIL,
        shipping_address={"addressLine1": STREET, "city": "San Francisco"},
    ))["id"]
    updates = {"state": state, **columns}
    assignments = ", ".join(f"{k} = :{k}" for k in updates)
    await database.execute(f"UPDATE reap_agentic_purchases SET {assignments} WHERE id = :id", {**updates, "id": rp})
    return await purchases.ensure_reap_parent(rp)


async def _events():
    rows = await database.fetch_all("SELECT * FROM agent_purchase_events ORDER BY created_at, event_id")
    out = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        out.append(item)
    return out


class _Recorder:
    """Stands in for agent_webhook_service.emit_agent_webhook_event."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result or {"status": "delivered", "delivery_id": "whd_x"}
        self.raises = raises

    async def __call__(self, agent_id, *, event_type, payload, request_id=None, event_id=None):
        self.calls.append({"agent_id": agent_id, "event_type": event_type, "payload": payload, "event_id": event_id})
        if self.raises:
            raise self.raises
        return dict(self.result)


# ── schema and vocabulary ────────────────────────────────────────────────────────────────────


def _check_values(ddl: str, name: str) -> tuple:
    match = re.search(rf"CONSTRAINT {name}\s+CHECK \(\w+ IN \(([^)]*)\)\)", ddl)
    assert match, name
    return tuple(v.strip().strip("'") for v in match.group(1).split(","))


@pytest.mark.parametrize("source", ["module", "migration"])
def test_the_checks_are_the_python_vocabularies(source):
    ddl = events_db._CREATE_TABLE_PG if source == "module" else (_REPO / "db/migrations/265_agent_purchase_events.sql").read_text()
    assert _check_values(ddl, "ck_agent_purchase_events_type") == events_db.EVENT_TYPES
    assert _check_values(ddl, "ck_agent_purchase_events_status") == events_db.STATUSES


def test_the_migration_holds_the_modules_ddl():
    migration = " ".join((_REPO / "db/migrations/265_agent_purchase_events.sql").read_text().split())
    for statement in (events_db._CREATE_TABLE_PG, events_db._CREATE_UNIQUE_INDEX, events_db._CREATE_PENDING_INDEX):
        assert " ".join(statement.split()) in migration


def test_every_event_type_is_in_the_webhook_catalog_and_subscribable():
    assert set(events_db.EVENT_TYPES) <= set(webhooks.DEFAULT_WEBHOOK_EVENTS)
    assert set(purchase_events.EVENT_FOR_STATE.values()) == set(events_db.EVENT_TYPES)
    assert webhooks._normalize_events(["purchase.completed", "nonsense"]) == ["purchase.completed"]


def test_neither_module_opens_a_transaction():
    for path in ("db/agent_purchase_events.py", "services/payment_orchestration/purchase_events.py"):
        tree = ast.parse((_REPO / path).read_text())
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "transaction"], path


def test_the_prepare_gate_collects_the_postgres_statements():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_gate", _REPO / "tests/test_repo_sql_prepare_postgres.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    collected = [" ".join(str(sql).split()) for _where, sql in gate.collect_statements()]
    for statement in (events_db._INSERT_SQL, events_db._CLAIM_SQL, events_db._FINISH_SQL, events_db._RELEASE_SQL,
                      events_db._SELECT_CLAIMABLE_SQL, events_db._REAP_CANDIDATES_SQL, events_db._CREATE_TABLE_PG):
        assert " ".join(statement.split()) in collected, statement.strip().splitlines()[0]


# ── record / claim / finish / release ────────────────────────────────────────────────────────


async def _record(purchase_id="pp_a", rail_state="completed", event_type="purchase.completed"):
    return await events_db.record_event(purchase_id=purchase_id, agent_id=AGENT, rail="reap", rail_state=rail_state,
                                        unified_state="completed", event_type=event_type, payload={"x": 1})


async def test_an_event_is_recorded_once_per_purchase_state():
    first = await _record()
    second = await _record()
    assert first == second and first.startswith("evt_")
    assert len(await _events()) == 1
    other = await _record(rail_state="failed", event_type="purchase.failed")
    assert other != first and len(await _events()) == 2


async def test_an_unknown_event_type_is_refused():
    with pytest.raises(ValueError):
        await _record(event_type="purchase.teleported")


async def test_two_workers_never_hold_one_event():
    await _record()
    mine = await events_db.claim_pending(worker="w1")
    theirs = await events_db.claim_pending(worker="w2")
    assert len(mine) == 1 and theirs == []
    assert mine[0]["payload"] == {"x": 1} and mine[0]["attempts"] == 0  # a claim is not an attempt


async def test_an_expired_lease_is_reclaimed_and_the_old_holder_cannot_finish():
    event_id = await _record()
    await events_db.claim_pending(worker="w1")
    await database.execute("UPDATE agent_purchase_events SET claimed_at = datetime('now', '-600 seconds')")
    [again] = await events_db.claim_pending(worker="w2", lease_seconds=300)
    assert again["event_id"] == event_id and again["attempts"] == 0
    assert await events_db.finish(event_id, worker="w1", status="handed_over") is False
    assert await events_db.finish(event_id, worker="w2", status="handed_over", delivery_id="whd_1") is True
    [row] = await _events()
    assert row["status"] == "handed_over" and row["delivery_id"] == "whd_1" and row["claimed_by"] is None


async def test_a_finished_event_is_never_claimed_again():
    event_id = await _record()
    await events_db.claim_pending(worker="w1")
    await events_db.finish(event_id, worker="w1", status="skipped", skip_reason="webhook_not_configured")
    await database.execute("UPDATE agent_purchase_events SET claimed_at = datetime('now', '-600 seconds')")
    assert await events_db.claim_pending(worker="w2") == []


async def test_release_returns_the_event_until_max_attempts_then_abandons_it():
    event_id = await _record()
    for attempt in (1, 2):
        await database.execute("UPDATE agent_purchase_events SET claimed_at = datetime('now', '-600 seconds')")
        await events_db.claim_pending(worker="w1")
        status = await events_db.release(event_id, worker="w1", max_attempts=2)
    assert status == "abandoned"
    assert await events_db.claim_pending(worker="w1") == []


# ── detection ────────────────────────────────────────────────────────────────────────────────


async def test_detection_records_the_states_that_warrant_an_event_and_nothing_else():
    approve = await _purchase("awaiting_approval", hosted_url=URL, hosted_url_expires_at=FUTURE,
                              reap_quote_expires_at=FUTURE)
    done = await _purchase("completed", reap_order_id="ord_9", final_total_minor=4650)
    failed = await _purchase("failed")
    quiet = [await _purchase(s) for s in ("resolving", "quoting", "processing")]
    counts = await purchase_events.detect()
    assert counts["recorded"] == 3
    by_purchase = {e["purchase_id"]: e for e in await _events()}
    assert set(by_purchase) == {approve, done, failed}
    assert not set(quiet) & set(by_purchase)
    assert by_purchase[approve]["event_type"] == "purchase.action_required"
    assert by_purchase[approve]["payload"]["next_action"]["url"] == URL
    assert by_purchase[done]["event_type"] == "purchase.completed"
    assert by_purchase[done]["payload"]["order_reference"] == "ord_9"
    assert by_purchase[failed]["event_type"] == "purchase.failed"
    assert {e["status"] for e in by_purchase.values()} == {"pending"}


async def test_detection_is_idempotent():
    await _purchase("completed", reap_order_id="ord_9")
    assert (await purchase_events.detect()).get("recorded") == 1
    assert (await purchase_events.detect()).get("recorded") is None
    assert len(await _events()) == 1


async def test_a_new_state_is_a_new_event():
    pp = await _purchase("awaiting_approval", hosted_url=URL, hosted_url_expires_at=FUTURE, reap_quote_expires_at=FUTURE)
    await purchase_events.detect()
    rp = (await database.fetch_one("SELECT rail_purchase_id FROM agent_purchases WHERE id = :p", {"p": pp}))[0]
    await database.execute("UPDATE reap_agentic_purchases SET state = 'completed', reap_order_id = 'ord_1', "
                           "updated_at = CURRENT_TIMESTAMP WHERE id = :i", {"i": rp})
    await purchase_events.detect()
    # Two events, one per state. Delivery ORDER is not promised (a retried hand-over can deliver an
    # older event after a newer one); each event carries its own state, and get_order is authoritative.
    assert sorted(e["event_type"] for e in await _events()) == ["purchase.action_required", "purchase.completed"]


async def test_an_approval_wait_with_no_page_yet_waits_for_one():
    pp = await _purchase("awaiting_approval")
    counts = await purchase_events.detect()
    assert counts == {} and await _events() == []  # not even a candidate until the page is attached
    rp = (await database.fetch_one("SELECT rail_purchase_id FROM agent_purchases WHERE id = :p", {"p": pp}))[0]
    await database.execute("UPDATE reap_agentic_purchases SET hosted_url = :u, hosted_url_expires_at = :f, "
                           "reap_quote_expires_at = :f WHERE id = :i", {"u": URL, "f": FUTURE, "i": rp})
    assert (await purchase_events.detect()).get("recorded") == 1


async def test_purchases_outside_the_lookback_are_not_replayed():
    pp = await _purchase("completed", reap_order_id="ord_old")
    rp = (await database.fetch_one("SELECT rail_purchase_id FROM agent_purchases WHERE id = :p", {"p": pp}))[0]
    await database.execute("UPDATE reap_agentic_purchases SET state_entered_at = datetime('now', '-3 days') "
                           "WHERE id = :i", {"i": rp})
    assert (await purchase_events.detect(lookback_hours=24)).get("recorded") is None
    assert await _events() == []


async def test_the_window_is_when_the_state_was_entered_not_the_last_touch():
    """`updated_at` moves on every claim/release/requeue; `state_entered_at` only on a transition."""
    old = await _purchase("completed", reap_order_id="ord_old")
    new = await _purchase("completed", reap_order_id="ord_new")
    ids = {pp: (await database.fetch_one("SELECT rail_purchase_id FROM agent_purchases WHERE id = :p", {"p": pp}))[0]
           for pp in (old, new)}
    await database.execute("UPDATE reap_agentic_purchases SET state_entered_at = datetime('now', '-3 days'), "
                           "updated_at = CURRENT_TIMESTAMP WHERE id = :i", {"i": ids[old]})
    await database.execute("UPDATE reap_agentic_purchases SET updated_at = datetime('now', '-3 days') "
                           "WHERE id = :i", {"i": ids[new]})
    await purchase_events.detect(lookback_hours=24)
    assert [e["purchase_id"] for e in await _events()] == [new]


async def test_the_payload_is_the_route_body_and_carries_no_pii():
    pp = await _purchase("completed", reap_order_id="ord_9", final_total_minor=4650)
    await purchase_events.detect()
    [event] = await _events()
    parent = await purchases.get_for_owner(pp, AGENT, OWNER)
    body, problem = await purchase_view.unified_purchase(parent, agent_id=AGENT, agent_user_ref_hash=OWNER)
    assert problem is None
    assert event["payload"] == purchase_events._json_ready(body)
    text = json.dumps(event["payload"])
    for needle in (EMAIL, STREET, "bref_ada", OWNER):
        assert needle not in text


async def test_an_unmapped_state_is_recorded_skipped_once_and_never_sent(monkeypatch):
    await _purchase("completed", reap_order_id="ord_9")
    monkeypatch.setattr(purchase_view, "unified_state", lambda rail, state: None)
    assert (await purchase_events.detect()) == {"state_unmapped": 1}
    [row] = await _events()
    assert (row["status"], row["skip_reason"], row["payload"]) == ("skipped", "state_unmapped", {})
    assert (await purchase_events.detect()) == {}  # not re-read every tick
    assert await events_db.claim_pending(worker="w1") == []


async def test_an_owner_mismatch_is_recorded_skipped_once(monkeypatch):
    pp = await _purchase("completed", reap_order_id="ord_9")
    await database.execute("UPDATE agent_purchases SET agent_id = 'agent_other' WHERE id = :p", {"p": pp})
    assert (await purchase_events.detect()) == {"not_found": 1}
    [row] = await _events()
    assert (row["status"], row["skip_reason"], row["agent_id"]) == ("skipped", "not_found", "agent_other")
    assert (await purchase_events.detect()) == {}


async def test_a_page_the_view_refuses_is_recorded_skipped_once():
    await _purchase("awaiting_approval", hosted_url="https://evil.example/approve", hosted_url_expires_at=FUTURE,
                    reap_quote_expires_at=FUTURE)
    assert (await purchase_events.detect()) == {"no_action": 1}
    [row] = await _events()
    assert (row["status"], row["skip_reason"]) == ("skipped", "no_action")
    assert (await purchase_events.detect()) == {}


async def test_pages_that_are_missing_or_dead_never_crowd_newer_purchases_out():
    for _ in range(3):
        await _purchase("needs_enrollment")  # no page yet
    await _purchase("awaiting_approval", hosted_url=URL, hosted_url_expires_at="2000-01-01 00:00:00",
                    reap_quote_expires_at=FUTURE)  # page dead
    await _purchase("awaiting_approval", hosted_url=URL, hosted_url_expires_at=FUTURE,
                    reap_quote_expires_at="2000-01-01 00:00:00")  # quote dead
    await _purchase("needs_enrollment", hosted_url=URL)  # enrollment page without a deadline
    done = await _purchase("completed", reap_order_id="ord_9")
    assert (await purchase_events.detect(limit=3)) == {"recorded": 1}
    assert [e["purchase_id"] for e in await _events()] == [done]


def test_the_candidate_states_are_exactly_the_states_that_map_to_an_event():
    from services.payment_orchestration.rails import REAP_STATE_MAP

    want = {rail for rail, unified in REAP_STATE_MAP.items() if unified in purchase_events.EVENT_FOR_STATE}
    for sql in (events_db._REAP_CANDIDATES_SQL, events_db._REAP_CANDIDATES_SQL_SQLITE):
        listed = re.search(r"r\.state IN \(([^)]*)\)", sql).group(1)
        assert {v.strip().strip("'") for v in listed.split(",")} == want


# ── hand-over ────────────────────────────────────────────────────────────────────────────────


async def test_hand_over_passes_the_stored_event_id_and_records_the_delivery(monkeypatch, delivery_on):
    await _purchase("completed", reap_order_id="ord_9")
    await purchase_events.detect()
    [event] = await _events()
    recorder = _Recorder({"status": "retrying", "delivery_id": "whd_7"})
    monkeypatch.setattr(webhooks, "emit_agent_webhook_event", recorder)
    assert (await purchase_events.hand_over(worker="w1")) == {"handed_over": 1}
    [call] = recorder.calls
    assert call["event_id"] == event["event_id"] and call["agent_id"] == AGENT
    assert call["event_type"] == "purchase.completed" and call["payload"] == event["payload"]
    [row] = await _events()
    assert (row["status"], row["delivery_id"], row["delivery_status"]) == ("handed_over", "whd_7", "retrying")
    assert (await purchase_events.hand_over(worker="w1")) == {}


async def test_a_skip_by_the_sender_is_recorded_not_retried(monkeypatch, delivery_on):
    await _record()
    monkeypatch.setattr(webhooks, "emit_agent_webhook_event", _Recorder({"status": "skipped", "reason": "event_not_subscribed"}))
    assert (await purchase_events.hand_over(worker="w1")) == {"skipped": 1}
    [row] = await _events()
    assert (row["status"], row["skip_reason"]) == ("skipped", "event_not_subscribed")


async def test_a_failing_hand_over_is_released_and_never_dropped(monkeypatch, delivery_on):
    await _record()
    monkeypatch.setattr(webhooks, "emit_agent_webhook_event", _Recorder(raises=RuntimeError("db down")))
    assert (await purchase_events.hand_over(worker="w1", max_attempts=2)) == {"released": 1}
    [row] = await _events()
    assert row["status"] == "pending" and row["claimed_by"] is None and row["attempts"] == 1
    # backed off for one lease: not re-sent in the next tick
    assert (await purchase_events.hand_over(worker="w1", max_attempts=2)) == {}
    await database.execute("UPDATE agent_purchase_events SET claimed_at = datetime('now', '-600 seconds')")
    assert (await purchase_events.hand_over(worker="w1", max_attempts=2)) == {"abandoned": 1}
    [row] = await _events()
    assert row["status"] == "abandoned"


async def test_with_delivery_off_nothing_is_claimed_and_nothing_is_lost(monkeypatch):
    """The kill switch: off means untouched, on again means delivered (not finished as skipped)."""
    from services import webhook_delivery_gate

    event_id = await _record()
    recorder = _Recorder()
    monkeypatch.setattr(webhooks, "emit_agent_webhook_event", recorder)
    monkeypatch.setattr(webhook_delivery_gate, "delivery_enabled", lambda *a, **k: False)
    assert (await purchase_events.hand_over(worker="w1")) == {"delivery_disabled": 1}
    [row] = await _events()
    assert (row["status"], row["claimed_by"], row["attempts"]) == ("pending", None, 0) and recorder.calls == []
    monkeypatch.setattr(webhook_delivery_gate, "delivery_enabled", lambda *a, **k: True)
    assert (await purchase_events.hand_over(worker="w1")) == {"handed_over": 1}
    assert [c["event_id"] for c in recorder.calls] == [event_id]


async def test_a_gate_skip_mid_send_is_released_without_an_attempt(monkeypatch, delivery_on):
    from services import webhook_delivery_gate

    await _record()
    skipped = webhook_delivery_gate.skipped_result(event_type="purchase.completed", delivery_id=None)
    monkeypatch.setattr(webhooks, "emit_agent_webhook_event", _Recorder(skipped))
    assert (await purchase_events.hand_over(worker="w1")) == {"delivery_disabled": 1}
    [row] = await _events()
    assert (row["status"], row["claimed_by"], row["attempts"]) == ("pending", None, 0)


async def test_a_cancelled_tick_gives_back_every_untried_claim(monkeypatch, delivery_on):
    import asyncio

    for i in range(3):
        await _record(purchase_id=f"pp_{i}")
    calls = []

    async def _cut(agent_id, *, event_type, payload, request_id=None, event_id=None):
        calls.append(event_id)
        if len(calls) == 2:
            raise asyncio.CancelledError()
        return {"status": "delivered", "delivery_id": "whd_1"}

    monkeypatch.setattr(webhooks, "emit_agent_webhook_event", _cut)
    with pytest.raises(asyncio.CancelledError):
        await purchase_events.hand_over(worker="w1")
    rows = {r["event_id"]: r for r in await _events()}
    assert rows[calls[0]]["status"] == "handed_over"
    untried = [r for r in rows.values() if r["status"] == "pending"]
    assert len(untried) == 2 and all(r["claimed_by"] is None and r["attempts"] == 0 for r in untried)
    assert len(await events_db.claim_pending(worker="w2")) == 2  # immediately, not after a lease


async def test_hand_over_starts_no_new_round_after_its_budget(monkeypatch, delivery_on):
    for i in range(12):
        await _record(purchase_id=f"pp_{i}")
    monkeypatch.setattr(webhooks, "emit_agent_webhook_event", _Recorder())
    ticks = iter([0.0, 0.0, 999.0, 999.0])
    counts = await purchase_events.hand_over(worker="w1", budget_seconds=150, clock=lambda: next(ticks))
    assert counts == {"handed_over": purchase_events.HAND_OVER_BATCH}
    assert len(await events_db.claim_pending(worker="w2")) == 12 - purchase_events.HAND_OVER_BATCH


# ── the job and the sender's event id ────────────────────────────────────────────────────────


async def test_the_job_is_dark_until_both_dials_are_on(monkeypatch):
    called = []

    async def _detect(**k):
        called.append("detect")
        return {}

    async def _hand_over(**k):
        called.append("hand_over")
        return {}

    monkeypatch.setattr(purchase_events, "detect", _detect)
    monkeypatch.setattr(purchase_events, "hand_over", _hand_over)
    for webhooks_on, ledger_on in ((None, "1"), ("1", None), (None, None)):
        for env, value in ((purchase_events.AGENT_PURCHASE_WEBHOOKS_ENABLED_ENV, webhooks_on),
                           (purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, ledger_on)):
            if value is None:
                monkeypatch.delenv(env, raising=False)
            else:
                monkeypatch.setenv(env, value)
        assert await job.run_agent_purchase_events() == {"skipped": "disabled"}
    assert called == []
    monkeypatch.setenv(purchase_events.AGENT_PURCHASE_WEBHOOKS_ENABLED_ENV, "1")
    monkeypatch.setenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, "1")
    await job.run_agent_purchase_events()
    assert called == ["detect", "hand_over"]


async def test_the_sender_uses_the_callers_event_id(monkeypatch):
    seen = {}

    async def _attempt(**kwargs):
        seen.update(kwargs)
        return {"status": "delivered", "delivery_id": "whd_1"}

    async def _config(agent_id):
        return {"destination_url": "https://agent.example/hooks", "enabled": True, "signing_secret": "s",
                "subscribed_events": json.dumps(webhooks.DEFAULT_WEBHOOK_EVENTS)}

    monkeypatch.setattr(webhooks, "_attempt_delivery", _attempt)
    monkeypatch.setattr(webhooks, "_get_or_create_raw_config", _config)
    await webhooks.emit_agent_webhook_event(AGENT, event_type="purchase.completed", payload={}, event_id="evt_fixed")
    assert seen["event_id"] == "evt_fixed"


async def test_the_claim_itself_refuses_an_event_finished_after_the_claimable_read():
    """The race the claim statement exists for: the claimable SELECT saw the row pending, then another
    worker finished it before this worker's UPDATE. The UPDATE must refuse it on its own."""
    event_id = await _record()
    await events_db.claim_pending(worker="w1")
    await events_db.finish(event_id, worker="w1", status="handed_over", delivery_id="whd_1")
    got = await database.fetch_one(events_db._CLAIM_SQL_SQLITE, {"event_id": event_id, "worker": "w2", "lease_seconds": 0})
    assert got is None
    [row] = await _events()
    assert row["status"] == "handed_over" and row["claimed_by"] is None


async def test_a_purchase_that_moved_between_reads_is_recorded_on_a_later_run(monkeypatch):
    await _purchase("completed", reap_order_id="ord_9")
    real = purchase_view.unified_purchase

    async def _moved(parent, **kwargs):
        body, problem = await real(parent, **kwargs)
        return {**body, "rail_state": "processing", "state": "placing"}, problem

    monkeypatch.setattr(purchase_view, "unified_purchase", _moved)
    assert (await purchase_events.detect()).get("moved") == 1
    assert await _events() == []
    monkeypatch.setattr(purchase_view, "unified_purchase", real)
    assert (await purchase_events.detect()).get("recorded") == 1

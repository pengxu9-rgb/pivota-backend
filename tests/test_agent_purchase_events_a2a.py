"""Purchase webhook events for the A2A token rail, on SQLite.

The detector (services/payment_orchestration/purchase_events.detect) reads each rail by its own
candidate statement; tests/test_agent_purchase_events.py covers the shared machinery on Reap rows.
Here: which A2A states become events, the page conditions an action event needs, the payload, and
the parent heal that keeps a purchase whose parent row was not written from being missed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402
import db.agent_purchase_events as events_db  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
from services.payment_orchestration import a2a_purchase_view, purchase_events, purchase_view  # noqa: E402

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres is tests/test_agent_purchase_events_postgres.py")

AGENT = "agent_events_a2a"
OWNER = "h" * 64
EMAIL = "ada@example.test"
STREET = "900 Brannan St"
PAGE_HOST = "pay.rail.test"
PAGE = f"https://{PAGE_HOST}/approve?t=abc"
FUTURE = "2999-01-01 00:00:00"
PAST = "2000-01-01 00:00:00"


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    for table in ("agent_purchase_events", "agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs",
                  "reap_agentic_purchases"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    yield


@pytest.fixture(autouse=True)
def _page_host(monkeypatch):
    monkeypatch.setenv(a2a_purchase_view.HOSTED_HOSTS_ENV, hashlib.sha256(PAGE_HOST.encode()).hexdigest())


async def _purchase(state="opened", *, parent=True, **columns) -> str:
    """An A2A purchase in `state` (columns set directly). Returns its pp_ id, or its ap_ id with no parent."""
    ap = (await a2a_ledger.create_purchase(
        agent_id=AGENT, agent_user_ref_hash=OWNER, merchant_domain="brand.example",
        checkout_url="https://brand.example/cart/123:1?attributes[pivota_click_id]=c1",
        currency="USD", our_price_minor=4650, product_name="Glow Serum", consent_version="v1",
        buyer_email=EMAIL, shipping_address={"addressLine1": STREET, "city": "San Francisco"},
    ))["id"]
    updates = {"state": state, **columns}
    assignments = ", ".join(f"{k} = :{k}" for k in updates)
    await database.execute(f"UPDATE a2a_rail_purchases SET {assignments} WHERE id = :id", {**updates, "id": ap})
    if not parent:
        return ap
    return await purchases.ensure_a2a_parent(ap)


def _page(kind):
    return {"buyer_action_kind": kind, "buyer_action_url": PAGE, "buyer_action_expires_at": FUTURE}


async def _events():
    rows = await database.fetch_all("SELECT * FROM agent_purchase_events ORDER BY created_at, event_id")
    out = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        out.append(item)
    return out


async def test_the_states_that_warrant_an_event_are_recorded_and_nothing_else():
    binding = await _purchase("needs_payment_method", **_page("card_binding"))
    approval = await _purchase("awaiting_authorization", **_page("approval"))
    done = await _purchase("completed", merchant_order_id="1001", final_total_minor=5120)
    ended = {s: await _purchase(s) for s in ("failed", "refused", "expired")}
    quiet = [await _purchase(s) for s in ("opened", "quoting", "paying")]
    counts = await purchase_events.detect()
    assert counts == {"recorded": 6}
    by_purchase = {e["purchase_id"]: e for e in await _events()}
    assert set(by_purchase) == {binding, approval, done, *ended.values()}
    assert not set(quiet) & set(by_purchase)
    assert {e["rail"] for e in by_purchase.values()} == {"a2a"}
    for pp, kind in ((binding, "card_binding"), (approval, "approval")):
        event = by_purchase[pp]
        assert event["event_type"] == "purchase.action_required"
        assert event["payload"]["next_action"] == {"type": "open_url", "kind": kind, "url": PAGE,
                                                   "expires_at": event["payload"]["next_action"]["expires_at"]}
    assert by_purchase[done]["event_type"] == "purchase.completed"
    assert by_purchase[done]["payload"]["order_reference"] == "1001"
    assert {by_purchase[pp]["event_type"] for pp in ended.values()} == {"purchase.failed"}
    assert (await purchase_events.detect()) == {}  # idempotent


@pytest.mark.parametrize("columns", [
    {},  # no page attached yet
    {"buyer_action_kind": "approval", "buyer_action_expires_at": FUTURE},  # a kind and a deadline, no URL
    {"buyer_action_kind": "card_binding", "buyer_action_url": PAGE, "buyer_action_expires_at": FUTURE},  # wrong kind
    {"buyer_action_kind": "approval", "buyer_action_url": PAGE, "buyer_action_expires_at": PAST},  # page dead
], ids=["no_page", "no_url", "kind_of_another_state", "page_expired"])
async def test_an_approval_without_its_live_page_is_not_a_candidate(columns):
    await _purchase("awaiting_authorization", **columns)
    assert (await purchase_events.detect()) == {}
    assert await _events() == []


async def test_a_page_on_a_host_the_view_refuses_is_skipped_and_rechecked(monkeypatch):
    pp = await _purchase("needs_payment_method", buyer_action_kind="card_binding",
                         buyer_action_url="https://evil.example/bind", buyer_action_expires_at=FUTURE)
    assert (await purchase_events.detect()) == {"no_action": 1}
    [row] = await _events()
    assert (row["purchase_id"], row["status"], row["skip_reason"]) == (pp, "skipped", "no_action")
    assert (await purchase_events.detect()) == {}  # not re-read every tick


async def test_the_payload_is_the_unified_body_and_carries_no_pii():
    pp = await _purchase("awaiting_authorization", **_page("approval"))
    await purchase_events.detect()
    [event] = await _events()
    parent = await purchases.get_for_owner(pp, AGENT, OWNER)
    body, problem = await purchase_view.unified_purchase(parent, agent_id=AGENT, agent_user_ref_hash=OWNER)
    assert problem is None
    assert event["payload"] == purchase_events._json_ready(body)
    member = (await database.fetch_one("SELECT member_id FROM a2a_rail_purchases"))["member_id"]
    text = json.dumps(event["payload"])
    for needle in (EMAIL, STREET, member, OWNER):
        assert needle not in text


async def test_a_purchase_whose_parent_was_never_written_is_healed_and_told_about():
    ap = await _purchase("completed", parent=False, merchant_order_id="1002")
    assert await database.fetch_one("SELECT id FROM agent_purchases") is None
    counts = await purchase_events.detect()
    assert counts == {"a2a_parent_healed": 1, "recorded": 1}
    parent = await database.fetch_one("SELECT id, rail_purchase_id FROM agent_purchases")
    assert parent["rail_purchase_id"] == ap
    [event] = await _events()
    assert (event["purchase_id"], event["event_type"]) == (parent["id"], "purchase.completed")
    assert (await purchase_events.detect()) == {}  # healed once; nothing left to heal


async def test_a_failing_heal_does_not_stop_detection(monkeypatch):
    done = await _purchase("completed", merchant_order_id="1003")

    async def broken(**_kw):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(purchases, "backfill_a2a_parents", broken)
    assert (await purchase_events.detect()) == {"recorded": 1}
    assert [e["purchase_id"] for e in await _events()] == [done]


async def test_both_rails_are_read_in_one_run():
    import db.reap_agentic_ledger as reap_ledger

    rp = (await reap_ledger.create_purchase(
        buyer_ref="bref_ada", agent_id=AGENT, agent_user_ref_hash=OWNER, merchant_domain="brand.example",
        product_key="pk_1", product_name="Glow Serum", quantity=1, currency="USD", our_price_minor=4250,
    ))["id"]
    await database.execute("UPDATE reap_agentic_purchases SET state = 'failed' WHERE id = :i", {"i": rp})
    reap_pp = await purchases.ensure_reap_parent(rp)
    a2a_pp = await _purchase("failed")
    assert (await purchase_events.detect()) == {"recorded": 2}
    assert {(e["purchase_id"], e["rail"]) for e in await _events()} == {(reap_pp, "reap"), (a2a_pp, "a2a")}


async def test_purchases_outside_the_lookback_are_not_replayed():
    await _purchase("completed", merchant_order_id="1004", state_entered_at="2000-01-01 00:00:00")
    assert (await purchase_events.detect(lookback_hours=24)) == {}


def test_the_candidate_states_are_exactly_the_a2a_states_that_map_to_an_event():
    from services.payment_orchestration.rails import A2A_STATE_MAP

    want = {rail for rail, unified in A2A_STATE_MAP.items() if unified in purchase_events.EVENT_FOR_STATE}
    assert want <= a2a_ledger.PURCHASE_STATES
    for sql in (events_db._A2A_CANDIDATES_SQL, events_db._A2A_CANDIDATES_SQL_SQLITE):
        listed = re.search(r"r\.state IN \(([^)]*)\)", sql).group(1)
        assert {v.strip().strip("'") for v in listed.split(",")} == want


def test_the_page_kinds_in_the_candidate_read_are_the_views():
    """The candidate read restates the view's kind-for-state rule; drift would skip or spin rows."""
    want = {(state, kind) for state, kind in a2a_purchase_view._KIND_FOR_STATE.items()
            if state in ("needs_payment_method", "awaiting_authorization")}
    for sql in (events_db._A2A_CANDIDATES_SQL, events_db._A2A_CANDIDATES_SQL_SQLITE):
        pairs = set(re.findall(r"r\.state = '(\w+)' AND r\.buyer_action_kind = '(\w+)'", sql))
        assert pairs == want


# ── review round 1 ───────────────────────────────────────────────────────────────────────────


async def _reap_failed() -> str:
    import db.reap_agentic_ledger as reap_ledger

    rp = (await reap_ledger.create_purchase(
        buyer_ref="bref_ada", agent_id=AGENT, agent_user_ref_hash=OWNER, merchant_domain="brand.example",
        product_key="pk_1", product_name="Glow Serum", quantity=1, currency="USD", our_price_minor=4250,
    ))["id"]
    await database.execute("UPDATE reap_agentic_purchases SET state = 'failed' WHERE id = :i", {"i": rp})
    return await purchases.ensure_reap_parent(rp)


async def test_an_unreadable_a2a_table_does_not_silence_reap_events():
    reap_pp = await _reap_failed()
    await database.execute("DROP TABLE a2a_rail_purchases")
    counts = await purchase_events.detect()
    assert counts == {"a2a_read_failed": 1, "recorded": 1}
    assert [(e["purchase_id"], e["rail"]) for e in await _events()] == [(reap_pp, "reap")]


async def test_the_job_hands_over_recorded_events_even_when_detection_fails(monkeypatch):
    import jobs.agent_purchase_events as job
    import services.agent_webhook_service as webhooks
    from services import webhook_delivery_gate

    await _reap_failed()
    await purchase_events.detect()  # one pending event, recorded before the failure below
    sent = []

    async def emit(agent_id, *, event_type, payload, request_id=None, event_id=None):
        sent.append(event_id)
        return {"status": "delivered", "delivery_id": "whd_x"}

    async def broken(**_kw):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(webhooks, "emit_agent_webhook_event", emit)
    monkeypatch.setattr(webhook_delivery_gate, "delivery_enabled", lambda *a, **k: True)
    monkeypatch.setattr(purchase_events, "detect", broken)
    monkeypatch.setenv(purchase_events.AGENT_PURCHASE_WEBHOOKS_ENABLED_ENV, "1")
    monkeypatch.setenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, "1")
    result = await job.run_agent_purchase_events()
    assert result["detected"] == {"detect_failed": 1} and result["handed"] == {"handed_over": 1}
    assert len(sent) == 1


async def test_the_heal_is_bounded_per_run(monkeypatch):
    monkeypatch.setattr(purchase_events, "HEAL_A2A_PARENTS_PER_RUN", 2)
    for _ in range(3):
        await _purchase("quoting", parent=False)
    assert (await purchase_events.detect()).get("a2a_parent_healed") == 2
    assert (await purchase_events.detect()).get("a2a_parent_healed") == 1
    assert (await purchase_events.detect()).get("a2a_parent_healed") is None


async def test_the_heal_skips_a_purchase_that_entered_its_state_before_the_lookback():
    old = await _purchase("completed", parent=False, merchant_order_id="1005")
    await database.execute("UPDATE a2a_rail_purchases SET state_entered_at = '2000-01-01 00:00:00' WHERE id = :i",
                           {"i": old})
    assert (await purchase_events.detect(lookback_hours=24)).get("a2a_parent_healed") is None
    assert await database.fetch_one("SELECT id FROM agent_purchases") is None


@pytest.mark.parametrize("created_hours_ago", [30, 100])
async def test_the_heal_reaches_every_purchase_the_detector_could_announce(created_hours_ago):
    """Bounded by the detector's own clock (`state_entered_at`), not by `created_at`: a purchase created
    before the lookback that changed state inside it is healed, and its event is recorded."""
    ap = await _purchase("completed", parent=False, merchant_order_id="1006")
    await database.execute("UPDATE a2a_rail_purchases SET created_at = datetime('now', '-' || :h || ' hours') WHERE id = :i",
                           {"h": created_hours_ago, "i": ap})
    assert await purchase_events.detect(lookback_hours=24) == {"a2a_parent_healed": 1, "recorded": 1}
    parent = await database.fetch_one("SELECT id, rail_purchase_id FROM agent_purchases")
    assert parent["rail_purchase_id"] == ap
    assert [(e["purchase_id"], e["event_type"]) for e in await _events()] == [(parent["id"], "purchase.completed")]


async def test_a_refused_a2a_page_is_rechecked_and_a_republished_page_becomes_the_event():
    pp = await _purchase("awaiting_authorization", buyer_action_kind="approval",
                         buyer_action_url="https://evil.example/approve", buyer_action_expires_at=FUTURE)
    assert (await purchase_events.detect()) == {"no_action": 1}
    [row] = await _events()
    first_id = row["event_id"]
    age = "UPDATE agent_purchase_events SET checked_at = datetime('now', '-600 seconds')"
    await database.execute(age)
    assert (await purchase_events.detect()) == {"no_action": 1}  # re-checked, still refused
    ap = (await database.fetch_one("SELECT rail_purchase_id FROM agent_purchases WHERE id = :p", {"p": pp}))[0]
    await database.execute("UPDATE a2a_rail_purchases SET buyer_action_url = :u WHERE id = :i", {"u": PAGE, "i": ap})
    await database.execute(age)
    assert (await purchase_events.detect()) == {"recorded": 1}
    [row] = await _events()
    assert (row["status"], row["event_id"], row["payload"]["next_action"]["url"]) == ("pending", first_id, PAGE)

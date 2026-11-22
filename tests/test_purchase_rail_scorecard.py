"""jobs/purchase_rail_scorecard.py: the arithmetic, the reads on rows built the way the rails build
them, what the output may not contain, and that the job never writes. SQLite arm; the Postgres
statements are planned by tests/test_repo_sql_prepare_postgres.py.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
import db.reap_agentic_ledger as reap_ledger  # noqa: E402
import jobs.purchase_rail_scorecard as scorecard  # noqa: E402
from services.payment_orchestration import a2a_rail_worker as worker  # noqa: E402

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm")

AGENT = "agent_scorecard"
TEST_AGENT = "agent_scorecard_rig"
ALICE = "a" * 64
BOB = "b" * 64
EMAIL = "ada@example.test"
STREET = "900 Brannan St"
SHOP = "brand.example"
OTHER_SHOP = "other.example"
HOLD = sorted(worker.held_checkout_codes())[0]


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    for table in ("agent_purchases", "reap_agentic_purchases", "reap_agentic_enrollments",
                  "a2a_rail_purchases", "a2a_rail_buyer_refs"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    yield


async def _reap(owner=ALICE, agent_id=AGENT, **over) -> str:
    kwargs = dict(buyer_ref="bref_one", agent_id=agent_id, agent_user_ref_hash=owner, merchant_domain=SHOP,
                  product_key="pk_1", quantity=1, currency="USD", our_price_minor=4250, market_country="US")
    kwargs.update(over)
    return (await reap_ledger.create_purchase(**kwargs))["id"]


async def _a2a(owner=ALICE, agent_id=AGENT, **over) -> str:
    kwargs = dict(agent_id=agent_id, agent_user_ref_hash=owner, merchant_domain=SHOP,
                  checkout_url=f"https://{SHOP}/cart/123:1?attributes[pivota_click_id]=c1", currency="USD",
                  our_price_minor=4650, product_name="Glow Serum", consent_version="v1", buyer_email=EMAIL,
                  shipping_address={"addressLine1": STREET, "city": "San Francisco"})
    kwargs.update(over)
    created = await a2a_ledger.create_purchase(**kwargs)
    return created["id"] if isinstance(created, dict) else created


async def _set(table: str, purchase_id: str, **columns) -> None:
    sets = ", ".join(f"{name} = :{name}" for name in columns)
    await database.execute(f"UPDATE {table} SET {sets} WHERE id = :id", {**columns, "id": purchase_id})


async def _age(table: str, purchase_id: str, days: int) -> None:
    await database.execute(
        f"UPDATE {table} SET created_at = datetime('now', :w) WHERE id = :id", {"w": f"-{days} days", "id": purchase_id})


async def _report(*argv: str) -> dict:
    out = io.StringIO()
    assert await scorecard.run(list(argv), out=out) == scorecard.EXIT_OK
    [line] = out.getvalue().splitlines()
    return json.loads(line)


def _group(report: dict, rail: str, buyer: str = "new", shop: str = SHOP) -> dict:
    [group] = [g for g in report["groups"] if (g["rail"], g["buyer"], g["merchant_domain"]) == (rail, buyer, shop)]
    return group


# ── the arithmetic ───────────────────────────────────────────────────────────────────────────


def _row(state, **over):
    row = {"rail_purchase_id": f"id_{state}_{len(over)}", "state": state, "merchant_domain": SHOP, "market": "US",
           "agent_id": AGENT, "refusal_reason": None, "last_error_code": None, "priced": 0, "order_recorded": 0,
           "held": 0, "returning_buyer": 0}
    row.update(over)
    return row


def _build(reap=(), a2a=(), plans=(), **over):
    args = dict(reap_rows=list(reap), a2a_rows=list(a2a), plans=list(plans), a2a_held_codes={HOLD}, days=14,
                min_sample=3, max_rows=100, generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    args.update(over)
    return scorecard.build_scorecard(**args)


def test_a_held_purchase_counts_against_the_rail_and_an_in_flight_one_is_not_judged():
    report = _build(reap=[
        _row("completed", priced=1, order_recorded=1), _row("completed", priced=1, order_recorded=1, held=0),
        _row("failed", last_error_code="checkout_failed:declined by the issuer", priced=1),
        _row("processing", held=1, last_error_code="checkout_unresolvable:read"),
        _row("awaiting_approval"), _row("quoting"),
    ])
    [group] = report["groups"]
    assert (group["routed"], group["completed"], group["failed"], group["held"], group["in_flight"]) == (6, 2, 1, 1, 2)
    assert group["finished"] == 4 and group["success_rate"] == 0.5 and group["sample_ok"] is True
    assert (group["priced"], group["order_recorded"]) == (3, 2)
    assert group["ended_by"] == {"failed:checkout_failed": 1, "held:checkout_unresolvable": 1}
    assert report["generated_at"] == "2026-01-02T03:04:05+00:00" and report["truncated"] is False


def test_nothing_finished_has_no_rate_and_a_small_group_is_marked():
    report = _build(reap=[_row("quoting")], a2a=[_row("completed"), _row("expired", refusal_reason="buyer_left")])
    waiting, done = _group(report, "reap"), _group(report, "a2a")
    assert (waiting["finished"], waiting["success_rate"], waiting["sample_ok"]) == (0, None, False)
    assert (done["finished"], done["success_rate"], done["sample_ok"]) == (2, 0.5, False)
    assert done["ended_by"] == {"expired:buyer_left": 1}


def test_each_rails_code_is_read_from_its_own_end_and_a_detail_is_never_echoed():
    report = _build(
        reap=[_row("failed", last_error_code="transport_error:ConnectTimeout to 10.0.0.9"),
              _row("failed", last_error_code="Not A Token"), _row("failed")],
        a2a=[_row("failed", last_error_code=f"step 3 said no:{HOLD}"), _row("paying", last_error_code=f"x:{HOLD}"),
             _row("paying", last_error_code="quote_pending"), _row("refused", refusal_reason="Has Spaces")])
    assert _group(report, "reap")["ended_by"] == {"failed:other": 2, "failed:transport_error": 1}
    a2a = _group(report, "a2a")
    assert a2a["ended_by"] == {f"failed:{HOLD}": 1, f"held:{HOLD}": 1, "refused:other": 1}
    assert (a2a["held"], a2a["in_flight"]) == (1, 1)
    text = json.dumps(report)
    for leaked in ("ConnectTimeout", "10.0.0.9", "Not A Token", "step 3", "Has Spaces"):
        assert leaked not in text


def test_groups_split_by_merchant_market_and_buyer_and_the_token_rails_market_is_the_plans():
    plans = [{"rail": "a2a", "rail_purchase_id": "ap_1", "routing_plan": json.dumps({"basis": "orchestrator_selected_rail",
              "policy": "env_v1", "market": "SG", "dropped": {"reap": "a2a_preferred"}})},
             {"rail": "a2a", "rail_purchase_id": "ap_2", "routing_plan": {"basis": "backfill"}}]
    report = _build(
        reap=[_row("completed"), _row("completed", merchant_domain=OTHER_SHOP), _row("completed", market="sg"),
              _row("completed", returning_buyer=1)],
        a2a=[_row("completed", rail_purchase_id="ap_1", market=None), _row("completed", rail_purchase_id="ap_2", market=None),
             _row("completed", rail_purchase_id="ap_3", market=None)],
        plans=plans)
    keys = [(g["rail"], g["merchant_domain"], g["market"], g["buyer"], g["routed"]) for g in report["groups"]]
    assert keys == [
        ("a2a", SHOP, None, "new", 2), ("a2a", SHOP, "SG", "new", 1),
        ("reap", SHOP, "SG", "new", 1), ("reap", SHOP, "US", "new", 1), ("reap", SHOP, "US", "returning", 1),
        ("reap", OTHER_SHOP, "US", "new", 1),
    ]
    assert report["decisions"] == [
        {"rail": "a2a", "basis": "backfill", "policy": None, "dropped": None, "purchases": 1},
        {"rail": "a2a", "basis": "orchestrator_selected_rail", "policy": "env_v1", "dropped": "reap=a2a_preferred",
         "purchases": 1},
        {"rail": "a2a", "basis": "unrecorded", "policy": None, "dropped": None, "purchases": 1},
        {"rail": "reap", "basis": "unrecorded", "policy": None, "dropped": None, "purchases": 4},
    ]


def test_an_excluded_agents_purchases_are_left_out_and_counted():
    report = _build(reap=[_row("completed"), _row("failed", agent_id=TEST_AGENT)], a2a=[_row("failed", agent_id=TEST_AGENT)],
                    exclude_agent_ids=[TEST_AGENT, ""])
    assert report["rows_excluded_by_agent"] == 2 and report["rows_read"] == {"reap": 2, "a2a": 1, "parents": 0}
    [group] = report["groups"]
    assert (group["rail"], group["routed"], group["success_rate"]) == ("reap", 1, 1.0)


@pytest.mark.parametrize("reap_n,a2a_n,truncated", [(2, 0, False), (3, 0, True), (0, 3, True)])
def test_a_rail_that_reached_the_row_bound_says_so(reap_n, a2a_n, truncated):
    report = _build(reap=[_row("completed")] * reap_n, a2a=[_row("completed")] * a2a_n, max_rows=3)
    assert report["truncated"] is truncated


@pytest.mark.parametrize("stored", ["not json", "[1, 2]", 7, None, {"basis": "Has Spaces", "dropped": {"A Rail": "Why Not"}}])
def test_a_plan_that_is_not_the_expected_shape_never_fails_the_report(stored):
    report = _build(reap=[_row("completed", rail_purchase_id="rp_1")],
                    plans=[{"rail": "reap", "rail_purchase_id": "rp_1", "routing_plan": stored}])
    [decision] = report["decisions"]
    assert decision["basis"] == "other" and decision["purchases"] == 1
    assert "Has Spaces" not in json.dumps(report) and "Why Not" not in json.dumps(report)


# ── the reads, on rows the rails' own ledgers wrote ──────────────────────────────────────────


async def test_the_job_reads_both_rails_and_the_parents_and_prints_one_object():
    done = await _reap()
    await _set("reap_agentic_purchases", done, state="completed", quoted_total_minor=4250, final_total_minor=4250)
    await purchases.ensure_reap_parent(done)
    refused = await _reap(owner=BOB)
    await _set("reap_agentic_purchases", refused, state="refused", refusal_reason="quote_mismatch")
    token_done = await _a2a()
    await _set("a2a_rail_purchases", token_done, state="completed", quoted_total_minor=4650, order_id="ord_1")
    await purchases.ensure_a2a_parent(token_done, basis="orchestrator_selected_rail", dropped={"reap": "a2a_preferred"})
    held = await _a2a(owner=BOB)
    await _set("a2a_rail_purchases", held, state="paying", last_error_code=HOLD)

    report = await _report("--min-sample", "2")
    assert report["job"] == "purchase_rail_scorecard" and report["window_days"] == 14
    assert report["rows_read"] == {"reap": 2, "a2a": 2, "parents": 2} and report["truncated"] is False
    reap, token = _group(report, "reap"), _group(report, "a2a")
    assert (reap["market"], reap["routed"], reap["completed"], reap["refused"], reap["success_rate"]) == ("US", 2, 1, 1, 0.5)
    assert (reap["priced"], reap["order_recorded"], reap["sample_ok"]) == (1, 1, True)
    assert reap["ended_by"] == {"refused:quote_mismatch": 1}
    assert (token["market"], token["completed"], token["held"], token["success_rate"]) == (None, 1, 1, 0.5)
    assert token["ended_by"] == {f"held:{HOLD}": 1}
    assert {(d["rail"], d["basis"], d["dropped"]) for d in report["decisions"]} == {
        ("reap", "agent_selected_rail", None), ("reap", "unrecorded", None),
        ("a2a", "orchestrator_selected_rail", "reap=a2a_preferred"), ("a2a", "unrecorded", None)}


async def test_only_purchases_created_inside_the_window_are_read():
    old, new = await _reap(), await _reap(owner=BOB)
    await _age("reap_agentic_purchases", old, days=20)
    old_token = await _a2a()
    await _age("a2a_rail_purchases", old_token, days=20)
    assert (await _report())["rows_read"] == {"reap": 1, "a2a": 0, "parents": 0}
    assert (await _report("--days", "30"))["rows_read"] == {"reap": 2, "a2a": 1, "parents": 0}
    assert new != old


@pytest.mark.parametrize("rail", ["reap", "a2a"])
async def test_a_buyer_with_an_earlier_completed_purchase_on_that_rail_is_returning_however_old(rail):
    table = "reap_agentic_purchases" if rail == "reap" else "a2a_rail_purchases"
    make = _reap if rail == "reap" else _a2a
    first = await make()
    await _set(table, first, state="completed")
    await _age(table, first, days=200)
    again = await make()
    other_buyer = await make(owner=BOB)
    other_rail_only = await (_a2a if rail == "reap" else _reap)()
    report = await _report()
    assert _group(report, rail, "returning")["routed"] == 1 and _group(report, rail, "new")["routed"] == 1
    other = "a2a" if rail == "reap" else "reap"
    assert [g["buyer"] for g in report["groups"] if g["rail"] == other] == ["new"]
    assert len({first, again, other_buyer, other_rail_only}) == 4


async def test_an_earlier_purchase_that_did_not_complete_does_not_make_a_buyer_returning():
    first = await _reap()
    await _set("reap_agentic_purchases", first, state="failed")
    await _age("reap_agentic_purchases", first, days=2)
    await _reap()
    report = await _report()
    assert [(g["buyer"], g["routed"]) for g in report["groups"]] == [("new", 2)]


async def test_held_on_the_hosted_approval_rail_is_its_ledgers_own_count():
    unresolved = await _reap()
    await _set("reap_agentic_purchases", unresolved, state="processing", reap_checkout_id="chk_1",
               last_error_code="checkout_unresolvable:read")
    parked = await _reap(owner=BOB)
    await _set("reap_agentic_purchases", parked, state="quoting", checkout_dispatch_key="dk_1")
    for state, columns in (
        ("processing", dict(reap_checkout_id="chk_2", last_error_code="transport_error:x")),
        ("processing", dict(last_error_code="checkout_unresolvable:read")),  # no checkout id
        ("processing", dict(reap_checkout_id="chk_3", last_error_code="checkout_unresolvablex")),
        ("awaiting_approval", {}), ("resolving", {}),
    ):
        other = await _reap(owner="c" * 64)
        await _set("reap_agentic_purchases", other, state=state, **columns)
    report = await _report()
    held = sum(g["held"] for g in report["groups"])
    assert held == await reap_ledger.count_checkout_needs_human() and held >= 2
    # ... and the predicate TEXT is the ledger's, on both dialects: a change there fails here.
    squash = lambda sql: re.sub(r"\s+", " ", sql)  # noqa: E731
    for mine, theirs in ((scorecard._REAP_ROWS_SQL, reap_ledger._COUNT_CHECKOUT_NEEDS_HUMAN_SQL),
                         (scorecard._REAP_ROWS_SQL_SQLITE, reap_ledger._COUNT_CHECKOUT_NEEDS_HUMAN_SQL_SQLITE)):
        assert squash(theirs.split("WHERE", 1)[1]).strip() in squash(mine)


async def test_a_quoting_purchase_the_ledger_tracks_is_in_flight_not_held():
    await _reap()
    row = await database.fetch_one("SELECT state, dispatch_tracking_version FROM reap_agentic_purchases")
    await database.execute("UPDATE reap_agentic_purchases SET state = 'quoting'")
    report = await _report()
    expected_held = await reap_ledger.count_checkout_needs_human()
    assert sum(g["held"] for g in report["groups"]) == expected_held
    assert sum(g["in_flight"] for g in report["groups"]) == 1 - expected_held and row is not None


# ── what the job may not print, and may not do ───────────────────────────────────────────────


async def test_the_output_names_no_buyer_agent_purchase_or_link():
    rp = await _reap(buyer_email=EMAIL, shipping_address={"addressLine1": STREET})
    await purchases.ensure_reap_parent(rp)
    ap = await _a2a()
    await _set("a2a_rail_purchases", ap, state="failed", last_error_code=f"{EMAIL}:{HOLD}")
    out = io.StringIO()
    assert await scorecard.run([], out=out) == scorecard.EXIT_OK
    text = out.getvalue()
    for secret in (EMAIL, STREET, ALICE, AGENT, rp, ap, "pivota_click_id", "bref_one", "4250", "4650", "Glow Serum"):
        assert secret not in text, secret


async def test_the_job_writes_nothing(monkeypatch):
    await _reap()
    await _a2a()

    async def _refuse(*a, **k):
        raise AssertionError("the scorecard wrote to the database")

    before = {t: (await database.fetch_one(f"SELECT count(*) AS n FROM {t}"))["n"]
              for t in ("reap_agentic_purchases", "a2a_rail_purchases", "agent_purchases")}
    monkeypatch.setattr(database, "execute", _refuse)
    monkeypatch.setattr(database, "execute_many", _refuse)
    assert (await _report())["rows_read"] == {"reap": 1, "a2a": 1, "parents": 0}
    monkeypatch.undo()
    after = {t: (await database.fetch_one(f"SELECT count(*) AS n FROM {t}"))["n"] for t in before}
    assert after == before == {"reap_agentic_purchases": 1, "a2a_rail_purchases": 1, "agent_purchases": 0}


def test_every_statement_is_one_select():
    for name in ("_REAP_ROWS_SQL", "_A2A_ROWS_SQL", "_PARENT_PLANS_SQL"):
        for sql in (getattr(scorecard, name), getattr(scorecard, name + "_SQLITE")):
            assert sql.strip().upper().startswith("SELECT") and ";" not in sql
            assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE|GRANT)\b", sql.upper())
            assert "LIMIT :max_rows" in sql
    assert "clock_timestamp()" not in scorecard._REAP_ROWS_SQL_SQLITE + scorecard._A2A_ROWS_SQL_SQLITE
    assert "chr(58)" in scorecard._REAP_ROWS_SQL and "chr(58)" not in scorecard._REAP_ROWS_SQL_SQLITE


@pytest.mark.parametrize("argv", [["--days", "0"], ["--days", "367"], ["--max-rows", "0"], ["--min-sample", "0"],
                                  ["--days", "x"], ["--apply"]])
async def test_a_bad_argument_is_refused_before_any_read(monkeypatch, argv):
    async def _refuse(*a, **k):
        raise AssertionError("read before the arguments were checked")

    monkeypatch.setattr(scorecard, "_read", _refuse)
    assert await scorecard.run(argv, out=io.StringIO()) == scorecard.EXIT_BAD_ARGS


async def test_the_row_bound_is_applied_per_rail_newest_first():
    for owner in ("d" * 64, "e" * 64, "f" * 64):
        await _reap(owner=owner)
    report = await _report("--max-rows", "2")
    assert report["rows_read"]["reap"] == 2 and report["truncated"] is True

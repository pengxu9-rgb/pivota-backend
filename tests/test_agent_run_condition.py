"""ADR-030 D11's run condition (services/payment_orchestration/agent_run_condition.py) and its router
settings (services/payment_orchestration/router.py).

The two doors that call it are tested in tests/test_agent_commerce_purchase_create.py (the token rail)
and tests/test_agent_commerce_reap_routes.py (Reap). Here: which rails the setting names, the rule set
a decision records, the router's new drop reason, and the filter over the REAL tables of migrations 294
and 295 (on the process database: SQLite, or the throwaway Postgres database of the gate).
"""

from __future__ import annotations

import pytest

from db import merchant_agent_checkout_facts as facts
from db import partner_agent_merchant_blocks as blocks
from db.database import database
from services.payment_orchestration import agent_run_condition as run_condition
from services.payment_orchestration import router as rail_router
from tests.test_merchant_agent_checkout_facts import NEGATIVE, POSITIVE_UCP, _age
from tests.test_merchant_agent_checkout_facts import _assert_throwaway_database
from tests.test_merchant_agent_checkout_facts import _create_table as _create_facts_table
from tests.test_partner_agent_merchant_blocks import _create_table as _create_blocks_table

SHOP = "brand-a.example"
RAILS_ENV = rail_router.PARTNER_AGENT_RUN_CONDITION_RAILS_ENV


# ── the setting ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,rails", [
    (None, set()), ("", set()), ("a2a", {"a2a"}), (" Reap , a2a ,", {"reap", "a2a"}),
    ("reap,reap", {"reap"}), ("all", set()), ("*", set()), ("a2a_rail, true, 1", set()),
])
def test_the_setting_names_rails_and_nothing_else(monkeypatch, raw, rails):
    if raw is None:
        monkeypatch.delenv(RAILS_ENV, raising=False)
    else:
        monkeypatch.setenv(RAILS_ENV, raw)
    assert rail_router.run_condition_rails() == rails
    for rail in rail_router.RAILS:
        assert rail_router.run_condition_enforced(rail) == (rail in rails)


@pytest.mark.parametrize("raw,policy", [
    (None, "env_v1"), ("reap", "env_v1"), ("a2a", "env_v2"), ("reap,a2a", "env_v2"),
])
def test_a_decision_records_the_rule_set_in_force(monkeypatch, raw, policy):
    """The token rail's filter is a routing rule; Reap's is the Reap route's own admission."""
    if raw is None:
        monkeypatch.delenv(RAILS_ENV, raising=False)
    else:
        monkeypatch.setenv(RAILS_ENV, raw)
    choice = rail_router.choose_rail(
        agent_id="agent_x", merchant_domain=SHOP, market_country="US", item_source="cart_link", currency="USD",
        worker_alive=True)
    assert rail_router.policy_version() == policy
    assert rail_router.decision_record(choice, market_country="us")["policy"] == policy
    assert rail_router.POLICY_VERSION == "env_v1", "the name of the rules without the filter does not move"


@pytest.fixture
def a2a_open(monkeypatch):
    """Every token-rail filter passes for agent_x on SHOP in the US."""
    monkeypatch.setenv("A2A_RAIL_ENABLED", "1")
    monkeypatch.setenv(rail_router.A2A_RAIL_CREATE_ENABLED_ENV, "1")
    monkeypatch.setenv(rail_router.A2A_RAIL_PILOT_AGENT_IDS_ENV, "agent_x")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:US")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:10000")
    monkeypatch.delenv(RAILS_ENV, raising=False)


def _reason(**over):
    args = dict(agent_id="agent_x", merchant_domain=SHOP, market_country="US", item_source="cart_link",
                currency="USD", worker_alive=True)
    args.update(over)
    return rail_router.a2a_unavailable_reason(**args)


def test_the_router_drops_the_token_rail_only_while_its_filter_is_on(a2a_open, monkeypatch):
    assert _reason() is None and _reason(agent_may_run=False) is None, "off: the read's answer is ignored"
    monkeypatch.setenv(RAILS_ENV, "reap")
    assert _reason(agent_may_run=False) is None, "Reap's filter is not the token rail's"
    monkeypatch.setenv(RAILS_ENV, "a2a")
    assert _reason() is None
    assert _reason(agent_may_run=False) == "agent_run_condition_unmet"
    # Last of the reasons: every other one answers first (the route reads only when none does).
    assert _reason(agent_may_run=False, agent_id="agent_y") == "agent_not_in_pilot"
    assert _reason(agent_may_run=False, item_source="reap_variant") == "item_source_unsupported"
    choice = rail_router.choose_rail(**dict(
        agent_id="agent_x", merchant_domain=SHOP, market_country="US", item_source="cart_link", currency="USD",
        worker_alive=True), agent_may_run=False)
    assert (choice.rail, choice.dropped) == ("reap", {"a2a": "agent_run_condition_unmet"})
    pinned = rail_router.choose_rail(**dict(
        agent_id="agent_x", merchant_domain=SHOP, market_country="US", item_source="cart_link", currency="USD",
        worker_alive=True), agent_may_run=False, preferred="a2a")
    assert (pinned.rail, pinned.refusal) == (None, "agent_run_condition_unmet")


def test_the_router_still_reads_no_database():
    """The route reads both records and passes the answer in (the router's rule since P2c)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(rail_router))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {f"{node.module}.{alias.name}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 for alias in node.names}
    for name in imported:
        assert not name.startswith("db.") and "agent_run_condition" not in name, name


# ── the filter over the real tables ──────────────────────────────────────────────────────────


@pytest.fixture
async def records(monkeypatch):
    """Both tables, empty, on the process database; emptied and rebuilt afterwards."""
    _assert_throwaway_database()
    monkeypatch.delenv(facts.MAX_AGE_ENV, raising=False)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    for create, table in ((_create_facts_table, facts.TABLE), (_create_blocks_table, blocks.TABLE)):
        await create()
        await database.execute(f"DELETE FROM {table}")
    yield
    for create, table in ((_create_facts_table, facts.TABLE), (_create_blocks_table, blocks.TABLE)):
        await create()
        await database.execute(f"DELETE FROM {table}")
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _block(rail: str, domain: str = SHOP, purchase: str = "ap_test_0001") -> None:
    assert await blocks.record_block(rail, domain, "captcha", source=blocks.SOURCE_WORKER, purchase_id=purchase) == "recorded"


async def test_no_stored_fact_is_not_met(records):
    for rail in rail_router.RAILS:
        assert await run_condition.unmet_cause(rail, SHOP) == run_condition.NO_CHECKOUT_FACT


async def test_a_fresh_positive_with_no_open_event_is_met_on_both_rails(records):
    assert await facts.record_check(SHOP, POSITIVE_UCP)
    for rail in rail_router.RAILS:
        assert await run_condition.unmet_cause(rail, SHOP) is None


async def test_an_open_event_closes_its_own_rail_only(records):
    assert await facts.record_check(SHOP, POSITIVE_UCP)
    await _block("a2a")
    assert await run_condition.unmet_cause("a2a", SHOP) == run_condition.OPEN_BLOCK
    assert await run_condition.unmet_cause("reap", SHOP) is None


async def test_a_cleared_event_reopens_the_rail(records):
    assert await facts.record_check(SHOP, POSITIVE_UCP)
    await _block("reap", purchase="rp_test_0001")
    [event] = await blocks.list_blocks(rail="reap", domain=SHOP, open_only=True)
    assert await blocks.clear_blocks("reap", SHOP, ids=[event["id"]], basis="written_permission",
                                     cleared_by="Ops Person One", evidence="ticket OPS-0001") == 1
    assert await run_condition.unmet_cause("reap", SHOP) is None


async def test_a_negative_and_an_aged_positive_are_not_met(records):
    assert await facts.record_check(SHOP, POSITIVE_UCP)
    await _age(SHOP, "ucp_checkout_at", facts.DEFAULT_MAX_AGE_DAYS + 1)
    assert await run_condition.unmet_cause("a2a", SHOP) == run_condition.NO_CHECKOUT_FACT
    assert await facts.record_check(SHOP, POSITIVE_UCP)
    assert await run_condition.unmet_cause("a2a", SHOP) is None
    assert await facts.record_check(SHOP, NEGATIVE)
    assert await run_condition.unmet_cause("a2a", SHOP) == run_condition.NO_CHECKOUT_FACT


@pytest.mark.parametrize("spelling", ["www.brand-a.example", "WWW.Brand-A.example", "brand-a.example"])
async def test_one_host_spelled_as_requests_name_it_reads_the_one_row(records, spelling):
    assert await facts.record_check("www.brand-a.example", POSITIVE_UCP)
    await _block("a2a", domain="Brand-A.example")
    assert await run_condition.unmet_cause("reap", spelling.lower()) is None
    assert await run_condition.unmet_cause("a2a", spelling.lower()) == run_condition.OPEN_BLOCK


async def test_a_missing_table_is_not_met(records):
    """Before migrations 294 and 295 run, a rail whose filter is on takes nothing."""
    assert await facts.record_check(SHOP, POSITIVE_UCP)
    await database.execute(f"DROP TABLE {blocks.TABLE}")
    assert await run_condition.unmet_cause("a2a", SHOP) == run_condition.OPEN_BLOCK
    await database.execute(f"DROP TABLE {facts.TABLE}")
    assert await run_condition.unmet_cause("a2a", SHOP) == run_condition.NO_CHECKOUT_FACT


async def test_a_reader_that_raises_anyway_is_not_met(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("reader broke its promise")

    monkeypatch.setattr(facts, "meets_run_condition", boom)
    assert await run_condition.unmet_cause("a2a", SHOP) == run_condition.NO_CHECKOUT_FACT


async def test_nothing_is_read_while_the_rails_filter_is_off(monkeypatch):
    async def forbidden(*_a, **_k):
        raise AssertionError("read while off")

    monkeypatch.setattr(facts, "meets_run_condition", forbidden)
    monkeypatch.setattr(blocks, "is_clear", forbidden)
    monkeypatch.setenv(RAILS_ENV, "reap")
    assert await run_condition.agent_may_run("a2a", SHOP, agent_id="agent_x") is True


async def test_a_failure_is_logged_with_its_cause_and_the_answer_says_only_no(monkeypatch, caplog):
    async def no(*_a, **_k):
        return False

    monkeypatch.setattr(facts, "meets_run_condition", no)
    monkeypatch.setenv(RAILS_ENV, "a2a")
    with caplog.at_level("WARNING", logger=run_condition.__name__):
        assert await run_condition.agent_may_run("a2a", SHOP, agent_id="agent_x") is False
    assert f"rail=a2a merchant={SHOP} agent=agent_x cause={run_condition.NO_CHECKOUT_FACT}" in caplog.text

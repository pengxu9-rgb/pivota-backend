"""Pure parts of agent share accrual, the model/migration parity, and the ops script's safety.
The ledger logic runs on Postgres in tests/test_agent_share_accrual_postgres.py."""

import re
from pathlib import Path

import pytest

from services import agent_share_accrual as svc


@pytest.mark.parametrize(
    "billed, share, target",
    [
        (450, 2500, 112),     # 112.5 floors to 112: rounding favours Pivota
        (300, 2500, 75),
        (0, 2500, 0),         # nothing billed, nothing shared
        (450, 0, 0),          # no rate
        (-100, 2500, 0),      # never negative
        (100000, 10000, 100000),
    ],
)
def test_the_share_is_a_fraction_of_what_was_billed(billed, share, target):
    assert svc.compute_share(billed, share) == target


def test_the_unknown_sentinel_is_not_an_agent():
    assert svc._real_agent("unknown") is None
    assert svc._real_agent("  ") is None
    assert svc._real_agent(" agent_x ") == "agent_x"


def test_the_job_is_dark_unless_flagged(monkeypatch):
    monkeypatch.delenv(svc.FLAG, raising=False)
    assert svc.is_enabled() is False
    monkeypatch.setenv(svc.FLAG, "1")
    assert svc.is_enabled() is True


def _ddl_columns(sql, table):
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.S).group(1)
    return {line.split()[0] for line in body.splitlines()
            if line.strip() and not line.strip().startswith(("CONSTRAINT", "--"))}


def _added_columns(sql, table):
    body = re.search(rf"ALTER TABLE IF EXISTS {table}(.*?);", sql, re.S).group(1)
    return set(re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", body))


def test_the_model_and_the_migrations_declare_the_same_tables():
    from db.agent_share import agent_share_ledger, agent_share_rates

    root = Path(__file__).resolve().parent.parent
    sql = (root / "db/migrations/235_agent_share_accrual.sql").read_text()
    added = _added_columns((root / "db/migrations/236_agent_share_after_partner.sql").read_text(),
                           "agent_share_ledger")
    assert added == {"partner_settled_minor", "merchant_billed_minor", "partner_cut_minor"}
    assert _ddl_columns(sql, "agent_share_rates") == {c.name for c in agent_share_rates.columns}
    assert _ddl_columns(sql, "agent_share_ledger") | added == {c.name for c in agent_share_ledger.columns}
    for table in (agent_share_rates, agent_share_ledger):
        model_checks = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
        for name in model_checks:
            assert f"CONSTRAINT {name} CHECK" in sql, name


def test_main_registers_the_tables_for_create_all():
    main = (Path(__file__).resolve().parent.parent / "main.py").read_text()
    assert "import db.agent_share" in main


def test_the_ops_script_writes_nothing_without_apply():
    from scripts import agent_share as script

    p = script.build_parser()
    assert p.parse_args(["set-rate", "--agent-id", "a", "--share-bp", "1", "--effective-from",
                         "2026-10-01T00:00:00Z", "--created-by", "x"]).apply is False
    assert p.parse_args(["accrue", "--line-id", "7"]).apply is False
    with pytest.raises(SystemExit):
        p.parse_args(["set-rate", "--agent-id", "a", "--share-bp", "1", "--effective-from",
                      "2026-10-01T00:00:00", "--created-by", "x"])


def test_schema_guard_lands_migration_236_in_prod():
    # create_all never alters an existing table and prod skips numbered migrations.
    guard = (Path(__file__).resolve().parent.parent / "db/schema_guard.py").read_text()
    assert _added_columns(guard, "agent_share_ledger") == {"partner_settled_minor", "merchant_billed_minor",
                                                           "partner_cut_minor"}


@pytest.mark.parametrize(
    "line, paid, total, cut",
    [
        (450, 90, 450, 90),
        (451, 90, 1000, 41),     # 40.59 rounds UP
        (450, 0, 450, 0),        # the partner was paid nothing
        (0, 90, 450, 0),
        (450, 1000, 450, 450),   # paid more than the merchant was billed: the whole line
        (450, 90, 0, 450),       # nothing billed in the run but a partner was paid: whole line
        (-5, 90, 450, 0),
    ],
)
def test_the_partner_cut_rounds_up_and_stays_within_the_line(line, paid, total, cut):
    assert svc.partner_cut(line, paid, total) == cut


@pytest.mark.parametrize("lines", [[450], [100, 200, 151], [1, 1, 1, 997], [333] * 7, [5, 10000, 7, 1]])
@pytest.mark.parametrize("paid_fraction", [0.0, 0.01, 0.2, 0.3333, 0.5, 0.99, 1.0])
@pytest.mark.parametrize("agent_bp", [2500, 10000])
def test_partner_plus_agents_never_exceed_what_the_merchant_was_billed(lines, paid_fraction, agent_bp):
    total = sum(lines)
    paid = int(total * paid_fraction)
    cuts = [svc.partner_cut(line, paid, total) for line in lines]
    # The cuts cover at least what the partners were paid...
    assert sum(cuts) >= paid
    # ...so partners + agents never exceed the merchant's billed total, nor any line exceed itself.
    agents = [svc.compute_share(line - cut, agent_bp) for line, cut in zip(lines, cuts)]
    assert paid + sum(agents) <= total
    assert all(c + a <= line for line, c, a in zip(lines, cuts, agents))


@pytest.mark.parametrize(
    "payload, cents",
    [
        ({"merchant_accruals": {"m": {"gmv_share_cents": 90}}}, 90),          # v2 engine
        ({"merchant_accruals": {"m": {"gmv_take_rev_cents": 70}}}, 70),       # v1 engine
        ('{"merchant_accruals": {"m": {"gmv_share_cents": 55}}}', 55),        # JSONB returned as text
        ({"merchant_accruals": {"other": {"gmv_share_cents": 90}}}, 0),       # not this merchant
        ({"merchant_accruals": {"m": {"gmv_share_cents": -5}}}, 0),
        ({}, 0),
        ("not json", 0),
        (None, 0),
    ],
)
def test_the_settled_share_is_read_from_either_engine_s_snapshot(payload, cents):
    from services.partner_settlement_service import merchant_gmv_share_cents

    assert merchant_gmv_share_cents(payload, "m") == cents

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


def test_the_model_and_the_migration_declare_the_same_tables():
    from db.agent_share import agent_share_ledger, agent_share_rates

    sql = (Path(__file__).resolve().parent.parent / "db/migrations/235_agent_share_accrual.sql").read_text()
    for table in (agent_share_rates, agent_share_ledger):
        assert _ddl_columns(sql, table.name) == {c.name for c in table.columns}, table.name
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

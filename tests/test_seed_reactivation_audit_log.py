"""Every operator tool that sets a seed back to `active` outside the draft-approval rule logs it (owner 2026-11-08).

The tools stay ops-only and ungated (docs/runbooks/seed_reactivation_outside_approval.md). What this pins:
- each reactivation site logs one `external_seed_reactivated_outside_approval` line per seed it reactivates, with the
  seed id, the tool and the previous status, and nothing that names a person;
- a logging failure changes nothing the tool does: the same statements, the same return value.

The fakes are the ones each tool's own tests use. All names are placeholders.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import seed_reactivation_audit as audit  # noqa: E402

FIELDS = {"event", "seed_id", "tool", "previous_status", "row_confirmed"}


class _Recorder:
    def __init__(self, fail: bool = False):
        self.lines: List[Dict[str, Any]] = []
        self.fail = fail

    def warning(self, message, *args, **kwargs):
        if self.fail:
            raise RuntimeError("log sink on fire")
        self.lines.append(json.loads(message))


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(audit, "_logger", rec)
    return rec


@pytest.fixture
def failing_logger(monkeypatch):
    rec = _Recorder(fail=True)
    monkeypatch.setattr(audit, "_logger", rec)
    return rec


def _lines_for(rec, tool):
    out = [line for line in rec.lines if line["tool"] == tool]
    for line in out:
        assert set(line) == FIELDS and line["event"] == audit.EVENT
    return out


# ---------------------------------------------------------------- the helper


def test_one_line_per_seed_with_only_the_audit_fields(recorder):
    audit.log_seed_reactivated_outside_approval(["seed_a", "seed_b"], tool="tool_x", previous_status="inactive",
                                                row_confirmed=False)
    assert recorder.lines == [
        {"event": audit.EVENT, "seed_id": "seed_a", "tool": "tool_x", "previous_status": "inactive", "row_confirmed": False},
        {"event": audit.EVENT, "seed_id": "seed_b", "tool": "tool_x", "previous_status": "inactive", "row_confirmed": False},
    ]
    audit.log_seed_reactivated_outside_approval([], tool="tool_x")
    audit.log_seed_reactivated_outside_approval(None, tool="tool_x")
    assert len(recorder.lines) == 2


def test_the_helper_never_raises(failing_logger):
    audit.log_seed_reactivated_outside_approval(["seed_a"], tool="tool_x")

    class _Unlistable:
        def __iter__(self):
            raise TypeError("not a list")

    audit.log_seed_reactivated_outside_approval(_Unlistable(), tool="tool_x")


def test_the_line_reaches_the_pivota_logger():
    import logging

    assert audit._logger.name == "pivota.external_seed_reactivation"
    assert audit._logger.getEffectiveLevel() <= logging.WARNING


# ---------------------------------------------------------------- withdraw_catalog_rows --revert


def _withdraw_revert(monkeypatch):
    import scripts.withdraw_catalog_rows as withdraw
    from tests.test_withdraw_catalog_rows import _FakeDB, _ours, _patch

    db = _FakeDB({"ck_promo": True})
    _patch(monkeypatch, db)
    out = asyncio.run(withdraw._revert([_ours(seed_ids=("seed_a", "seed_b"))]))
    return withdraw, db, out


def test_withdraw_revert_logs_each_reactivated_seed(monkeypatch, recorder):
    withdraw, _, _ = _withdraw_revert(monkeypatch)
    lines = _lines_for(recorder, withdraw.SCRIPT_NAME)
    assert [(l["seed_id"], l["previous_status"], l["row_confirmed"]) for l in lines] == [
        ("seed_a", "inactive", False), ("seed_b", "inactive", False)]


def test_withdraw_revert_is_unchanged_when_logging_fails(monkeypatch, recorder):
    _, db_ok, out_ok = _withdraw_revert(monkeypatch)
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    _, db_fail, out_fail = _withdraw_revert(monkeypatch)
    assert db_fail.executed == db_ok.executed and out_fail == out_ok


# ---------------------------------------------------------------- remediate_unpublished_crawl_rows --revert


def _remediate_revert(monkeypatch, prior="active"):
    import scripts.remediate_unpublished_crawl_rows as remediate
    from tests.test_remediate_unpublished_crawl_rows import _FakeDB, _patch, _row

    db = _FakeDB({"ck_1": True})
    _patch(monkeypatch, db)
    row = _row("seed_r", "https://shop-a.example/products/a")
    row["suppression_metadata"] = {"script": remediate.SCRIPT_NAME, "prior_seed_status": prior}
    out = asyncio.run(remediate._revert([row]))
    return remediate, db, out


def test_remediate_revert_logs_the_reactivated_seed_and_only_that(monkeypatch, recorder):
    remediate, _, _ = _remediate_revert(monkeypatch)
    (line,) = _lines_for(recorder, remediate.SCRIPT_NAME)
    assert (line["seed_id"], line["previous_status"], line["row_confirmed"]) == ("seed_r", "inactive", False)
    recorder.lines.clear()
    _remediate_revert(monkeypatch, prior="inactive")
    assert recorder.lines == [], "a seed the revert leaves dead is not logged as reactivated"


def test_remediate_revert_is_unchanged_when_logging_fails(monkeypatch, recorder):
    _, db_ok, out_ok = _remediate_revert(monkeypatch)
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    _, db_fail, out_fail = _remediate_revert(monkeypatch)
    assert db_fail.executed == db_ok.executed and out_fail == out_ok


# ---------------------------------------------------------------- repair_same_title_listings (its revert)


class _RepairDB:
    def __init__(self):
        self.calls: List[Any] = []

    async def fetch_all(self, query, values=None):
        self.calls.append((" ".join(query.split()), dict(values or {})))
        return [{"id": (values or {}).get("id")}]


def _repair_reverse():
    import scripts.repair_same_title_listings as repair

    db = _RepairDB()
    manifest = {"run_id": "run_1", "groups": [{
        "product_key": "pk_1", "repoint": None, "restore": [], "suppress": [], "suppress_prior_metadata": {},
        "deactivate": ["seed_s"], "deactivate_prior_updated_at": {"seed_s": "2026-11-01T00:00:00+00:00"},
    }]}
    out = asyncio.run(repair._reverse(db, manifest))
    return db, out


def test_repair_same_title_revert_logs_the_reactivated_seed(recorder):
    _, out = _repair_reverse()
    (line,) = _lines_for(recorder, "repair_same_title_listings")
    assert (line["seed_id"], line["previous_status"], line["row_confirmed"]) == ("seed_s", "inactive", True)
    assert out == {"reactivated": 1}


def test_repair_same_title_revert_is_unchanged_when_logging_fails(monkeypatch, recorder):
    db_ok, out_ok = _repair_reverse()
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    db_fail, out_fail = _repair_reverse()
    assert db_fail.calls == db_ok.calls and out_fail == out_ok


# ---------------------------------------------------------------- identity_resolution revert_run / revert_proposal


class _Conn:
    """An asyncpg-shaped connection answering each statement of the two reverts by identity."""

    def __init__(self, mod):
        self.mod = mod
        self.calls: List[Any] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        m = self.mod
        if sql is m.RUN_EVENTS_SQL:
            return [{"detail": json.dumps({"deactivated_seed_ids": ["seed_i1", "seed_i2"]}), "proposal_id": None}]
        if sql is m.REACTIVATE_SEEDS_SQL:
            return [{"id": s} for s in args[0] if s != "seed_i2"]  # seed_i2 was no longer inactive
        if sql in (m.PROPOSAL_SUPPRESSED_ROWS_SQL, m.REVERT_PROPOSAL_ROWS_SQL):
            return [{"product_key": "pk_dup"}]
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        m = self.mod
        if sql is m.PROPOSAL_FOR_REVERT_SQL:
            return {"status": "applied", "run_id": "run_a", "kind": "suppress_dup"}
        if sql is m.PROPOSAL_APPLIED_EVENT_SQL:
            return {"detail": {"suppressed": 1, "deactivated_seed_ids": ["seed_p"]}}
        return None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "UPDATE 1"


def _identity(which):
    from services import identity_resolution as mod

    conn = _Conn(mod)
    if which == "run":
        out = asyncio.run(mod.revert_run(conn, "run_a"))
    else:
        out = asyncio.run(mod.revert_proposal(conn, "prop_1", dry_run=False, revert_run_id="rv_1"))
    return conn, out


@pytest.mark.parametrize("which,tool,expected", [
    ("run", "identity_resolution.revert_run", ["seed_i1"]),
    ("proposal", "identity_resolution.revert_proposal", ["seed_p"]),
])
def test_identity_reverts_log_exactly_the_seeds_they_reactivated(recorder, which, tool, expected):
    _, out = _identity(which)
    lines = _lines_for(recorder, tool)
    assert [l["seed_id"] for l in lines] == expected == out["reactivated_seeds"]
    assert all(l["previous_status"] == "inactive" and l["row_confirmed"] for l in lines)


@pytest.mark.parametrize("which", ["run", "proposal"])
def test_identity_reverts_are_unchanged_when_logging_fails(monkeypatch, recorder, which):
    conn_ok, out_ok = _identity(which)
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    conn_fail, out_fail = _identity(which)
    assert conn_fail.calls == conn_ok.calls and out_fail == out_ok


# ---------------------------------------------------------------- every reactivation site is covered


def test_every_operator_reactivation_site_calls_the_audit_helper():
    """A new `SET status = 'active'` on seeds in an operator tool must log too (or be a staff door)."""
    import re

    repo = Path(__file__).resolve().parents[1]
    sites = {
        "scripts/withdraw_catalog_rows.py": 1, "scripts/remediate_unpublished_crawl_rows.py": 1,
        "scripts/repair_same_title_listings.py": 1, "services/identity_resolution.py": 2,
    }
    for rel, calls in sites.items():
        assert (repo / rel).read_text().count("log_seed_reactivated_outside_approval(") == calls, rel
    pattern = re.compile(r"UPDATE external_product_seeds\s+SET\s+status\s*=\s*'active'", re.I)
    found = set()
    for folder in ("scripts", "services", "jobs"):
        for path in (repo / folder).rglob("*.py"):
            if pattern.search(path.read_text(errors="replace")):
                found.add(path.relative_to(repo).as_posix())
    assert found <= set(sites), f"a seed reactivation outside the publish rule that does not log: {found - set(sites)}"

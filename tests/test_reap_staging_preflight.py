"""scripts/ops/reap_staging_preflight.py: the staging census and scrub must never touch production.

run_oneoff_job.sh defaults to PRODUCTION (project, DATABASE_URL, PIVOTA_ENV). An operator who
drops the staging overrides reaches this program with prod's URL and env; the target check must
abort before any statement that reads or writes a Reap row. These tests drive `main` with a fake
connection that records every statement. The real-Postgres run (abort on a wrong host or
database, scrub on a matching one, both UPDATEs atomic) is recorded in the PR.
"""

from __future__ import annotations

import ast
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "reap_staging_preflight.py"
_spec = importlib.util.spec_from_file_location("reap_staging_preflight_under_test", SCRIPT)
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

STAGING_URL = "postgresql://u:pw" + chr(64) + "10.122.0.3:5432/pivota?sslmode=require"
PROD_URL = "postgresql://u:pw" + chr(64) + "10.25.0.2:5432/pivota?sslmode=require"
STAGING_ENV = {"PIVOTA_ENV": "staging", "DATABASE_URL": STAGING_URL}


class FakeConn:
    def __init__(self, *, database="pivota", server_addr="10.122.0.3", live=True, fail_on=None):
        self.database = database
        self.server_addr = server_addr
        self.live = live
        self.fail_on = fail_on
        self.statements = []
        self.in_tx = False
        self.tx_statements = []
        self.committed = False
        self.closed = False

    async def fetchval(self, sql, *args):
        self.statements.append(sql)
        if "current_database" in sql:
            if isinstance(self.database, Exception):
                raise self.database
            return self.database
        if "inet_server_addr" in sql:
            return self.server_addr
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        self.statements.append(sql)
        if not self.live:
            return []
        if "reap_agentic_purchases" in sql:
            return [{"state": "processing", "n": 2, "with_email": 2, "with_link": 1}]
        return [{"status": "active", "n": 1}]

    async def execute(self, sql, *args):
        self.statements.append(sql)
        if self.in_tx:
            self.tx_statements.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")
        if sql.startswith("UPDATE reap_agentic_purchases"):
            self.live = False
            return "UPDATE 2"
        if sql.startswith("UPDATE reap_agentic_enrollments"):
            return "UPDATE 1"
        return "SET"

    def transaction(self):
        conn = self

        @asynccontextmanager
        async def _tx():
            conn.in_tx = True
            try:
                yield
                conn.committed = True
            finally:
                conn.in_tx = False

        return _tx()

    async def close(self):
        self.closed = True


async def _main(argv, env, conn, **kw):
    lines = []

    async def connect(url):
        return conn

    code = await P.main(argv, env, connect=connect, out=lines.append, **kw)
    return code, lines


def _touched_reap_rows(conn):
    return [s for s in conn.statements if "reap_agentic" in s]


# --- the target check aborts before any Reap statement --------------------------------------


@pytest.mark.parametrize("argv", [["census"], ["scrub"], ["scrub", "--apply"]])
@pytest.mark.parametrize("env,conn_kw,reason", [
    ({"PIVOTA_ENV": "production", "DATABASE_URL": PROD_URL}, {}, "PIVOTA_ENV"),   # runner defaults
    ({"PIVOTA_ENV": "staging", "DATABASE_URL": PROD_URL}, {}, "DATABASE_URL host"),
    ({"PIVOTA_ENV": "production", "DATABASE_URL": STAGING_URL}, {}, "PIVOTA_ENV"),
    ({"DATABASE_URL": STAGING_URL}, {}, "PIVOTA_ENV"),
    (STAGING_ENV, {"database": "pivota_prod_copy"}, "current_database()"),
    (STAGING_ENV, {"database": RuntimeError("x")}, "could not read current_database()"),
    ({"PIVOTA_ENV": "staging",
      "DATABASE_URL": "postgresql://u:pw" + chr(64) + "10.122.0.30:5432/pivota"}, {}, "host"),
])
async def test_anything_but_staging_aborts_before_touching_a_reap_row(argv, env, conn_kw, reason):
    conn = FakeConn(**conn_kw)
    code, lines = await _main(argv, env, conn)
    assert code == P.EXIT_ABORT
    assert any(reason in ln for ln in lines), lines
    assert _touched_reap_rows(conn) == []
    assert conn.closed


async def test_no_database_url_aborts():
    code, lines = await _main(["census"], {"PIVOTA_ENV": "staging"}, FakeConn())
    assert code == P.EXIT_ABORT


@pytest.mark.parametrize("argv", [[], ["scrub", "--force"], ["census", "--apply"], ["apply"]])
async def test_unknown_arguments_abort(argv):
    conn = FakeConn()
    code, _ = await _main(argv, STAGING_ENV, conn)
    assert code == P.EXIT_ABORT and conn.statements == []


def test_the_target_cannot_be_overridden_from_the_environment_or_argv():
    """The expected host and database are constants; only a test can pass others to `main`."""
    src = SCRIPT.read_text()
    assert 'EXPECTED_HOST = "10.122.0.3"' in src and 'EXPECTED_DB = "pivota"' in src
    assert "getenv(\"EXPECTED" not in src and "environ[\"EXPECTED" not in src


# --- on staging -----------------------------------------------------------------------------


async def test_census_is_read_only_and_reports_stop():
    conn = FakeConn()
    code, lines = await _main(["census"], STAGING_ENV, conn)
    assert code == P.EXIT_STOP
    assert "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" in conn.statements
    assert not [s for s in conn.statements if s.startswith("UPDATE")]
    assert "STOP: live rows present" in lines


async def test_scrub_without_apply_is_a_dry_run():
    conn = FakeConn()
    code, lines = await _main(["scrub"], STAGING_ENV, conn)
    assert code == P.EXIT_STOP
    assert not [s for s in conn.statements if s.startswith("UPDATE")]


async def test_scrub_apply_runs_both_updates_in_one_transaction_and_prints_counts_only():
    conn = FakeConn()
    code, lines = await _main(["scrub", "--apply"], STAGING_ENV, conn)
    assert code == P.EXIT_OK
    assert [s.split()[1] for s in conn.tx_statements] == [
        "reap_agentic_purchases", "reap_agentic_enrollments"]
    assert conn.committed
    assert "scrubbed purchases 2 enrollments 1" in lines
    assert "READ ONLY" not in " ".join(conn.statements)
    joined = "\n".join(lines)
    assert "u:pw" not in joined and "sslmode" not in joined


async def test_a_failing_second_update_rolls_back_the_first():
    conn = FakeConn(fail_on="UPDATE reap_agentic_enrollments")
    with pytest.raises(RuntimeError):
        await _main(["scrub", "--apply"], STAGING_ENV, conn)
    assert not conn.committed
    assert conn.closed


async def test_nothing_live_scrubs_nothing():
    conn = FakeConn(live=False)
    code, lines = await _main(["scrub", "--apply"], STAGING_ENV, conn)
    assert code == P.EXIT_OK
    assert not [s for s in conn.statements if s.startswith("UPDATE")]


def test_the_scrub_nulls_pii_claims_and_the_live_approval_link():
    sql = P._SCRUB_PURCHASES
    for column in ("shipping_address = NULL", "buyer_email = NULL", "offer_code = NULL",
                   "hosted_url = NULL", "hosted_url_expires_at = NULL", "claimed_by = NULL",
                   "claimed_at = NULL", "next_poll_at = NULL"):
        assert column in sql
    assert "hosted_url = NULL" in P._SCRUB_ENROLLMENTS
    assert "hosted_url_expires_at = NULL" in P._SCRUB_ENROLLMENTS


def test_the_program_is_self_contained_and_has_no_at_sign():
    """It runs inline through run_oneoff_job.sh (`-c "$(cat ...)"`) before it is in any image:
    stdlib + asyncpg only, and no at-sign so the runner's --args delimiter choice is stable."""
    src = SCRIPT.read_text()
    assert chr(64) not in src
    allowed = {"__future__", "asyncio", "os", "sys", "typing", "urllib.parse", "asyncpg"}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            assert {a.name for a in node.names} <= allowed
        elif isinstance(node, ast.ImportFrom):
            assert node.module in allowed

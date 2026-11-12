"""scripts/ops/agent_total_orders_census.py: the drift arithmetic, on seeded tables.

The census is read-only SQL run by hand in a one-off job, so nothing else exercises it. These run
its real statements on an in-memory SQLite database (the two psycopg2 spellings, `%(fix)s` and a
doubled percent sign, are translated; the session SETs are skipped). The Postgres dialect is not
covered here.
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ops" / "agent_total_orders_census.py"
FIX = "2026-11-10T03:30:33Z"


@pytest.fixture(scope="module")
def census():
    spec = importlib.util.spec_from_file_location("agent_total_orders_census_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def __init__(self, db, statements):
        self._cur = db.cursor()
        self._statements = statements

    def execute(self, sql, params=None):
        self._statements.append(sql)
        if sql.startswith("SET "):
            return
        self._cur.execute(sql.replace("%(fix)s", ":fix").replace("%%", "%"), params or {})

    @property
    def description(self):
        return self._cur.description

    def fetchall(self):
        return self._cur.fetchall()


class _Conn:
    def __init__(self, db):
        self.db = db
        self.statements = []

    def cursor(self):
        return _Cursor(self.db, self.statements)


def _log(agent, endpoint, method, status, order_id, ts):
    return (agent, endpoint, method, status, order_id, ts)


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE agents(agent_id TEXT, is_active INT, created_at TEXT, total_orders INT, total_gmv NUMERIC);
        CREATE TABLE orders(order_id TEXT, agent_id TEXT, total NUMERIC, metadata TEXT, created_at TEXT);
        CREATE TABLE agent_usage_logs(agent_id TEXT, endpoint TEXT, method TEXT, status_code INT,
                                      order_id TEXT, timestamp TEXT);
        -- inflated: 2 creates + 2 confirms + 3 reads + 1 track before the fix = 8 counted
        INSERT INTO agents VALUES ('agent_inflated', 1, '2026-01-01', 8, 150);
        -- exact, but its one order is a stamped test order
        INSERT INTO agents VALUES ('agent_exact', 1, '2026-01-01', 1, 10);
        -- under: an order row that never passed the create route's counter
        INSERT INTO agents VALUES ('agent_under', 1, '2026-01-01', 0, 0);
        -- no orders anywhere: left out of the report
        INSERT INTO agents VALUES ('agent_idle', 1, '2026-01-01', 0, 0);
        INSERT INTO orders VALUES ('ORD_1', 'agent_inflated', 50, '{}', '2026-10-01');
        INSERT INTO orders VALUES ('ORD_2', 'agent_inflated', 100, NULL, '2026-10-02');
        INSERT INTO orders VALUES ('ORD_3', 'agent_exact', 10, '{"test_traffic": true}', '2026-10-02');
        INSERT INTO orders VALUES ('ORD_4', 'agent_under', 5, '{}', '2026-10-02');
        """
    )
    rows = [
        _log("agent_inflated", "/agent/v1/orders/create", "POST", 200, "ORD_1", "2026-10-01T00:00:00Z"),
        _log("agent_inflated", "/agent/v1/orders/create", "POST", 200, "ORD_2", "2026-10-02T00:00:00Z"),
        # a failed create never counted
        _log("agent_inflated", "/agent/v1/orders/create", "POST", 500, "ORD_X", "2026-10-02T00:00:00Z"),
        _log("agent_inflated", "/agent/v1/orders/ORD_1/confirm-payment", "POST", 200, "ORD_1", "2026-10-01T00:01:00Z"),
        _log("agent_inflated", "/agent/v1/orders/ORD_2/confirm-payment", "POST", 200, "ORD_2", "2026-10-02T00:01:00Z"),
        _log("agent_inflated", "/agent/v1/orders/ORD_1", "GET", 200, "ORD_1", "2026-10-03T00:00:00Z"),
        _log("agent_inflated", "/agent/v1/orders/ORD_1", "GET", 200, "ORD_1", "2026-10-03T00:00:01Z"),
        _log("agent_inflated", "/agent/v1/orders/ORD_1", "GET", 200, "ORD_1", "2026-10-04T00:00:00Z"),
        _log("agent_inflated", "/agent/v1/fulfillment/track/ORD_1", "GET", 200, "ORD_1", "2026-10-05T00:00:00Z"),
        # a read after the fix: logged, not counted
        _log("agent_inflated", "/agent/v1/orders/ORD_1", "GET", 200, "ORD_1", "2026-11-11T00:00:00Z"),
        # no order_id: never counted
        _log("agent_inflated", "/agent/v1/search", "POST", 200, None, "2026-10-05T00:00:00Z"),
        _log("agent_exact", "/agent/v1/orders/create", "POST", 200, "ORD_3", "2026-10-02T00:00:00Z"),
    ]
    db.executemany("INSERT INTO agent_usage_logs VALUES (?, ?, ?, ?, ?, ?)", rows)
    yield _Conn(db)
    db.close()


def _rows(out, tag):
    import json

    return [json.loads(line[len(tag) + 1:]) for line in out.splitlines() if line.startswith(tag + " ")]


def test_the_census_sizes_the_drift_per_agent(census, conn, capsys):
    totals = census.run(conn, FIX)
    out = capsys.readouterr().out
    by_agent = {r["agent_id"]: r for r in _rows(out, "ATO_ROW")}

    assert sorted(by_agent) == ["agent_exact", "agent_inflated", "agent_under"]

    inflated = by_agent["agent_inflated"]
    assert (inflated["stored_orders"], inflated["orders_rows"], inflated["drift"]) == (8, 2, 6)
    assert (inflated["log_create"], inflated["log_confirm"], inflated["log_read"], inflated["log_track"]) == (2, 2, 4, 1)
    assert inflated["log_other"] == 0
    assert (inflated["log_noncreate_before_fix"], inflated["log_noncreate_after_fix"]) == (6, 1)
    # 2 creates + 6 counted non-creates reproduce the stored 8: the bug explains all of it
    assert inflated["unexplained"] == 0
    assert inflated["log_distinct_orders"] == 2

    exact = by_agent["agent_exact"]
    assert (exact["drift"], exact["orders_rows"], exact["orders_rows_real"]) == (0, 1, 0)

    under = by_agent["agent_under"]
    assert (under["drift"], under["log_create"]) == (-1, 0)

    assert (totals["agents"], totals["agents_over"], totals["agents_exact"], totals["agents_under"]) == (3, 1, 1, 1)
    assert totals["drift"] == 5
    assert totals["truncated"] is False
    # worst drift first
    assert [r["agent_id"] for r in _rows(out, "ATO_ROW")][0] == "agent_inflated"


def test_the_census_opens_a_read_only_session_before_any_query(census, conn, capsys):
    census.run(conn, FIX)
    capsys.readouterr()
    assert conn.statements[0] == "SET TRANSACTION READ ONLY"
    assert conn.statements[1].startswith("SET LOCAL statement_timeout")
    for sql in conn.statements[2:]:
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), sql[:80]
    source = SCRIPT.read_text()
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "ALTER ", "DROP ", "TRUNCATE "):
        assert verb not in source, verb


def test_the_census_source_survives_the_inline_dash_c_form():
    # run_oneoff_job.sh -c "$(cat file)" needs the program free of the at-sign
    assert chr(64) not in SCRIPT.read_text()


def test_the_census_run_as_a_file_without_a_database_url_exits_2(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "PYTHONPATH")}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "ATO_FATAL" in proc.stderr

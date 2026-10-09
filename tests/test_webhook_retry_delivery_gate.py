"""Webhook RETRIES deliver only in production unless WEBHOOK_RETRY_DELIVERY_ENABLED says otherwise.

Staging runs on a restored copy of production data with production credentials, and the two
retry loops start on every process (`web` included), plus `list_deliveries` drains due retries
inline. So a `retrying` row copied from prod would be POSTed from staging to a real agent or
merchant endpoint. services/webhook_retry_delivery_gate.py closes that. These cases pin:

  * the parser's contract, and that prod with the variable unset still delivers;
  * that outside production NEITHER path (the loop, the inline call in `list_deliveries`) selects
    or delivers anything, and that the explicit opt-in and kill switch both work;
  * that prod's due-row query and delivery calls are exactly what an enabled process issues.

Both services carry an independent copy of this code, so every service case runs against both.
"""

from __future__ import annotations

import asyncio
import importlib
import logging

import pytest

from services import webhook_retry_delivery_gate as gate

MODULES = ["services.agent_webhook_service", "services.merchant_webhook_service"]
VAR = gate.ENV_VAR


# ── the parser ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "env, expected",
    [
        # unset: production delivers, everything else does not
        ({"PIVOTA_ENV": "production"}, True),
        ({"PIVOTA_ENV": "prod"}, True),
        ({"PIVOTA_ENV": "staging"}, False),
        ({"PIVOTA_ENV": "test"}, False),
        ({}, False),  # local default resolves to development
        # empty or whitespace is unset, not "off": prod keeps delivering
        ({"PIVOTA_ENV": "production", VAR: ""}, True),
        ({"PIVOTA_ENV": "production", VAR: "   "}, True),
        ({"PIVOTA_ENV": "staging", VAR: "  "}, False),
        # explicit opt-in works in any environment
        ({"PIVOTA_ENV": "staging", VAR: "true"}, True),
        ({"PIVOTA_ENV": "staging", VAR: "1"}, True),
        ({"PIVOTA_ENV": "staging", VAR: " YES "}, True),
        ({"PIVOTA_ENV": "staging", VAR: "on"}, True),
        ({VAR: "true"}, True),
        # explicit kill switch works in production too
        ({"PIVOTA_ENV": "production", VAR: "false"}, False),
        ({"PIVOTA_ENV": "production", VAR: "0"}, False),
        ({"PIVOTA_ENV": "production", VAR: "OFF"}, False),
        ({"PIVOTA_ENV": "production", VAR: "no"}, False),
        # an unparseable value is OFF, everywhere
        ({"PIVOTA_ENV": "production", VAR: "ture"}, False),
        ({"PIVOTA_ENV": "staging", VAR: "enabled"}, False),
        # the opt-in is exact, not a prefix or substring
        ({"PIVOTA_ENV": "staging", VAR: "truex"}, False),
        ({"PIVOTA_ENV": "staging", VAR: "t"}, False),
    ],
)
def test_the_contract(env, expected):
    assert gate.retry_delivery_enabled(env) is expected, env


def test_an_unparseable_value_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        assert gate.retry_delivery_enabled({"PIVOTA_ENV": "production", VAR: "ture"}) is False
    assert any(
        r.levelno == logging.WARNING and VAR in r.getMessage() and "OFF" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_it_reads_the_live_environment(monkeypatch):
    """No caching: the value is read per call, so a test (or a restart with new env) sees it."""
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    monkeypatch.delenv(VAR, raising=False)
    assert gate.retry_delivery_enabled() is False
    monkeypatch.setenv(VAR, "true")
    assert gate.retry_delivery_enabled() is True
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.delenv(VAR, raising=False)
    assert gate.retry_delivery_enabled() is True


# ── the two services ─────────────────────────────────────────────────────────────────────


def _owner_key(mod):
    return "agent_id" if "agent" in mod.__name__ else "merchant_id"


def _due_rows(mod, n):
    key = _owner_key(mod)
    return [{key: f"x{i}", "delivery_id": f"d{i}"} for i in range(n)]


class _Recorder:
    """Stands in for the database and the HTTP delivery. It records every query and every
    delivery, so a case can assert what was NOT done as well as what was."""

    def __init__(self, mod, due: int):
        self.mod = mod
        self.due = due
        self.queries = []
        self.delivered = []
        self.ensured = 0

    async def fetch_all(self, query, params=None):
        self.queries.append((query, dict(params or {})))
        if "status = 'retrying'" in query:
            return _due_rows(self.mod, self.due)
        return []  # the listing query in list_deliveries

    async def retry_delivery(self, owner_id, delivery_id):
        self.delivered.append((owner_id, delivery_id))
        return {"status": "delivered"}

    async def ensure(self, *a, **k):
        self.ensured += 1

    async def summary(self, *a, **k):
        return {"total": 0}

    def due_queries(self):
        return [q for q in self.queries if "status = 'retrying'" in q[0]]


@pytest.fixture(params=MODULES)
def svc(request, monkeypatch):
    mod = importlib.import_module(request.param)
    monkeypatch.setattr(mod, "_db_now", lambda: 0, raising=False)
    return mod


def _arm(mod, monkeypatch, due=3):
    rec = _Recorder(mod, due)
    monkeypatch.setattr(mod.database, "fetch_all", rec.fetch_all)
    monkeypatch.setattr(mod, "retry_delivery", rec.retry_delivery)
    ensure = "ensure_agent_webhook_tables" if "agent" in mod.__name__ else "ensure_merchant_webhook_tables"
    monkeypatch.setattr(mod, ensure, rec.ensure)
    monkeypatch.setattr(mod, "get_delivery_summary", rec.summary)
    return rec


def _set(monkeypatch, pivota_env, value=None):
    monkeypatch.setenv("PIVOTA_ENV", pivota_env)
    if value is None:
        monkeypatch.delenv(VAR, raising=False)
    else:
        monkeypatch.setenv(VAR, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("pivota_env", ["staging", "development", "test"])
async def test_outside_production_nothing_is_selected_or_delivered(svc, monkeypatch, pivota_env):
    """THE POINT. Due rows exist, and none is even SELECTED, let alone POSTed."""
    _set(monkeypatch, pivota_env)
    rec = _arm(svc, monkeypatch, due=5)
    assert await svc.process_due_retries(limit=20) == 0
    assert rec.delivered == [], f"{pivota_env} delivered {rec.delivered}"
    assert rec.due_queries() == [], "a disabled process still selected due rows"


@pytest.mark.asyncio
async def test_production_unset_delivers_every_due_row(svc, monkeypatch):
    """Prod unchanged: with the variable unset, every due row is delivered, in order."""
    _set(monkeypatch, "production")
    rec = _arm(svc, monkeypatch, due=5)
    assert await svc.process_due_retries(limit=20) == 5
    assert rec.delivered == [(f"x{i}", f"d{i}") for i in range(5)]
    assert len(rec.due_queries()) == 1


@pytest.mark.asyncio
async def test_production_issues_exactly_what_an_enabled_process_issues(svc, monkeypatch):
    """Byte-for-byte: the query text, its params, the ensure call and the deliveries are the same
    for prod-with-the-variable-unset as for an explicitly enabled process. The gate adds a check
    in front of the path; it changes nothing on it."""
    _set(monkeypatch, "production")
    prod = _arm(svc, monkeypatch, due=4)
    await svc.process_due_retries(limit=7)

    _set(monkeypatch, "staging", "true")
    opted_in = _arm(svc, monkeypatch, due=4)
    await svc.process_due_retries(limit=7)

    assert prod.queries == opted_in.queries
    assert prod.delivered == opted_in.delivered
    assert prod.ensured == opted_in.ensured == 1
    assert prod.queries[0][1] == {"now": 0, "limit": 7}


@pytest.mark.asyncio
async def test_staging_opt_in_delivers(svc, monkeypatch):
    _set(monkeypatch, "staging", "true")
    rec = _arm(svc, monkeypatch, due=2)
    assert await svc.process_due_retries() == 2
    assert len(rec.delivered) == 2


@pytest.mark.asyncio
async def test_the_kill_switch_stops_production(svc, monkeypatch):
    _set(monkeypatch, "production", "false")
    rec = _arm(svc, monkeypatch, due=2)
    assert await svc.process_due_retries() == 0
    assert rec.delivered == [] and rec.due_queries() == []


@pytest.mark.asyncio
async def test_list_deliveries_outside_production_lists_but_does_not_deliver(svc, monkeypatch):
    """The INLINE path: reading the delivery log in the portal used to drain due retries as a side
    effect. In staging the read must still work and must not deliver."""
    _set(monkeypatch, "staging")
    rec = _arm(svc, monkeypatch, due=5)
    result = await svc.list_deliveries("owner_1")
    assert result["status"] == "success"
    assert rec.delivered == []
    assert rec.due_queries() == []
    assert len(rec.queries) == 1, "the listing query itself must still run"


@pytest.mark.asyncio
async def test_list_deliveries_in_production_still_drains_inline(svc, monkeypatch):
    """The prod counterpart, so the case above cannot pass because the inline call was deleted."""
    _set(monkeypatch, "production")
    rec = _arm(svc, monkeypatch, due=3)
    await svc.list_deliveries("owner_1")
    assert len(rec.delivered) == 3
    assert rec.due_queries()[0][1]["limit"] == 10


@pytest.mark.asyncio
async def test_the_loop_outside_production_exits_at_once_and_says_so(svc, monkeypatch, caplog):
    """The boot loop must not poll at all. Its one WARNING is the only sign on a staging process
    that retries are parked on purpose, so it is asserted too."""
    _set(monkeypatch, "staging")
    rec = _arm(svc, monkeypatch, due=5)
    monkeypatch.setattr(svc.database, "is_connected", True, raising=False)
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger=svc.__name__):
        await asyncio.wait_for(svc._retry_worker_loop(stop), timeout=2)
    assert rec.queries == [] and rec.delivered == []
    lines = [r.getMessage() for r in caplog.records if r.name == svc.__name__]
    assert any("NOT started" in m and "platform_env='staging'" in m and VAR in m for m in lines), lines


@pytest.mark.asyncio
async def test_the_loop_in_production_delivers(svc, monkeypatch):
    """The loop counterpart: in prod it reaches a delivery, and stops when told to."""
    _set(monkeypatch, "production")
    rec = _arm(svc, monkeypatch, due=2)
    monkeypatch.setattr(svc.database, "is_connected", True, raising=False)
    stop = asyncio.Event()
    task = asyncio.get_running_loop().create_task(svc._retry_worker_loop(stop))
    for _ in range(100):
        if rec.delivered:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(rec.delivered) == 2

"""Outbound webhooks deliver only in production unless WEBHOOK_DELIVERY_ENABLED says otherwise.

Staging runs on a restored copy of production data with production credentials. Every webhook
config in it points at a real agent or merchant endpoint and carries that customer's prod signing
secret. Outbound delivery could leave staging on four paths: the retry loops (started on every
process, `web` included) with the inline drain in `list_deliveries`, first sends
(`emit_*_webhook_event`), a user-clicked per-row retry, and a test send.
services/webhook_delivery_gate.py closes all four. These cases pin:

  * the parser's contract, and that prod with the variable unset still delivers;
  * that outside production NO path POSTs or writes a delivery row, and that the explicit opt-in
    and the kill switch both work, on every path;
  * that `_attempt_delivery` is the only place either service touches httpx, so the one gate
    there cannot be bypassed by a new POST site added elsewhere;
  * that prod's due-row query and delivery calls are exactly what an enabled process issues.

Both services carry an independent copy of this code, so every service case runs against both.
"""

from __future__ import annotations

import asyncio
import importlib
import logging

import pytest

from services import webhook_delivery_gate as gate

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
    assert gate.delivery_enabled(env) is expected, env


def test_an_unparseable_value_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        assert gate.delivery_enabled({"PIVOTA_ENV": "production", VAR: "ture"}) is False
    assert any(
        r.levelno == logging.WARNING and VAR in r.getMessage() and "OFF" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_it_reads_the_live_environment(monkeypatch):
    """No caching: the value is read per call, so a test (or a restart with new env) sees it."""
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    monkeypatch.delenv(VAR, raising=False)
    assert gate.delivery_enabled() is False
    monkeypatch.setenv(VAR, "true")
    assert gate.delivery_enabled() is True
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.delenv(VAR, raising=False)
    assert gate.delivery_enabled() is True


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


# ── every outbound path ends at _attempt_delivery ────────────────────────────────────────


class _FakeHttp:
    """Replaces httpx.AsyncClient. It records every POST and answers 200."""

    posts: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        _FakeHttp.posts.append(url)

        class _Resp:
            status_code = 200
            text = "ok"

        return _Resp()


DEST = "https://customer.example/hook"


def _arm_paths(mod, monkeypatch):
    """Stub the DB and the network under the REAL emit / send_test_webhook / retry_delivery, so
    the gate is exercised where it is enforced, not around a stub."""
    _FakeHttp.posts = []
    persisted = []
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeHttp)

    events = list(getattr(mod, "DEFAULT_MERCHANT_WEBHOOK_EVENTS", None) or mod.DEFAULT_WEBHOOK_EVENTS)
    config = {
        "destination_url": DEST,
        "enabled": True,
        "subscribed_events": events,
        "signing_secret": "whsec_test",
    }

    async def raw_config(*a, **k):
        return dict(config)

    async def persist(**kwargs):
        persisted.append(kwargs["status"])

    async def noop(*a, **k):
        return None

    async def secret(*a, **k):
        return "whsec_test"

    key = _owner_key(mod)

    async def fetch_one(query, params=None):
        return {
            key: "owner_1", "delivery_id": "d1", "event_id": "e1", "event_type": events[0],
            "destination_url": DEST, "payload": {"data": {"a": 1}}, "created_at": None,
            "request_id": None, "attempt_count": 1,
        }

    monkeypatch.setattr(mod, "_get_or_create_raw_config", raw_config)
    monkeypatch.setattr(mod, "_persist_delivery_attempt", persist)
    if hasattr(mod, "_ensure_signing_secret"):
        monkeypatch.setattr(mod, "_ensure_signing_secret", secret)
    ensure = "ensure_agent_webhook_tables" if "agent" in mod.__name__ else "ensure_merchant_webhook_tables"
    monkeypatch.setattr(mod, ensure, noop)
    monkeypatch.setattr(mod.database, "fetch_one", fetch_one)
    monkeypatch.setattr(mod.database, "execute", noop)
    return events[0], persisted


def _paths(mod, event_type):
    """The three request-triggered outbound paths, each as a zero-arg coroutine factory."""
    emit = getattr(mod, "emit_agent_webhook_event", None) or mod.emit_merchant_webhook_event
    return {
        "emit": lambda: emit("owner_1", event_type=event_type, payload={"a": 1}),
        "send_test_webhook": lambda: mod.send_test_webhook("owner_1"),
        "retry_delivery": lambda: mod.retry_delivery("owner_1", "d1"),
    }


PATHS = ["emit", "send_test_webhook", "retry_delivery"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("pivota_env", ["staging", "development", "test"])
async def test_outside_production_no_path_posts_or_writes_a_row(svc, monkeypatch, path, pivota_env):
    """THE POINT of the second half: first sends, per-row retries and test sends are gated too."""
    _set(monkeypatch, pivota_env)
    event_type, persisted = _arm_paths(svc, monkeypatch)
    result = await _paths(svc, event_type)[path]()
    assert _FakeHttp.posts == [], f"{path} POSTed from {pivota_env}"
    assert persisted == [], f"{path} wrote a delivery row from {pivota_env}"
    assert result["status"] == "skipped" and result["reason"] == gate.SKIP_REASON, result


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_production_unset_posts_on_every_path(svc, monkeypatch, path):
    """The counterpart: prod with the variable unset delivers exactly as before."""
    _set(monkeypatch, "production")
    event_type, persisted = _arm_paths(svc, monkeypatch)
    result = await _paths(svc, event_type)[path]()
    assert _FakeHttp.posts == [DEST]
    assert persisted == ["delivered"]
    assert result["status"] == "delivered", result


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_staging_opt_in_posts_on_every_path(svc, monkeypatch, path):
    _set(monkeypatch, "staging", "true")
    event_type, _ = _arm_paths(svc, monkeypatch)
    await _paths(svc, event_type)[path]()
    assert _FakeHttp.posts == [DEST]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_the_kill_switch_stops_every_path_in_production(svc, monkeypatch, path):
    _set(monkeypatch, "production", "off")
    event_type, persisted = _arm_paths(svc, monkeypatch)
    result = await _paths(svc, event_type)[path]()
    assert _FakeHttp.posts == [] and persisted == []
    assert result["reason"] == gate.SKIP_REASON


@pytest.mark.asyncio
async def test_existing_skip_reasons_are_unchanged_in_staging(svc, monkeypatch):
    """The gate sits BELOW the existing skips, so an unconfigured destination still reports
    `webhook_not_configured`, not the environment, in every environment."""
    _set(monkeypatch, "staging")
    event_type, _ = _arm_paths(svc, monkeypatch)

    async def unconfigured(*a, **k):
        return {"destination_url": "", "enabled": False, "subscribed_events": [], "signing_secret": "s"}

    monkeypatch.setattr(svc, "_get_or_create_raw_config", unconfigured)
    emit = getattr(svc, "emit_agent_webhook_event", None) or svc.emit_merchant_webhook_event
    result = await emit("owner_1", event_type=event_type, payload={})
    assert result["reason"] == "webhook_not_configured"


def _httpx_sites(mod):
    """Names of the top-level functions whose bodies mention `httpx`."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mod))
    sites = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(n, ast.Name) and n.id == "httpx" for n in ast.walk(node)):
                sites.add(node.name)
        elif not isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(isinstance(n, ast.Name) and n.id == "httpx" for n in ast.walk(node)):
                sites.add("<module level>")
    return sites


@pytest.mark.parametrize("modname", MODULES)
def test_attempt_delivery_is_the_only_place_that_touches_httpx(modname):
    """The gate lives in `_attempt_delivery` on the claim that it is the one POST site. A second
    httpx call anywhere else in the module would bypass it silently, so the claim is pinned."""
    mod = importlib.import_module(modname)
    assert _httpx_sites(mod) == {"_attempt_delivery"}


@pytest.mark.parametrize("modname", MODULES)
def test_attempt_delivery_checks_the_gate_before_anything_else(modname):
    """Checking first means no id is minted, no payload is signed and nothing is written when
    delivery is off. The first statement of the body must be the gate."""
    import ast
    import inspect

    mod = importlib.import_module(modname)
    fn = ast.parse(inspect.getsource(mod._attempt_delivery)).body[0]
    first = fn.body[0]
    assert isinstance(first, ast.If), ast.dump(first)[:200]
    assert "delivery_enabled" in ast.unparse(first.test)

"""OFFERS_RESOLVE_GROUP_SEED_SIBLINGS=shadow on POSTGRES, through the REAL handler.

Shadow serves the OFF answer byte for byte and, for a RESOLVED group only, works out the group-mode
best price after the response is built (a FastAPI BackgroundTask), on a connection of its own, in a
READ ONLY transaction, every statement under a server-side statement_timeout, and logs one line
(`offers_resolve_group_shadow`). This file pins each of those claims against a real engine:

  * the response: off == shadow, the full JSON in key order, for a resolved and an unresolved group;
  * the line: a stale and a fresh comparable offer, and `no_fresh_comparable_price`;
  * the budget (`budget_exceeded`), a server-side statement timeout, and an exception: each logged,
    none served, and the NEXT request on the same connection context answers exactly as before;
  * no link minted, no click issued, no gate, no live verification, no outbound HTTP;
  * the shadow's connection is not the request's, and its transaction is READ ONLY;
  * an unresolved (or merchant-scoped) request sends exactly the off path's queries -- zero extra.

The background tasks run as Starlette runs them: after the handler returned, awaited in the same task
(and so the same `databases` connection context) as the request. The callable only SPAWNS the shadow
(Starlette 0.27 under uvicorn holds the next keep-alive request behind a background task), so each
test then waits for the detached shadow (`_Harness.settle`). The rows and the schema are the
group lane's own Postgres gate's (tests/test_offers_resolve_group_siblings_postgres.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from tests.test_offers_resolve_group_siblings_postgres import (  # noqa: F401 -- `schema_engine` is a fixture
    _IS_PG,
    _SCHEMA,
    BRAND_SIG,
    CK,
    DATABASE_URL,
    RETAILER_SIG,
    _rows,
    schema_engine,
)

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)"
)

FLAG = "OFFERS_RESOLVE_GROUP_SEED_SIBLINGS"
EVENT = "offers_resolve_group_shadow"


class _Lines(logging.Handler):
    """The shadow's lines as `utils.logger` ("pivota", propagate=False) emits them: `<event> <json>`."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.all: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith(EVENT):
            self.all.append(record)

    def events(self, event: str) -> List[Dict[str, Any]]:
        out = []
        for r in self.all:
            name, _, payload = r.getMessage().partition(" ")
            if name == event:
                out.append(json.loads(payload))
        return out

    @property
    def records(self) -> List[logging.LogRecord]:
        return [r for r in self.all if r.getMessage().partition(" ")[0] == EVENT]

    @property
    def parsed(self) -> List[Dict[str, Any]]:
        return self.events(EVENT)


@pytest.fixture
def lines():
    handler = _Lines()
    pivota = logging.getLogger("pivota")
    pivota.addHandler(handler)
    try:
        yield handler
    finally:
        pivota.removeHandler(handler)


@pytest.fixture(autouse=True)
def _fresh_shadow_cache(monkeypatch):
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "_GROUP_SHADOW_CKS",
                        {"cks": None, "loaded_at": 0.0, "overflow": False, "refreshing": False, "failed_at": None})
    monkeypatch.setattr(gateway, "_GROUP_SHADOW_IN_FLIGHT", set())
    monkeypatch.setattr(gateway, "_GROUP_SHADOW_SKIPS", {})
    monkeypatch.setattr(gateway, "_GROUP_SHADOW_CONNECTED", {"n": 0})


async def _drain(timeout: float = 5.0) -> None:
    """Wait until every detached shadow is DONE and its done callback has run."""
    import routes.agent_shop_gateway as gateway

    deadline = time.perf_counter() + timeout
    while gateway._GROUP_SHADOW_IN_FLIGHT and time.perf_counter() < deadline:
        await asyncio.wait(set(gateway._GROUP_SHADOW_IN_FLIGHT), timeout=max(0.01, deadline - time.perf_counter()))
        await asyncio.sleep(0)  # done callbacks run on the next loop iteration
    assert not gateway._GROUP_SHADOW_IN_FLIGHT, "a shadow task never finished"


def _normalized(body: Dict[str, Any]) -> Dict[str, Any]:
    def walk(x):
        if isinstance(x, dict):
            return {k: (0 if k == "latency_ms" else walk(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return walk(body)


def _bytes(body: Dict[str, Any]) -> str:
    """The serialized response in KEY ORDER (no sort_keys): a reordered key is a different answer."""
    return json.dumps(_normalized(body), default=str)


class _Harness:
    """One connected Database for a whole test, so several requests share one worker's pool and the
    test's task carries one request connection context across them."""

    def __init__(self, monkeypatch, db):
        self.monkeypatch = monkeypatch
        self.db = db

    async def call(self, product_id: str, *, flag: Optional[str], seed_reasons=(), limit=10,
                   metadata: Optional[Dict[str, Any]] = None):
        import routes.agent_shop_gateway as gateway
        from fastapi import BackgroundTasks

        from routes.agent_shop_gateway import OffersResolvePayload, _handle_offers_resolve

        if flag is None:
            self.monkeypatch.delenv(FLAG, raising=False)
        else:
            self.monkeypatch.setenv(FLAG, flag)

        async def gate(row, **kwargs):
            return False, type("GateStatus", (), {"blocker_anomaly_types": list(seed_reasons)})()

        self.monkeypatch.setattr(gateway, "should_block_external_referral_runtime", gate)
        # Click ids restart per request, so two runs of one request mint the same links.
        counter = itertools.count(1)
        self.monkeypatch.setattr(gateway, "new_click_id", lambda: f"clk_{next(counter):04d}")
        bt = BackgroundTasks()
        res = await _handle_offers_resolve(
            OffersResolvePayload(product={"product_id": product_id}, limit=limit, market="US", tool="*",
                                 commerce_surface="agent_api"),
            metadata, bt,
        )
        return res, bt

    async def settle(self, bt) -> None:
        """Run the background tasks as Starlette does, then wait for the detached shadow they spawned."""
        await bt()
        await _drain()


def _run(monkeypatch, body, **pool):
    import databases

    import db.database as dbmod
    import db.merchant_purchasability as mp
    import routes.agent_shop_gateway as gateway
    import services.external_offer_dual_write as dual_write
    import services.outbound_links_service as outbound_links

    monkeypatch.setattr(dual_write, "_price_check_column", {"present": False, "checked": None})
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[]))
    monkeypatch.setattr(mp, "human_handoff_allowed", AsyncMock(return_value=True))
    # The signed /r token carries iat/exp: one clock for the whole test, so two runs of one request
    # sign the same bytes whatever second they land in.
    monkeypatch.setattr(outbound_links, "_now_ts", lambda: 1_790_000_000)

    async def go():
        db = databases.Database(DATABASE_URL, server_settings={"search_path": _SCHEMA}, **pool)
        await db.connect()
        monkeypatch.setattr(dbmod, "database", db)  # the handler imports it at call time
        monkeypatch.setattr(dual_write, "database", db)  # the price_checked_at column probe
        try:
            return await body(_Harness(monkeypatch, db))
        finally:
            for task in list(gateway._GROUP_SHADOW_IN_FLIGHT):  # a test that left one running
                task.cancel()
            await _drain()
            await db.disconnect()

    return asyncio.run(go())


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------------ the flag


@pytest.mark.parametrize("raw, mode", [
    (None, "off"), ("", "off"), ("0", "off"), ("off", "off"), ("false", "off"), ("no", "off"),
    ("1", "on"), ("true", "on"), ("on", "on"), ("yes", "on"), (" ON ", "on"),
    ("shadow", "shadow"), (" Shadow ", "shadow"),
    ("shadoww", "off"), ("2", "off"), ("enabled", "off"),
])
def test_the_flag_reads_three_modes_and_anything_else_is_off(monkeypatch, raw, mode):
    import routes.agent_shop_gateway as gateway

    if raw is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, raw)
    assert gateway._group_seed_siblings_mode() == mode


# ------------------------------------------------------------------------------- the response


@pytest.mark.parametrize("ident", [RETAILER_SIG, BRAND_SIG, CK])
def test_shadow_serves_the_off_response_for_a_resolved_group(schema_engine, monkeypatch, lines, ident):
    _rows(schema_engine, retailer_checked=None)  # the cheaper retailer's price is stale

    async def body(h: _Harness):
        off, off_bt = await h.call(ident, flag=None)
        shadow, shadow_bt = await h.call(ident, flag="shadow")
        assert not off_bt.tasks and len(shadow_bt.tasks) == 1
        await h.settle(shadow_bt)  # as Starlette runs it: after the response, in the request's task
        again, _ = await h.call(ident, flag=None)
        return off, shadow, again

    off, shadow, again = _run(monkeypatch, body)
    assert "best_price_offer_id" not in off and "best_price" not in off["metadata"]
    assert _bytes(shadow) == _bytes(off)
    assert _bytes(again) == _bytes(off), "running the shadow changed nothing the next answer reads"
    [line] = lines.parsed
    assert line["content_key"] == CK and "error" not in line


def test_shadow_serves_the_off_response_for_an_unresolved_group_and_logs_nothing(schema_engine, monkeypatch, lines):
    _rows(schema_engine, retailer_checked=_now(), resolved=False)

    async def body(h: _Harness):
        off, _ = await h.call(RETAILER_SIG, flag=None)
        cold, cold_bt = await h.call(RETAILER_SIG, flag="shadow")
        assert len(cold_bt.tasks) == 1, "a cold key set is refreshed off the request path"
        await h.settle(cold_bt)
        warm, warm_bt = await h.call(RETAILER_SIG, flag="shadow")
        assert not warm_bt.tasks, "a warm key set naming no resolved group schedules nothing"
        return off, cold, warm

    off, cold, warm = _run(monkeypatch, body)
    assert _bytes(cold) == _bytes(off) and _bytes(warm) == _bytes(off)
    assert lines.parsed == []


# ----------------------------------------------------------------------------------- the line


def test_the_line_for_a_stale_and_a_fresh_comparable_offer(schema_engine, monkeypatch, lines):
    """of_pg_sk ($12, stylekorean) was never price-checked; of_pg_ys ($24, yesstyle) was read a day ago.
    The brand seed is gated out, so the catalog arm alone answers. The off answer leads with the
    stale $12; group mode marks the fresh $24 best -- and the shadow says exactly what `on` serves."""
    _rows(schema_engine, retailer_checked=None)
    gated = ("currency_mismatch",)

    async def body(h: _Harness):
        shadow, bt = await h.call(RETAILER_SIG, flag="shadow", seed_reasons=gated)
        started = time.perf_counter()
        await h.settle(bt)
        elapsed_ms = (time.perf_counter() - started) * 1000
        on, _ = await h.call(RETAILER_SIG, flag="1", seed_reasons=gated)
        return shadow, on, elapsed_ms

    shadow, on, elapsed_ms = _run(monkeypatch, body)
    [line] = lines.parsed
    [record] = lines.records
    assert record.levelno == logging.WARNING and record.name == "pivota"
    assert "\n" not in record.getMessage()
    assert line["event"] == EVENT
    assert line["content_key"] == CK and line["product_id"] == RETAILER_SIG and line["market"] == "US"
    assert line["members"] == 3 and line["resolved"] == 3
    assert [o["offer_id"] for o in shadow["offers"]] == ["of:catalog_offer:of_pg_sk", "of:catalog_offer:of_pg_ys"]
    assert line["legacy_best"] == {"offer_id": "of:catalog_offer:of_pg_sk", "price": 12.0, "currency": "USD"}
    assert line["legacy_cheapest"] == line["legacy_best"]
    assert line["group_best"] == {"offer_id": "of:catalog_offer:of_pg_ys", "price": 24.0, "currency": "USD"}
    assert line["group_best"]["offer_id"] == on["best_price_offer_id"], "the shadow mirrors what `on` marks"
    assert line["best_differs"] is True
    assert line["price_stale"] == [
        {"offer_id": "of:catalog_offer:of_pg_sk", "host": "stylekorean.com", "price_checked_at": None}]
    assert (line["comparable"], line["fresh"], line["stale"], line["group_offers"]) == (1, 1, 1, 2)
    assert line["sibling_seeds_not_evaluated"] == 1, "the brand seed the sibling lane would gate"
    assert line["price_column"] is True and line["catalog_rows"] == 2
    assert 0 <= line["shadow_db_ms"] <= line["shadow_ms"] <= elapsed_ms + 1
    assert line["shadow_statements"] == 6  # key set, members, proposals, seeds, column probe, catalog
    assert "error" not in line


def test_the_line_when_no_comparable_price_is_fresh(schema_engine, monkeypatch, lines):
    _rows(schema_engine, retailer_checked=None, retailer2_checked=_now() - timedelta(days=9))

    async def body(h: _Harness):
        _, bt = await h.call(RETAILER_SIG, flag="shadow", seed_reasons=("currency_mismatch",))
        await h.settle(bt)
        on, _ = await h.call(RETAILER_SIG, flag="1", seed_reasons=("currency_mismatch",))
        return on

    on = _run(monkeypatch, body)
    [line] = lines.parsed
    assert line["group_best"] == "no_fresh_comparable_price" and on["best_price_offer_id"] is None
    assert (line["comparable"], line["fresh"], line["stale"]) == (0, 0, 2)
    assert sorted(o["offer_id"] for o in line["price_stale"]) == [
        "of:catalog_offer:of_pg_sk", "of:catalog_offer:of_pg_ys"]
    ys = next(o for o in line["price_stale"] if o["offer_id"] == "of:catalog_offer:of_pg_ys")
    assert ys["host"] == "yesstyle.com" and ys["price_checked_at"].endswith("+00:00")
    assert line["legacy_best"]["offer_id"] == "of:catalog_offer:of_pg_sk"
    assert "error" not in line


# ------------------------------------------------------------------------- failure is never served


def test_a_shadow_past_its_budget_is_abandoned_logged_and_never_served(schema_engine, monkeypatch, lines):
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SIBLING_BUDGET_SECONDS", 0.4)

    async def body(h: _Harness):
        off, _ = await h.call(RETAILER_SIG, flag=None)
        _, warm = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(warm)  # one healthy shadow first: the key set is warm, so the budget below is the group read's

        async def hangs(*a, **k):
            await asyncio.sleep(5)

        monkeypatch.setattr(gateway, "_fetch_resolved_group", hangs)
        shadow, bt = await h.call(RETAILER_SIG, flag="shadow")
        started = time.perf_counter()
        await h.settle(bt)  # the budget timer cancels it; it unwinds and leaves the in-flight set
        waited = time.perf_counter() - started
        after, _ = await h.call(RETAILER_SIG, flag=None)
        return off, shadow, after, waited

    off, shadow, after, waited = _run(monkeypatch, body)
    assert waited < 0.4 + 0.3, f"the shadow outlived its budget: {waited:.2f}s"
    assert _bytes(shadow) == _bytes(off) and _bytes(after) == _bytes(off)
    healthy, timed_out = lines.parsed
    assert "error" not in healthy
    assert timed_out["error"] == "budget_exceeded" and timed_out["product_id"] == RETAILER_SIG
    assert 400 <= timed_out["shadow_ms"] < 700
    assert "group_best" not in timed_out


def test_a_server_side_statement_timeout_ends_a_slow_statement_and_the_next_request_is_served(
        schema_engine, monkeypatch, lines):
    """The shadow's statement runs past its server-side statement_timeout: POSTGRES cancels it (not an
    asyncio cancel), the error is logged, and the next request -- same task, same connection
    context -- and the next shadow both work."""
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    real_fetch = gateway._fetch_resolved_group

    async def body(h: _Harness):
        off, _ = await h.call(RETAILER_SIG, flag=None)

        async def slow(db, **kw):
            await db.fetch_all("SELECT pg_sleep(5)")

        monkeypatch.setattr(gateway, "_fetch_resolved_group", slow)
        shadow, bt = await h.call(RETAILER_SIG, flag="shadow")
        started = time.perf_counter()
        await h.settle(bt)
        waited = time.perf_counter() - started
        after, _ = await h.call(RETAILER_SIG, flag=None)
        monkeypatch.setattr(gateway, "_fetch_resolved_group", real_fetch)
        _, healthy_bt = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(healthy_bt)
        return off, shadow, after, waited

    off, shadow, after, waited = _run(monkeypatch, body)
    assert waited < gateway.OFFERS_RESOLVE_GROUP_SIBLING_BUDGET_SECONDS + 0.3
    assert _bytes(shadow) == _bytes(off) and _bytes(after) == _bytes(off)
    failed, healthy = lines.parsed
    assert "QueryCanceled" in failed["error"] and "statement timeout" in failed["error"], failed
    assert "error" not in healthy and healthy["group_best"]["offer_id"] == "of:catalog_offer:of_pg_sk"


def test_a_shadow_exception_is_swallowed_logged_and_never_served(schema_engine, monkeypatch, lines):
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(gateway, "_group_shadow_compute", boom)

    async def body(h: _Harness):
        off, _ = await h.call(RETAILER_SIG, flag=None)
        shadow, bt = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(bt)  # raises nothing
        after, _ = await h.call(RETAILER_SIG, flag=None)
        return off, shadow, after

    off, shadow, after = _run(monkeypatch, body)
    assert _bytes(shadow) == _bytes(off) and _bytes(after) == _bytes(off)
    [line] = lines.parsed
    assert line["error"] == "RuntimeError: boom" and line["content_key"] == CK
    assert line["shadow_statements"] > 0


# ------------------------------------------------------------------ no clicks, no HTTP, no writes


def test_the_shadow_mints_no_link_issues_no_click_and_sends_no_http(schema_engine, monkeypatch, lines):
    import httpx

    import routes.agent_shop_gateway as gateway
    import services.live_offer_verification as live_offer_verification

    _rows(schema_engine, retailer_checked=None)
    calls: Dict[str, int] = {}

    def spy(name):
        def record(*a, **k):
            calls[name] = calls.get(name, 0) + 1
            raise AssertionError(f"the shadow called {name}")

        return record

    async def body(h: _Harness):
        shadow, bt = await h.call(RETAILER_SIG, flag="shadow")
        # From here on, anything called is called by the shadow.
        for name in ("_make_external_redirect_url", "_issue_served_clicks", "issue_clicks", "new_click_id",
                     "should_block_external_referral_runtime", "_append_external_offers_from_seed_rows"):
            if hasattr(gateway, name):
                monkeypatch.setattr(gateway, name, spy(name))
        monkeypatch.setattr(live_offer_verification, "verify_offers", spy("verify_offers"))
        monkeypatch.setattr(httpx.AsyncClient, "send", spy("httpx.AsyncClient.send"))
        monkeypatch.setattr(httpx.Client, "send", spy("httpx.Client.send"))
        await h.settle(bt)
        return shadow

    _run(monkeypatch, body)
    assert calls == {}
    [line] = lines.parsed
    assert "error" not in line and line["group_best"]["offer_id"] == "of:catalog_offer:of_pg_ys"


def test_the_shadow_reads_on_its_own_connection_in_a_read_only_transaction(schema_engine, monkeypatch, lines):
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    seen: List[Any] = []
    real_bounded = gateway._GroupShadowReader._bounded

    async def bounded(self, method, query, values):
        import db.database as dbmod

        conn = dbmod.database.connection()
        row = await conn.fetch_one("SHOW transaction_read_only")
        timeout = await conn.fetch_one("SHOW statement_timeout")
        out = await real_bounded(self, method, query, values)
        after = await conn.fetch_one("SHOW statement_timeout")
        seen.append((conn, row[0], timeout[0], after[0]))
        return out

    monkeypatch.setattr(gateway._GroupShadowReader, "_bounded", bounded)

    async def body(h: _Harness):
        _, bt = await h.call(RETAILER_SIG, flag="shadow")
        request_conn = h.db.connection()
        await h.settle(bt)
        # The request's connection context holds no transaction and no statement_timeout afterwards.
        assert not request_conn._transaction_stack
        assert (await h.db.fetch_one("SHOW statement_timeout"))[0] == "0"
        return request_conn

    request_conn = _run(monkeypatch, body)
    assert seen, "the shadow sent no statement"
    assert all(conn is not request_conn for conn, *_ in seen), "the shadow ran on the request's connection"
    assert all(ro == "on" for _, ro, _, _ in seen), seen
    # Each statement ran under a server-side timeout inside the budget (SET LOCAL lands just before it).
    assert all(after.endswith("ms") and 0 < int(after[:-2]) < 1500 for *_, after in seen), seen
    assert "error" not in lines.parsed[0]


# --------------------------------------------------------------------------------- the load bound


def _count_statements(monkeypatch, db, sink: List[str]):
    for method in ("fetch_all", "fetch_one", "fetch_val", "execute", "execute_many", "iterate"):
        real = getattr(db, method)

        def make(real, method):
            if method == "iterate":
                def iterate(query, values=None):
                    sink.append(str(query))
                    return real(query, values)

                return iterate

            async def run(query, values=None, *a, **k):
                sink.append(str(query))
                return await real(query, values, *a, **k)

            return run

        monkeypatch.setattr(db, method, make(real, method))


def test_an_unresolved_or_merchant_scoped_request_sends_no_extra_query(schema_engine, monkeypatch, lines):
    _rows(schema_engine, retailer_checked=_now(), resolved=False)
    scoped = {"merchant_id": "agent_seed::retailer::stylekorean.com"}

    async def body(h: _Harness):
        _, warm = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(warm)  # the resolved-key set is loaded once per TTL, off the request path
        sent: List[str] = []
        _count_statements(monkeypatch, h.db, sent)
        out = {}
        for name, flag, meta in (("off", None, None), ("shadow", "shadow", None),
                                 ("scoped_off", None, scoped), ("scoped_shadow", "shadow", scoped)):
            sent.clear()
            res, bt = await h.call(RETAILER_SIG, flag=flag, metadata=meta)
            assert not bt.tasks, name
            await h.settle(bt)
            out[name] = (list(sent), res)
        return out

    out = _run(monkeypatch, body)
    assert out["shadow"][0] == out["off"][0] and out["off"][0], "shadow sent other statements than off"
    assert out["scoped_shadow"][0] == out["scoped_off"][0] and out["scoped_off"][0]
    assert _bytes(out["shadow"][1]) == _bytes(out["off"][1])
    assert _bytes(out["scoped_shadow"][1]) == _bytes(out["scoped_off"][1])
    assert lines.parsed == []


def test_a_cold_key_set_costs_one_background_read_and_no_group_read(schema_engine, monkeypatch, lines):
    _rows(schema_engine, retailer_checked=_now(), resolved=False)

    async def body(h: _Harness):
        sent: List[str] = []
        _count_statements(monkeypatch, h.db, sent)
        _, bt = await h.call(RETAILER_SIG, flag="shadow")
        sent.clear()
        await h.settle(bt)
        return sent

    sent = _run(monkeypatch, body)
    reads = [q for q in sent if not q.startswith("SET LOCAL statement_timeout")]
    assert len(reads) == 1 and "FROM identity_resolution_proposals" in reads[0] and "DISTINCT content_key" in reads[0]
    assert lines.parsed == []


def test_a_failed_key_set_read_is_logged_once_and_not_retried_per_request(schema_engine, monkeypatch, lines):
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    monkeypatch.setattr(gateway, "_GROUP_SHADOW_RESOLVED_CKS_SQL", (
        "SELECT content_key FROM no_such_table WHERE CAST(:kinds AS TEXT[]) IS NOT NULL"
        " AND CAST(:strategies AS TEXT[]) IS NOT NULL LIMIT :limit"))

    async def body(h: _Harness):
        off, _ = await h.call(RETAILER_SIG, flag=None)
        first, bt = await h.call(RETAILER_SIG, flag="shadow")
        assert len(bt.tasks) == 1
        await h.settle(bt)
        second, bt2 = await h.call(RETAILER_SIG, flag="shadow")
        assert not bt2.tasks, "a failed refresh backs off: no task, no query, until the retry window"
        return off, first, second

    off, first, second = _run(monkeypatch, body)
    assert _bytes(first) == _bytes(off) and _bytes(second) == _bytes(off)
    [line] = lines.parsed
    assert "UndefinedTable" in line["error"] and "content_key" not in line


# ------------------------------------------------------------------ round 2: detached, capped, bounded


def test_the_background_callable_returns_before_the_shadow_finishes(schema_engine, monkeypatch, lines):
    """Starlette 0.27 under uvicorn holds the next request on a keep-alive connection until this
    request's background tasks finish, so the callable must only spawn the shadow."""
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    real = gateway._fetch_resolved_group

    async def slow(db, **kw):
        await asyncio.sleep(0.5)
        return await real(db, **kw)

    async def body(h: _Harness):
        _, warm = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(warm)
        monkeypatch.setattr(gateway, "_fetch_resolved_group", slow)
        _, bt = await h.call(RETAILER_SIG, flag="shadow")
        started = time.perf_counter()
        await bt()  # what Starlette awaits
        returned = time.perf_counter() - started
        [task] = gateway._GROUP_SHADOW_IN_FLIGHT
        running = not task.done()
        lines_before = len(lines.parsed)
        await _drain()
        return returned, running, lines_before

    returned, running, lines_before = _run(monkeypatch, body)
    assert returned < 0.05, f"the background callable waited for the shadow: {returned:.3f}s"
    assert running and lines_before == 1, "the shadow was still working when the callable returned"
    assert len(lines.parsed) == 2 and "error" not in lines.parsed[1]


def test_the_cap_skips_and_a_plain_request_is_not_held_behind_shadows(schema_engine, monkeypatch, lines):
    """Pool of 3. Six resolved requests, each shadow holding a connection for 0.7 s: two run (the cap),
    four are SKIPPED (never queued), and a plain request issued meanwhile gets a connection at once."""
    import contextvars

    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    real = gateway._fetch_resolved_group

    async def slow(db, **kw):
        await db.fetch_all("SELECT pg_sleep(0.7)")
        return await real(db, **kw)

    async def body(h: _Harness):
        _, warm = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(warm)
        monkeypatch.setattr(gateway, "_fetch_resolved_group", slow)
        for _ in range(6):
            _, bt = await h.call(RETAILER_SIG, flag="shadow")
            await bt()
        in_flight = len(gateway._GROUP_SHADOW_IN_FLIGHT)
        await asyncio.sleep(0.1)  # both shadows are inside pg_sleep, each on its own connection

        async def plain():
            started = time.perf_counter()
            await h.db.fetch_one("SELECT 1")
            return time.perf_counter() - started

        waited = await asyncio.get_running_loop().create_task(plain(), context=contextvars.Context())
        await _drain()
        return in_flight, waited

    in_flight, waited = _run(monkeypatch, body, min_size=1, max_size=3)
    assert waited < 0.2, f"a plain request waited {waited:.3f}s for a connection behind the shadows"
    assert in_flight == 2 == gateway.OFFERS_RESOLVE_GROUP_SHADOW_MAX_IN_FLIGHT
    warm_line, *slow_lines = lines.parsed
    assert len(slow_lines) == 2 and all("error" not in line for line in slow_lines), slow_lines
    # The skips ride on the next shadow line, once: no line per skipped request.
    assert [line.get("skipped") for line in slow_lines] == [{"cap": 4}, None]


def test_no_shadow_starts_when_the_pool_has_no_spare_connection(schema_engine, monkeypatch, lines):
    _rows(schema_engine, retailer_checked=_now())

    async def body(h: _Harness):
        _, warm = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(warm)
        pool = h.db._backend._pool
        held = [await pool.acquire(), await pool.acquire()]  # 2 of 3 taken: 1 free, which is the reserve
        try:
            _, bt = await h.call(RETAILER_SIG, flag="shadow")
            await bt()
            import routes.agent_shop_gateway as gateway

            skipped_in_flight = len(gateway._GROUP_SHADOW_IN_FLIGHT)
        finally:
            for conn in held:
                await pool.release(conn)
        _, bt = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(bt)
        return skipped_in_flight

    assert _run(monkeypatch, body, min_size=1, max_size=3) == 0
    warm_line, next_line = lines.parsed
    assert next_line["skipped"] == {"pool_busy": 1} and "error" not in next_line


def test_an_overflowing_key_set_turns_the_shadow_off_and_says_so_once(schema_engine, monkeypatch, lines):
    """At the key limit the set cannot gate. The shadow then shadows NOTHING (not everything), logs
    `offers_resolve_group_shadow_disabled` once for that refresh, and counts the skips."""
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())  # one resolved key: limit 1 overflows
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SHADOW_CK_LIMIT", 1)
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SHADOW_SKIP_SUMMARY_EVERY", 4)

    async def body(h: _Harness):
        off, _ = await h.call(RETAILER_SIG, flag=None)
        _, first = await h.call(RETAILER_SIG, flag="shadow")
        assert len(first.tasks) == 1  # cold: the refresh runs in the background
        await h.settle(first)
        scheduled = []
        for ident in (RETAILER_SIG, BRAND_SIG, CK):
            res, bt = await h.call(ident, flag="shadow")
            scheduled.append(len(bt.tasks))
        return scheduled

    assert _run(monkeypatch, body) == [0, 0, 0]
    assert lines.parsed == [], "nothing is shadowed while the set overflows"
    [disabled] = lines.events(EVENT + "_disabled")
    assert disabled["reason"] == "overflow" and disabled["keys"] == 1 and disabled["limit"] == 1
    [summary] = lines.events(EVENT + "_skips")
    assert summary["skipped"] == {"overflow": 4}


def _shutdown_case(monkeypatch, lines, hang):
    """Start a shadow, then cancel every other task as a shutting-down loop does. The loop's exception
    handler must see nothing (no "Task exception was never retrieved"), and the in-flight set must
    empty, so the cap is never held by a dead shadow."""
    import gc

    import routes.agent_shop_gateway as gateway

    seen: List[Dict[str, Any]] = []

    async def body(h: _Harness):
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))
        _, warm = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(warm)
        monkeypatch.setattr(gateway, "_fetch_resolved_group", hang)
        _, bt = await h.call(RETAILER_SIG, flag="shadow")
        await bt()
        await asyncio.sleep(0.1)
        [task] = gateway._GROUP_SHADOW_IN_FLIGHT
        for other in asyncio.all_tasks():
            if other is not asyncio.current_task():
                other.cancel()
        await _drain()
        await asyncio.sleep(gateway.OFFERS_RESOLVE_GROUP_SIBLING_BUDGET_SECONDS + 0.1)  # the timer is gone too
        del task
        gc.collect()
        await asyncio.sleep(0)
        return len(gateway._GROUP_SHADOW_IN_FLIGHT)

    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SIBLING_BUDGET_SECONDS", 0.5)
    assert _run(monkeypatch, body) == 0
    assert seen == [], seen
    return lines.parsed


def test_a_shutdown_cancel_unwinds_the_shadow_and_leaves_nothing_unretrieved(schema_engine, monkeypatch, lines):
    _rows(schema_engine, retailer_checked=_now())

    async def hang(*a, **k):
        await asyncio.sleep(5)

    parsed = _shutdown_case(monkeypatch, lines, hang)
    assert len(parsed) == 1, "a cancelled shadow logs nothing, and its budget timer never fires"


def test_a_shadow_that_fails_while_unwinding_is_still_retrieved(schema_engine, monkeypatch, lines):
    _rows(schema_engine, retailer_checked=_now())

    async def hang(*a, **k):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise RuntimeError("rollback failed while unwinding")

    parsed = _shutdown_case(monkeypatch, lines, hang)
    assert len(parsed) == 2 and parsed[1]["error"] == "RuntimeError: rollback failed while unwinding"


# ----------------------------------------------------------------- round 3: the reserve, exactly


def test_two_spawns_in_one_tick_cannot_both_take_the_reserve(schema_engine, monkeypatch, lines):
    """1 of 3 connections held: the pool reports 2 free. Two shadows spawned in the SAME loop tick
    (neither has connected yet) must not both start -- the first takes the spare connection, the
    second sees 2 free minus 1 spawned-not-connected = the reserve, and skips. A plain request issued
    while the first shadow holds its connection still gets one at once."""
    import contextvars

    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    real = gateway._fetch_resolved_group

    async def slow(db, **kw):
        await db.fetch_all("SELECT pg_sleep(0.7)")
        return await real(db, **kw)

    async def body(h: _Harness):
        _, warm = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(warm)
        monkeypatch.setattr(gateway, "_fetch_resolved_group", slow)
        pool = h.db._backend._pool
        _, bt1 = await h.call(RETAILER_SIG, flag="shadow")
        _, bt2 = await h.call(RETAILER_SIG, flag="shadow")
        held = await pool.acquire()
        try:
            free_before = pool.get_idle_size() + pool.get_max_size() - pool.get_size()
            # Starlette awaits each callable in turn; neither suspends, so both run in ONE tick.
            await bt1()
            await bt2()
            spawned = len(gateway._GROUP_SHADOW_IN_FLIGHT)
            await asyncio.sleep(0.1)  # the spawned shadow is now inside pg_sleep

            async def plain():
                started = time.perf_counter()
                await h.db.fetch_one("SELECT 1")
                return time.perf_counter() - started

            waited = await asyncio.get_running_loop().create_task(plain(), context=contextvars.Context())
            await _drain()
        finally:
            await pool.release(held)
        return free_before, spawned, waited

    free_before, spawned, waited = _run(monkeypatch, body, min_size=1, max_size=3)
    assert free_before == 2
    assert waited < 0.2, f"a plain request waited {waited:.3f}s behind the shadows"
    assert spawned == 1
    warm_line, shadow_line = lines.parsed
    assert shadow_line["skipped"] == {"pool_busy": 1} and "error" not in shadow_line


@pytest.mark.parametrize("pool_state", ["missing", "raises"])
def test_an_unreadable_pool_fails_closed(schema_engine, monkeypatch, lines, pool_state):
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())

    class _Raises:
        def __getattr__(self, name):
            raise RuntimeError("pool is gone")

    async def body(h: _Harness):
        _, warm = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(warm)
        _, bt = await h.call(RETAILER_SIG, flag="shadow")
        backend = h.db._backend
        real_pool = backend._pool
        backend._pool = None if pool_state == "missing" else _Raises()
        try:
            await bt()  # does not suspend: the swapped pool is all it sees
            spawned = len(gateway._GROUP_SHADOW_IN_FLIGHT)
        finally:
            backend._pool = real_pool
        _, after = await h.call(RETAILER_SIG, flag="shadow")
        await h.settle(after)
        return spawned

    assert _run(monkeypatch, body) == 0
    warm_line, next_line = lines.parsed
    assert next_line["skipped"] == {"pool_unknown": 1} and "error" not in next_line


# ---------------------------------------------- arming: proof the shadow is alive with no traffic


CACHE = EVENT + "_cache"


async def _stop(task) -> None:
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    await _drain()


async def _until(predicate, timeout: float = 3.0) -> None:
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        await asyncio.sleep(0.02)


def _refresh_statements(sent: List[Any]) -> List[Any]:
    return [entry for entry in sent if "DISTINCT content_key" in entry[1]]


def _timed_statements(monkeypatch, db, sink: List[Any]):
    """Like `_count_statements`, with the time each statement was sent."""
    for method in ("fetch_all", "fetch_one", "fetch_val", "execute"):
        real = getattr(db, method)

        def make(real):
            async def run(query, values=None, *a, **k):
                sink.append((time.perf_counter(), str(query)))
                return await real(query, values, *a, **k)

            return run

        monkeypatch.setattr(db, method, make(real))


@pytest.mark.parametrize("flag", [None, "0", "off", "1", "garbage"])
def test_arming_starts_nothing_unless_the_mode_is_shadow(schema_engine, monkeypatch, lines, flag):
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SHADOW_ARM_DELAY_SECONDS", 0.0)

    async def body(h: _Harness):
        if flag is None:
            monkeypatch.delenv(FLAG, raising=False)
        else:
            monkeypatch.setenv(FLAG, flag)
        sent: List[Any] = []
        _timed_statements(monkeypatch, h.db, sent)
        before = set(asyncio.all_tasks())
        task = gateway.start_group_shadow_arming()
        await asyncio.sleep(0.3)
        return task, set(asyncio.all_tasks()) - before, sent

    task, new_tasks, sent = _run(monkeypatch, body)
    assert task is None and not new_tasks, "no task"
    assert sent == [], "no query"
    assert lines.all == [], "no line"


def test_shadow_announces_itself_at_startup_with_keys_and_pool_state(schema_engine, monkeypatch, lines):
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SHADOW_ARM_DELAY_SECONDS", 0.0)
    monkeypatch.setenv(FLAG, "shadow")

    async def body(h: _Harness):
        sent: List[Any] = []
        _timed_statements(monkeypatch, h.db, sent)
        task = gateway.start_group_shadow_arming()
        await _until(lambda: lines.events(CACHE))
        await _stop(task)
        return sent

    sent = _run(monkeypatch, body)
    [line] = lines.events(CACHE)
    assert line["trigger"] == "startup" and line["mode"] == "shadow" and line["refreshed"] is True
    assert line["keys"] == 1 and line["overflow"] is False and "error" not in line
    assert isinstance(line["pool_free"], int) and line["pool_free"] > 0
    assert line["in_flight"] == 1 and line["connected"] == 0  # logged by the refresh task, after its txn
    assert line["skipped"] == {} and line["pid"] == os.getpid() and line["refresh_ms"] >= 0
    assert len(_refresh_statements(sent)) == 1
    assert lines.parsed == [], "arming logs the cache line only"


def test_an_unreadable_pool_is_reported_unknown_and_nothing_is_read(schema_engine, monkeypatch, lines):
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SHADOW_ARM_DELAY_SECONDS", 0.0)
    monkeypatch.setenv(FLAG, "shadow")

    class _Raises:
        def __getattr__(self, name):
            raise RuntimeError("pool is gone")

    async def body(h: _Harness):
        sent: List[Any] = []
        _timed_statements(monkeypatch, h.db, sent)
        backend = h.db._backend
        real_pool = backend._pool
        backend._pool = _Raises()
        try:
            task = gateway.start_group_shadow_arming()
            await _until(lambda: lines.events(CACHE))
            await _stop(task)
        finally:
            backend._pool = real_pool
        return sent

    sent = _run(monkeypatch, body)
    [line] = lines.events(CACHE)
    assert line["pool_free"] == "unknown" and line["refreshed"] is False and line["reason"] == "pool_unknown"
    assert line["keys"] is None
    assert sent == [], "fail closed: no query"


def test_one_refresh_per_ttl_across_the_arming_loop_and_lazy_refreshes(schema_engine, monkeypatch, lines):
    """TTL 1 s, the arming loop running, and resolved shadow requests every 0.15 s (each of which would
    refresh a due key set lazily). Over ~2.6 s: about three refreshes, never two within a TTL, and one
    cache line per refresh."""
    import routes.agent_shop_gateway as gateway

    ttl = 1.0
    _rows(schema_engine, retailer_checked=_now())
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SHADOW_ARM_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SHADOW_CK_TTL_SECONDS", ttl)

    async def body(h: _Harness):
        sent: List[Any] = []
        _timed_statements(monkeypatch, h.db, sent)
        monkeypatch.setenv(FLAG, "shadow")
        task = gateway.start_group_shadow_arming()
        deadline = time.perf_counter() + 2.6
        while time.perf_counter() < deadline:
            _, bt = await h.call(RETAILER_SIG, flag="shadow")
            await h.settle(bt)
            await asyncio.sleep(0.15)
        await _stop(task)
        return sent

    sent = _run(monkeypatch, body)
    times = [t for t, _q in _refresh_statements(sent)]
    assert 2 <= len(times) <= 4, times
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= ttl * 0.95, f"two key-set refreshes within one TTL: {gaps}"
    cache_lines = lines.events(CACHE)
    assert len(cache_lines) == len(times) and all(line["refreshed"] for line in cache_lines)
    assert cache_lines[0]["trigger"] in ("startup", "request")


def test_a_failed_arming_refresh_logs_its_error_and_backs_off(schema_engine, monkeypatch, lines):
    import routes.agent_shop_gateway as gateway

    _rows(schema_engine, retailer_checked=_now())
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SHADOW_ARM_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SHADOW_CK_RETRY_SECONDS", 0.6)
    monkeypatch.setattr(gateway, "_GROUP_SHADOW_RESOLVED_CKS_SQL", (
        "SELECT content_key FROM no_such_table WHERE CAST(:kinds AS TEXT[]) IS NOT NULL"
        " AND CAST(:strategies AS TEXT[]) IS NOT NULL LIMIT :limit"))
    monkeypatch.setenv(FLAG, "shadow")

    async def body(h: _Harness):
        sent: List[Any] = []
        _timed_statements(monkeypatch, h.db, sent)
        task = gateway.start_group_shadow_arming()
        await _until(lambda: lines.events(CACHE))
        _, bt = await h.call(RETAILER_SIG, flag="shadow")
        lazy_tasks = len(bt.tasks)  # inside the backoff: a request schedules no refresh either
        await asyncio.sleep(1.0)
        await _stop(task)
        return sent, lazy_tasks

    sent, lazy_tasks = _run(monkeypatch, body)
    assert lazy_tasks == 0
    cache_lines = lines.events(CACHE)
    assert len(cache_lines) == 2, cache_lines  # t=0, then once the 0.6 s backoff has passed
    assert all(line["refreshed"] is False and "UndefinedTable" in line["error"] for line in cache_lines)
    times = [t for t, q in sent if "no_such_table" in q]
    assert len(times) == 2 and times[1] - times[0] >= 0.6, times


def test_main_lifespan_starts_the_arming_loop_and_cancels_it():
    import inspect

    import main

    src = inspect.getsource(main.app_lifespan)
    assert "start_group_shadow_arming()" in src and "group_shadow_armer.cancel()" in src

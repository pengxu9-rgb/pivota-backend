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
(and so the same `databases` connection context) as the request. The rows and the schema are the
group lane's own Postgres gate's (tests/test_offers_resolve_group_siblings_postgres.py).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
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
    """The shadow's lines as `utils.logger` ("pivota", propagate=False) emits them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith(EVENT + " "):
            self.records.append(record)

    @property
    def parsed(self) -> List[Dict[str, Any]]:
        return [json.loads(r.getMessage()[len(EVENT) + 1:]) for r in self.records]


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


def _run(monkeypatch, body):
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
        db = databases.Database(DATABASE_URL, server_settings={"search_path": _SCHEMA})
        await db.connect()
        monkeypatch.setattr(dbmod, "database", db)  # the handler imports it at call time
        monkeypatch.setattr(dual_write, "database", db)  # the price_checked_at column probe
        try:
            return await body(_Harness(monkeypatch, db))
        finally:
            await asyncio.sleep(0.05)  # let an abandoned shadow task unwind before the pool closes
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
    assert gateway._group_seed_siblings_enabled() is (mode == "on")


# ------------------------------------------------------------------------------- the response


@pytest.mark.parametrize("ident", [RETAILER_SIG, BRAND_SIG, CK])
def test_shadow_serves_the_off_response_for_a_resolved_group(schema_engine, monkeypatch, lines, ident):
    _rows(schema_engine, retailer_checked=None)  # the cheaper retailer's price is stale

    async def body(h: _Harness):
        off, off_bt = await h.call(ident, flag=None)
        shadow, shadow_bt = await h.call(ident, flag="shadow")
        assert not off_bt.tasks and len(shadow_bt.tasks) == 1
        await shadow_bt()  # as Starlette runs it: after the response, in the request's task
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
        await cold_bt()
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
        await bt()
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
        await bt()
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
        await warm()  # one healthy shadow first: the key set is warm, so the budget below is the group read's

        async def hangs(*a, **k):
            await asyncio.sleep(5)

        monkeypatch.setattr(gateway, "_fetch_resolved_group", hangs)
        shadow, bt = await h.call(RETAILER_SIG, flag="shadow")
        started = time.perf_counter()
        await bt()
        waited = time.perf_counter() - started
        after, _ = await h.call(RETAILER_SIG, flag=None)
        return off, shadow, after, waited

    off, shadow, after, waited = _run(monkeypatch, body)
    assert waited < 0.4 + 0.3, f"the background callable outlived its budget: {waited:.2f}s"
    assert _bytes(shadow) == _bytes(off) and _bytes(after) == _bytes(off)
    healthy, timed_out = lines.parsed
    assert "error" not in healthy
    assert timed_out["error"] == "budget_exceeded" and timed_out["product_id"] == RETAILER_SIG
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
        await bt()
        waited = time.perf_counter() - started
        after, _ = await h.call(RETAILER_SIG, flag=None)
        monkeypatch.setattr(gateway, "_fetch_resolved_group", real_fetch)
        _, healthy_bt = await h.call(RETAILER_SIG, flag="shadow")
        await healthy_bt()
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
        await bt()  # raises nothing
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
        await bt()
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
        await bt()
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
        await warm()  # the resolved-key set is loaded once per TTL, off the request path
        sent: List[str] = []
        _count_statements(monkeypatch, h.db, sent)
        out = {}
        for name, flag, meta in (("off", None, None), ("shadow", "shadow", None),
                                 ("scoped_off", None, scoped), ("scoped_shadow", "shadow", scoped)):
            sent.clear()
            res, bt = await h.call(RETAILER_SIG, flag=flag, metadata=meta)
            assert not bt.tasks, name
            await bt()
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
        await bt()
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
        await bt()
        second, bt2 = await h.call(RETAILER_SIG, flag="shadow")
        assert not bt2.tasks, "a failed refresh backs off: no task, no query, until the retry window"
        return off, first, second

    off, first, second = _run(monkeypatch, body)
    assert _bytes(first) == _bytes(off) and _bytes(second) == _bytes(off)
    [line] = lines.parsed
    assert "UndefinedTable" in line["error"] and "content_key" not in line

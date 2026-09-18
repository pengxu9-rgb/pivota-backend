"""Market telemetry on this door's find_products_multi (services/market_telemetry.py).

The record must say what the door RESOLVED and BOUND, observed at the point of use -- so these
tests compare it against what recall was actually handed (the values the SQL received), never
against the record's own word. The Node half of this work (PIVOTA-Agent #2239) first shipped a
re-derived market and review found it wrong three ways; review of THIS PR found a bind site nobody
observed and a record that never left the process. These tests exist to stop all of that.

They read the EMITTED JSON line from stdout -- what Cloud Run actually stores -- not pytest's log
capture, which forces INFO through and so hid the fact that the first revision emitted nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

import httpx
import pytest

import routes.agent_shop_gateway as gateway
import services.external_seed_search as seed_search
from main import app
from models.catalog import PivotQueryResponse
from services import market_telemetry as mt
from services.agent_task_manager import AgentTaskManager


class FakeSeedDB:
    """Stands in for the database under ``fetch_external_seed_rows``. No ``transaction`` attribute,
    so recall takes its plain path; records the bind values each query was handed.

    ``with_merchant`` answers the merchant roster with one merchant, as prod always does. Without
    one the door returns an empty page before any lane or fallback runs (``if not has_merchants and
    not external_seed_wrappers``), which is not a state prod is ever in."""

    def __init__(self, with_merchant: bool = False) -> None:
        self.values: List[Dict[str, Any]] = []
        self.with_merchant = with_merchant

    async def fetch_all(self, query: str, values: Any = None) -> List[Any]:
        self.values.append(dict(values or {}))
        if self.with_merchant and "FROM merchant_onboarding" in str(query):
            return [{"merchant_id": "merch_1", "business_name": "Demo Merchant"}]
        return []

    async def fetch_one(self, query: str, values: Any = None) -> Dict[str, Any]:
        return {"total_count": 0}

    async def execute(self, query: str, values: Any = None) -> None:
        return None

    def markets_bound(self) -> List[str]:
        # What the SQL got: the :market bind, or "*" where the query had no partition at all.
        return [v.get("market", mt.UNPARTITIONED) for v in self.values if "status" in v]


def _patch_shared_db(monkeypatch: pytest.MonkeyPatch, db: FakeSeedDB) -> None:
    """The handler re-imports `database` locally (`from db.database import database`), so replacing
    the module attribute does not reach it. Patch the SHARED object's methods instead -- the same
    object every lane, including seed recall, queries through."""
    monkeypatch.setattr(gateway.database, "fetch_all", db.fetch_all)
    monkeypatch.setattr(gateway.database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(gateway.database, "execute", db.execute)


def _emitted(capsys: pytest.CaptureFixture) -> List[Dict[str, Any]]:
    """The multi.invoke.market events actually written to stdout, parsed as JSON."""
    out = capsys.readouterr().out
    events = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{") and '"event": "multi.invoke.market"' in line:
            events.append(json.loads(line))
    return events


# --- the requested side: read from the RAW request, before pydantic drops search.market ---------


def test_every_place_a_caller_can_name_a_market_is_recognised_in_a_fixed_order() -> None:
    cases = [
        ({"search": {"market": "SG"}}, {}, ("SG", "explicit_search")),
        ({"market": "SG"}, {}, ("SG", "explicit_payload")),
        ({"metadata": {"market": "SG"}}, {}, ("SG", "explicit_payload_metadata")),
        ({}, {"market": "SG"}, ("SG", "explicit_metadata")),
        ({}, {"locale": "en-SG"}, ("en-SG", "explicit_locale")),
        ({}, {}, (None, "defaulted")),
        ({"search": {"market": "SG"}}, {"market": "US"}, ("SG", "explicit_search")),
        # every adjacent pair in the order, so no two sources can swap unnoticed
        ({"search": {"market": "SG"}, "market": "JP"}, {}, ("SG", "explicit_search")),
        ({"market": "JP", "metadata": {"market": "KR"}}, {}, ("JP", "explicit_payload")),
        ({"metadata": {"market": "KR"}}, {"market": "US"}, ("KR", "explicit_payload_metadata")),
        ({}, {"market": "US", "locale": "en-SG"}, ("US", "explicit_metadata")),
    ]
    for payload, envelope, (requested, source) in cases:
        got = mt.describe_requested(payload, envelope)
        assert (got["market_requested"], got["market_source"]) == (requested, source), (payload, envelope)


def test_requested_is_verbatim_and_capped() -> None:
    assert mt.describe_requested({"search": {"market": "sg"}}, {})["market_requested"] == "sg"
    long = mt.describe_requested({"search": {"market": "X" * 500}}, {})["market_requested"]
    assert long == "X" * mt.MAX_REQUESTED_CHARS + "…"
    assert mt.describe_requested({"search": {"market": ""}}, {"market": "SG"})["market_source"] == "explicit_metadata"
    assert mt.describe_requested({"search": {"market": False}}, {"market": "SG"})["market_source"] == "explicit_metadata"
    assert mt.describe_requested(None, None)["market_source"] == "defaulted"


# --- observations -----------------------------------------------------------------------------


def test_observe_writes_only_into_a_request_that_carries_an_observation() -> None:
    store: Dict[str, Any] = {}
    meta = {mt.OBSERVATION_KEY: store}
    mt.observe_resolved(meta, "SG")
    mt.observe_bound(meta, None)
    mt.observe_bound(meta, "JP")
    assert store == {"market_resolved": "SG", "market_bound": ["*", "JP"]}
    plain = {"source": "x"}
    mt.observe_resolved(plain, "SG")
    mt.observe_bound(plain, None)
    assert plain == {"source": "x"}
    mt.observe_resolved(None, "SG")
    mt.observe_bound("not a dict", "SG")


def test_bound_list_is_capped() -> None:
    store: Dict[str, Any] = {}
    for _ in range(50):
        mt.observe_bound({mt.OBSERVATION_KEY: store}, None)
    assert len(store["market_bound"]) == mt.MAX_BOUND


def test_a_seed_bind_is_recorded_only_inside_an_open_sink() -> None:
    store: Dict[str, Any] = {}
    mt.record_seed_bind("SG")  # no sink open: a no-op, not a throw
    assert store == {}
    token = mt.open_seed_bind_sink({mt.OBSERVATION_KEY: store})
    try:
        mt.record_seed_bind("SG")
        mt.record_seed_bind("")  # empty = no partition
    finally:
        mt.close_seed_bind_sink(token)
    mt.record_seed_bind("JP")  # closed again
    assert store == {"market_bound": ["SG", "*"]}


def test_served_currency_mismatch_means_more_than_one_KNOWN_currency() -> None:
    s = mt.summarise_served_products
    assert s([{"currency": "SGD"}, {"currency": "USD"}])["served_currency_mismatch"] is True
    assert s([{"currency": "SGD"}, {"currency": "sgd"}]) == {"served_currencies": ["SGD"], "served_currency_mismatch": False}
    assert s([{"currency": "SGD"}, {"title": "unpriced"}])["served_currency_mismatch"] is False
    assert s([{"title": "unpriced"}])["served_currencies"] == ["unknown"]
    assert s([{"price_currency": " usd "}])["served_currencies"] == ["USD"]
    assert s(None) == {"served_currencies": [], "served_currency_mismatch": False}


def test_served_via_says_whether_this_request_s_own_lanes_ran() -> None:
    assert mt.served_via(dedup_cache_hit=True, dedup_inflight_joined=False, result={}) == "dedup_cache"
    assert mt.served_via(dedup_cache_hit=False, dedup_inflight_joined=True, result={}) == "dedup_inflight"
    assert mt.served_via(dedup_cache_hit=False, dedup_inflight_joined=False, result={"status": "pending"}) == "pending"
    assert mt.served_via(dedup_cache_hit=False, dedup_inflight_joined=False, result={"products": []}) == "fresh"


def test_the_observation_never_leaves_the_process() -> None:
    meta = {"source": "x", mt.OBSERVATION_KEY: {"market_resolved": "SG"}}
    assert mt.strip_for_forwarding(dict(meta)) == {"source": "x"}


def test_request_metadata_model_ignores_the_observation_key() -> None:
    model = gateway.RequestMetadata(**{"source": "public_api", mt.OBSERVATION_KEY: {"a": 1}})
    assert mt.OBSERVATION_KEY not in model.model_dump()


# --- fallback fields: "served by the fallback" is applied, not the served response's attempted ---


def test_fallback_applied_is_the_truthful_signal_and_implies_attempted() -> None:
    def record(envelope: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
        return mt.build_record(raw_payload={}, envelope_metadata=envelope, observation={}, result={"metadata": metadata})

    healthy = record({}, {})
    assert (healthy["upstream_fallback_hop"], healthy["upstream_fallback_applied"], healthy["upstream_fallback_attempted"]) == (0, False, False)
    # Review of this PR: when the fallback SUCCEEDS the served dict carries the second request's own
    # `upstream_fallback_attempted: false`, so reading that flag said "healthy". `applied` says true.
    served_by_fallback = record({}, {"upstream_fallback_attempted": False, "upstream_fallback": {"applied": True}})
    assert served_by_fallback["upstream_fallback_applied"] is True
    assert served_by_fallback["upstream_fallback_attempted"] is True
    tried_and_failed = record({}, {"upstream_fallback_attempted": True})
    assert (tried_and_failed["upstream_fallback_applied"], tried_and_failed["upstream_fallback_attempted"]) == (False, True)
    assert record({"upstream_fallback_hop": 1}, {})["upstream_fallback_hop"] == 1
    for junk in ("x", None, -3, {}):
        assert record({"upstream_fallback_hop": junk}, {})["upstream_fallback_hop"] >= 0


# --- emission: the record must actually leave the process ------------------------------------


def test_the_event_is_written_as_one_json_line_even_when_the_root_logger_is_at_WARNING(
    capsys: pytest.CaptureFixture,
) -> None:
    # Production's logging state, measured: root at WARNING with no handlers, so the gateway module
    # logger's INFO is dropped and `extra` is never rendered. The first revision logged through
    # exactly that path and emitted nothing in prod while every test passed.
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    root.setLevel(logging.WARNING)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    try:
        mt.emit({"market_requested": "SG", "market_source": "explicit_search", "market_bound": ["*"]})
    finally:
        root.setLevel(saved_level)
        for handler in saved_handlers:
            root.addHandler(handler)
    events = _emitted(capsys)
    assert events == [{"event": "multi.invoke.market", "market_requested": "SG",
                       "market_source": "explicit_search", "market_bound": ["*"]}]


def test_emit_never_raises_on_unserialisable_values(capsys: pytest.CaptureFixture) -> None:
    mt.emit({"odd": object(), "set": {1, 2}})
    assert len(_emitted(capsys)) == 1


# --- the lanes record what recall was HANDED --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope,expected",
    [({"source": "shopping_agent", "market": "SG"}, "SG"), ({"source": "shopping_agent"}, "US")],
)
async def test_pivot_lane_records_exactly_the_market_it_hands_recall(
    monkeypatch: pytest.MonkeyPatch, envelope: Dict[str, Any], expected: str,
) -> None:
    handed: Dict[str, Any] = {}

    class Handed(Exception):
        pass

    async def fake_search(req):
        handed["market"] = req.market
        raise Handed()

    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_ENABLED", True)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SHADOW_ENABLED", False)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST", {"shopping_agent"})
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_MAX_PAGE", 1)
    monkeypatch.setattr(gateway, "search_pivot_catalog", fake_search)

    store: Dict[str, Any] = {}
    payload = gateway.FindProductsMultiPayload(
        search=gateway.MultiSearchFilters(query="vitamin c", page=1, limit=10, in_stock_only=False),
        metadata=gateway.RequestMetadata(source="shopping_agent"),
    )
    with pytest.raises(Handed):
        await gateway._handle_find_products_multi(payload, {**envelope, mt.OBSERVATION_KEY: store}, gateway.BackgroundTasks())

    assert handed["market"] == expected
    assert store["market_resolved"] == handed["market"]


@pytest.mark.asyncio
async def test_the_pivot_lane_s_own_seed_fallback_bind_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review of this PR, finding 3: the pivot lane's external fallback (pivot_query_service) binds
    # `market=request.market` for most beauty pages, nothing observed it, and the record said "no
    # seed recall ran". Here the pivot lane calls the REAL fetch_external_seed_rows exactly as that
    # fallback does -- and the record must equal what the SQL received.
    db = FakeSeedDB()

    class Done(Exception):
        pass

    async def pivot_with_seed_fallback(req):
        await seed_search.fetch_external_seed_rows(database=db, market=req.market, query="gloss", limit=5,
                                                   only_unattached=False)
        raise Done()

    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_ENABLED", True)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SHADOW_ENABLED", False)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST", {"shopping_agent"})
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_MAX_PAGE", 1)
    monkeypatch.setattr(gateway, "search_pivot_catalog", pivot_with_seed_fallback)

    store: Dict[str, Any] = {}
    payload = gateway.FindProductsMultiPayload(
        search=gateway.MultiSearchFilters(query="metal serum gloss", page=1, limit=10, in_stock_only=False),
        metadata=gateway.RequestMetadata(source="shopping_agent"),
    )
    with pytest.raises(Done):
        await gateway._handle_find_products_multi(
            payload, {"source": "shopping_agent", "market": "SG", mt.OBSERVATION_KEY: store}, gateway.BackgroundTasks(),
        )

    assert db.markets_bound() == ["SG"], "premise: the SQL was partitioned on SG"
    assert store["market_bound"] == db.markets_bound()


@pytest.mark.asyncio
async def test_legacy_lane_records_the_partition_its_seed_recall_actually_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    # The REAL fetch_external_seed_rows runs against a fake database, so the record is compared with
    # the bind values the SQL received -- including stage B, which review found unpinned.
    db = FakeSeedDB()
    _patch_shared_db(monkeypatch, db)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_ENABLED", False)

    store: Dict[str, Any] = {}
    payload = gateway.FindProductsMultiPayload(
        search=gateway.MultiSearchFilters(query="fenty beauty gloss", page=1, limit=10, in_stock_only=False)
    )
    await gateway._handle_find_products_multi(
        payload, {"source": "creator-agent-ui", mt.OBSERVATION_KEY: store}, gateway.BackgroundTasks()
    )

    bound = db.markets_bound()
    assert bound, "premise: the legacy seed lane ran recall"
    assert store["market_bound"] == bound
    assert all(m == mt.UNPARTITIONED for m in bound), "premise: this lane binds no partition today"

    # The sink CLOSES when the handler returns: a bind made afterwards, outside any request, must
    # not be written into this request's record.
    recorded = list(store["market_bound"])
    await seed_search.fetch_external_seed_rows(database=FakeSeedDB(), market="JP", query="x", limit=1)
    assert store["market_bound"] == recorded


@pytest.mark.asyncio
async def test_the_choke_point_records_the_value_the_SQL_bound_not_the_argument_it_was_given() -> None:
    # Recall normalises the market before binding it. The record must be the bound value.
    store: Dict[str, Any] = {}
    db = FakeSeedDB()
    token = mt.open_seed_bind_sink({mt.OBSERVATION_KEY: store})
    try:
        await seed_search.fetch_external_seed_rows(database=db, market=" sg ", query="x", limit=1)
        await seed_search.fetch_external_seed_rows(database=db, market="   ", query="x", limit=1)
    finally:
        mt.close_seed_bind_sink(token)
    assert db.markets_bound() == ["SG", "*"], "premise: SQL bound SG, then no partition at all"
    assert store["market_bound"] == db.markets_bound()


@pytest.mark.asyncio
async def test_a_bind_outside_a_find_products_multi_request_is_not_recorded() -> None:
    db = FakeSeedDB()
    await seed_search.fetch_external_seed_rows(database=db, market="SG", query="x", limit=1)
    assert db.markets_bound() == ["SG"]  # the call happened; nothing was listening, nothing recorded


# --- the route: one emitted record per request, isolated, and it cannot fail the request ------


def _body(search_market: Any = None, envelope_market: Any = None, query: str = "test", source: Any = None) -> Dict[str, Any]:
    search: Dict[str, Any] = {"query": query, "page": 1, "limit": 10, "in_stock_only": False}
    if search_market is not None:
        search["market"] = search_market
    metadata: Dict[str, Any] = {}
    if envelope_market is not None:
        metadata["market"] = envelope_market
    if source is not None:
        metadata["source"] = source
    return {"operation": "find_products_multi", "payload": {"search": search}, "metadata": metadata}


@pytest.fixture
def queue_manager() -> None:
    gateway.agent_task_manager = AgentTaskManager(
        max_workers=1, max_queue_size=16, task_timeout_seconds=5.0,
        max_calls_per_session=100, max_duplicate_payloads=10,
    )


@pytest.mark.asyncio
async def test_the_route_emits_one_record_carrying_what_the_lane_observed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, queue_manager: None,
) -> None:
    seen: Dict[str, Any] = {}

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        seen["has_observation"] = isinstance(metadata.get(mt.OBSERVATION_KEY), dict)
        mt.observe_resolved(metadata, "SG")
        mt.observe_bound(metadata, None)
        return {"products": [{"currency": "SGD"}, {"currency": "USD"}], "metadata": {"query_source": "pivot_semantic_core_multi"}}

    monkeypatch.setattr(gateway, "_handle_find_products_multi", fake_handler)
    capsys.readouterr()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/agent/shop/v1/invoke", json=_body(search_market="SG"))

    assert resp.status_code == 200
    assert seen["has_observation"] is True
    events = _emitted(capsys)
    assert len(events) == 1
    r = events[0]
    assert (r["market_requested"], r["market_source"]) == ("SG", "explicit_search")
    assert r["market_resolved"] == "SG"
    assert r["market_bound"] == ["*"]
    assert r["lane"] == "pivot_semantic_core_multi"
    assert r["served_via"] == "fresh"
    assert r["served_currencies"] == ["SGD", "USD"] and r["served_currency_mismatch"] is True
    assert "market_resolved" not in resp.text and mt.OBSERVATION_KEY not in resp.text


@pytest.mark.asyncio
async def test_a_queued_request_keeps_its_own_observation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, queue_manager: None,
) -> None:
    # One worker: request 2 is started by the queue from inside request 1's task. The observation
    # rides in each request's own metadata dict, so each record carries its own market.
    # NOTE: requests run one at a time here, so this does NOT prove isolation under overlap -- a
    # single shared dict would pass it. The next test does that.
    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        await asyncio.sleep(0.05)
        mt.observe_resolved(metadata, payload.search.query.upper())
        return {"products": [], "metadata": {"query_source": "pivot_semantic_core_multi"}}

    monkeypatch.setattr(gateway, "_handle_find_products_multi", fake_handler)
    capsys.readouterr()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*[
            client.post("/agent/shop/v1/invoke", json=_body(envelope_market=code, query=code))
            for code in ("sg", "jp", "kr")
        ])

    assert all(r.status_code == 200 for r in responses)
    events = _emitted(capsys)
    assert len(events) == 3
    for record in events:
        assert record["market_resolved"] == record["market_requested"].upper(), record


@pytest.mark.asyncio
async def test_overlapping_requests_never_see_each_other_s_observation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    # Three requests genuinely in flight at once: each writes, then WAITS until all three have
    # written, and only then returns and logs. With one shared observation dict the last writer
    # wins and two records would carry the wrong market.
    gateway.agent_task_manager = AgentTaskManager(
        max_workers=3, max_queue_size=16, task_timeout_seconds=5.0,
        max_calls_per_session=100, max_duplicate_payloads=10,
    )
    written = 0
    all_written = asyncio.Event()

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        nonlocal written
        mt.observe_resolved(metadata, payload.search.query.upper())
        written += 1
        if written == 3:
            all_written.set()
        await asyncio.wait_for(all_written.wait(), timeout=4.0)
        return {"products": [], "metadata": {"query_source": "pivot_semantic_core_multi"}}

    monkeypatch.setattr(gateway, "_handle_find_products_multi", fake_handler)
    capsys.readouterr()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*[
            client.post("/agent/shop/v1/invoke", json=_body(envelope_market=code, query=code))
            for code in ("sg", "jp", "kr")
        ])

    assert all(r.status_code == 200 for r in responses)
    assert all_written.is_set(), "premise: all three were in flight together"
    events = _emitted(capsys)
    assert sorted(r["market_requested"] for r in events) == ["jp", "kr", "sg"]
    for record in events:
        assert record["market_resolved"] == record["market_requested"].upper(), record


@pytest.mark.asyncio
@pytest.mark.parametrize("flag,expected", [("cache", "dedup_cache"), ("inflight", "dedup_inflight")])
async def test_the_shopping_path_reports_a_deduplicated_request_as_such(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, flag: str, expected: str,
) -> None:
    # Review of this PR, R22/R23: the dedup flags were severed from served_via unnoticed, because
    # every route test took the queue path. This drives the prod-default shopping path.
    monkeypatch.setattr(gateway, "INVOKE_MULTI_BYPASS_QUEUE_SHOPPING", True)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_PAGE_REQUEST_DEDUP_ENABLED", True)
    monkeypatch.setattr(gateway, "_build_multi_page_request_dedup_key", lambda **kwargs: "k")
    cached = {"products": [{"currency": "USD"}], "metadata": {"query_source": "cache_multi_intent"}}
    if flag == "cache":
        monkeypatch.setattr(gateway, "_multi_page_request_cache_get", lambda key: dict(cached))
    else:
        monkeypatch.setattr(gateway, "_multi_page_request_cache_get", lambda key: None)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        future.set_result(dict(cached))
        monkeypatch.setitem(gateway._MULTI_SEARCH_PAGE_REQUEST_INFLIGHT, "k", future)

    async def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a deduplicated request must not run its own lanes")

    monkeypatch.setattr(gateway, "_handle_find_products_multi", must_not_run)
    capsys.readouterr()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/agent/shop/v1/invoke",
                                 json=_body(envelope_market="SG", source="shopping_agent"))

    assert resp.status_code == 200
    events = _emitted(capsys)
    assert len(events) == 1
    assert events[0]["served_via"] == expected
    # This request's lanes did not run, so it observed nothing -- and says so rather than guessing.
    assert events[0]["market_resolved"] is None and events[0]["market_bound"] is None


@pytest.mark.asyncio
async def test_a_telemetry_failure_never_changes_the_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, queue_manager: None,
) -> None:
    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        return {"products": [{"currency": "SGD"}], "metadata": {"query_source": "x"}}

    monkeypatch.setattr(gateway, "_handle_find_products_multi", fake_handler)
    capsys.readouterr()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        baseline = await client.post("/agent/shop/v1/invoke", json=_body(search_market="SG"))

        def boom(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(mt, "build_record", boom)
        broken = await client.post("/agent/shop/v1/invoke", json=_body(search_market="SG"))

    assert broken.status_code == baseline.status_code
    assert broken.json()["products"] == baseline.json()["products"]
    events = _emitted(capsys)
    assert events[-1].get("market_telemetry_error") == "boom"


@pytest.mark.asyncio
async def test_upstream_fallback_never_forwards_the_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: Dict[str, Any] = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> Dict[str, Any]:
            return {"products": [], "metadata": {}}

    class FakeClient:
        async def post(self, url: str, json: Any = None, **kwargs: Any) -> FakeResponse:
            sent["json"] = json
            return FakeResponse()

    async def fake_client() -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(gateway, "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL", "http://upstream.test")
    monkeypatch.setattr(gateway, "_get_shared_upstream_http_client", fake_client)
    payload = gateway.FindProductsMultiPayload(
        search=gateway.MultiSearchFilters(query="x", page=1, limit=10, in_stock_only=False)
    )
    store = {"market_resolved": "SG"}
    request_metadata = {"source": "shopping_agent", mt.OBSERVATION_KEY: store}
    await gateway._invoke_multi_upstream_fallback(payload, request_metadata, timeout_seconds=1.0, hop=0)

    assert "json" in sent, "premise: the fallback posted"
    assert mt.OBSERVATION_KEY not in sent["json"]["metadata"]
    assert request_metadata[mt.OBSERVATION_KEY] is store


# --- the MAIN route, end to end --------------------------------------------------------------


def _pivot_serving_rig(monkeypatch: pytest.MonkeyPatch, items: List[Any], handed: Dict[str, Any]) -> None:
    async def fake_search(req):
        handed["market"] = req.market
        return PivotQueryResponse(query="vitamin c", total=len(items), items=items)

    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_ENABLED", True)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SHADOW_ENABLED", False)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST", {"shopping_agent"})
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_MAX_PAGE", 1)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_INCLUDE_EXTERNAL", True)
    monkeypatch.setattr(gateway, "search_pivot_catalog", fake_search)
    # The fallback is CONFIGURED, as in prod.
    monkeypatch.setattr(gateway, "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL", "http://upstream.test")


_MAIN_ROUTE_BODY = {
    "operation": "find_products_multi",
    "payload": {"search": {"query": "vitamin c", "page": 1, "limit": 10, "in_stock_only": False, "market": "SG"}},
    "metadata": {"source": "shopping_agent", "market": "SG"},
}


@pytest.mark.asyncio
async def test_main_route_is_answered_by_its_own_lane_and_never_touches_the_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    # Everything real from the route inward -- invoke, the multi handler, the pivot lane -- except
    # recall itself. The fallback is configured but booby-trapped: the NEXT test proves the trap is
    # reachable, so passing here means the main route really did not use it.
    from test_agent_shop_gateway_pivot_multi import _sample_pivot_item

    handed: Dict[str, Any] = {}
    _pivot_serving_rig(monkeypatch, [_sample_pivot_item(sku_key="sku::1", variant_id="var_1", sku="SKU-1",
                                                        title="Vitamin C Serum")], handed)
    calls: List[Any] = []

    async def fallback_must_not_run(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        raise AssertionError("the main route relied on the upstream fallback")

    monkeypatch.setattr(gateway, "_invoke_multi_upstream_fallback", fallback_must_not_run)
    capsys.readouterr()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/agent/shop/v1/invoke", json=_MAIN_ROUTE_BODY)

    assert resp.status_code == 200
    assert calls == []
    data = resp.json()
    assert data["metadata"]["query_source"] == "pivot_semantic_core_multi"
    assert len(data["products"]) == 1, "premise: the main route served a real product"
    events = _emitted(capsys)
    assert len(events) == 1
    r = events[0]
    assert r["lane"] == "pivot_semantic_core_multi"
    assert (r["upstream_fallback_hop"], r["upstream_fallback_applied"], r["upstream_fallback_attempted"]) == (0, False, False)
    assert r["served_via"] == "fresh"
    assert (r["market_requested"], r["market_source"]) == ("SG", "explicit_search")
    assert r["market_resolved"] == handed["market"] == "SG"
    # Recall is faked in this rig and binds nothing, so nothing is recorded as bound. (The pivot
    # lane's real seed-fallback bind is pinned by its own test above.)
    assert r["market_bound"] is None


@pytest.mark.asyncio
async def test_when_the_main_route_finds_nothing_the_fallback_IS_reached_and_recorded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    # Review of this PR, finding 4: the booby-trap above was unreachable in its rig -- with the pivot
    # lane empty, the request 500'd on a missing table before ever reaching the fallback. This proves
    # the trap is live: an empty main route DOES reach the fallback, and the record says so.
    handed: Dict[str, Any] = {}
    _pivot_serving_rig(monkeypatch, [], handed)
    _patch_shared_db(monkeypatch, FakeSeedDB(with_merchant=True))
    calls: List[Any] = []

    async def fallback_serves(payload: Any, request_metadata: Any, **kwargs: Any) -> Dict[str, Any]:
        calls.append(1)
        return {"products": [{"currency": "USD", "title": "from upstream"}], "total": 1,
                "metadata": {"query_source": "upstream", "upstream_fallback": {"applied": True},
                             "upstream_fallback_attempted": False}}

    monkeypatch.setattr(gateway, "_invoke_multi_upstream_fallback", fallback_serves)
    capsys.readouterr()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/agent/shop/v1/invoke", json=_MAIN_ROUTE_BODY)

    assert resp.status_code == 200, resp.text[:300]
    assert calls, "the fallback was not reached -- the main-route booby-trap would be unreachable"
    r = _emitted(capsys)[-1]
    # Served by the fallback: the record must NOT read as a healthy main-route request, even though
    # the served dict carries the second request's own `upstream_fallback_attempted: false`.
    assert r["upstream_fallback_applied"] is True
    assert r["upstream_fallback_attempted"] is True


"""Market telemetry on this door's find_products_multi (services/market_telemetry.py).

The record must say what the door RESOLVED and BOUND, observed at the point of use -- so these
tests compare it against what recall was actually handed, never against the record's own word.
The Node half of this work (PIVOTA-Agent #2239) first shipped a re-derived market and review found
it wrong three ways; this is the regression those tests exist to stop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import httpx
import pytest

import routes.agent_shop_gateway as gateway
from main import app
from services import market_telemetry as mt
from models.catalog import PivotQueryResponse
from services.agent_task_manager import AgentTaskManager


# --- the requested side: read from the RAW request, before pydantic drops search.market ---------


def test_every_place_a_caller_can_name_a_market_is_recognised_in_a_fixed_order() -> None:
    cases = [
        ({"search": {"market": "SG"}}, {}, ("SG", "explicit_search")),
        ({"market": "SG"}, {}, ("SG", "explicit_payload")),
        ({"metadata": {"market": "SG"}}, {}, ("SG", "explicit_payload_metadata")),
        ({}, {"market": "SG"}, ("SG", "explicit_metadata")),
        ({}, {"locale": "en-SG"}, ("en-SG", "explicit_locale")),
        ({}, {}, (None, "defaulted")),
        # search first: it is what a caller means most specifically, even though THIS door drops it.
        ({"search": {"market": "SG"}}, {"market": "US"}, ("SG", "explicit_search")),
    ]
    for payload, envelope, (requested, source) in cases:
        got = mt.describe_requested(payload, envelope)
        assert (got["market_requested"], got["market_source"]) == (requested, source), (payload, envelope)


def test_requested_is_verbatim_and_capped() -> None:
    assert mt.describe_requested({"search": {"market": "sg"}}, {})["market_requested"] == "sg"
    long = mt.describe_requested({"search": {"market": "X" * 500}}, {})["market_requested"]
    assert long == "X" * mt.MAX_REQUESTED_CHARS + "…"
    # falsy raw values are not "named", exactly as they would not be truthy anywhere else.
    assert mt.describe_requested({"search": {"market": ""}}, {"market": "SG"})["market_source"] == "explicit_metadata"
    assert mt.describe_requested({"search": {"market": False}}, {"market": "SG"})["market_source"] == "explicit_metadata"
    assert mt.describe_requested(None, None)["market_source"] == "defaulted"


# --- observations: written at the point of use, into the request's own dict --------------------


def test_observe_writes_only_into_a_request_that_carries_an_observation() -> None:
    store: Dict[str, Any] = {}
    meta = {mt.OBSERVATION_KEY: store}
    mt.observe_resolved(meta, "SG")
    mt.observe_bound(meta, None)
    mt.observe_bound(meta, "JP")
    # None -- no partition -- is "*", distinguishable from "no recall ran" (market_bound absent).
    assert store == {"market_resolved": "SG", "market_bound": ["*", "JP"]}
    # A request with no observation is untouched, and nothing raises.
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
    # RequestMetadata(**request_metadata) runs inside the multi handler. An extra key must not
    # raise there, or telemetry would break the request it measures.
    model = gateway.RequestMetadata(**{"source": "public_api", mt.OBSERVATION_KEY: {"a": 1}})
    assert mt.OBSERVATION_KEY not in model.model_dump()


# --- the lanes record what recall was HANDED ---------------------------------------------------


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

    # Stop at the moment recall is called: that is the only moment this test is about, and
    # letting the request continue would fall through to lanes that need a database.
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
    # Not the record's word for it: it equals what recall received.
    assert store["market_resolved"] == handed["market"]


@pytest.mark.asyncio
async def test_legacy_lane_records_the_partition_its_seed_recall_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Any] = []

    async def fake_fetch_external_seed_rows(**kwargs):
        calls.append(kwargs.get("market", "<absent>"))
        return {"rows": [], "query_timeout": False, "query_ms": 1, "total_count": 0}

    async def fake_fetch_all(query: str, values=None):
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "fetch_external_seed_rows", fake_fetch_external_seed_rows)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_ENABLED", False)

    store: Dict[str, Any] = {}
    payload = gateway.FindProductsMultiPayload(
        search=gateway.MultiSearchFilters(query="fenty beauty gloss", page=1, limit=10, in_stock_only=False)
    )
    await gateway._handle_find_products_multi(
        payload, {"source": "creator-agent-ui", mt.OBSERVATION_KEY: store}, gateway.BackgroundTasks()
    )

    assert calls, "premise: the legacy seed lane ran"
    # One observation per recall call, each recording the partition that call was handed.
    assert store["market_bound"] == ["*" if c is None else c for c in calls]
    assert all(c is None for c in calls), "premise: this lane binds no partition today"


# --- the route: one record per request, isolated, and it cannot fail the request ---------------


def _records(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "multi.invoke.market"]


def _body(search_market: Any = None, envelope_market: Any = None, query: str = "test") -> Dict[str, Any]:
    search: Dict[str, Any] = {"query": query, "page": 1, "limit": 10, "in_stock_only": False}
    if search_market is not None:
        search["market"] = search_market
    metadata: Dict[str, Any] = {}
    if envelope_market is not None:
        metadata["market"] = envelope_market
    return {"operation": "find_products_multi", "payload": {"search": search}, "metadata": metadata}


@pytest.fixture
def queue_manager() -> None:
    gateway.agent_task_manager = AgentTaskManager(
        max_workers=1,  # ONE worker: the second request waits and is started from the first's task.
        max_queue_size=16,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=10,
    )


@pytest.mark.asyncio
async def test_the_route_emits_one_record_carrying_what_the_lane_observed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, queue_manager: None,
) -> None:
    seen: Dict[str, Any] = {}

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        seen["has_observation"] = isinstance(metadata.get(mt.OBSERVATION_KEY), dict)
        mt.observe_resolved(metadata, "SG")
        mt.observe_bound(metadata, None)
        return {"products": [{"currency": "SGD"}, {"currency": "USD"}], "metadata": {"query_source": "pivot_semantic_core_multi"}}

    monkeypatch.setattr(gateway, "_handle_find_products_multi", fake_handler)
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/agent/shop/v1/invoke", json=_body(search_market="SG"))

    assert resp.status_code == 200
    assert seen["has_observation"] is True
    records = _records(caplog)
    assert len(records) == 1
    r = records[0].__dict__
    assert (r["market_requested"], r["market_source"]) == ("SG", "explicit_search")
    assert r["market_resolved"] == "SG"
    assert r["market_bound"] == ["*"]
    assert r["lane"] == "pivot_semantic_core_multi"
    assert r["served_via"] == "fresh"
    assert r["served_currencies"] == ["SGD", "USD"] and r["served_currency_mismatch"] is True
    # Operational only: none of it is added to the response.
    assert "market_resolved" not in resp.text and mt.OBSERVATION_KEY not in resp.text


@pytest.mark.asyncio
async def test_a_queued_request_keeps_its_own_observation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, queue_manager: None,
) -> None:
    # The hazard a ContextVar would have: with ONE worker, request 2 is started by the queue from
    # inside request 1's task, and create_task copies request 1's context. The observation rides
    # in each request's own metadata dict instead, so each record must carry its own market.
    # NOTE: with one worker these requests run one at a time, so this does NOT prove isolation
    # under overlap -- a single shared dict would pass it. The next test does that.
    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        await asyncio.sleep(0.05)
        mt.observe_resolved(metadata, payload.search.query.upper())
        return {"products": [], "metadata": {"query_source": "pivot_semantic_core_multi"}}

    monkeypatch.setattr(gateway, "_handle_find_products_multi", fake_handler)
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*[
            client.post("/agent/shop/v1/invoke", json=_body(envelope_market=code, query=code))
            for code in ("sg", "jp", "kr")
        ])

    assert all(r.status_code == 200 for r in responses)
    records = [r.__dict__ for r in _records(caplog)]
    assert len(records) == 3
    for record in records:
        # Each record's resolved market is ITS OWN query -- never another request's.
        assert record["market_resolved"] == record["market_requested"].upper(), record


@pytest.mark.asyncio
async def test_overlapping_requests_never_see_each_other_s_observation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Three requests genuinely in flight at once: each writes its observation, then WAITS until all
    # three have written, and only then returns and logs. With one shared observation dict the last
    # writer wins and two of the three records would carry the wrong market -- which is what the
    # previous test, run one-at-a-time, could not see (review of this PR's own sweep: it survived).
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
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*[
            client.post("/agent/shop/v1/invoke", json=_body(envelope_market=code, query=code))
            for code in ("sg", "jp", "kr")
        ])

    assert all(r.status_code == 200 for r in responses)
    assert all_written.is_set(), "premise: all three were in flight together"
    records = [r.__dict__ for r in _records(caplog)]
    assert sorted(r["market_requested"] for r in records) == ["jp", "kr", "sg"]
    for record in records:
        assert record["market_resolved"] == record["market_requested"].upper(), record


@pytest.mark.asyncio
async def test_a_telemetry_failure_never_changes_the_response(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, queue_manager: None,
) -> None:
    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        return {"products": [{"currency": "SGD"}], "metadata": {"query_source": "x"}}

    monkeypatch.setattr(gateway, "_handle_find_products_multi", fake_handler)
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        baseline = await client.post("/agent/shop/v1/invoke", json=_body(search_market="SG"))

        def boom(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(mt, "build_record", boom)
        broken = await client.post("/agent/shop/v1/invoke", json=_body(search_market="SG"))

    assert broken.status_code == baseline.status_code
    assert broken.json()["products"] == baseline.json()["products"]
    last = _records(caplog)[-1].__dict__
    assert last.get("market_telemetry_error") == "boom"


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
    # And the caller's own dict is untouched -- only the forwarded copy is stripped.
    assert request_metadata[mt.OBSERVATION_KEY] is store


# --- the MAIN route, end to end: answered by this door's own lane, never by the fallback --------


@pytest.mark.asyncio
async def test_main_route_is_answered_by_its_own_lane_and_never_touches_the_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Everything real from the route inward -- invoke, the multi handler, the pivot lane -- except
    # recall itself. The fallback is CONFIGURED (as in prod) but booby-trapped: if the main route
    # leaned on it, this test fails. Telemetry must describe a request the main route answered.
    from test_agent_shop_gateway_pivot_multi import _sample_pivot_item

    handed: Dict[str, Any] = {}

    async def fake_search(req):
        handed["market"] = req.market
        return PivotQueryResponse(
            query="vitamin c",
            total=1,
            items=[_sample_pivot_item(sku_key="sku::1", variant_id="var_1", sku="SKU-1", title="Vitamin C Serum")],
        )

    async def fallback_must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the main route relied on the upstream fallback")

    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_ENABLED", True)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SHADOW_ENABLED", False)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST", {"shopping_agent"})
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_MAX_PAGE", 1)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_INCLUDE_EXTERNAL", True)
    monkeypatch.setattr(gateway, "search_pivot_catalog", fake_search)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL", "http://upstream.test")
    monkeypatch.setattr(gateway, "_invoke_multi_upstream_fallback", fallback_must_not_run)
    caplog.set_level(logging.INFO)

    body = {
        "operation": "find_products_multi",
        "payload": {"search": {"query": "vitamin c", "page": 1, "limit": 10, "in_stock_only": False, "market": "SG"}},
        "metadata": {"source": "shopping_agent", "market": "SG"},
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/agent/shop/v1/invoke", json=body)

    assert resp.status_code == 200
    data = resp.json()
    assert data["metadata"]["query_source"] == "pivot_semantic_core_multi"
    assert len(data["products"]) == 1, "premise: the main route served a real product"

    records = _records(caplog)
    assert len(records) == 1
    r = records[0].__dict__
    assert r["lane"] == "pivot_semantic_core_multi"
    assert r["upstream_fallback_attempted"] is False and r["upstream_fallback_hop"] == 0
    assert r["served_via"] == "fresh"
    # What the caller named, what the door resolved -- and that the resolution is what recall got.
    assert (r["market_requested"], r["market_source"]) == ("SG", "explicit_search")
    assert r["market_resolved"] == handed["market"] == "SG"
    # The pivot lane's canonical recall binds no market: nothing is recorded as bound.
    assert r["market_bound"] is None
    assert r["served_currencies"] == sorted({str(p.get("currency") or "unknown").upper() for p in data["products"]})


def test_a_fallback_hop_is_marked_so_a_request_is_counted_once() -> None:
    caller = mt.build_record(raw_payload={}, envelope_metadata={}, observation={}, result={"metadata": {}})
    hop = mt.build_record(raw_payload={}, envelope_metadata={"upstream_fallback_hop": 1}, observation={},
                          result={"metadata": {}})
    relied = mt.build_record(raw_payload={}, envelope_metadata={}, observation={},
                             result={"metadata": {"upstream_fallback_attempted": True}})
    assert (caller["upstream_fallback_hop"], caller["upstream_fallback_attempted"]) == (0, False)
    assert hop["upstream_fallback_hop"] == 1
    assert relied["upstream_fallback_attempted"] is True
    # Garbage in the hop field never raises and never goes negative.
    for junk in ("x", None, -3, {}):
        assert mt.build_record(raw_payload={}, envelope_metadata={"upstream_fallback_hop": junk},
                               observation={}, result={})["upstream_fallback_hop"] >= 0

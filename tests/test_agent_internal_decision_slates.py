"""POST /agent/internal/decision-slates: gateway-served slates into the decision ledger."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.agent_internal_decision_slates as module

KEY = "test-internal-key"
CK = "ck_" + "0123456789abcdef" * 2
URL = "/agent/internal/decision-slates"
HEADERS = {"X-Internal-Key": KEY}


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A fake store with the claim semantics of the real one: first claim of an id wins, later
    claims get the stored row back."""
    seen: Dict[str, Any] = {"claims": [], "candidates": [], "exposures": [], "decisions": {}}

    async def fake_resolve(rows: List[Dict[str, Any]]) -> None:
        for row in rows:
            if row.get("product_key") == "pk_known":
                row["content_key"] = CK

    async def fake_claim(**kwargs: Any) -> Optional[Dict[str, Any]]:
        seen["claims"].append(kwargs)
        stored = seen["decisions"].get(kwargs["decision_id"])
        if stored is None:
            seen["decisions"][kwargs["decision_id"]] = {
                "surface": kwargs["surface"], "agent_context": kwargs["agent_context"]}
            return None
        return stored

    async def fake_candidates(decision_id: str, rows: List[Dict[str, Any]]) -> None:
        seen["candidates"].append((decision_id, rows))

    async def fake_exposures(decision_id: str, rows: List[Dict[str, Any]]) -> None:
        seen["exposures"].append((decision_id, rows))

    monkeypatch.setattr(module, "resolve_card_identities", fake_resolve)
    monkeypatch.setattr(module, "claim_decision_event", fake_claim)
    monkeypatch.setattr(module, "record_decision_candidates", fake_candidates)
    monkeypatch.setattr(module, "record_exposure_events", fake_exposures)
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", KEY)
    monkeypatch.setenv("DECISION_SLATES_INGEST_ENABLED", "true")
    return seen


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def _slate(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "decision_id": "dec-gw-1",
        "surface": "gateway.find_products_multi",
        "channel": "mcp",
        "agent_id": "agent_abc",
        "query": "vitamin c serum",
        "market": "US",
        "request_id": "req-1",
        "primary_path": "local",
        "cards": [
            {"position": 0, "product_id": "p0", "merchant_id": "merch_1", "product_key": "pk_known",
             "offer_id": "of:catalog_offer:off_1", "platform": "shopify", "source": "beauty_direct"},
            {"position": 1, "product_id": "p1", "merchant_id": "merch_2", "product_key": "pk_unknown"},
        ],
    }
    body.update(overrides)
    return body


def test_is_off_unless_enabled(client: TestClient, calls: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECISION_SLATES_INGEST_ENABLED")
    assert client.post(URL, json=_slate(), headers=HEADERS).status_code == 404
    monkeypatch.setenv("DECISION_SLATES_INGEST_ENABLED", "false")
    assert client.post(URL, json=_slate(), headers=HEADERS).status_code == 404
    assert calls["claims"] == []


def test_requires_the_internal_key(client: TestClient, calls: Dict[str, Any]) -> None:
    assert client.post(URL, json=_slate()).status_code == 403
    assert client.post(URL, json=_slate(), headers={"X-Internal-Key": "wrong"}).status_code == 403
    # non-ASCII must be a 403, not a 500 (#1883 class)
    assert client.post(URL, json=_slate(), headers={"X-Internal-Key": "kéy".encode("latin-1")}).status_code == 403
    assert calls["claims"] == []


def test_refuses_when_the_key_is_not_configured(
    client: TestClient, calls: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY")
    assert client.post(URL, json=_slate(), headers=HEADERS).status_code == 500
    assert calls["claims"] == []


def test_records_the_slate_through_the_store(client: TestClient, calls: Dict[str, Any]) -> None:
    response = client.post(URL, json=_slate(), headers=HEADERS)

    assert response.status_code == 202
    assert response.json() == {
        "decision_id": "dec-gw-1", "replayed": False, "cards": 2, "content_key_resolved": 1, "offer_id_kept": 1}

    claim = calls["claims"][0]
    assert claim["decision_id"] == "dec-gw-1"
    assert claim["surface"] == "gateway.find_products_multi"
    assert claim["channel"] == "mcp"
    context = dict(claim["agent_context"])
    assert len(context.pop("slate_fingerprint")) == 64
    assert context == {
        "agent_id": "agent_abc", "agent_id_source": "gateway_relay", "query": "vitamin c serum",
        "market": "US", "request_id": "req-1", "primary_path": "local", "result_count": 2,
    }
    decision_id, exposures = calls["exposures"][0]
    assert decision_id == "dec-gw-1"
    assert [r["content_key"] for r in exposures] == [CK, None]
    assert [r["position"] for r in exposures] == [0, 1]
    assert exposures[0]["offer_id"] == "of:catalog_offer:off_1"
    assert exposures[1]["eligibility_flags"]["product_key"] == "pk_unknown"
    assert calls["candidates"][0][1] is exposures  # one row set, two tables


def test_decision_id_is_required(client: TestClient, calls: Dict[str, Any]) -> None:
    body = _slate()
    del body["decision_id"]
    assert client.post(URL, json=body, headers=HEADERS).status_code == 422
    assert calls["claims"] == []


def test_an_identical_replay_is_a_no_op_under_the_same_row_ids(client: TestClient, calls: Dict[str, Any]) -> None:
    first = client.post(URL, json=_slate(), headers=HEADERS)
    second = client.post(URL, json=_slate(), headers=HEADERS)

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["replayed"] is True
    rows_1, rows_2 = calls["exposures"][0][1], calls["exposures"][1][1]
    assert [r["exposure_id"] for r in rows_1] == [r["exposure_id"] for r in rows_2]
    assert [r["candidate_id"] for r in rows_1] == [r["candidate_id"] for r in rows_2]
    assert len({r["exposure_id"] for r in rows_1} | {r["candidate_id"] for r in rows_1}) == 4


@pytest.mark.parametrize(
    "second",
    [
        {"agent_id": "agent_other"},
        {"surface": "gateway.other_surface"},
        {"cards": [{"position": i, "product_key": f"other{i}"} for i in range(4)]},
    ],
)
def test_a_different_slate_under_a_used_decision_id_is_a_conflict(
    client: TestClient, calls: Dict[str, Any], second: Dict[str, Any]
) -> None:
    assert client.post(URL, json=_slate(), headers=HEADERS).status_code == 202
    response = client.post(URL, json=_slate(**second), headers=HEADERS)
    assert response.status_code == 409
    assert len(calls["exposures"]) == 1  # the second slate wrote nothing


def test_an_id_another_writer_owns_is_a_conflict(client: TestClient, calls: Dict[str, Any]) -> None:
    # agent_v2 search mints its own decision rows; they carry no slate fingerprint.
    calls["decisions"]["dec-v2"] = {"surface": "agent_v2.products.search", "agent_context": {"agent_id": "agent_abc"}}
    assert client.post(URL, json=_slate(decision_id="dec-v2"), headers=HEADERS).status_code == 409
    assert calls["exposures"] == []


def test_row_ids_differ_across_decisions(client: TestClient, calls: Dict[str, Any]) -> None:
    client.post(URL, json=_slate(decision_id="dec-a"), headers=HEADERS)
    client.post(URL, json=_slate(decision_id="dec-b"), headers=HEADERS)
    a, b = calls["exposures"][0][1], calls["exposures"][1][1]
    assert not {r["exposure_id"] for r in a} & {r["exposure_id"] for r in b}
    assert not {r["candidate_id"] for r in a} & {r["candidate_id"] for r in b}


def test_duplicate_positions_are_refused(client: TestClient, calls: Dict[str, Any]) -> None:
    cards = [{"position": 0, "product_id": "p0"}, {"position": 0, "product_id": "p1"}]
    assert client.post(URL, json=_slate(cards=cards), headers=HEADERS).status_code == 422
    assert calls["claims"] == []


def test_a_card_without_a_position_puts_every_card_on_its_index(client: TestClient, calls: Dict[str, Any]) -> None:
    cards = [{"position": 1, "product_id": "p0"}, {"product_id": "p1"}]  # explicit 1 + fallback 1 collided before
    assert client.post(URL, json=_slate(cards=cards), headers=HEADERS).status_code == 202
    rows = calls["exposures"][0][1]
    assert [r["position"] for r in rows] == [0, 1]
    assert len({r["exposure_id"] for r in rows}) == 2


def test_an_empty_slate_records_the_decision_only(client: TestClient, calls: Dict[str, Any]) -> None:
    assert client.post(URL, json=_slate(cards=[]), headers=HEADERS).status_code == 202
    assert len(calls["claims"]) == 1
    assert calls["exposures"] == [] and calls["candidates"] == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"decision_id": "d" * 65},  # agent_decision_events.decision_id is VARCHAR(64)
        {"cards": [{"product_id": f"p{i}"} for i in range(module.MAX_CARDS + 1)]},
        {"surface": ""},
    ],
)
def test_rejects_oversized_or_malformed_slates(
    client: TestClient, calls: Dict[str, Any], overrides: Dict[str, Any]
) -> None:
    assert client.post(URL, json=_slate(**overrides), headers=HEADERS).status_code == 422
    assert calls["claims"] == []

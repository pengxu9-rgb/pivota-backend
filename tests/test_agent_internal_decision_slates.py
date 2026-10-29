"""POST /agent/internal/decision-slates: gateway-served slates into the decision ledger."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.agent_internal_decision_slates as module

KEY = "test-internal-key"
CK = "ck_" + "0123456789abcdef" * 2


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    seen: Dict[str, List[Any]] = {"decision": [], "candidates": [], "exposures": [], "resolve": []}

    async def fake_resolve(rows: List[Dict[str, Any]]) -> None:
        seen["resolve"].append([dict(r) for r in rows])
        for row in rows:
            if row.get("product_key") == "pk_known":
                row["content_key"] = CK

    async def fake_decision(**kwargs: Any) -> str:
        seen["decision"].append(kwargs)
        return kwargs["decision_id"]

    async def fake_candidates(decision_id: str, rows: List[Dict[str, Any]]) -> None:
        seen["candidates"].append((decision_id, rows))

    async def fake_exposures(decision_id: str, rows: List[Dict[str, Any]]) -> None:
        seen["exposures"].append((decision_id, rows))

    monkeypatch.setattr(module, "resolve_card_identities", fake_resolve)
    monkeypatch.setattr(module, "record_decision_event", fake_decision)
    monkeypatch.setattr(module, "record_decision_candidates", fake_candidates)
    monkeypatch.setattr(module, "record_exposure_events", fake_exposures)
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", KEY)
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


def test_requires_the_internal_key(client: TestClient, calls: Dict[str, List[Any]]) -> None:
    assert client.post("/agent/internal/decision-slates", json=_slate()).status_code == 403
    assert client.post(
        "/agent/internal/decision-slates", json=_slate(), headers={"X-Internal-Key": "wrong"}
    ).status_code == 403
    assert client.post(
        "/agent/internal/decision-slates", json=_slate(), headers={"X-Internal-Key": "kéy".encode("latin-1")}
    ).status_code == 403  # non-ASCII must not 500
    assert calls["decision"] == []


def test_refuses_when_the_key_is_not_configured(
    client: TestClient, calls: Dict[str, List[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY")
    response = client.post("/agent/internal/decision-slates", json=_slate(), headers={"X-Internal-Key": KEY})
    assert response.status_code == 500
    assert calls["decision"] == []


def test_records_the_slate_through_the_store(client: TestClient, calls: Dict[str, List[Any]]) -> None:
    response = client.post("/agent/internal/decision-slates", json=_slate(), headers={"X-Internal-Key": KEY})

    assert response.status_code == 202
    assert response.json() == {"decision_id": "dec-gw-1", "cards": 2, "content_key_resolved": 1, "offer_id_kept": 1}

    decision = calls["decision"][0]
    assert decision["decision_id"] == "dec-gw-1"
    assert decision["surface"] == "gateway.find_products_multi"
    assert decision["channel"] == "mcp"
    assert decision["agent_context"] == {
        "agent_id": "agent_abc", "query": "vitamin c serum", "market": "US",
        "request_id": "req-1", "primary_path": "local", "result_count": 2,
    }
    decision_id, exposures = calls["exposures"][0]
    assert decision_id == "dec-gw-1"
    assert [r["content_key"] for r in exposures] == [CK, None]
    assert [r["position"] for r in exposures] == [0, 1]
    assert exposures[0]["offer_id"] == "of:catalog_offer:off_1"
    assert exposures[1]["eligibility_flags"]["product_key"] == "pk_unknown"
    assert calls["candidates"][0][1] is exposures  # one row set, two tables


def test_a_retried_slate_reuses_its_row_ids(client: TestClient, calls: Dict[str, List[Any]]) -> None:
    for _ in range(2):
        client.post("/agent/internal/decision-slates", json=_slate(), headers={"X-Internal-Key": KEY})
    first, second = calls["exposures"][0][1], calls["exposures"][1][1]
    assert [r["exposure_id"] for r in first] == [r["exposure_id"] for r in second]
    assert [r["candidate_id"] for r in first] == [r["candidate_id"] for r in second]
    assert len({r["exposure_id"] for r in first} | {r["candidate_id"] for r in first}) == 4


def test_mints_a_decision_id_when_none_is_sent(client: TestClient, calls: Dict[str, List[Any]]) -> None:
    body = _slate()
    del body["decision_id"]
    response = client.post("/agent/internal/decision-slates", json=body, headers={"X-Internal-Key": KEY})
    assert response.status_code == 202
    assert len(response.json()["decision_id"]) == 36


def test_an_empty_slate_records_the_decision_only(client: TestClient, calls: Dict[str, List[Any]]) -> None:
    response = client.post(
        "/agent/internal/decision-slates", json=_slate(cards=[]), headers={"X-Internal-Key": KEY}
    )
    assert response.status_code == 202
    assert len(calls["decision"]) == 1
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
    client: TestClient, calls: Dict[str, List[Any]], overrides: Dict[str, Any]
) -> None:
    response = client.post(
        "/agent/internal/decision-slates", json=_slate(**overrides), headers={"X-Internal-Key": KEY}
    )
    assert response.status_code == 422
    assert calls["decision"] == []

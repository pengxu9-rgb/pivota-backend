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
    seen: Dict[str, Any] = {"claims": [], "candidates": [], "exposures": [], "decisions": {}, "lose": 0}

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

    async def fake_write(decision_id: str, rows: List[Dict[str, Any]]) -> int:
        if not rows:  # the real store writes nothing for an empty slate
            return 0
        if isinstance(seen["lose"], Exception):
            raise seen["lose"]
        seen["candidates"].append((decision_id, rows))
        seen["exposures"].append((decision_id, rows))
        return int(seen["lose"])

    monkeypatch.setattr(module, "resolve_card_identities", fake_resolve)
    monkeypatch.setattr(module, "claim_decision_event", fake_claim)
    monkeypatch.setattr(module, "write_slate_rows", fake_write)
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
        "decision_id": "gw:dec-1",
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
        "decision_id": "gw:dec-1", "replayed": False, "cards": 2, "rows_lost": 0,
        "content_key_resolved": 1, "offer_id_kept": 1}

    claim = calls["claims"][0]
    assert claim["decision_id"] == "gw:dec-1"
    assert claim["surface"] == "gateway.find_products_multi"
    assert claim["channel"] == "mcp"
    context = dict(claim["agent_context"])
    assert len(context.pop("slate_fingerprint")) == 64
    assert context == {
        "agent_id": "agent_abc", "agent_id_source": "gateway_relay", "query": "vitamin c serum",
        "market": "US", "request_id": "req-1", "primary_path": "local", "result_count": 2,
    }
    decision_id, exposures = calls["exposures"][0]
    assert decision_id == "gw:dec-1"
    assert [r["content_key"] for r in exposures] == [CK, None]
    assert [r["position"] for r in exposures] == [0, 1]
    assert exposures[0]["offer_id"] == "of:catalog_offer:off_1"
    assert exposures[1]["eligibility_flags"]["product_key"] == "pk_unknown"
    assert calls["candidates"][0][1] is exposures  # one row set, written to both tables before the answer


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


@pytest.mark.parametrize("decision_id", ["3f2c7a9e-1b4d-4c8e-9a21-6f0e5d4c3b2a", "v2:abc", "gw:", "gw:has space", "gw:" + "x" * 62])
def test_only_gateway_prefixed_ids_are_accepted(client: TestClient, calls: Dict[str, Any], decision_id: str) -> None:
    # A bare uuid4 is what agent_v2 mints for its own (queued) decision rows; accepting one here
    # could let a gateway claim land first and collect v2's rows.
    assert client.post(URL, json=_slate(decision_id=decision_id), headers=HEADERS).status_code == 422
    assert calls["claims"] == []


def test_a_gateway_id_whose_row_carries_no_fingerprint_is_a_conflict(client: TestClient, calls: Dict[str, Any]) -> None:
    calls["decisions"]["gw:legacy"] = {"surface": "gateway.find_products_multi", "agent_context": {"agent_id": "agent_abc"}}
    assert client.post(URL, json=_slate(decision_id="gw:legacy"), headers=HEADERS).status_code == 409
    assert calls["exposures"] == []


def test_the_flag_is_checked_before_the_key(client: TestClient, calls: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    # Off means invisible: the same 404 whatever key is sent, and even with no key configured.
    monkeypatch.delenv("DECISION_SLATES_INGEST_ENABLED")
    for headers in ({}, {"X-Internal-Key": "wrong"}, HEADERS):
        response = client.post(URL, json=_slate(), headers=headers)
        assert (response.status_code, response.json()["detail"]["error"]) == (404, "NOT_ENABLED")
    monkeypatch.delenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY")
    assert client.post(URL, json=_slate(), headers=HEADERS).status_code == 404


def test_a_replay_with_reordered_cards_is_the_same_slate(client: TestClient, calls: Dict[str, Any]) -> None:
    cards = [{"position": 0, "product_id": "p0", "product_key": "pk_known"},
             {"position": 1, "product_id": "p1"}, {"position": 2, "product_id": "p2"}]
    assert client.post(URL, json=_slate(cards=cards), headers=HEADERS).status_code == 202
    reordered = [cards[2], cards[0], cards[1]]
    response = client.post(URL, json=_slate(cards=reordered), headers=HEADERS)
    assert response.status_code == 200 and response.json()["replayed"] is True


def test_an_omitted_field_and_an_explicit_null_are_the_same_slate(client: TestClient, calls: Dict[str, Any]) -> None:
    sparse = [{"position": 0, "product_id": "p0"}]
    explicit = [{"position": 0, "product_id": "p0", "merchant_id": None, "product_key": None, "offer_id": None}]
    assert client.post(URL, json=_slate(cards=sparse), headers=HEADERS).status_code == 202
    assert client.post(URL, json=_slate(cards=explicit), headers=HEADERS).status_code == 200


def test_row_ids_differ_across_decisions(client: TestClient, calls: Dict[str, Any]) -> None:
    client.post(URL, json=_slate(decision_id="gw:dec-a"), headers=HEADERS)
    client.post(URL, json=_slate(decision_id="gw:dec-b"), headers=HEADERS)
    a, b = calls["exposures"][0][1], calls["exposures"][1][1]
    assert not {r["exposure_id"] for r in a} & {r["exposure_id"] for r in b}
    assert not {r["candidate_id"] for r in a} & {r["candidate_id"] for r in b}


def test_duplicate_positions_are_refused(client: TestClient, calls: Dict[str, Any]) -> None:
    cards = [{"position": 0, "product_id": "p0"}, {"position": 0, "product_id": "p1"}]
    assert client.post(URL, json=_slate(cards=cards), headers=HEADERS).status_code == 422
    assert calls["claims"] == []


def test_mixed_explicit_and_missing_positions_are_refused(client: TestClient, calls: Dict[str, Any]) -> None:
    cards = [{"position": 1, "product_id": "p0"}, {"product_id": "p1"}]
    response = client.post(URL, json=_slate(cards=cards), headers=HEADERS)
    assert (response.status_code, response.json()["detail"]["error"]) == (422, "MIXED_POSITIONS")
    assert calls["claims"] == []


def test_cards_without_positions_take_their_list_index(client: TestClient, calls: Dict[str, Any]) -> None:
    cards = [{"product_id": "p0"}, {"product_id": "p1"}]
    assert client.post(URL, json=_slate(cards=cards), headers=HEADERS).status_code == 202
    assert [r["position"] for r in calls["exposures"][0][1]] == [0, 1]


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


# --- adversarial review r3 -----------------------------------------------------------------------

@pytest.mark.parametrize(
    "second",
    [
        {"query": "something else"},
        {"market": "JP"},
        {"merchant_id": "m9"},
        {"channel": "acp"},
        {"protocol": "ucp_session"},
        {"primary_path": "upstream"},
        {"cards": [{"position": 0, "product_id": "p0", "merchant_id": "merch_1", "product_key": "pk_known",
                    "offer_id": "of:catalog_offer:off_1", "platform": "shopify", "source": "beauty_direct",
                    "in_stock": False},
                   {"position": 1, "product_id": "p1", "merchant_id": "merch_2", "product_key": "pk_unknown"}]},
    ],
)
def test_any_difference_in_the_request_is_a_conflict_not_a_replay(
    client: TestClient, calls: Dict[str, Any], second: Dict[str, Any]
) -> None:
    assert client.post(URL, json=_slate(), headers=HEADERS).status_code == 202
    assert client.post(URL, json=_slate(**second), headers=HEADERS).status_code == 409
    assert len(calls["exposures"]) == 1


def test_two_empty_searches_under_one_id_are_a_conflict(client: TestClient, calls: Dict[str, Any]) -> None:
    assert client.post(URL, json=_slate(cards=[], query="q1"), headers=HEADERS).status_code == 202
    assert client.post(URL, json=_slate(cards=[], query="q2"), headers=HEADERS).status_code == 409


def test_a_database_failure_is_a_503_and_the_resend_fills_the_slate(client: TestClient, calls: Dict[str, Any]) -> None:
    calls["lose"] = ConnectionError("pool checkout timed out")
    response = client.post(URL, json=_slate(), headers=HEADERS)
    assert (response.status_code, response.json()["detail"]["error"]) == (503, "LEDGER_UNAVAILABLE")
    assert calls["exposures"] == []

    calls["lose"] = 0
    resend = client.post(URL, json=_slate(), headers=HEADERS)
    assert resend.status_code == 200 and resend.json()["replayed"] is True
    assert len(calls["exposures"][0][1]) == 2


def test_rows_lost_to_row_level_errors_are_reported(client: TestClient, calls: Dict[str, Any]) -> None:
    calls["lose"] = 1
    response = client.post(URL, json=_slate(), headers=HEADERS)
    assert response.status_code == 202 and response.json()["rows_lost"] == 1


def test_a_failing_claim_is_a_503_and_an_unstorable_value_a_422(
    client: TestClient, calls: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncpg.exceptions as pg

    async def down(**kwargs: Any) -> None:
        raise ConnectionError("db down")

    async def unstorable(**kwargs: Any) -> None:
        raise pg.UntranslatableCharacterError("unsupported Unicode escape sequence")

    monkeypatch.setattr(module, "claim_decision_event", down)
    assert client.post(URL, json=_slate(), headers=HEADERS).status_code == 503
    monkeypatch.setattr(module, "claim_decision_event", unstorable)
    assert client.post(URL, json=_slate(), headers=HEADERS).status_code == 422
    assert calls["exposures"] == []


def test_nul_characters_are_stripped_before_they_reach_postgres(client: TestClient, calls: Dict[str, Any]) -> None:
    cards = [{"position": 0, "product_id": "p\u00000", "product_key": "pk\u0000x"}]
    body = _slate(query="a\u0000b", surface="gateway.\u0000fpm", agent_id="ag\u0000ent", cards=cards)
    assert client.post(URL, json=body, headers=HEADERS).status_code == 202
    claim = calls["claims"][0]
    assert (claim["surface"], claim["agent_context"]["query"], claim["agent_context"]["agent_id"]) == (
        "gateway.fpm", "ab", "agent")
    row = calls["exposures"][0][1][0]
    assert row["product_key"] == "pkx" and row["eligibility_flags"]["product_id"] == "p0"


def test_numeric_ids_and_the_catalog_offer_id_spelling_are_accepted(client: TestClient, calls: Dict[str, Any]) -> None:
    cards = [{"position": 0, "product_id": 123, "merchant_id": 456, "catalog_offer_id": "of:catalog_offer:off_9"}]
    response = client.post(URL, json=_slate(cards=cards), headers=HEADERS)
    assert response.status_code == 202
    row = calls["exposures"][0][1][0]
    assert row["eligibility_flags"]["product_id"] == "123" and row["eligibility_flags"]["merchant_id"] == "456"
    assert row["offer_id"] == "of:catalog_offer:off_9"


def test_offer_id_kept_counts_only_ids_the_store_will_write(client: TestClient, calls: Dict[str, Any]) -> None:
    cards = [{"position": 0, "product_id": "p0", "offer_id": "o" * 300},
             {"position": 1, "product_id": "p1", "offer_id": "of:catalog_offer:off_1"}]
    assert client.post(URL, json=_slate(cards=cards), headers=HEADERS).json()["offer_id_kept"] == 1



# --- controller review of #2673 ------------------------------------------------------------------

def test_a_resend_with_a_new_request_id_is_the_same_slate(client: TestClient, calls: Dict[str, Any]) -> None:
    # the gateway mints a request id per HTTP attempt; a resend after a 503 carries a new one
    assert client.post(URL, json=_slate(request_id="attempt-1"), headers=HEADERS).status_code == 202
    response = client.post(URL, json=_slate(request_id="attempt-2"), headers=HEADERS)
    assert response.status_code == 200 and response.json()["replayed"] is True
    assert calls["claims"][0]["agent_context"]["request_id"] == "attempt-1"


def test_a_nul_in_the_decision_id_is_refused_not_stripped(client: TestClient, calls: Dict[str, Any]) -> None:
    assert client.post(URL, json=_slate(decision_id="gw:x\u0000y"), headers=HEADERS).status_code == 422
    assert calls["claims"] == []


def test_a_numeric_slate_merchant_id_is_coerced(client: TestClient, calls: Dict[str, Any]) -> None:
    assert client.post(URL, json=_slate(merchant_id=789), headers=HEADERS).status_code == 202
    assert calls["claims"][0]["merchant_id"] == "789"

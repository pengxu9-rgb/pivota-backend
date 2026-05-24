from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

import services.gmv_attribution_service as service


pytestmark = pytest.mark.asyncio


class _FakeGmvAttributionDatabase:
    def __init__(self) -> None:
        self.edges: dict[str, dict[str, Any]] = {}

    def add_edge(self, edge_id: str) -> None:
        self.edges[edge_id] = {
            "edge_id": edge_id,
            "gmv_channel": None,
            "third_party_platform": None,
            "third_party_platform_fee_pct": None,
        }

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = " ".join(str(query).split()).lower()
        params = dict(values or {})
        if not sql.startswith("update commerce_attribution_edges"):
            raise AssertionError(f"Unhandled fetch_one query: {query}")

        edge = self.edges.get(params["edge_id"])
        if edge is None:
            return None

        edge.update(
            {
                "gmv_channel": params["gmv_channel"],
                "third_party_platform": params["third_party_platform"],
                "third_party_platform_fee_pct": params["third_party_platform_fee_pct"],
            }
        )
        return {"edge_id": edge["edge_id"]}


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeGmvAttributionDatabase:
    db = _FakeGmvAttributionDatabase()
    monkeypatch.setattr(service, "database", db)
    return db


async def test_record_classification_personal(fake_db: _FakeGmvAttributionDatabase) -> None:
    fake_db.add_edge("edge_personal")

    await service.record_classification(
        edge_id="edge_personal",
        gmv_channel="personal_agent",
        third_party_platform=None,
        third_party_platform_fee_pct=None,
    )

    assert fake_db.edges["edge_personal"]["gmv_channel"] == "personal_agent"
    assert fake_db.edges["edge_personal"]["third_party_platform"] is None
    assert fake_db.edges["edge_personal"]["third_party_platform_fee_pct"] is None


async def test_record_classification_third_party(fake_db: _FakeGmvAttributionDatabase) -> None:
    fake_db.add_edge("edge_third_party")

    await service.record_classification(
        edge_id="edge_third_party",
        gmv_channel="third_party_agent",
        third_party_platform="openai",
        third_party_platform_fee_pct=Decimal("0.65"),
    )

    assert fake_db.edges["edge_third_party"]["gmv_channel"] == "third_party_agent"
    assert fake_db.edges["edge_third_party"]["third_party_platform"] == "openai"
    assert fake_db.edges["edge_third_party"]["third_party_platform_fee_pct"] == Decimal("0.65")


async def test_record_classification_rejects_personal_with_platform(
    fake_db: _FakeGmvAttributionDatabase,
) -> None:
    fake_db.add_edge("edge_invalid_personal")

    with pytest.raises(ValueError, match="personal_agent"):
        await service.record_classification(
            edge_id="edge_invalid_personal",
            gmv_channel="personal_agent",
            third_party_platform="openai",
            third_party_platform_fee_pct=Decimal("0.65"),
        )


async def test_record_classification_rejects_third_party_without_platform(
    fake_db: _FakeGmvAttributionDatabase,
) -> None:
    fake_db.add_edge("edge_invalid_third_party")

    with pytest.raises(ValueError, match="third_party_platform"):
        await service.record_classification(
            edge_id="edge_invalid_third_party",
            gmv_channel="third_party_agent",
            third_party_platform=None,
            third_party_platform_fee_pct=Decimal("0.65"),
        )


async def test_record_classification_rejects_third_party_with_invalid_fee(
    fake_db: _FakeGmvAttributionDatabase,
) -> None:
    fake_db.add_edge("edge_invalid_fee")

    with pytest.raises(ValueError, match="between 0 and 1"):
        await service.record_classification(
            edge_id="edge_invalid_fee",
            gmv_channel="third_party_agent",
            third_party_platform="openai",
            third_party_platform_fee_pct=Decimal("1.01"),
        )


async def test_record_classification_unknown_edge_raises(
    fake_db: _FakeGmvAttributionDatabase,
) -> None:
    with pytest.raises(ValueError, match="Attribution edge not found"):
        await service.record_classification(
            edge_id="missing_edge",
            gmv_channel="personal_agent",
            third_party_platform=None,
            third_party_platform_fee_pct=None,
        )

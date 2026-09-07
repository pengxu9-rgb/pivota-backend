"""Tests for the seed_data_writer -> agent_pdp_view refresh hook."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import seed_data_writer as writer  # noqa: E402


class _RecordingTxn:
    def __init__(self, db: "_RecordingDB") -> None:
        self.db = db

    async def __aenter__(self):
        self.db.events.append("txn_enter")
        return self

    async def __aexit__(self, *exc):
        self.db.events.append("txn_exit")
        return False


class _RecordingDB:
    def __init__(
        self,
        *,
        current_row: Optional[Dict[str, Any]] = None,
        seed_row: Optional[Dict[str, Any]] = None,
        catalog_row: Optional[Dict[str, Any]] = None,
        products: Optional[List[Dict[str, Any]]] = None,
        skus: Optional[List[Dict[str, Any]]] = None,
        offers: Optional[List[Dict[str, Any]]] = None,
        external_seed: Optional[Dict[str, Any]] = None,
        proposal_id: int = 101,
        raise_on_agent_upsert: bool = False,
    ) -> None:
        self.current_row = current_row
        self.seed_row = seed_row
        self.catalog_row = catalog_row
        self.products = products or []
        self.skus = skus or []
        self.offers = offers or []
        self.external_seed = external_seed
        self.proposal_id = proposal_id
        self.raise_on_agent_upsert = raise_on_agent_upsert
        self.events: List[str] = []
        self.fetch_one_calls: List[Dict[str, Any]] = []
        self.fetch_all_calls: List[Dict[str, Any]] = []
        self.execute_calls: List[Dict[str, Any]] = []

    def transaction(self):
        return _RecordingTxn(self)

    async def fetch_one(self, sql: str, params: Dict[str, Any] = None):
        params = params or {}
        self.fetch_one_calls.append({"sql": sql, "params": params})
        if "INSERT INTO seed_data_proposals" in sql and "RETURNING id" in sql:
            return {"id": self.proposal_id}
        if "SELECT seed_data, content_lock" in sql:
            return self.current_row
        if "SELECT attached_product_key" in sql:
            return self.seed_row
        if "SELECT content_key" in sql:
            return self.catalog_row
        if "FROM external_product_seeds" in sql and "WHERE id = :seed_id" in sql:
            return self.external_seed
        return None

    async def fetch_all(self, sql: str, params: Dict[str, Any] = None):
        params = params or {}
        self.fetch_all_calls.append({"sql": sql, "params": params})
        if "FROM catalog_products cp" in sql:
            return self.products
        if "FROM catalog_skus" in sql:
            return self.skus
        if "FROM catalog_offers" in sql:
            return self.offers
        return []

    async def execute(self, sql: str, params: Dict[str, Any] = None) -> int:
        params = params or {}
        self.execute_calls.append({"sql": sql, "params": params})
        if self.raise_on_agent_upsert and "INSERT INTO agent_pdp_view" in sql:
            raise RuntimeError("agent_pdp_view upsert failed")
        return 1


def _catalog_product(*, product_key: str = "pk_1", title: str = "Acme Serum") -> Dict[str, Any]:
    return {
        "product_key": product_key,
        "merchant_id": "m_primary",
        "platform": "shopify",
        "source_product_id": "sp_1",
        "title": title,
        "description": "Brightening serum.",
        "brand": "Acme",
        "product_type": "serum",
        "category": "Skin Care",
        "image_url": "https://img.example/acme.jpg",
        "product_payload": {},
        "tags": None,
        "price_tier": None,
        "use_case_tags": None,
        "lifestyle_tags": None,
        "demographic": None,
        "pdp_lifecycle_stage": "published",
        "pivota_signature_id": "sig_acme",
        "canonical_url": "https://merchant.example/acme",
        "sync_status": "live",
        "product_group_id": "pg_acme",
        "group_is_primary": True,
    }


def _offer() -> Dict[str, Any]:
    return {
        "merchant_id": "m_primary",
        "merchant_name": "Primary Merchant",
        "availability": "in_stock",
        "currency": "USD",
        "list_price": Decimal("32.00"),
        "merchant_effective_price": None,
        "estimated_best_price": None,
    }


@pytest.mark.asyncio
async def test_hook_fires_after_apply_merge_success(monkeypatch) -> None:
    fake_db = _RecordingDB()
    refresh_calls: List[Dict[str, Any]] = []

    async def fake_refresh(**kwargs):
        refresh_calls.append({**kwargs, "events_before_refresh": list(fake_db.events)})

    monkeypatch.setattr(writer, "database", fake_db)
    monkeypatch.setattr(writer, "refresh_agent_pdp_view_for_seed", fake_refresh)

    await writer._apply_merge(
        seed_id="seed_1",
        merged_seed_data={"description": "new"},
        updated_lock={},
        proposal_id=42,
    )

    assert refresh_calls == [
        {
            "seed_id": "seed_1",
            "proposal_id": 42,
            "refresh_source": "writer_commit:42",
            "events_before_refresh": ["txn_enter", "txn_exit"],
        }
    ]


@pytest.mark.asyncio
async def test_hook_fires_after_insert_new_row_success(monkeypatch) -> None:
    fake_db = _RecordingDB(proposal_id=77)
    refresh_calls: List[Dict[str, Any]] = []

    async def fake_refresh(**kwargs):
        refresh_calls.append({**kwargs, "events_before_refresh": list(fake_db.events)})

    monkeypatch.setattr(writer, "database", fake_db)
    monkeypatch.setattr(writer, "refresh_agent_pdp_view_for_seed", fake_refresh)

    result = await writer._insert_new_row(
        seed_id="seed_new",
        external_product_id="external_1",
        cleaned_proposal={"description": "Fresh PDP copy."},
        audit_summary={"review_status": "no_issues_found"},
        proposer="tester",
        source="unit",
        notes=None,
    )

    assert result.status == "merged"
    assert result.proposal_id == 77
    assert refresh_calls == [
        {
            "seed_id": "seed_new",
            "proposal_id": 77,
            "refresh_source": "writer_commit:77",
            "events_before_refresh": ["txn_enter", "txn_exit"],
        }
    ]


@pytest.mark.asyncio
async def test_refresh_returns_early_when_attached_product_key_is_null(monkeypatch) -> None:
    fake_db = _RecordingDB(seed_row={"attached_product_key": None})
    monkeypatch.setattr(writer, "database", fake_db)

    outcome = await writer.refresh_agent_pdp_view_for_seed(
        seed_id="seed_unattached",
        proposal_id=1,
        refresh_source="writer_commit:1",
    )

    assert outcome == "skipped_no_attached_key"
    assert len(fake_db.fetch_one_calls) == 1
    assert fake_db.fetch_all_calls == []
    assert fake_db.execute_calls == []


@pytest.mark.asyncio
async def test_refresh_returns_early_when_content_key_is_null(monkeypatch) -> None:
    fake_db = _RecordingDB(
        seed_row={"attached_product_key": "pk_legacy"},
        catalog_row={"content_key": None},
    )
    monkeypatch.setattr(writer, "database", fake_db)

    outcome = await writer.refresh_agent_pdp_view_for_seed(
        seed_id="seed_legacy",
        proposal_id=2,
        refresh_source="writer_commit:2",
    )

    assert outcome == "skipped_no_content_key"
    assert len(fake_db.fetch_one_calls) == 2
    assert fake_db.fetch_all_calls == []
    assert fake_db.execute_calls == []


@pytest.mark.asyncio
async def test_refresh_returns_early_when_assemble_row_returns_none(monkeypatch) -> None:
    fake_db = _RecordingDB(
        seed_row={"attached_product_key": "pk_no_title"},
        catalog_row={"content_key": "ck_no_title"},
        products=[_catalog_product(product_key="pk_no_title", title="")],
    )
    monkeypatch.setattr(writer, "database", fake_db)

    outcome = await writer.refresh_agent_pdp_view_for_seed(
        seed_id="seed_no_title",
        proposal_id=3,
        refresh_source="writer_commit:3",
    )
    assert outcome == "skipped_not_refreshable"

    assert any(call["params"] == {"ck": "ck_no_title"} for call in fake_db.fetch_all_calls)
    assert fake_db.execute_calls == []


@pytest.mark.asyncio
async def test_refresh_failure_does_not_raise_from_parent_writer(monkeypatch) -> None:
    fake_db = _RecordingDB(
        current_row={"seed_data": {"description": "old"}, "content_lock": {}},
        seed_row={"attached_product_key": "pk_1"},
        catalog_row={"content_key": "ck_target"},
        products=[_catalog_product()],
        offers=[_offer()],
        proposal_id=88,
        raise_on_agent_upsert=True,
    )
    monkeypatch.setattr(writer, "database", fake_db)

    result = await writer.upsert_seed_data(
        seed_id="seed_1",
        external_product_id="external_1",
        proposed_seed_data={"description": "A longer, cleaner product description."},
        proposer="tester",
        source="unit",
    )

    assert result.status == "merged"
    assert result.proposal_id == 88
    assert any("UPDATE external_product_seeds" in call["sql"] for call in fake_db.execute_calls)
    assert any("INSERT INTO agent_pdp_view" in call["sql"] for call in fake_db.execute_calls)


@pytest.mark.asyncio
async def test_refresh_sql_targets_the_right_content_key(monkeypatch) -> None:
    fake_db = _RecordingDB(
        seed_row={"attached_product_key": "pk_anchor"},
        catalog_row={"content_key": "ck_target"},
        products=[_catalog_product(product_key="pk_anchor")],
        offers=[_offer()],
    )
    monkeypatch.setattr(writer, "database", fake_db)

    outcome = await writer.refresh_agent_pdp_view_for_seed(
        seed_id="seed_attached",
        proposal_id=909,
        refresh_source="writer_commit:909",
    )
    assert outcome == "refreshed", "the success path must say so — callers count it now"

    product_fetch = next(
        call for call in fake_db.fetch_all_calls
        if "FROM catalog_products cp" in call["sql"]
    )
    assert product_fetch["params"] == {"ck": "ck_target"}

    upsert = next(call for call in fake_db.execute_calls if "INSERT INTO agent_pdp_view" in call["sql"])
    assert upsert["params"]["content_key"] == "ck_target"
    assert upsert["params"]["refresh_source"] == "writer_commit:909"
    assert upsert["params"]["refreshed_by_proposal_id"] == 909

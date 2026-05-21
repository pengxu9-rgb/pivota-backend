"""Tests for external_seed price mainline repair."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import repair_external_seed_offer_mainline as repair  # noqa: E402


def test_offer_chain_targets_use_mainline_seed_price_only() -> None:
    sql = repair.OFFER_CHAIN_TARGETS_SQL

    assert "eps.price_amount > 0" in sql
    assert "catalog_offers" in sql
    assert "co.list_price > 0" in sql
    assert "seed_data#>>" not in sql
    assert "product_payload" not in sql


def test_apv_update_sql_touches_only_offer_fields() -> None:
    sql = repair.APV_OFFER_FIELDS_UPDATE_SQL.lower()

    for forbidden in (
        "title",
        "description",
        "image_url",
        "image_urls",
        "seed_data",
        "catalog_products",
    ):
        assert forbidden not in sql
    for expected in ("currency", "price_min", "price_max", "offer_count", "offers"):
        assert expected in sql


@pytest.mark.asyncio
async def test_build_apv_update_uses_assembled_offer_price(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_products(content_key: str, *, db: Any) -> List[Dict[str, Any]]:
        return [{"product_key": "pk_1"}]

    async def fake_skus(product_keys: List[str], *, db: Any) -> List[Dict[str, Any]]:
        return []

    async def fake_offers(product_keys: List[str], *, db: Any) -> List[Dict[str, Any]]:
        return [{"offer_id": "offer_1"}]

    async def fake_seed(product_keys: List[str], *, db: Any) -> Dict[str, Any]:
        return {}

    def fake_assemble_row(**kwargs: Any) -> Dict[str, Any]:
        return {
            "currency": "USD",
            "price_min": Decimal("22.00"),
            "price_max": Decimal("22.00"),
            "offer_count": 1,
            "offers": [{"merchant_id": "external_seed", "price": 22.0}],
        }

    monkeypatch.setattr(repair, "fetch_products_for_key", fake_products)
    monkeypatch.setattr(repair, "fetch_skus_for_keys", fake_skus)
    monkeypatch.setattr(repair, "fetch_offers_for_keys", fake_offers)
    monkeypatch.setattr(repair, "fetch_external_seed_for_keys", fake_seed)
    monkeypatch.setattr(repair, "assemble_row", fake_assemble_row)

    params = await repair._build_apv_offer_field_update("ck_1", db=object())

    assert params["currency"] == "USD"
    assert params["price_min"] == Decimal("22.00")
    assert params["price_max"] == Decimal("22.00")
    assert params["offer_count"] == 1
    assert params["refresh_source"] == repair.MAINLINE_REFRESH_SOURCE
    assert params["has_positive_offer_price"] is True


@pytest.mark.asyncio
async def test_build_apv_update_clears_fallback_price_without_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_products(content_key: str, *, db: Any) -> List[Dict[str, Any]]:
        return [{"product_key": "pk_1"}]

    async def fake_empty(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    def fake_assemble_row(**kwargs: Any) -> Dict[str, Any]:
        return {
            "currency": None,
            "price_min": None,
            "price_max": None,
            "offer_count": 0,
            "offers": None,
        }

    monkeypatch.setattr(repair, "fetch_products_for_key", fake_products)
    monkeypatch.setattr(repair, "fetch_skus_for_keys", fake_empty)
    monkeypatch.setattr(repair, "fetch_offers_for_keys", fake_empty)
    monkeypatch.setattr(repair, "fetch_external_seed_for_keys", fake_empty)
    monkeypatch.setattr(repair, "assemble_row", fake_assemble_row)

    params = await repair._build_apv_offer_field_update("ck_1", db=object())

    assert params["currency"] is None
    assert params["price_min"] is None
    assert params["price_max"] is None
    assert params["offer_count"] == 0
    assert params["offers"] is None
    assert params["refresh_source"] == repair.NO_OFFER_REFRESH_SOURCE
    assert params["has_positive_offer_price"] is False


class _FakeDB:
    is_connected = True

    def __init__(self) -> None:
        self.execute_calls: List[Dict[str, Any]] = []

    async def connect(self) -> None:
        self.is_connected = True

    async def execute(self, sql: str, params: Dict[str, Any]) -> int:
        self.execute_calls.append({"sql": sql, "params": params})
        return 1


@pytest.mark.asyncio
async def test_drive_dry_run_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = _FakeDB()

    async def fake_chain_targets(limit: int, *, db: Any) -> List[Dict[str, Any]]:
        return [{
            "content_key": "ck_offer",
            "product_key": "pk_offer",
            "id": "eps_1",
            "external_product_id": "ext_1",
            "price_amount": Decimal("22.00"),
            "price_currency": "USD",
        }]

    async def fake_fallback_targets(limit: int, *, db: Any) -> List[str]:
        return ["ck_fallback"]

    async def fake_apv_update(content_key: str, *, db: Any) -> Dict[str, Any]:
        return {
            "content_key": content_key,
            "currency": "USD",
            "price_min": Decimal("22.00"),
            "price_max": Decimal("22.00"),
            "offer_count": 1,
            "offers": "[]",
            "refresh_source": repair.MAINLINE_REFRESH_SOURCE,
            "has_positive_offer_price": True,
        }

    monkeypatch.setattr(repair, "_fetch_offer_chain_targets", fake_chain_targets)
    monkeypatch.setattr(repair, "_fetch_fallback_refresh_targets", fake_fallback_targets)
    monkeypatch.setattr(repair, "_build_apv_offer_field_update", fake_apv_update)

    report = await repair._drive(
        SimpleNamespace(
            apply=False,
            limit=500,
            refresh_fallback_tagged=True,
            refresh_limit=1000,
        ),
        db=fake_db,
    )

    assert report["outcome_counts"]["offer_chain_targets"] == 1
    assert report["outcome_counts"]["apv_refresh_targets"] == 2
    assert report["outcome_counts"]["dry_run_skipped_writes"] == 3
    assert fake_db.execute_calls == []

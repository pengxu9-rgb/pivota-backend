"""Tests for the price-only agent_pdp_view repair script."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import repair_agent_pdp_view_source_prices as repair  # noqa: E402


def test_build_update_params_rejects_nonpositive_prices() -> None:
    assert repair._build_update_params(
        "ck_zero",
        {"currency": "USD", "price_min": Decimal("0.00"), "price_max": Decimal("0.00")},
    ) is None
    assert repair._build_update_params(
        "ck_negative",
        {"currency": "USD", "price_min": Decimal("-1.00"), "price_max": Decimal("10.00")},
    ) is None


def test_build_update_params_keeps_patch_price_only() -> None:
    params = repair._build_update_params(
        "ck_price",
        {
            "currency": "usd",
            "price_min": Decimal("13.00"),
            "price_max": Decimal("15.00"),
            "title": "Do not write this",
            "description": "Do not write this either",
        },
    )

    assert params == {
        "content_key": "ck_price",
        "currency": "USD",
        "price_min": Decimal("13.00"),
        "price_max": Decimal("15.00"),
        "refresh_source": repair.PRICE_REPAIR_REFRESH_SOURCE,
    }


def test_update_sql_does_not_touch_content_fields() -> None:
    sql = repair.PRICE_REPAIR_UPDATE_SQL.lower()
    for forbidden in ("title", "description", "image_url", "image_urls", "offers", "variants"):
        assert forbidden not in sql
    assert "price_min" in sql
    assert "price_max" in sql
    assert "currency" in sql


class _FakeDB:
    is_connected = True

    def __init__(self) -> None:
        self.execute_calls: List[Dict[str, Any]] = []

    async def fetch_all(self, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [{"content_key": "ck_patch"}, {"content_key": "ck_skip"}]

    async def execute(self, sql: str, params: Dict[str, Any]) -> int:
        self.execute_calls.append({"sql": sql, "params": params})
        return 1

    async def connect(self) -> None:
        self.is_connected = True


@pytest.mark.asyncio
async def test_drive_updates_only_rows_with_positive_source_price(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = _FakeDB()

    async def fake_assemble_price_update(content_key: str, *, db: Any):
        if content_key == "ck_skip":
            return None
        return {
            "content_key": content_key,
            "currency": "USD",
            "price_min": Decimal("13.00"),
            "price_max": Decimal("13.00"),
            "refresh_source": repair.PRICE_REPAIR_REFRESH_SOURCE,
        }

    monkeypatch.setattr(repair, "_assemble_price_update", fake_assemble_price_update)
    report = await repair._drive(
        SimpleNamespace(apply=True, limit=100, content_key=None),
        db=fake_db,
    )

    assert report["outcome_counts"]["content_keys_considered"] == 2
    assert report["outcome_counts"]["rows_with_source_price"] == 1
    assert report["outcome_counts"]["rows_updated"] == 1
    assert report["outcome_counts"]["rows_skipped_no_source_price"] == 1
    assert len(fake_db.execute_calls) == 1
    assert fake_db.execute_calls[0]["params"]["content_key"] == "ck_patch"

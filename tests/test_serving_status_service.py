"""Tests for the merchant serving-visibility status service."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import serving_status_service as svc  # noqa: E402


def test_reason_for_eligible_is_none() -> None:
    assert svc.reason_for(True, "none") is None
    assert svc.reason_for(True, None) is None


def test_reason_for_pending_when_unclassified() -> None:
    assert svc.reason_for(None, None) == svc.PENDING_REASON
    # Not eligible but no specific blocker → pending, not a scary message.
    assert svc.reason_for(False, "none") == svc.PENDING_REASON
    assert svc.reason_for(False, "") == svc.PENDING_REASON


def test_reason_for_known_blockers_are_plain_english_actions() -> None:
    assert svc.reason_for(False, "no_image") == svc.BLOCKER_CODE_TO_REASON["no_image"]
    assert svc.reason_for(False, "no_price") == svc.BLOCKER_CODE_TO_REASON["no_price"]
    # An unknown/new code degrades to the pending message, never a raw code.
    assert svc.reason_for(False, "some_new_code") == svc.PENDING_REASON


def test_no_internal_metric_names_leak_into_reasons() -> None:
    banned = ("blocker_code", "content_quality_score", "quality score",
              "serving_eligible", "index_pipeline", "product quality score")
    for reason in list(svc.BLOCKER_CODE_TO_REASON.values()) + [svc.PENDING_REASON]:
        low = reason.lower()
        for term in banned:
            assert term not in low, f"leaked internal term '{term}' in: {reason}"


class _FakeDB:
    def __init__(self, *, composites: List[Dict[str, Any]], statuses: List[Dict[str, Any]]) -> None:
        self._composites = composites
        self._statuses = statuses

    async def fetch_all(self, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        if "unnest" in sql:  # RESOLVE_COMPOSITES_SQL
            return self._composites
        if "ANY(:product_keys)" in sql:  # BY_PRODUCT_KEYS_SQL
            wanted = set(params.get("product_keys") or [])
            return [r for r in self._statuses if r["product_key"] in wanted]
        return []


@pytest.mark.asyncio
async def test_serving_status_for_sku_keys_resolves_and_marks_missing() -> None:
    db = _FakeDB(
        composites=[{"product_key": "pk-1", "platform": "shopify", "source_product_id": "111"}],
        statuses=[{
            "product_key": "pk-1", "platform": "shopify", "source_product_id": "111",
            "title": "Glow Serum", "serving_eligible": False, "blocker_code": "no_image",
        }],
    )
    out = await svc.serving_status_for_sku_keys(
        "m1", ["shopify:111", "shopify:999"], db=db
    )
    by_key = {r["sku_key"]: r for r in out}
    # Resolved + blocked with a plain-English reason.
    assert by_key["shopify:111"]["agent_visible"] is False
    assert by_key["shopify:111"]["blocker_code"] == "no_image"
    assert by_key["shopify:111"]["blocker_reason"] == svc.BLOCKER_CODE_TO_REASON["no_image"]
    # Unknown ref is reported (not 404), with a not-found reason.
    assert by_key["shopify:999"]["product_key"] is None
    assert "couldn't find" in by_key["shopify:999"]["blocker_reason"].lower()


@pytest.mark.asyncio
async def test_serving_status_marks_eligible_visible() -> None:
    db = _FakeDB(
        composites=[{"product_key": "pk-2", "platform": "shopify", "source_product_id": "222"}],
        statuses=[{
            "product_key": "pk-2", "platform": "shopify", "source_product_id": "222",
            "title": "Day Cream", "serving_eligible": True, "blocker_code": "none",
        }],
    )
    out = await svc.serving_status_for_sku_keys("m1", ["shopify:222"], db=db)
    assert out[0]["agent_visible"] is True
    assert out[0]["serving_eligible"] is True
    assert out[0]["blocker_code"] is None
    assert out[0]["blocker_reason"] is None

"""
Unit tests for `services.agent_center_sku_match_service`.

Two layers of tests:

  1. Pure-function tests of `check_product_for_findings`: no DB, no async,
     just feed it a StandardProduct-shaped dict and assert the right
     issues come back.
  2. End-to-end tests of `run_sku_match` using the same FakeDB pattern
     as `test_agent_center_service.py`, plus a monkeypatch on
     `_fetch_products_for_merchant` so we don't need a real
     `products_cache` simulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

# Reuse FakeDB from the sibling test module so tables / SQL behaviour stays
# in one place.
from tests.test_agent_center_service import FakeDB


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def _good_product() -> Dict[str, Any]:
    return {
        "product_id": "P1",
        "title": "Vitamin C Tonic",
        "sku": "VCT-50",
        "price": 19.99,
        "currency": "USD",
        "image_url": "https://example.com/p1.jpg",
        "images": ["https://example.com/p1.jpg"],
        "inventory_quantity": 12,
        "in_stock": True,
        "variants": [],
    }


def test_check_product_finds_nothing_on_clean_product() -> None:
    from services.agent_center_sku_match_service import check_product_for_findings
    findings = check_product_for_findings(_good_product())
    assert findings == []


@pytest.mark.parametrize(
    "mutation, expected_issue",
    [
        ({"sku": None}, "sku_missing"),
        ({"sku": ""}, "sku_missing"),
        ({"price": None}, "price_missing"),
        ({"price": 0}, "price_missing"),
        ({"price": -1.5}, "price_missing"),
        ({"currency": ""}, "currency_missing"),
        ({"currency": "DOLLAR"}, "currency_missing"),
        ({"image_url": "", "images": []}, "image_missing"),
        ({"image_url": None, "images": []}, "image_missing"),
        ({"inventory_quantity": None, "in_stock": None}, "inventory_unknown"),
    ],
)
def test_check_product_finds_each_issue_type(mutation: Dict[str, Any], expected_issue: str) -> None:
    from services.agent_center_sku_match_service import check_product_for_findings
    product = {**_good_product(), **mutation}
    findings = check_product_for_findings(product)
    issue_types = {f["issue_type"] for f in findings}
    assert expected_issue in issue_types, f"expected {expected_issue} in {issue_types}"


def test_check_product_top_sku_missing_but_variant_sku_present_passes() -> None:
    """A missing top-level SKU is fine if at least one variant has one —
    that's the canonical multi-variant pattern."""
    from services.agent_center_sku_match_service import check_product_for_findings
    product = {
        **_good_product(),
        "sku": None,
        "variants": [
            {"sku": "VCT-50-Red"},
            {"sku": ""},
        ],
    }
    findings = check_product_for_findings(product)
    issue_types = {f["issue_type"] for f in findings}
    assert "sku_missing" not in issue_types


def test_check_product_image_url_or_any_image_passes() -> None:
    from services.agent_center_sku_match_service import check_product_for_findings
    # No top image_url, but images[] has at least one entry.
    p = {**_good_product(), "image_url": None, "images": ["https://example.com/img2.jpg"]}
    issue_types = {f["issue_type"] for f in check_product_for_findings(p)}
    assert "image_missing" not in issue_types


# ---------------------------------------------------------------------------
# End-to-end runner tests (FakeDB + monkeypatched product fetch)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)
    return db


def _product_row(
    *,
    merchant_id: str = "merch_a",
    platform: str = "shopify",
    platform_product_id: str,
    product: Dict[str, Any],
    cached_at: datetime = None,
) -> Dict[str, Any]:
    return {
        "id": 1,
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "product_data": product,
        "product_data_decoded": product,
        "cached_at": cached_at or datetime.now(timezone.utc),
        "expires_at": None,
    }


@pytest.mark.asyncio
async def test_run_sku_match_walks_full_lifecycle_creates_issues(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import agent_center_service as ac
    from services import agent_center_sku_match_service as sms

    # Two products: one clean, one with several findings.
    products = [
        _product_row(
            platform_product_id="P_clean",
            product=_good_product(),
        ),
        _product_row(
            platform_product_id="P_dirty",
            product={
                **_good_product(),
                "sku": None,
                "variants": [],
                "price": 0,
                "image_url": None,
                "images": [],
            },
        ),
    ]

    async def _fake_fetch(**_kwargs):
        return products
    monkeypatch.setattr(sms, "_fetch_products_for_merchant", _fake_fetch)

    target = await ac.create_scan_target(
        merchant_id="merch_a", store_id="store_a",
        scan_mode="sku_match",
        payload={"options": {"limit": 50, "platform": "shopify"}},
    )
    final = await sms.run_sku_match(target["id"])

    assert final["status"] == "succeeded"
    run_summary = final["payload"]["run"]
    assert run_summary["products_checked"] == 2
    assert run_summary["products_with_findings"] == 1
    assert len(run_summary["issue_ids"]) >= 3  # sku_missing + price_missing + image_missing

    # Issues persisted with product_entity_id reference.
    issues = fake_db._tables["agent_center_issues"]
    issue_types = {row["issue_type"] for row in issues}
    assert "sku_missing" in issue_types
    assert "price_missing" in issue_types
    assert "image_missing" in issue_types
    for row in issues:
        # All issues from this run carry the dirty product's identity.
        assert row["product_entity_id"] == "merch_a|shopify|P_dirty"

    # Usage event recorded once with the right contract bits.
    usage = fake_db._tables["agent_center_usage_events"]
    assert len(usage) == 1
    assert usage[0]["agent_type"] == "sku_match"
    assert usage[0]["workflow_type"] == "sku_match_readiness"
    assert usage[0]["provider"] == "internal"
    assert usage[0]["billing_mode"] == "preview_only"
    assert usage[0]["quantity"] == 2  # products_checked


@pytest.mark.asyncio
async def test_run_sku_match_emits_stale_cache_finding_for_old_rows(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import agent_center_service as ac
    from services import agent_center_sku_match_service as sms

    old = datetime.now(timezone.utc) - timedelta(days=30)
    products = [_product_row(platform_product_id="P_old", product=_good_product(), cached_at=old)]
    async def _fake_fetch(**_kwargs):
        return products
    monkeypatch.setattr(sms, "_fetch_products_for_merchant", _fake_fetch)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
        payload={"options": {"max_age_days": 7}},
    )
    await sms.run_sku_match(target["id"])

    issue_types = {row["issue_type"] for row in fake_db._tables["agent_center_issues"]}
    assert "stale_cache" in issue_types


@pytest.mark.asyncio
async def test_run_sku_match_replays_idempotently(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same idempotency_key → second run reuses the existing usage row."""
    from services import agent_center_service as ac
    from services import agent_center_sku_match_service as sms

    async def _fake_fetch(**_kwargs):
        return [_product_row(platform_product_id="P1", product=_good_product())]
    monkeypatch.setattr(sms, "_fetch_products_for_merchant", _fake_fetch)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
    )
    await sms.run_sku_match(target["id"])
    await sms.run_sku_match(target["id"])

    assert len(fake_db._tables["agent_center_usage_events"]) == 1


@pytest.mark.asyncio
async def test_run_sku_match_refuses_wrong_scan_mode(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import agent_center_service as ac
    from services import agent_center_sku_match_service as sms

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    with pytest.raises(ValueError, match="this runner only handles 'sku_match'"):
        await sms.run_sku_match(target["id"])


@pytest.mark.asyncio
async def test_run_sku_match_accepts_running_status_from_lock(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner must accept `running` as a valid prior status — the route
    handler now acquires the run-lock atomically (try_acquire_run_lock flips
    to running) before scheduling the background runner. Refusing `running`
    here would break the production flow."""
    from services import agent_center_service as ac
    from services import agent_center_sku_match_service as sms

    async def _empty_fetch(**_kwargs):
        return []
    monkeypatch.setattr(sms, "_fetch_products_for_merchant", _empty_fetch)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
    )
    # Simulate the route having already acquired the lock.
    await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert fake_db._tables["agent_center_scan_targets"][0]["status"] == "running"
    # Runner must complete without raising.
    await sms.run_sku_match(target["id"])
    final = fake_db._tables["agent_center_scan_targets"][0]
    assert final["status"] == "succeeded"


@pytest.mark.asyncio
async def test_run_sku_match_unknown_target_raises(fake_db: FakeDB) -> None:
    from services import agent_center_sku_match_service as sms
    with pytest.raises(LookupError):
        await sms.run_sku_match("acst_nonexistent")


@pytest.mark.asyncio
async def test_run_sku_match_marks_failed_on_query_error(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import agent_center_service as ac
    from services import agent_center_sku_match_service as sms

    async def _bad_fetch(**_kwargs):
        raise RuntimeError("DB outage")
    monkeypatch.setattr(sms, "_fetch_products_for_merchant", _bad_fetch)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
    )
    with pytest.raises(RuntimeError, match="DB outage"):
        await sms.run_sku_match(target["id"])

    rows = fake_db._tables["agent_center_scan_targets"]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["payload"]["error"]["kind"] == "products_cache_query"

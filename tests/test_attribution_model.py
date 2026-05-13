from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

import services.attribution_model as attribution_model

NOW = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)


def _event(
    event_id: str,
    *,
    days_ago: float,
    source_channel: Optional[str],
    stage: str,
    product_key: Optional[str] = "sku-1",
    merchant_id: str = "merchant-1",
    session_id: Optional[str] = "session-1",
) -> Dict[str, Any]:
    attribution: Dict[str, Any] = {}
    if session_id:
        attribution["session_id"] = session_id
    return {
        "event_id": event_id,
        "merchant_id": merchant_id,
        "product_key": product_key,
        "source_channel": source_channel,
        "stage": stage,
        "occurred_at": NOW - timedelta(days=days_ago),
        "attribution_jsonb": attribution,
    }


def _install_fixture(monkeypatch: pytest.MonkeyPatch, rows: List[Dict[str, Any]]) -> None:
    async def fake_fetch(
        *,
        merchant_id: str,
        product_key: Optional[str],
        cutoff: datetime,
    ) -> List[Dict[str, Any]]:
        return rows

    monkeypatch.setattr(attribution_model, "_fetch_funnel_events", fake_fetch)
    monkeypatch.setattr(attribution_model, "_now_utc", lambda: NOW)


def _channel(result: Dict[str, Any], source_channel: str) -> Dict[str, Any]:
    for row in result["attribution_by_channel"]:
        if row["source_channel"] == source_channel:
            return row
    raise AssertionError(f"missing channel {source_channel!r}: {result['attribution_by_channel']!r}")


@pytest.mark.asyncio
async def test_last_click_single_touch_path_credits_one_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fixture(
        monkeypatch,
        [
            _event("e1", days_ago=0.10, source_channel="ai_agent", stage="click"),
            _event("e2", days_ago=0.00, source_channel="direct", stage="conversion"),
        ],
    )

    result = await attribution_model.compute_attribution(
        merchant_id="merchant-1",
        model="last_click",
    )

    assert result["attributed_conversions_total"] == 1
    assert result["sample_size_funnel_paths"] == 1
    assert result["attribution_by_channel"] == [
        {"source_channel": "ai_agent", "attributed_conversions": 1.0, "percentage": 100.0}
    ]


@pytest.mark.asyncio
async def test_last_click_multi_touch_path_credits_only_last_touch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fixture(
        monkeypatch,
        [
            _event("e1", days_ago=3.0, source_channel="editorial", stage="impression"),
            _event("e2", days_ago=0.5, source_channel="seo_organic", stage="click"),
            _event("e3", days_ago=0.0, source_channel="direct", stage="conversion"),
        ],
    )

    result = await attribution_model.compute_attribution(
        merchant_id="merchant-1",
        model="last_click",
    )

    assert result["attributed_conversions_total"] == 1
    assert _channel(result, "seo_organic")["attributed_conversions"] == 1.0
    assert len(result["attribution_by_channel"]) == 1


@pytest.mark.asyncio
async def test_multi_touch_linear_splits_three_touches_equally(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fixture(
        monkeypatch,
        [
            _event("e1", days_ago=3.0, source_channel="ai_agent", stage="impression"),
            _event("e2", days_ago=2.0, source_channel="social_own", stage="click"),
            _event("e3", days_ago=1.0, source_channel="editorial", stage="pdp_view"),
            _event("e4", days_ago=0.0, source_channel="direct", stage="conversion"),
        ],
    )

    result = await attribution_model.compute_attribution(
        merchant_id="merchant-1",
        model="multi_touch_linear",
    )

    assert result["attributed_conversions_total"] == 1
    assert _channel(result, "ai_agent")["attributed_conversions"] == pytest.approx(0.333333)
    assert _channel(result, "social_own")["attributed_conversions"] == pytest.approx(0.333333)
    assert _channel(result, "editorial")["attributed_conversions"] == pytest.approx(0.333333)


@pytest.mark.asyncio
async def test_multi_touch_linear_counts_repeat_channel_touches(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fixture(
        monkeypatch,
        [
            _event("e1", days_ago=3.0, source_channel="ai_agent", stage="impression"),
            _event("e2", days_ago=2.0, source_channel="ai_agent", stage="click"),
            _event("e3", days_ago=1.0, source_channel="editorial", stage="pdp_view"),
            _event("e4", days_ago=0.0, source_channel="direct", stage="conversion"),
        ],
    )

    result = await attribution_model.compute_attribution(
        merchant_id="merchant-1",
        model="multi_touch_linear",
    )

    assert _channel(result, "ai_agent")["attributed_conversions"] == pytest.approx(0.666667)
    assert _channel(result, "editorial")["attributed_conversions"] == pytest.approx(0.333333)


@pytest.mark.asyncio
async def test_time_decay_heavily_weights_recent_touch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fixture(
        monkeypatch,
        [
            _event("e1", days_ago=14.0, source_channel="editorial", stage="impression"),
            _event("e2", days_ago=1.0, source_channel="ai_agent", stage="click"),
            _event("e3", days_ago=0.0, source_channel="direct", stage="conversion"),
        ],
    )

    result = await attribution_model.compute_attribution(
        merchant_id="merchant-1",
        model="time_decay",
        time_decay_half_life_days=7.0,
    )

    recent_credit = _channel(result, "ai_agent")["attributed_conversions"]
    old_credit = _channel(result, "editorial")["attributed_conversions"]
    assert recent_credit > old_credit * 3
    assert recent_credit + old_credit == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_empty_funnel_returns_zero_conversion_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fixture(monkeypatch, [])

    result = await attribution_model.compute_attribution(
        merchant_id="merchant-1",
        model="last_click",
    )

    assert result == {
        "merchant_id": "merchant-1",
        "product_key": None,
        "model": "last_click",
        "lookback_days": 30,
        "attributed_conversions_total": 0,
        "attribution_by_channel": [],
        "sample_size_funnel_paths": 0,
        "computed_at": NOW.isoformat(),
    }


@pytest.mark.asyncio
async def test_brand_level_product_key_none_aggregates_all_products(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fixture(
        monkeypatch,
        [
            _event("e1", days_ago=1.0, source_channel="ai_agent", stage="click", product_key="sku-1", session_id="s1"),
            _event("e2", days_ago=0.9, source_channel="direct", stage="conversion", product_key="sku-1", session_id="s1"),
            _event("e3", days_ago=1.0, source_channel="social_own", stage="click", product_key="sku-2", session_id="s2"),
            _event("e4", days_ago=0.9, source_channel="direct", stage="conversion", product_key="sku-2", session_id="s2"),
        ],
    )

    result = await attribution_model.compute_attribution(
        merchant_id="merchant-1",
        product_key=None,
        model="last_click",
    )

    assert result["attributed_conversions_total"] == 2
    assert _channel(result, "ai_agent")["attributed_conversions"] == 1.0
    assert _channel(result, "social_own")["attributed_conversions"] == 1.0


@pytest.mark.asyncio
async def test_lookback_days_limits_considered_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fixture(
        monkeypatch,
        [
            _event("e1", days_ago=8.5, source_channel="editorial", stage="click", session_id="old"),
            _event("e2", days_ago=8.0, source_channel="direct", stage="conversion", session_id="old"),
            _event("e3", days_ago=2.0, source_channel="retail", stage="click", session_id="recent"),
            _event("e4", days_ago=1.0, source_channel="direct", stage="conversion", session_id="recent"),
        ],
    )

    result = await attribution_model.compute_attribution(
        merchant_id="merchant-1",
        model="last_click",
        lookback_days=7,
    )

    assert result["lookback_days"] == 7
    assert result["attributed_conversions_total"] == 1
    assert result["attribution_by_channel"] == [
        {"source_channel": "retail", "attributed_conversions": 1.0, "percentage": 100.0}
    ]


@pytest.mark.asyncio
async def test_fallback_paths_without_identity_are_product_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fixture(
        monkeypatch,
        [
            _event("e1", days_ago=3.0, source_channel="ai_agent", stage="click", product_key="sku-1", session_id=None),
            _event("e2", days_ago=1.0, source_channel="social_own", stage="click", product_key="sku-2", session_id=None),
            _event("e3", days_ago=0.0, source_channel="direct", stage="conversion", product_key="sku-1", session_id=None),
        ],
    )

    result = await attribution_model.compute_attribution(
        merchant_id="merchant-1",
        model="last_click",
    )

    assert result["attributed_conversions_total"] == 1
    assert result["attribution_by_channel"] == [
        {"source_channel": "ai_agent", "attributed_conversions": 1.0, "percentage": 100.0}
    ]

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest
from fastapi import BackgroundTasks

from routes import agent_products


def _make_product_data(
    title: str,
    group_id: Optional[str],
    facets: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
):
    facets = facets or {}
    tags = tags or []
    recommendation_meta = {
        "version": 1,
        "group_id": group_id,
        "tags_raw": tags,
        "tags": tags,
        "facets": facets,
    }
    return {
        "title": title,
        "status": "active",
        "recommendation_meta": recommendation_meta,
        # Minimal raw payload to make _extract_product_card happy.
        "raw": {
            "title": title,
            "variants": [
                {"price": "10.0", "currency": "USD", "inventory_quantity": 5},
            ],
            "images": [{"src": f"https://example.com/{title}.jpg"}],
        },
    }


@pytest.mark.asyncio
async def test_recommendations_primary_group_preferred(monkeypatch):
    """
    Insert one target, three same-group products and some facet-similar products.
    Verify that primary (same group_id) products are preferred.
    """

    merchant_id = "m1"
    platform_product_id = "p-target"
    group_id = "g123"

    now = datetime.utcnow()

    target_row = {
        "platform_product_id": platform_product_id,
        "product_data": _make_product_data(
            title="Target",
            group_id=group_id,
            facets={
                "use": ["blush"],
                "area": ["face"],
            },
            tags=["group-g123", "use:blush", "area:face"],
        ),
        "cached_at": now,
    }

    # Three primary same-group products
    primary_rows = [
        {
            "platform_product_id": f"p-primary-{i}",
            "product_data": _make_product_data(
                title=f"Primary {i}",
                group_id=group_id,
                facets={"use": ["blush"], "area": ["face"]},
                tags=["group-g123", "use:blush", "area:face"],
            ),
            "cached_at": now - timedelta(minutes=i),
        }
        for i in range(3)
    ]

    # Some secondary facet-similar products with different group
    secondary_rows = [
        {
            "platform_product_id": f"p-secondary-{i}",
            "product_data": _make_product_data(
                title=f"Secondary {i}",
                group_id=None,
                facets={"use": ["blush"], "area": ["face"]},
                tags=["use:blush", "area:face"],
            ),
            "cached_at": now - timedelta(minutes=10 + i),
        }
        for i in range(5)
    ]

    async def fake_get_product_cache_row(**kwargs):
        assert kwargs["merchant_id"] == merchant_id
        assert kwargs["platform"] == "shopify"
        assert kwargs["platform_product_id"] == platform_product_id
        return target_row

    async def fake_fetch_group_candidates(**kwargs):
        assert kwargs["merchant_id"] == merchant_id
        assert kwargs["platform_product_id"] == platform_product_id
        assert kwargs["group_id"] == group_id
        return primary_rows

    async def fake_fetch_secondary_candidates(**kwargs):
        assert kwargs["merchant_id"] == merchant_id
        assert kwargs["platform_product_id"] == platform_product_id
        tokens = kwargs["tokens"]
        # tokens should contain at least use:blush / area:face
        assert any(t.startswith("use:") for t in tokens) or any(
            t.startswith("area:") for t in tokens
        )
        return secondary_rows

    class DummyContext:
        agent_id = "agent-1"

        def can_access_merchant(self, mid):
            return mid == merchant_id

    async def dummy_log_agent_request(*args, **kwargs):
        return None

    def dummy_log_query_source(agent_id, merchant_id, endpoint, query_source, response_time_ms, product_count):
        assert endpoint == "/agent/v1/products/recommendations"

    monkeypatch.setattr(agent_products, "get_product_cache_row", fake_get_product_cache_row)
    monkeypatch.setattr(agent_products, "_fetch_group_candidates", fake_fetch_group_candidates)
    monkeypatch.setattr(agent_products, "_fetch_secondary_candidates", fake_fetch_secondary_candidates)
    monkeypatch.setattr(agent_products, "log_agent_request", dummy_log_agent_request)
    monkeypatch.setattr(agent_products, "log_query_source", dummy_log_query_source)

    # Call the endpoint coroutine directly.
    resp = await agent_products.get_product_recommendations(
        merchant_id=merchant_id,
        platform_product_id=platform_product_id,
        limit=8,
        context=DummyContext(),
        background_tasks=BackgroundTasks(),
    )

    assert resp["status"] == "success"
    assert resp["merchant_id"] == merchant_id
    assert resp["platform_product_id"] == platform_product_id

    recs = resp["recommendations"]
    # Should not exceed limit
    assert len(recs) <= 8

    # Primary products should appear before secondary ones in the ranked list.
    primary_ids = {row["platform_product_id"] for row in primary_rows}
    recommended_ids = [r["platform_product_id"] for r in recs]
    # At least one primary is returned
    assert primary_ids & set(recommended_ids)
    # All primary candidates, if present, should be ranked ahead of secondary-only ids.
    seen_secondary = False
    for pid in recommended_ids:
        if pid in primary_ids:
            # Once we encounter a secondary, we shouldn't see more primary after it.
            assert not seen_secondary
        else:
            seen_secondary = True

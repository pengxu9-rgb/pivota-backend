import pytest


def test_return_queries_avoid_unstable_timestamp_fields():
    from services import shopify_returns_service as svc

    assert "createdAt" not in svc._returns_list_query(first=5)
    assert "updatedAt" not in svc._returns_list_query(first=5)
    assert "createdAt" not in svc._shop_returns_list_query(first=5)
    assert "updatedAt" not in svc._shop_returns_list_query(first=5)
    probe_query = svc._order_return_probe_query(
        include_return_status=True,
        include_returns=True,
        returns_first=5,
    )
    assert "createdAt" not in probe_query
    assert "updatedAt" not in probe_query


def test_normalize_graphql_return_node_without_timestamps():
    from services import shopify_returns_service as svc

    payload = svc._normalize_graphql_return_node(
        {
            "id": "gid://shopify/Return/1",
            "status": "OPEN",
            "order": {
                "id": "gid://shopify/Order/7001",
                "legacyResourceId": "7001",
            },
        }
    )

    assert payload == {
        "id": "gid://shopify/Return/1",
        "status": "OPEN",
        "order_id": "7001",
    }


@pytest.mark.asyncio
async def test_sync_shopify_returns_best_effort_upserts_graphql_nodes_without_timestamps(monkeypatch):
    from services import shopify_returns_service as svc

    upserts = []

    async def fake_fetch_shopify_returns(**_kwargs):
        return [
            {
                "id": "gid://shopify/Return/1",
                "status": "OPEN",
                "order": {
                    "id": "gid://shopify/Order/7001",
                    "legacyResourceId": "7001",
                },
            }
        ]

    async def fake_upsert_shopify_return_record_best_effort(*, merchant_id, payload, topic, db=None):
        upserts.append(
            {
                "merchant_id": merchant_id,
                "payload": dict(payload),
                "topic": topic,
                "db": db,
            }
        )

    monkeypatch.setattr(svc, "fetch_shopify_returns", fake_fetch_shopify_returns)
    monkeypatch.setattr(svc, "upsert_shopify_return_record_best_effort", fake_upsert_shopify_return_record_best_effort)

    result = await svc.sync_shopify_returns_best_effort(
        merchant_id="merch_test",
        shop_domain="shop.myshopify.com",
        access_token="token",
        api_version="2025-10",
        limit=5,
    )

    assert result["ok"] is True
    assert result["fetched"] == 1
    assert result["upserted"] == 1
    assert upserts == [
        {
            "merchant_id": "merch_test",
            "payload": {
                "id": "gid://shopify/Return/1",
                "status": "OPEN",
                "order_id": "7001",
            },
            "topic": "returns/sync",
            "db": None,
        }
    ]

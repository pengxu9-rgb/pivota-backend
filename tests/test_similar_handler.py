import pytest
from models.standard_product import StandardProduct
from routes import agent_shop_gateway


@pytest.mark.asyncio
async def test_relaxed_filtering_used(monkeypatch):
    # Base product
    base = StandardProduct(
        id="base",
        platform="shopify",
        merchant_id="m1",
        title="Red Shirt",
        price=20.0,
        currency="USD",
        product_type="shirts",
        status=agent_shop_gateway.ProductStatus.ACTIVE,
        inventory_quantity=10,
        in_stock=True,
    )

    # Candidate with different creator_id to trigger strict rejection
    cand_prod = StandardProduct(
        id="cand",
        platform="shopify",
        merchant_id="m2",
        title="Blue Shirt",
        price=18.0,
        currency="USD",
        product_type="shirts",
        status=agent_shop_gateway.ProductStatus.ACTIVE,
        inventory_quantity=5,
        in_stock=True,
        platform_metadata={"creator_id": "other_creator"},
    )

    async def fake_load_base(pid):
        return base

    async def fake_load_many(ids):
        return {cand_prod.product_id: cand_prod}

    class FakeCand:
        def __init__(self, pid, score=0.5):
            self.productId = pid
            self.score = score

    async def fake_find_similar(params):
        return [FakeCand("cand", 0.5)]

    monkeypatch.setattr(agent_shop_gateway, "_load_product_by_id", fake_load_base)
    monkeypatch.setattr(agent_shop_gateway, "_load_products_by_ids", fake_load_many)
    monkeypatch.setattr(agent_shop_gateway.similarity_service, "findSimilar", fake_find_similar)
    monkeypatch.setattr(agent_shop_gateway.similarity_service, "hasCoViewData", lambda pid: False)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("SIMILARITY_WEIGHT_SIMILARITY", "0")
    monkeypatch.setenv("SIMILARITY_WEIGHT_PRICE", "1")
    monkeypatch.setenv("SIMILARITY_WEIGHT_MERCHANT", "0")
    monkeypatch.setenv("SIMILARITY_WEIGHT_PERSONALIZATION", "0")

    payload = agent_shop_gateway.FindSimilarProductsPayload(
        product_id="base",
        limit=3,
        creator_id="expected_creator",  # strict should exclude cand_prod
        strategy="content_embedding",
        debug=True,
    )

    result = await agent_shop_gateway._handle_find_similar_products(payload, request_metadata={})
    assert result["items"], "Relaxed filtering should return candidate even when strict is empty"
    assert result["items"][0]["product"]["id"] == "cand"
    # debug scores should be present in dev mode with debug flag
    assert result["items"][0].get("debug_scores") is not None

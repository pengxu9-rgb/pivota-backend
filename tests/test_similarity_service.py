import pytest

from services import similarity_service


class DummyProduct:
    def __init__(self, pid, title, ptype, merchant_id="m1", price=10.0):
        self.product_id = pid
        self.id = pid
        self.title = title
        self.product_type = ptype
        self.merchant_id = merchant_id
        self.status = None
        self.price = price


@pytest.mark.asyncio
async def test_content_strategy_excludes_base_and_limits(monkeypatch):
    base = DummyProduct("base", "Red T Shirt", "shirts")

    async def fake_load_base(pid):
        return base

    async def fake_search_candidates(base_product, limit, require_same_category, price_band, text_query):
        # include base, and two others
        def mk(pid, title, ptype, price, merchant="m1"):
            from models.standard_product import StandardProduct
            return StandardProduct(
                id=pid,
                platform="shopify",
                merchant_id=merchant,
                title=title,
                price=price,
                currency="USD",
                product_type=ptype,
            )

        return [
            mk("base", "Red T Shirt", "shirts", 10),
            mk("p2", "Blue T Shirt", "shirts", 12),
            mk("p3", "Red Hoodie", "hoodies", 20, merchant="m2"),
        ]

    svc = similarity_service.SimilarityService()
    monkeypatch.setattr(svc, "_load_base_product", fake_load_base)
    monkeypatch.setattr(svc, "_search_candidates_content", fake_search_candidates)

    result = await svc.findSimilar({"baseProductId": "base", "limit": 1, "strategy": "content_embedding"})
    assert len(result) <= 3
    # base should not appear
    ids = [c.productId for c in result]
    assert "base" not in ids


@pytest.mark.asyncio
async def test_coview_strategy_stub(monkeypatch):
    base = DummyProduct("base", "Red T Shirt", "shirts")

    async def fake_load_base(pid):
        return base

    async def fake_search_candidates(base_product, limit, require_same_category, price_band, text_query):
        return []

    svc = similarity_service.SimilarityService()
    monkeypatch.setattr(svc, "_load_base_product", fake_load_base)
    monkeypatch.setattr(svc, "_search_candidates_content", fake_search_candidates)

    result = await svc.findSimilar({"baseProductId": "base", "limit": 2, "strategy": "co_view"})
    assert isinstance(result, list)
    # stub returns empty list without throwing
    assert result == []


@pytest.mark.asyncio
async def test_has_coview_data_stub():
    svc = similarity_service.SimilarityService()
    assert await svc.hasCoViewData("any") is False


@pytest.mark.asyncio
async def test_content_strategy_fallback_levels(monkeypatch):
    base = DummyProduct("base", "Red T Shirt", "shirts")

    async def fake_load_base(pid):
        return base

    # Level1 returns none, Level2 returns candidates
    calls = {"count": 0}

    async def fake_search_candidates(base_product, limit, require_same_category, price_band, text_query):
        calls["count"] += 1
        if require_same_category and price_band:
            return []
        elif require_same_category and not price_band:
            from models.standard_product import StandardProduct

            return [
                StandardProduct(
                    id="p2",
                    platform="shopify",
                    merchant_id="m1",
                    title="Blue Shirt",
                    price=15,
                    currency="USD",
                    product_type="shirts",
                )
            ]
        else:
            return []

    svc = similarity_service.SimilarityService()
    monkeypatch.setattr(svc, "_load_base_product", fake_load_base)
    monkeypatch.setattr(svc, "_search_candidates_content", fake_search_candidates)

    result = await svc.findSimilar({"baseProductId": "base", "limit": 1, "strategy": "content_embedding"})
    ids = [c.productId for c in result]
    assert "p2" in ids
    assert calls["count"] >= 2

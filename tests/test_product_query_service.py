import pytest


@pytest.mark.asyncio
async def test_get_products_hybrid_force_cache_only_uses_stale_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.product_query_service as pqs
    from models.standard_product import ProductStatus, StandardProduct

    stale_product = StandardProduct(
        id="p_stale_1",
        product_id="p_stale_1",
        platform="shopify",
        merchant_id="m_001",
        title="Winona",
        description="stale cache entry",
        price=19.0,
        currency="USD",
        inventory_quantity=5,
        orderable=True,
        status=ProductStatus.ACTIVE,
    )

    async def fake_cache_all_platforms(
        merchant_id: str,
        limit: int,
        include_expired: bool = False,
        stale_cutoff=None,
    ):
        if include_expired:
            return [stale_product]
        return []

    monkeypatch.setattr(pqs, "_get_from_cache_all_platforms", fake_cache_all_platforms)
    monkeypatch.setattr(pqs, "STALE_CACHE_FALLBACK_ENABLED", True)

    products, source, error = await pqs.get_products_hybrid(
        merchant_id="m_001",
        limit=20,
        agent_id="test_agent",
        force_cache_only=True,
    )

    assert error is None
    assert source == "cache_all_platforms_stale"
    assert len(products) == 1
    assert products[0].id == "p_stale_1"


@pytest.mark.asyncio
async def test_get_products_hybrid_no_config_uses_stale_cache_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.product_query_service as pqs
    from models.standard_product import ProductStatus, StandardProduct

    stale_product = StandardProduct(
        id="p_stale_2",
        product_id="p_stale_2",
        platform="shopify",
        merchant_id="m_002",
        title="The Ordinary Niacinamide 10% + Zinc 1%",
        description="stale cache entry",
        price=24.0,
        currency="USD",
        inventory_quantity=4,
        orderable=True,
        status=ProductStatus.ACTIVE,
    )

    async def fake_config(_merchant_id: str):
        return None

    async def fake_cache_all_platforms(
        merchant_id: str,
        limit: int,
        include_expired: bool = False,
        stale_cutoff=None,
    ):
        if include_expired:
            return [stale_product]
        return []

    monkeypatch.setattr(pqs, "get_merchant_realtime_config", fake_config)
    monkeypatch.setattr(pqs, "_get_from_cache_all_platforms", fake_cache_all_platforms)
    monkeypatch.setattr(pqs, "STALE_CACHE_FALLBACK_ENABLED", True)

    products, source, error = await pqs.get_products_hybrid(
        merchant_id="m_002",
        limit=20,
        agent_id="test_agent",
        force_cache_only=False,
    )

    assert error is None
    assert source == "cache_stale_no_config"
    assert len(products) == 1
    assert products[0].id == "p_stale_2"

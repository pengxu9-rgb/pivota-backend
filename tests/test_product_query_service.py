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


@pytest.mark.asyncio
async def test_get_products_hybrid_no_config_disables_stale_cache_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.product_query_service as pqs

    async def fake_config(_merchant_id: str):
        return None

    async def fake_cache_all_platforms(
        merchant_id: str,
        limit: int,
        include_expired: bool = False,
        stale_cutoff=None,
    ):
        if include_expired:
            raise AssertionError("stale cache path should be skipped when allow_stale_cache=False")
        return []

    monkeypatch.setattr(pqs, "get_merchant_realtime_config", fake_config)
    monkeypatch.setattr(pqs, "_get_from_cache_all_platforms", fake_cache_all_platforms)
    monkeypatch.setattr(pqs, "STALE_CACHE_FALLBACK_ENABLED", True)

    products, source, error = await pqs.get_products_hybrid(
        merchant_id="m_003",
        limit=20,
        agent_id="test_agent",
        force_cache_only=False,
        allow_stale_cache=False,
    )

    assert error is None
    assert source == "cache"
    assert products == []


@pytest.mark.asyncio
async def test_get_products_hybrid_cache_path_disables_stale_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.product_query_service as pqs

    config = pqs.RealtimeConfig(
        realtime_enabled=False,
        api_endpoint=None,
        api_key=None,
        ttl_seconds=600,
        platform="shopify",
    )

    async def fake_config(_merchant_id: str):
        return config

    async def fake_cache(
        merchant_id: str,
        platform: str,
        limit: int,
        include_expired: bool = False,
    ):
        if include_expired:
            raise AssertionError("stale cache path should be skipped when allow_stale_cache=False")
        return []

    monkeypatch.setattr(pqs, "get_merchant_realtime_config", fake_config)
    monkeypatch.setattr(pqs, "_get_from_cache", fake_cache)
    monkeypatch.setattr(pqs, "STALE_CACHE_FALLBACK_ENABLED", True)

    products, source, error = await pqs.get_products_hybrid(
        merchant_id="m_004",
        limit=20,
        agent_id="test_agent",
        allow_stale_cache=False,
    )

    assert error is None
    assert source == "cache"
    assert products == []


@pytest.mark.asyncio
async def test_get_products_hybrid_budget_flag_off_keeps_realtime_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    import services.product_query_service as pqs
    from core.reliability.budget import RequestBudget

    config = pqs.RealtimeConfig(
        realtime_enabled=True,
        api_endpoint="https://merchant.example.com/api",
        api_key="k",
        ttl_seconds=600,
        platform="shopify",
    )

    async def fake_config(_merchant_id: str):
        return config

    class FakeAdapter:
        calls = []

        def __init__(self, endpoint: str, credentials):
            self.endpoint = endpoint
            self.credentials = credentials

        async def query_products(self, **kwargs):
            FakeAdapter.calls.append(kwargs)
            return [], None

    monkeypatch.setattr(pqs, "RELIABILITY_BUDGET_ENABLED", False)
    monkeypatch.setattr(pqs, "get_merchant_realtime_config", fake_config)
    monkeypatch.setattr(pqs, "MerchantAPIAdapter", FakeAdapter)

    expired_budget = RequestBudget(deadline_monotonic=time.monotonic() - 1.0)

    products, source, error = await pqs.get_products_hybrid(
        merchant_id="m_budget_off",
        limit=20,
        agent_id="agent_budget_off",
        request_budget=expired_budget,
    )

    assert error is None
    assert source == "realtime"
    assert products == []
    assert len(FakeAdapter.calls) == 1
    assert FakeAdapter.calls[0]["timeout_seconds"] == 1.0
    assert FakeAdapter.calls[0]["request_id"] == "pq:agent_budget_off:m_budget_off"


@pytest.mark.asyncio
async def test_get_products_hybrid_budget_exhausted_skips_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    import services.product_query_service as pqs
    from core.reliability.budget import RequestBudget
    from models.standard_product import ProductStatus, StandardProduct

    config = pqs.RealtimeConfig(
        realtime_enabled=True,
        api_endpoint="https://merchant.example.com/api",
        api_key="k",
        ttl_seconds=600,
        platform="shopify",
    )

    fallback_product = StandardProduct(
        id="p_budget_cache_1",
        product_id="p_budget_cache_1",
        platform="shopify",
        merchant_id="m_budget_on",
        title="Budget fallback product",
        description="cache",
        price=9.0,
        currency="USD",
        inventory_quantity=2,
        orderable=True,
        status=ProductStatus.ACTIVE,
    )

    async def fake_config(_merchant_id: str):
        return config

    async def fake_cache(_merchant_id: str, _platform: str, _limit: int, include_expired: bool = False):
        return [fallback_product]

    class FakeAdapter:
        calls = 0

        def __init__(self, endpoint: str, credentials):
            self.endpoint = endpoint
            self.credentials = credentials

        async def query_products(self, **kwargs):
            FakeAdapter.calls += 1
            return [], None

    monkeypatch.setattr(pqs, "RELIABILITY_BUDGET_ENABLED", True)
    monkeypatch.setattr(pqs, "get_merchant_realtime_config", fake_config)
    monkeypatch.setattr(pqs, "_get_from_cache", fake_cache)
    monkeypatch.setattr(pqs, "MerchantAPIAdapter", FakeAdapter)

    expired_budget = RequestBudget(deadline_monotonic=time.monotonic() - 1.0)

    products, source, error = await pqs.get_products_hybrid(
        merchant_id="m_budget_on",
        limit=20,
        agent_id="agent_budget_on",
        request_budget=expired_budget,
    )

    assert error is None
    assert source == "realtime_budget_exhausted_cache_fallback"
    assert len(products) == 1
    assert products[0].id == "p_budget_cache_1"
    assert FakeAdapter.calls == 0


@pytest.mark.asyncio
async def test_get_products_hybrid_budget_enabled_caps_timeout_for_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.product_query_service as pqs
    from core.reliability.budget import RequestBudget

    config = pqs.RealtimeConfig(
        realtime_enabled=True,
        api_endpoint="https://merchant.example.com/api",
        api_key="k",
        ttl_seconds=600,
        platform="shopify",
    )

    async def fake_config(_merchant_id: str):
        return config

    captured: dict = {}

    class FakeAdapter:
        def __init__(self, endpoint: str, credentials):
            self.endpoint = endpoint
            self.credentials = credentials

        async def query_products(self, **kwargs):
            captured.update(kwargs)
            return [], None

    monkeypatch.setattr(pqs, "RELIABILITY_BUDGET_ENABLED", True)
    monkeypatch.setattr(pqs, "get_merchant_realtime_config", fake_config)
    monkeypatch.setattr(pqs, "MerchantAPIAdapter", FakeAdapter)

    budget = RequestBudget.from_total_ms(250)

    products, source, error = await pqs.get_products_hybrid(
        merchant_id="m_budget_cap",
        limit=10,
        agent_id="agent_budget_cap",
        request_budget=budget,
    )

    assert error is None
    assert source == "realtime"
    assert products == []
    assert captured.get("request_id") == "pq:agent_budget_cap:m_budget_cap"
    timeout = float(captured.get("timeout_seconds") or 0.0)
    assert 0.1 <= timeout <= 1.0


@pytest.mark.asyncio
async def test_get_from_cache_all_platforms_retries_connection_acquire_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.product_query_service as pqs

    attempts = {"primary": 0, "retry": 0, "sleep": 0}

    async def fake_fetch_all(_query, _params):
        attempts["primary"] += 1
        raise RuntimeError("Connection is already acquired")

    class FakeConnection:
        async def fetch_all(self, _query, _params):
            attempts["retry"] += 1
            return [
                {
                    "product_data": {
                        "id": "p_retry_1",
                        "product_id": "p_retry_1",
                        "platform": "shopify",
                        "merchant_id": "m_retry",
                        "title": "Retry Product",
                        "price": 12.5,
                        "currency": "USD",
                        "inventory_quantity": 3,
                        "orderable": True,
                        "status": "active",
                    }
                }
            ]

    def fake_connection():
        return FakeConnection()

    async def fake_sleep(_seconds: float):
        attempts["sleep"] += 1

    monkeypatch.setattr(pqs.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(pqs.database, "connection", fake_connection)
    monkeypatch.setattr(pqs.asyncio, "sleep", fake_sleep)

    products = await pqs._get_from_cache_all_platforms(
        merchant_id="m_retry",
        limit=10,
        include_expired=False,
    )

    assert attempts["primary"] == 1
    assert attempts["retry"] == 1
    assert attempts["sleep"] == 1
    assert len(products) == 1
    assert products[0].id == "p_retry_1"


@pytest.mark.asyncio
async def test_get_from_cache_all_platforms_non_transient_error_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.product_query_service as pqs

    attempts = {"connection_factory": 0}

    async def fake_fetch_all(_query, _params):
        raise RuntimeError("database unavailable")

    class FakeConnection:
        async def fetch_all(self, _query, _params):
            raise AssertionError("retry path should not be used for non-transient errors")

    def fake_connection():
        attempts["connection_factory"] += 1
        return FakeConnection()

    monkeypatch.setattr(pqs.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(pqs.database, "connection", fake_connection)

    products = await pqs._get_from_cache_all_platforms(
        merchant_id="m_no_retry",
        limit=10,
        include_expired=False,
    )

    assert products == []
    assert attempts["connection_factory"] == 0

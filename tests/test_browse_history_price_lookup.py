import pytest

from routes import accounts_orders_api as route_module


class FakeDatabase:
    def __init__(self, external_rows=None, cache_rows=None):
        self.external_rows = external_rows or []
        self.cache_rows = cache_rows or []

    async def fetch_all(self, query, values=None):
        text = str(query)
        if "FROM external_product_seeds" in text:
            return self.external_rows
        if "FROM products_cache" in text:
            return self.cache_rows
        return []


@pytest.mark.asyncio
async def test_browse_history_price_lookup_uses_external_seed_price(monkeypatch):
    monkeypatch.setattr(
        route_module,
        "database",
        FakeDatabase(
            external_rows=[
                {
                    "id": "ext_1",
                    "external_product_id": "external_1",
                    "attached_product_key": None,
                    "attached_variant_id": None,
                    "price_amount": None,
                    "price_currency": None,
                    "seed_data": {"price_amount": 28, "price_currency": "USD"},
                }
            ]
        ),
    )

    lookup = await route_module._resolve_history_price_lookup(
        [{"product_id": "external_1", "price": 0}]
    )

    resolved = lookup["external_1::"]
    assert resolved.price == 28.0
    assert resolved.currency == "USD"
    assert resolved.source == "external_product_seeds"


@pytest.mark.asyncio
async def test_browse_history_price_lookup_uses_attached_external_seed_alias(monkeypatch):
    monkeypatch.setattr(
        route_module,
        "database",
        FakeDatabase(
            external_rows=[
                {
                    "id": "ext_1",
                    "external_product_id": "external_1",
                    "attached_product_key": "canonical_1",
                    "attached_variant_id": "variant_1",
                    "price_amount": 31,
                    "price_currency": "USD",
                    "seed_data": {},
                }
            ]
        ),
    )

    lookup = await route_module._resolve_history_price_lookup(
        [{"product_id": "canonical_1", "merchant_id": "external_seed", "price": 0}]
    )

    resolved = lookup["canonical_1::external_seed"]
    assert resolved.price == 31.0
    assert resolved.source == "external_product_seeds"


@pytest.mark.asyncio
async def test_browse_history_price_lookup_uses_products_cache_variant_price(monkeypatch):
    monkeypatch.setattr(
        route_module,
        "database",
        FakeDatabase(
            cache_rows=[
                {
                    "merchant_id": "merchant_1",
                    "platform_product_id": "prod_1",
                    "product_data": {
                        "product_id": "prod_1",
                        "currency": "USD",
                        "variants": [
                            {
                                "variant_id": "sku_1",
                                "price": {"current": {"amount": 42, "currency": "USD"}},
                            }
                        ],
                    },
                }
            ]
        ),
    )

    lookup = await route_module._resolve_history_price_lookup(
        [{"product_id": "sku_1", "price": 0}]
    )

    resolved = lookup["sku_1::"]
    assert resolved.price == 42.0
    assert resolved.currency == "USD"
    assert resolved.source == "products_cache"


@pytest.mark.asyncio
async def test_browse_history_price_lookup_uses_products_cache_canonical_group_alias(monkeypatch):
    monkeypatch.setattr(
        route_module,
        "database",
        FakeDatabase(
            cache_rows=[
                {
                    "merchant_id": "merchant_1",
                    "platform_product_id": "platform_1",
                    "product_data": {
                        "product_id": "prod_1",
                        "canonical_product_ref": {"product_id": "canonical_1"},
                        "product_group_id": "group_1",
                        "price_amount": "55.50",
                        "currency": "USD",
                    },
                }
            ]
        ),
    )

    lookup = await route_module._resolve_history_price_lookup(
        [{"product_id": "canonical_1", "merchant_id": "merchant_1", "price": 0}]
    )

    resolved = lookup["canonical_1::merchant_1"]
    assert resolved.price == 55.5
    assert resolved.source == "products_cache"


@pytest.mark.asyncio
async def test_create_browse_history_event_does_not_overwrite_valid_price_with_zero(monkeypatch):
    class WriteGuardDatabase:
        def __init__(self):
            self.updated_values = None

        async def fetch_one(self, query):
            text = str(query)
            if "SELECT" in text:
                return {
                    "id": 7,
                    "user_id": "user_1",
                    "product_id": "prod_1",
                    "merchant_id": "merchant_1",
                    "title": "Existing",
                    "price": 24,
                    "currency": "USD",
                    "image_url": "/placeholder.svg",
                    "viewed_at": "2026-05-06T00:00:00+00:00",
                    "created_at": "2026-05-06T00:00:00+00:00",
                }
            return None

        async def execute(self, query):
            self.updated_values = query.compile().params
            return 7

    fake_db = WriteGuardDatabase()
    async def noop():
        return None

    async def empty_lookup(rows):
        return {}

    monkeypatch.setattr(route_module, "database", fake_db)
    monkeypatch.setattr(route_module, "_ensure_database_connected", noop)
    monkeypatch.setattr(route_module, "_ensure_browse_history_schema", noop)
    monkeypatch.setattr(route_module, "_resolve_history_price_lookup", empty_lookup)

    principal = route_module.AccountsPrincipal(
        user_id="user_1",
        email="u@example.com",
        email_normalized="u@example.com",
        primary_role="customer",
    )

    await route_module.create_browse_history_event(
        route_module.BrowseHistoryEventRequest(
            product_id="prod_1",
            merchant_id="merchant_1",
            title="Updated",
            price=0,
            currency=None,
        ),
        principal,
    )

    assert fake_db.updated_values["price"] == 24

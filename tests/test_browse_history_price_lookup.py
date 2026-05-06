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

    assert lookup["external_1"] == (28.0, "USD")


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

    assert lookup["sku_1"] == (42.0, "USD")

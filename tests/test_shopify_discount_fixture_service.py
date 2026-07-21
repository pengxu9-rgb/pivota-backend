import pytest

import services.shopify_discount_fixture_service as module


@pytest.mark.asyncio
async def test_create_shopify_discount_validation_fixtures_builds_expected_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class _Cfg:
        shop_domain = "example.myshopify.com"
        access_token = "shpat_test"

        @property
        def is_configured(self) -> bool:
            return True

    async def fake_get_cfg(_merchant_id: str):
        return _Cfg()

    async def fake_graphql(**kwargs):
        calls.append(kwargs)
        query = kwargs["query"]
        variables = kwargs.get("variables") or {}
        if "query CustomerLookup" in query:
            return {
                "customers": {
                    "nodes": [
                        {
                            "id": "gid://shopify/Customer/1",
                            "email": "buyer@example.com",
                            "numberOfOrders": 0,
                        }
                    ]
                }
            }
        if "mutation CreateSegment" in query:
            name = variables["name"]
            return {
                "segmentCreate": {
                    "segment": {
                        "id": f"gid://shopify/Segment/{len(calls)}",
                        "name": name,
                        "query": variables["query"],
                    },
                    "userErrors": [],
                }
            }
        if "mutation CreateDiscountCode" in query:
            payload = variables["basicCodeDiscount"]
            return {
                "discountCodeBasicCreate": {
                    "codeDiscountNode": {
                        "id": f"gid://shopify/DiscountNode/{len(calls)}",
                        "codeDiscount": {
                            "title": payload["title"],
                            "startsAt": payload["startsAt"],
                            "endsAt": payload.get("endsAt"),
                            "usageLimit": payload.get("usageLimit"),
                            "appliesOncePerCustomer": payload.get("appliesOncePerCustomer"),
                            "codes": {"nodes": [{"code": payload["code"]}]},
                        },
                    },
                    "userErrors": [],
                }
            }
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(module, "get_shopify_config_for_merchant", fake_get_cfg)
    monkeypatch.setattr(module, "shopify_admin_graphql", fake_graphql)

    summary = await module.create_shopify_discount_validation_fixtures(
        merchant_id="merch_1",
        customer_email="buyer@example.com",
        code_prefix="pivota custom prefix",
        product_id="10064558129449",
        upcoming_starts_in_minutes=3,
        upcoming_duration_minutes=11,
        api_version="2026-04",
    )

    assert summary["run_key"] == "PIVOTA_CUSTOM_PREFIX"
    assert summary["customer"]["numberOfOrders"] == 0
    assert summary["segments"]["email_domain"]["query"] == "customer_email_domain = 'example.com'"
    assert summary["segments"]["new_customer"]["query"] == "number_of_orders = 0"
    assert summary["discounts"]["fixed_amount_product"]["codes"] == ["PIVOTA_CUSTOM_PREFIX_FIXPROD60"]
    assert summary["discounts"]["usage_limit"]["usageLimit"] == 1
    assert summary["discounts"]["usage_limit"]["appliesOncePerCustomer"] is True
    assert summary["discounts"]["segment_customer"]["codes"] == ["PIVOTA_CUSTOM_PREFIX_SEGMENT"]
    assert summary["discounts"]["new_customer"]["codes"] == ["PIVOTA_CUSTOM_PREFIX_NEWCUST"]

    discount_payloads = [
        call["variables"]["basicCodeDiscount"]
        for call in calls
        if "basicCodeDiscount" in (call.get("variables") or {})
    ]
    assert discount_payloads[0]["customerGets"]["value"]["discountAmount"]["amount"] == "0.60"
    assert discount_payloads[0]["customerGets"]["value"]["discountAmount"]["appliesOnEachItem"] is True
    assert discount_payloads[0]["customerGets"]["items"] == {
        "products": {
            "productsToAdd": ["gid://shopify/Product/10064558129449"],
        }
    }
    assert discount_payloads[1]["usageLimit"] == 1
    assert discount_payloads[1]["appliesOncePerCustomer"] is True
    assert discount_payloads[3]["context"]["customerSegments"]["add"] == [summary["segments"]["email_domain"]["id"]]
    assert discount_payloads[4]["context"]["customerSegments"]["add"] == [summary["segments"]["new_customer"]["id"]]

from models.standard_product import StandardProduct


def test_standard_product_derives_visible_attributes_from_title_product_type_and_tags() -> None:
    product = StandardProduct(
        id="prod_serum_visible_1",
        platform="shopify",
        merchant_id="merch_test_1",
        title="Winona Brightening Serum for Sensitive Skin",
        product_type="Serum",
        tags=["fragrance-free", "hydrating"],
        price=29.0,
    )

    assert product.visible_attributes == {
        "product_category": ["serum"],
        "skin_concern": ["sensitive_skin", "brightening", "hydrating"],
        "formula_constraint": ["fragrance_free"],
    }


def test_standard_product_does_not_derive_visible_attributes_from_description_only() -> None:
    product = StandardProduct(
        id="prod_serum_visible_2",
        platform="shopify",
        merchant_id="merch_test_1",
        title="Winona Soothing Repair",
        description="Fragrance-free hydrating serum for sensitive skin.",
        product_type="Treatment",
        tags=["barrier"],
        price=29.0,
    )

    assert product.visible_attributes == {}

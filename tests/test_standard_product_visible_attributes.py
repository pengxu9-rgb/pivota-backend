from models.standard_product import StandardProduct, StandardProductVariant


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


def test_standard_product_hydrates_structured_ingredient_ids_without_description_inference() -> None:
    product = StandardProduct(
        id="prod_serum_visible_3",
        platform="shopify",
        merchant_id="merch_test_1",
        title="Winona Soothing Repair Serum",
        description="Niacinamide + Panthenol serum for sensitive skin.",
        product_type="Serum",
        tags=[],
        price=29.0,
        platform_metadata={
            "reviewed_ingredient_ids": ["Niacinamide", "panthenol", "niacinamide"],
        },
    )

    assert product.ingredient_ids == ["niacinamide", "panthenol"]


def test_standard_variant_derives_visible_option_and_shade_labels_from_explicit_fields() -> None:
    variant = StandardProductVariant(
        id="var_foundation_210",
        title="Shade 210 Neutral Beige / XL",
        price=32.0,
        inventory_quantity=4,
        options={
            "Shade": "210 Neutral Beige",
            "Color": "Black",
            "Size": "XL",
        },
    )

    assert "shade_210_neutral_beige" in variant.visible_option_labels
    assert "color_black" in variant.visible_option_labels
    assert "size_xl" in variant.visible_option_labels


def test_standard_product_promotes_cosmetic_color_options_to_shade_labels() -> None:
    product = StandardProduct(
        id="prod_foundation_1",
        platform="shopify",
        merchant_id="merch_test_1",
        title="Soft Focus Foundation",
        product_type="Foundation",
        price=32.0,
        variants=[
            {
                "id": "var_foundation_1",
                "title": "Black / Default",
                "price": 32.0,
                "inventory_quantity": 5,
                "options": {"Color": "Black"},
            }
        ],
    )

    assert "color_black" in product.variants[0].visible_option_labels
    assert "shade_black" in product.variants[0].visible_option_labels

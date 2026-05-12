from scripts.recover_seed_data_from_catalog_extract import (
    build_proposed_seed_data,
    find_matching_variant,
    parse_csv,
    parse_key_value_items,
)


def test_parse_cli_helpers():
    assert parse_csv("a,b\nc") == ["a", "b", "c"]
    assert parse_key_value_items(["a=https://example.test/a=b"]) == {
        "a": "https://example.test/a=b"
    }


def test_build_proposal_uses_matching_shade_variant_image_and_skips_description():
    row = {
        "id": "seed:mac",
        "external_product_id": "mac:62c89320b830814c",
        "title": "Russian Red Matte Lipstick",
        "market": "US",
        "seed_data": {
            "brand": "MAC",
            "title": "Russian Red Matte Lipstick",
            "image_urls": [],
            "variants": [],
        },
    }
    extracted = {
        "title": "M.A.CXIMAL SILKY MATTE LIPSTICK",
        "description_raw": "Light beige toned rose",
        "image_url": "https://images.example/default.png",
        "variants": [
            {
                "id": "default",
                "sku": "NY9N37",
                "url": "https://www.maccosmetics.com/product?shade=Default",
                "image_url": "https://images.example/default.png",
            },
            {
                "id": "russian-red",
                "sku": "NY9N06",
                "url": "https://www.maccosmetics.com/product?shade=Russian%20Red",
                "image_url": "https://images.example/russian-red.png",
            },
        ],
    }

    matched = find_matching_variant(
        extracted,
        "https://www.maccosmetics.com/product?shade=Russian%20Red",
    )
    assert matched["sku"] == "NY9N06"

    proposed, summary = build_proposed_seed_data(
        row=row,
        extracted_product=extracted,
        target_url="https://www.maccosmetics.com/product?shade=Russian%20Red",
        proposer="test_recovery",
        skip_description=True,
    )

    assert proposed["image_url"] == "https://images.example/russian-red.png"
    assert proposed["image_urls"] == ["https://images.example/russian-red.png"]
    assert proposed["destination_url"].endswith("shade=Russian%20Red")
    assert proposed["selected_variant_id"] == "russian-red"
    assert "description" not in proposed
    assert summary["description_chars"] == 0


def test_build_proposal_includes_clean_description_for_exact_seller_page():
    row = {
        "id": "seed:ulta",
        "external_product_id": "ulta:5311e76277c7efd9",
        "title": "Russian Red Matte Lipstick",
        "market": "US",
        "seed_data": {"brand": "MAC", "title": "Russian Red Matte Lipstick"},
    }
    extracted = {
        "title": "M.A.Cximal Silky Matte Lipstick - Russian Red",
        "description_raw": "Iconic creamy lipstick with full coverage colour.",
        "image_url": "https://images.example/ulta.png",
        "image_urls": ["https://images.example/ulta.png"],
        "url": "https://www.ulta.com/p/macximal-silky-matte-lipstick-pimprod2043558?sku=2621437",
        "variants": [{"id": "2621437", "sku": "2621437"}],
    }

    proposed, summary = build_proposed_seed_data(
        row=row,
        extracted_product=extracted,
        target_url="https://www.ulta.com/p/macximal-silky-matte-lipstick-pimprod2043558?sku=2621437",
        proposer="test_recovery",
    )

    assert proposed["description"] == "Iconic creamy lipstick with full coverage colour."
    assert proposed["pdp_description_raw"] == proposed["description"]
    assert proposed["image_urls"] == ["https://images.example/ulta.png"]
    assert summary["description_chars"] > 0

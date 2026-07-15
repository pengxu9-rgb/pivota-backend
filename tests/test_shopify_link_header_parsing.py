from adapters.product_adapters import extract_shopify_next_page_token


def test_extract_shopify_next_page_token_from_valid_link_header() -> None:
    link = (
        '<https://test-shop.myshopify.com/admin/api/2025-10/products.json'
        '?limit=250&page_info=abc123>; rel="next"'
    )
    token, has_next, parse_error = extract_shopify_next_page_token(link)
    assert token == "abc123"
    assert has_next is True
    assert parse_error is None


def test_extract_shopify_next_page_token_reports_missing_page_info() -> None:
    link = '<https://test-shop.myshopify.com/admin/api/2025-10/products.json?limit=250>; rel="next"'
    token, has_next, parse_error = extract_shopify_next_page_token(link)
    assert token is None
    assert has_next is True
    assert parse_error == "next_link_missing_page_info"


def test_extract_shopify_next_page_token_handles_empty_header() -> None:
    token, has_next, parse_error = extract_shopify_next_page_token("")
    assert token is None
    assert has_next is False
    assert parse_error is None

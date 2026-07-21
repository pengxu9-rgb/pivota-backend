def test_should_overwrite_seed_variants_allows_equal_score_when_more_variants() -> None:
    from routes.employee_products import _should_overwrite_seed_variants

    existing = [
        {"variant_id": "TEF701", "title": "100 ml", "price_amount": 165.0, "price_currency": "USD"},
    ]
    incoming = [
        {"variant_id": "TEF501", "title": "10 ml", "price_amount": 39.0, "price_currency": "USD"},
        {"variant_id": "TEF601", "title": "50 ml", "price_amount": 115.0, "price_currency": "USD"},
        {"variant_id": "TEF701", "title": "100 ml", "price_amount": 165.0, "price_currency": "USD"},
    ]

    assert _should_overwrite_seed_variants(
        existing=existing,
        incoming=incoming,
        product_title="Eau d'Ombré Leather Eau de Toilette",
    )


def test_should_overwrite_seed_variants_does_not_downgrade_titles_even_if_more_variants() -> None:
    from routes.employee_products import _should_overwrite_seed_variants

    existing = [
        {"variant_id": "TEF701", "title": "100 ml"},
    ]
    incoming = [
        {"variant_id": "TEF501", "title": "T6K501"},
        {"variant_id": "TEF601", "title": "T6K601"},
        {"variant_id": "TEF701", "title": "T6K701"},
    ]

    assert not _should_overwrite_seed_variants(
        existing=existing,
        incoming=incoming,
        product_title="Eau d'Ombré Leather Eau de Toilette",
    )


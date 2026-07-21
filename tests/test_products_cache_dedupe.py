def test_dedupe_cached_products_rows_keeps_first_seen():
    from db.products import _dedupe_cached_products_rows

    rows = [
        {"id": 10, "platform_product_id": "p1", "cached_at": "2026-01-22"},
        {"id": 9, "platform_product_id": "p1", "cached_at": "2026-01-05"},
        {"id": 8, "platform_product_id": "p2", "cached_at": "2026-01-10"},
        {"id": 7, "platform_product_id": "p2", "cached_at": "2026-01-09"},
    ]

    deduped = _dedupe_cached_products_rows(rows)

    assert [r["platform_product_id"] for r in deduped] == ["p1", "p2"]
    assert deduped[0]["id"] == 10
    assert deduped[1]["id"] == 8


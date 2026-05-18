from db.catalog import catalog_products


def test_catalog_products_declares_category_path_write_columns():
    """Catalog sync writes these fields; SQLAlchemy metadata must accept them."""
    for column_name in ("category_path", "category_confidence", "category_label_source"):
        assert column_name in catalog_products.c

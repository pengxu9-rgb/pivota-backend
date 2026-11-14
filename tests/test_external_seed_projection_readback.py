import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from services.external_seed_projection_readback import MIRROR, SOURCE, read_external_seed_projection


SEED_ID = "eps_31f74c1ec20a4d5db0fa977f"
SELLER = "merch_obs_1c4bc54448d534ca"
DESTINATION = "https://amuseseoulmakeup.com/products/banana-lip-oil"
FIRST_ID = "52547428942138"
SECOND_ID = "52547429007674"


@pytest.fixture
def scope():
    return {
        "seed_id": SEED_ID,
        "market": "US",
        "seller_ref": SELLER,
        "status": "active",
        "destination_url": DESTINATION,
        "variants": [
            {"variant_id": FIRST_ID, "barcode": "8800299133494", "price": "15.00", "currency": "USD", "available": True},
            {"variant_id": SECOND_ID, "barcode": "8800299133517", "price": "15.00", "currency": "USD", "available": False},
        ],
    }


@pytest.fixture
def rows():
    product = {
        "product_key": "expected-product",
        "source_product_id": "10117885559098",
        "canonical_url": DESTINATION,
        "seller_observed": True,
        "serving_eligible": True,
        "pdp_lifecycle_stage": "published",
        "pdp_variants": [{"merchant_id": SELLER, "variant_id": vid} for vid in (FIRST_ID, SECOND_ID)],
    }
    return [
        dict(product, sku_key="sku-first", source_variant_id=f"gid://shopify/ProductVariant/{FIRST_ID}",
             barcode="08800299133494", offer_id="offer-first", list_price=Decimal("15.00"),
             availability="in_stock", offer_destination=f"{DESTINATION}?variant={FIRST_ID}", offer_variant_id=FIRST_ID),
        dict(product, sku_key="sku-second", source_variant_id=SECOND_ID,
             barcode="8800299133517", offer_id="offer-second", list_price=Decimal("15.00"),
             availability="out_of_stock", offer_destination=DESTINATION, offer_variant_id=SECOND_ID),
    ]


@pytest.mark.parametrize("status", ["draft", "disabled"])
async def test_non_active_seed_never_reads_projection(scope, status):
    db = AsyncMock()
    result = await read_external_seed_projection(db, **dict(scope, status=status))

    db.fetch_all.assert_not_awaited()
    assert result["status"] == "not_active"
    assert [row["variant_id"] for row in result["variants"]] == [FIRST_ID, SECOND_ID]


async def test_reads_only_scoped_index_witnesses_and_keeps_out_of_stock_offer(scope, rows):
    db = AsyncMock()
    db.fetch_all.return_value = rows
    result = await read_external_seed_projection(db, **scope)

    _, query_values = db.fetch_all.await_args.args
    assert query_values["seed_id"] == SEED_ID
    assert query_values["seller_ref"] == SELLER
    assert query_values["market"] == "US"
    assert query_values["currency"] == "USD"
    assert f"gid://shopify/ProductVariant/{FIRST_ID}" in query_values["variant_aliases"]
    assert result["status"] == "projected"
    assert result["product_key"] == "expected-product"
    assert result["variants"][0] == {
        "variant_id": FIRST_ID, "gtin": "08800299133494", "sku_key": "sku-first",
        "offer_id": "offer-first", "pdp_variant_present": True,
    }
    assert result["variants"][1]["offer_id"] == "offer-second"


@pytest.mark.parametrize("change", [
    {"list_price": Decimal("15.01")},
    {"availability": "in_stock"},
    {"offer_destination": f"{DESTINATION}?variant={FIRST_ID}"},
    {"offer_destination": "https://amusemakeup.com/products/banana-lip-oil"},
    {"offer_variant_id": FIRST_ID},
    {"barcode": "8800299133494"},
])
async def test_mismatched_variant_offer_cannot_complete_projection(scope, rows, change):
    db = AsyncMock()
    db.fetch_all.return_value = [rows[0], dict(rows[1], **change)]
    result = await read_external_seed_projection(db, **scope)

    assert result["status"] == "partial"
    assert result["variants"][0]["offer_id"] == "offer-first"
    assert result["variants"][1]["offer_id"] is None


async def test_synthetic_canonical_and_other_seller_pdp_variant_do_not_count(scope, rows):
    db = AsyncMock()
    db.fetch_all.return_value = [
        dict(rows[0], pdp_variants=[{"merchant_id": "other-seller", "variant_id": FIRST_ID}]),
        dict(rows[1], source_product_id=SECOND_ID, sku_key="expected-product::canonical"),
    ]
    result = await read_external_seed_projection(db, **scope)

    assert result["status"] == "partial"
    assert result["variants"][0]["offer_id"] == "offer-first"
    assert result["variants"][0]["pdp_variant_present"] is False
    assert result["variants"][1]["sku_key"] is None


async def test_duplicate_mirror_is_ambiguous(scope, rows):
    db = AsyncMock()
    db.fetch_all.return_value = [rows[0], dict(rows[1], product_key="other-product")]
    result = await read_external_seed_projection(db, **scope)

    assert result["status"] == "scope_unverified"
    assert result["product_key"] is None


async def test_no_mirror_is_pending(scope):
    db = AsyncMock()
    db.fetch_all.return_value = []
    result = await read_external_seed_projection(db, **scope)

    assert result["status"] == "pending"
    assert not result["pdp_published"]


async def test_unknown_stock_cannot_verify_an_existing_offer(scope, rows):
    db = AsyncMock()
    db.fetch_all.return_value = rows
    del scope["variants"][0]["available"]
    result = await read_external_seed_projection(db, **scope)

    assert result["status"] == "partial"
    assert result["variants"][0]["offer_id"] is None


async def test_conflicting_native_id_is_not_a_valid_scope(scope):
    db = AsyncMock()
    scope["variants"][0]["id"] = SECOND_ID
    result = await read_external_seed_projection(db, **scope)

    db.fetch_all.assert_not_awaited()
    assert result["status"] == "scope_unverified"


async def test_postgres_readback_filters_rows_from_wrong_scope(scope):
    import databases
    from db.sql_migrations import split_statements

    dsn = os.getenv("SEED_PROJECTION_TEST_DATABASE_URL", "")
    if urlsplit(dsn).hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.skip("requires a throwaway local PostgreSQL database")
    migrations = Path(__file__).resolve().parents[1] / "db" / "migrations"
    schema = f"seed_projection_test_{os.getpid()}"
    async with databases.Database(dsn) as db, db.transaction(force_rollback=True):
        await db.execute(f"CREATE SCHEMA {schema}")
        await db.execute(f"SET LOCAL search_path TO {schema}")
        for name in (
            "058_catalog_core.sql", "085_agent_pdp_view.sql", "098_index_pipeline_state.sql",
            "132_catalog_offer_suppression_writer_audit.sql", "135_catalog_product_sku_stale_suppression.sql",
            "149_catalog_offers_agent_decision_fields.sql",
        ):
            for statement in split_statements((migrations / name).read_text()):
                statement_upper = statement.strip().upper().removesuffix(";")
                if statement_upper in {"BEGIN", "COMMIT"} or statement_upper.startswith("CREATE INDEX CONCURRENTLY"):
                    continue
                await db.execute(statement)
        await db.execute("ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS content_key VARCHAR(40)")
        await db.execute("""
            INSERT INTO catalog_products
            (product_key, merchant_id, platform, source_product_id, source_system, source_ref, title, canonical_url, content_key)
            VALUES ('product', :seller, 'external_seed', '10117885559098', :source, :seed, 'Product', :url, 'content')
        """, {"seller": SELLER, "source": MIRROR, "seed": SEED_ID, "url": DESTINATION})
        await db.execute("INSERT INTO catalog_merchants (merchant_id, status) VALUES (:seller, 'observed')", {"seller": SELLER})
        await db.execute("INSERT INTO index_pipeline_state (content_key, serving_eligible) VALUES ('content', TRUE)")
        await db.execute("""
            INSERT INTO agent_pdp_view (content_key, title, pdp_lifecycle_stage, variants)
            VALUES ('content', 'Product', 'published', CAST(:variants AS JSONB))
        """, {"variants": json.dumps([{"variant_id": vid, "merchant_id": SELLER} for vid in (FIRST_ID, SECOND_ID)])})
        for label, variant_id, barcode in (
            ("one", FIRST_ID, "8800299133494"), ("two", SECOND_ID, "8800299133517"),
            ("product::canonical", "10117885559098", "8800299133494"),
        ):
            await db.execute("""
                INSERT INTO catalog_skus
                (sku_key, product_key, merchant_id, platform, source_product_id, source_variant_id, title, barcode)
                VALUES (:sku, 'product', :seller, 'external_seed', '10117885559098', :variant, 'Variant', :barcode)
            """, {"sku": label, "seller": SELLER, "variant": variant_id, "barcode": barcode})

        await db.execute("""
            INSERT INTO catalog_offers
            (offer_id, sku_key, product_key, merchant_id, market, currency, offer_mode, source_system,
             offer_payload, availability, list_price)
            VALUES
                ('valid', 'one', 'product', :seller, 'US', 'USD', 'redirect', :source, CAST(:first AS JSONB), 'in_stock', 15),
                ('wrong-market', 'two', 'product', :seller, 'GB', 'USD', 'redirect', :source, CAST(:second AS JSONB), 'out_of_stock', 15),
                ('wrong-seller', 'two', 'product', 'other', 'US', 'USD', 'redirect', :source, CAST(:second AS JSONB), 'out_of_stock', 15),
                ('suppressed', 'two', 'product', :seller, 'US', 'USD', 'redirect', :source, CAST(:second AS JSONB), 'out_of_stock', 15)
        """, {
            "seller": SELLER, "source": SOURCE,
            "first": json.dumps({"external_seed_id": SEED_ID, "variant_id": FIRST_ID, "price_from": "variant", "destination_url": DESTINATION}),
            "second": json.dumps({"external_seed_id": SEED_ID, "variant_id": SECOND_ID, "price_from": "variant", "destination_url": DESTINATION}),
        })
        await db.execute("UPDATE catalog_offers SET suppressed_at=NOW() WHERE offer_id='suppressed'")

        result = await read_external_seed_projection(db, **scope)

    assert result["status"] == "partial"
    assert [variant["offer_id"] for variant in result["variants"]] == ["valid", None]

"""The collector must report what it read, and say so when it read nothing.

Its whole purpose is to remove the gap between a claim and a row. A default emitted for a value it
could not read would be indistinguishable from a measurement once written into the JSON — which is
the failure this file exists to prevent, one layer earlier than the validator.
"""
import json

import pytest

from scripts.collect_curated_canary_evidence import SURFACES, collect
from scripts.validate_meitu_canary_evidence import evaluate

CASE = {
    "case_id": "two_sellers",
    "accepted_brands": ["Pyunkang Yul"],
    "seller_hosts": ["eyurs.com", "ohlolly.com"],
    "market": "US",
    "currency": "USD",
    "inci_source": "reseller_listing",
    "target_gtin": "08809486681497",
    "required_category_prefix": "beauty/skincare/cleanse/",
}


class FakeConn:
    """Answers by statement shape, so the collector's real SQL text is exercised."""

    def __init__(self, products=(), skus=(), offers=(), incis=(), groups=(), group_error=None):
        self.rows = {"catalog_products": list(products), "catalog_skus": list(skus),
                     "catalog_offers": list(offers), "beauty_sku_ingredients": list(incis),
                     "product_group_members": list(groups)}
        self.group_error = group_error
        self.seen = []

    async def fetch(self, sql, *args):
        self.seen.append((sql, args))
        for table, rows in self.rows.items():
            if table in sql:
                if table == "product_group_members" and self.group_error:
                    raise self.group_error
                return rows
        raise AssertionError(f"unexpected statement: {sql[:60]}")


def product_row(host, key, **overrides):
    row = {"product_key": key, "merchant_id": f"merch_{host}", "source_domain": host,
           "market": "US", "currency": "USD", "gtin": "08809486681497",
           "category_path": "beauty/skincare/cleanse/cleanser", "content_key": "ck_shared",
           "brand": "Pyunkang Yul", "title": "Deep Clear Cleansing Balm",
           "pivota_signature_id": "sig_x"}
    row.update(overrides)
    return row


async def test_it_reports_the_stored_ingredient_row_it_found():
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a")],
        skus=[{"product_key": "ext:retailer:a", "sku_key": "ext:retailer:a::v:1", "source_variant_id": "45001",
               "sku_payload": json.dumps({"variant_id": "45001", "variant_id_provenance": "merchant_issued"})}],
        offers=[{"product_key": "ext:retailer:a", "merchant_id": "merch_eyurs.com", "currency": "USD",
                 "market": "US", "offer_type": "retailer", "offer_mode": "redirect",
                 "source_domain": "eyurs.com", "destination_url": "https://eyurs.com/products/x"}],
        incis=[{"product_key": "ext:retailer:a", "source_system": "reseller_listing", "raw_inci_chars": 868}],
        groups=[{"product_key": "ext:retailer:a", "product_group_id": "pg_1"}],
    )
    out = await collect(conn, CASE)
    product = out["products"][0]
    assert product["inci_row"] == {"present": True, "source_system": "reseller_listing", "raw_inci_chars": 868}
    assert product["inci_source"] == "reseller_listing"
    assert product["product_group_id"] == "pg_1"
    # Provenance is read out of sku_payload JSON, which arrives as TEXT from asyncpg.
    assert product["variant_id"] == "45001"
    assert product["variant_id_provenance"] == "merchant_issued"


async def test_a_product_with_no_stored_ingredients_says_so_rather_than_claiming_the_case_value():
    """The A'PIEU lip oil's real shape: seller publishes no INCI, so no row exists. The collector
    must NOT fill inci_source from the case just because the case declares one."""
    conn = FakeConn(products=[product_row("eyurs.com", "ext:retailer:a")])
    product = (await collect(conn, CASE))["products"][0]
    assert product["inci_row"] == {"present": False, "source_system": None, "raw_inci_chars": 0}
    assert product["inci_source"] is None
    assert product["variant_id"] is None and product["variant_id_provenance"] is None


async def test_live_surfaces_are_null_and_never_empty_lists():
    conn = FakeConn(products=[product_row("eyurs.com", "ext:retailer:a")])
    out = await collect(conn, CASE)
    for surface in SURFACES:
        assert out[surface] is None, f"{surface} must be null, not a measured-looking []"
        assert surface in out["evidence_provenance"]["not_collected"]
    for field in ("second_ingest_added_product_keys", "identity_failures", "crawl"):
        assert out[field] is None


async def test_collected_evidence_does_not_pass_validation_on_its_own():
    """The collector deliberately produces an INCOMPLETE file: the live-door surfaces, the crawl
    report and the second ingest are not database facts. It must not look acceptable until those
    are measured and merged."""
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a"), product_row("ohlolly.com", "ext:retailer:b")],
        incis=[{"product_key": "ext:retailer:a", "source_system": "reseller_listing", "raw_inci_chars": 868},
               {"product_key": "ext:retailer:b", "source_system": "reseller_listing", "raw_inci_chars": 869}],
    )
    out = await collect(conn, CASE)
    result = evaluate({"cases": [CASE]}, {"two_sellers": out})
    assert result["passed"] == 0
    assert any("not collected" in r for r in result["cases"][0]["reasons"])


async def test_the_target_gtin_filters_what_is_collected():
    """A host's other products are not this case's evidence, however well they converge."""
    conn = FakeConn(products=[
        product_row("eyurs.com", "ext:retailer:a"),
        product_row("eyurs.com", "ext:retailer:other", gtin="08809530070499"),
    ])
    out = await collect(conn, CASE)
    assert [p["product_key"] for p in out["products"]] == ["ext:retailer:a"]


async def test_a_missing_group_table_is_reported_not_defaulted():
    conn = FakeConn(products=[product_row("eyurs.com", "ext:retailer:a")],
                    group_error=RuntimeError('relation "product_group_members" does not exist'))
    out = await collect(conn, CASE)
    assert out["products"][0]["product_group_id"] is None
    assert any("product_group_id not collected" in note
               for note in out["evidence_provenance"]["notes"])


async def test_the_canonical_sku_stub_is_not_offered_as_a_merchant_variant():
    """source_variant_id == product_key is the canonical stub; the validator rejects such a
    variant, so reporting it as the merchant's would manufacture a passing-looking field."""
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a")],
        skus=[{"product_key": "ext:retailer:a", "sku_key": "ext:retailer:a::canonical",
               "source_variant_id": "ext:retailer:a", "sku_payload": json.dumps({})}],
    )
    product = (await collect(conn, CASE))["products"][0]
    assert product["variant_id"] is None

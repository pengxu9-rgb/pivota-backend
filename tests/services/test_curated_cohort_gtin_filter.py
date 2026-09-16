"""A case is about specific products; the vendor filter was the narrowest tool the lane had.

Measured 2026-09-16: a Pyunkang Yul run at eyurs.com selects 10 products, 7 of which carry a
merchant product_type this taxonomy does not map ("Cleansers", "Moisturizers", "Sheet Masks",
"Cotton Pads"). Those resolve to the bare root, `resolve()` nulls them, and
`inspect_primary_plan` blocks the WHOLE cohort — over products the canary never wanted. The
target cleansing balm resolves cleanly at both hosts.
"""
import pytest

from scripts.onboard_curated_brands import _select_by_gtin
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

BALM = "8809486681497"          # the case's target, 13-digit merchant spelling
BALM_14 = "08809486681497"      # the stored GTIN-14 form
OTHER = "8809486680360"         # cotton pads: unmapped product_type, blocks the cohort


def record(gtin, *, title="Pyunkang Yul Deep Clear Cleansing Balm", product_type="Cleansing Balm"):
    return feed.shopify_product_to_record(
        {"id": abs(hash(gtin)) % 10**9, "vendor": "Pyunkang Yul", "title": title,
         "handle": title.lower().replace(" ", "-"), "product_type": product_type,
         "body_html": "<p>Ingredients: Water, Glycerin</p>",
         "images": [{"src": "https://cdn.example/i.jpg"}],
         "variants": [{"id": 40825422381214, "price": "18.00", "available": True,
                       "sku": "S1", "barcode": gtin}]},
        domain="eyurs.com", category_path="beauty", brand_override="Pyunkang Yul",
        currency="USD", source_role="retailer", retailer_name="eyurs.com",
        emit_native_variants=True,
    )


def test_it_keeps_only_the_named_product():
    records = [record(BALM), record(OTHER, title="Pyunkang Yul 1/3 Cotton Pads",
                                    product_type="Cotton Pads")]
    kept = _select_by_gtin(records, [BALM], domain="eyurs.com")
    assert len(kept) == 1
    assert kept[0]["pdp"]["barcode"] in (BALM, BALM_14)


def test_thirteen_and_fourteen_digit_spellings_are_the_same_product():
    """The merchant publishes 13 digits; the stored form is 14. A filter that treated them as
    different products would silently match nothing and raise on a correct request."""
    records = [record(BALM)]
    assert len(_select_by_gtin(records, [BALM_14], domain="eyurs.com")) == 1
    assert len(_select_by_gtin(records, [BALM], domain="eyurs.com")) == 1


def test_a_filter_matching_nothing_is_an_error_not_an_empty_run():
    """Mirrors --only-vendor. An empty cohort that exits 0 is how a canary certifies products it
    never selected."""
    with pytest.raises(ValueError, match="matched none"):
        _select_by_gtin([record(BALM)], ["08809530070499"], domain="eyurs.com")


def test_an_invalid_gtin_is_refused_rather_than_matching_nothing():
    with pytest.raises(ValueError, match="valid GS1"):
        _select_by_gtin([record(BALM)], ["not-a-gtin"], domain="eyurs.com")


def test_a_record_without_a_gtin_cannot_match():
    """No barcode means no identity to match on; it must not fall through into the cohort."""
    no_gtin = record(BALM)
    no_gtin["pdp"]["barcode"] = None
    no_gtin["pdp"]["gtin"] = None
    with pytest.raises(ValueError, match="matched none"):
        _select_by_gtin([no_gtin], [BALM], domain="eyurs.com")


def test_narrowing_the_cohort_unblocks_the_plan_the_unmapped_product_was_blocking():
    """The behaviour the flag exists for, asserted end to end through the real planner."""
    from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan

    cohort = [record(BALM), record(OTHER, title="Pyunkang Yul 1/3 Cotton Pads",
                                   product_type="Cotton Pads")]
    blocked = inspect_primary_plan(ingest_validated_jsonl(cohort))
    assert blocked["status"] == "blocked" and blocked["unresolved_category_count"] == 1

    narrowed = inspect_primary_plan(ingest_validated_jsonl(
        _select_by_gtin(cohort, [BALM], domain="eyurs.com")))
    assert narrowed["unresolved_category_count"] == 0
    assert narrowed["status"] == "ready_to_apply", narrowed["reasons"]


def test_the_cli_actually_applies_the_filter(monkeypatch, capsys):
    """The tests above prove the helper works; none proved _run CALLS it. Deleting the call site
    left every one of them passing — a transform that runs is not a transform whose output is used."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from scripts import onboard_curated_brands as cli

    cohort = [record(BALM), record(OTHER, title="Pyunkang Yul 1/3 Cotton Pads",
                                   product_type="Cotton Pads")]
    fetch = AsyncMock(return_value=feed.ShopifyProductBatch(cohort, scanned_products=434, pages=2))
    fetch.last_vendor_filter_report = None
    fetch.last_brand_census = None
    fetch.last_fold_report = None
    monkeypatch.setattr(cli, "records_for_brand", fetch)

    rc = cli.main(["--domain", "eyurs.com", "--category", "beauty", "--brand", "Pyunkang Yul",
                   "--only-vendor", "Pyunkang Yul", "--source-role", "retailer",
                   "--only-gtin", BALM])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gtin filter" in out and "2 -> 1 products" in out
    assert out.count("pdp {") == 1, "the unmapped product must not reach the plan"
    assert BALM_14 in out
    assert '"status": "ready_to_apply"' in out, "the narrowed cohort must no longer be blocked"

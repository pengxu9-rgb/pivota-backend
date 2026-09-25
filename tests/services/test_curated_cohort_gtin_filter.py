"""A case is about specific products; the vendor filter was the narrowest tool the lane had.

Measured 2026-09-16: a Pyunkang Yul run at eyurs.com selects 10 products, 7 of which carry a
merchant product_type this taxonomy does not map ("Cleansers", "Moisturizers", "Sheet Masks",
"Cotton Pads"). Those resolve to the bare root, `resolve()` nulls them, and
`inspect_primary_plan` blocks the WHOLE cohort — over products the canary never wanted. The
target cleansing balm resolves cleanly at both hosts.
"""
import pytest

from scripts.onboard_curated_brands import _canonical_gtins, _select_by_gtin
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
         "variants": [{"id": 41793713995959, "price": "18.00", "available": True,
                       "sku": "S1", "barcode": gtin}]},
        domain="eyurs.com", category_path="beauty", brand_override="Pyunkang Yul",
        currency="USD", source_role="retailer", retailer_name="eyurs.com",
        emit_native_variants=True,
    )


def test_it_keeps_only_the_named_product():
    records = [record(BALM), record(OTHER, title="Pyunkang Yul 1/3 Cotton Pads",
                                    product_type="Cotton Pads")]
    kept = _select_by_gtin(records, _canonical_gtins([BALM]), domain="eyurs.com")[0]
    assert len(kept) == 1
    assert kept[0]["pdp"]["barcode"] in (BALM, BALM_14)


def test_thirteen_and_fourteen_digit_spellings_are_the_same_product():
    """The merchant publishes 13 digits; the stored form is 14. A filter that treated them as
    different products would silently match nothing and raise on a correct request."""
    records = [record(BALM)]
    assert len(_select_by_gtin(records, _canonical_gtins([BALM_14]), domain="eyurs.com")[0]) == 1
    assert len(_select_by_gtin(records, _canonical_gtins([BALM]), domain="eyurs.com")[0]) == 1


def test_a_filter_matching_nothing_is_an_error_not_an_empty_run():
    """Mirrors --only-vendor. An empty cohort that exits 0 is how a canary certifies products it
    never selected."""
    with pytest.raises(ValueError, match="matched none"):
        _select_by_gtin([record(BALM)], _canonical_gtins(["08809530070499"]), domain="eyurs.com")


def test_an_invalid_gtin_is_refused_rather_than_matching_nothing():
    with pytest.raises(ValueError, match="valid GS1"):
        _canonical_gtins(["not-a-gtin"])


def test_clearing_only_the_pdp_barcode_still_matches_via_the_variant():
    """The product still carries the GTIN — on its variant — so it is still that product."""
    pdp_only = record(BALM)
    pdp_only["pdp"]["barcode"] = None
    pdp_only["pdp"]["gtin"] = None
    kept, _ = _select_by_gtin([pdp_only], _canonical_gtins([BALM]), domain="eyurs.com")
    assert len(kept) == 1


def test_a_record_without_any_gtin_cannot_match():
    """No barcode anywhere means no identity to match on; it must not fall into the cohort."""
    no_gtin = record(BALM)
    no_gtin["pdp"]["barcode"] = None
    no_gtin["pdp"]["gtin"] = None
    for variant in (no_gtin["pdp"].get("variants") or []):
        variant["barcode"] = None
        variant.pop("gtin", None)
    with pytest.raises(ValueError, match="matched none"):
        _select_by_gtin([no_gtin], _canonical_gtins([BALM]), domain="eyurs.com")


def test_narrowing_the_cohort_unblocks_the_plan_the_unmapped_product_was_blocking():
    """The behaviour the flag exists for, asserted end to end through the real planner."""
    from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan

    cohort = [record(BALM), record(OTHER, title="Pyunkang Yul 1/3 Cotton Pads",
                                   product_type="Cotton Pads")]
    blocked = inspect_primary_plan(ingest_validated_jsonl(cohort))
    assert blocked["status"] == "blocked" and blocked["unresolved_category_count"] == 1

    narrowed = inspect_primary_plan(ingest_validated_jsonl(
        _select_by_gtin(cohort, _canonical_gtins([BALM]), domain="eyurs.com")[0]))
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


def multi_variant_record(gtin_a, gtin_b):
    """What --emit-real-variants produces: barcodes live on the VARIANTS, and the PDP carries none."""
    return feed.shopify_product_to_record(
        {"id": 991, "vendor": "Pyunkang Yul", "title": "Pyunkang Yul Essence Toner",
         "handle": "essence-toner", "product_type": "Toner",
         "body_html": "<p>Ingredients: Water</p>", "images": [{"src": "https://cdn.example/i.jpg"}],
         # Distinguishing options are required for the lane to emit native variants at all;
         # without them the record carries no variants and the test would prove nothing.
         "variants": [
             # Realistic merchant ids: variant_identity drops ids it cannot place as
             # merchant-issued rather than minting them, so a toy id yields no variants at all.
             {"id": 41679354822839, "price": "17.00", "available": True, "sku": "A",
              "option1": "100ml", "barcode": gtin_a},
             {"id": 41679262417079, "price": "23.00", "available": True, "sku": "B",
              "option1": "200ml", "barcode": gtin_b}]},
        domain="eyurs.com", category_path="beauty", brand_override="Pyunkang Yul",
        currency="USD", source_role="retailer", retailer_name="eyurs.com",
        emit_native_variants=True,
    )


def test_a_multi_variant_product_is_selectable_by_a_variant_gtin():
    """pdp.barcode is set only for a single-variant, unfolded product, so matching PDP-level
    identity alone silently refused exactly the products --emit-real-variants creates."""
    record_mv = multi_variant_record("8809486680353", "8809486680360")
    assert (record_mv["pdp"].get("barcode") or record_mv["pdp"].get("gtin")) is None, \
        "precondition: this product carries no PDP-level GTIN"

    kept, matched = _select_by_gtin([record_mv], _canonical_gtins(["8809486680360"]),
                                    domain="eyurs.com")
    assert len(kept) == 1 and matched == {"08809486680360"}


def test_every_requested_gtin_must_be_valid_even_when_a_sibling_matches():
    """Ignoring a typo because another value matched is how a run quietly selects a cohort nobody
    asked for — the failure this flag exists to prevent, in partial form."""
    with pytest.raises(ValueError, match="not a valid GS1"):
        _canonical_gtins([BALM, "not-a-gtin"])
    with pytest.raises(ValueError, match="not a valid GS1"):
        _canonical_gtins([BALM, "8809486681498"])  # bad checksum


def test_a_requested_gtin_that_matches_nothing_in_the_run_fails_the_run(monkeypatch, capsys):
    from unittest.mock import AsyncMock

    from scripts import onboard_curated_brands as cli

    fetch = AsyncMock(return_value=feed.ShopifyProductBatch([record(BALM)], scanned_products=10, pages=1))
    fetch.last_vendor_filter_report = fetch.last_brand_census = fetch.last_fold_report = None
    monkeypatch.setattr(cli, "records_for_brand", fetch)

    rc = cli.main(["--domain", "eyurs.com", "--category", "beauty", "--brand", "Pyunkang Yul",
                   "--only-vendor", "Pyunkang Yul", "--source-role", "retailer",
                   "--only-gtin", BALM, "--only-gtin", "08809530070499"])
    assert rc == 2, "a silently dropped GTIN request must fail the run"
    assert "matched no product in this run" in capsys.readouterr().err


def test_a_roster_host_matching_nothing_fails_rather_than_shrinking_the_cohort(monkeypatch, tmp_path, capsys):
    """Per-row scoping: deleting the per-row filter (one pass over all records at the end) left
    every other test green, while letting a second host contribute nothing and pass."""
    import json as _json
    from unittest.mock import AsyncMock

    from scripts import onboard_curated_brands as cli

    other = record(OTHER, title="Pyunkang Yul 1/3 Cotton Pads", product_type="Cotton Pads")
    batches = [feed.ShopifyProductBatch([record(BALM)], scanned_products=10, pages=1),
               feed.ShopifyProductBatch([other], scanned_products=10, pages=1)]
    fetch = AsyncMock(side_effect=batches)
    fetch.last_vendor_filter_report = fetch.last_brand_census = fetch.last_fold_report = None
    monkeypatch.setattr(cli, "records_for_brand", fetch)

    roster = tmp_path / "roster.jsonl"
    roster.write_text("\n".join(_json.dumps({"domain": host, "category_path": "beauty",
                                             "brand": "Pyunkang Yul"})
                                for host in ("eyurs.com", "ohlolly.com")))
    rc = cli.main(["--file", str(roster), "--only-vendor", "Pyunkang Yul",
                   "--source-role", "retailer", "--only-gtin", BALM])
    assert rc == 2
    assert "ohlolly.com: --only-gtin matched none" in capsys.readouterr().err


def test_the_default_flag_path_matches_on_the_pdp_barcode():
    """Every other fixture here passes emit_native_variants=True, so every record also carries the
    GTIN on a variant — which means the PDP-level read could be deleted with all of them still
    green. Under the lane's DEFAULT flags a single-variant record has pdp.barcode set and NO
    variants, and that read is the only thing making --only-gtin work at all."""
    default_record = feed.shopify_product_to_record(
        {"id": 771, "vendor": "Pyunkang Yul", "title": "Pyunkang Yul Deep Clear Cleansing Balm",
         "handle": "deep-clear-cleansing-balm", "product_type": "Cleansing Balm",
         "body_html": "<p>Ingredients: Water</p>", "images": [{"src": "https://cdn.example/i.jpg"}],
         "variants": [{"id": 41793713995959, "price": "18.00", "available": True,
                       "sku": "S1", "barcode": BALM}]},
        domain="eyurs.com", category_path="beauty", brand_override="Pyunkang Yul",
        currency="USD", source_role="retailer", retailer_name="eyurs.com",
    )
    assert not (default_record["pdp"].get("variants") or []), "precondition: no emitted variants"
    assert default_record["pdp"]["barcode"], "precondition: the GTIN is at PDP level only"

    kept, matched = _select_by_gtin([default_record], _canonical_gtins([BALM]), domain="eyurs.com")
    assert len(kept) == 1 and matched == {BALM_14}


def test_gtins_may_be_split_across_roster_hosts(monkeypatch, tmp_path, capsys):
    """The documented reason the unmatched check is run-wide rather than per row: one roster row
    may legitimately carry only some of the requested GTINs. Without a test, the union
    accumulation could be replaced by assignment and only produce a FALSE failure."""
    import json as _json
    from unittest.mock import AsyncMock

    from scripts import onboard_curated_brands as cli

    first = record(BALM)
    second = record(OTHER, title="Pyunkang Yul 1/3 Cotton Pads", product_type="Cotton Pads")
    fetch = AsyncMock(side_effect=[
        feed.ShopifyProductBatch([first], scanned_products=10, pages=1),
        feed.ShopifyProductBatch([second], scanned_products=10, pages=1)])
    fetch.last_vendor_filter_report = fetch.last_brand_census = fetch.last_fold_report = None
    monkeypatch.setattr(cli, "records_for_brand", fetch)

    roster = tmp_path / "roster.jsonl"
    roster.write_text("\n".join(_json.dumps({"domain": host, "category_path": "beauty",
                                             "brand": "Pyunkang Yul"})
                                for host in ("eyurs.com", "ohlolly.com")))
    rc = cli.main(["--file", str(roster), "--only-vendor", "Pyunkang Yul",
                   "--source-role", "retailer", "--only-gtin", BALM, "--only-gtin", OTHER])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert out.out.count("pdp {") == 2, "each host contributes the GTIN it carries"


def test_one_host_matching_several_gtins_reports_all_of_them():
    """Within a host, `matched |= found` vs `matched = found` differ only when ONE host matches
    more than one requested GTIN — the roster test has one per host, so it cannot see the
    difference. Overwriting would forget the earlier match and fail a correct run at the end."""
    cohort = [record(BALM), record(OTHER, title="Pyunkang Yul 1/3 Cotton Pads",
                                   product_type="Cotton Pads")]
    kept, matched = _select_by_gtin(cohort, _canonical_gtins([BALM, OTHER]), domain="eyurs.com")
    assert len(kept) == 2
    assert matched == {BALM_14, "08809486680360"}, "every GTIN this host matched must be reported"

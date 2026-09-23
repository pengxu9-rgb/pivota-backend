"""--exclude-handle: leave out one product the MERCHANT mislabels.

Measured 2026-09-23: k-touch.us types "3CE - TONE UP TINT 40ml" as `LIP TINT` while its own
description calls it a tone-up cream for the complexion. It carries no GTIN, so --only-gtin cannot
route around it, and a lip-only pass would file a face cream on the lip shelf.
"""
from unittest.mock import AsyncMock

import pytest

import scripts.onboard_curated_brands as cli
from services import curated_brand_feed as feed


def record(title, ptype, handle):
    return feed.shopify_product_to_record(
        {"id": abs(hash(handle)) % 10**9, "vendor": "3CE", "title": title, "handle": handle,
         "product_type": ptype, "body_html": "<p>x</p>", "images": [{"src": "https://cdn.example/i.jpg"}],
         "variants": [{"id": abs(hash(handle + "v")) % 10**12, "price": "20.00", "available": True, "sku": handle}]},
        domain="k-touch.us", category_path="beauty", brand_override="3CE", currency="USD",
        source_role="retailer", retailer_name="k-touch.us", emit_native_variants=True,
    )


def cohort():
    return [
        record("3CE - TONE UP TINT 40ml", "LIP TINT", "3ce-tone-up-tint-40ml"),
        record("3CE - Velvet Lip Tint Plush 4g", "LIP TINT", "3ce-velvet-lip-tint-plush-4g-11colors"),
    ]


def test_the_handle_is_read_from_the_offer_url():
    assert cli._record_handle(cohort()[0]) == "3ce-tone-up-tint-40ml"
    assert cli._record_url(cohort()[0]) == "https://k-touch.us/products/3ce-tone-up-tint-40ml"


def test_it_drops_only_the_named_product_and_prints_it(capsys):
    kept, matched = cli._exclude_by_handle(cohort(), {"3ce-tone-up-tint-40ml"}, domain="k-touch.us")
    assert [r["pdp"]["product_name"] for r in kept] == ["3CE - Velvet Lip Tint Plush 4g"]
    assert matched == {"3ce-tone-up-tint-40ml"}
    out = capsys.readouterr().out
    assert out.count(cli.EXCLUDED_PDP_PREFIX) == 1 and "TONE UP TINT" in out


def test_a_substring_is_not_a_handle():
    kept, matched = cli._exclude_by_handle(cohort(), {"3ce-tone-up"}, domain="k-touch.us")
    assert len(kept) == 2 and not matched


def _stub(monkeypatch):
    async def fetch(**_):
        return feed.ShopifyProductBatch(cohort(), scanned_products=2, pages=1)
    stub = AsyncMock(side_effect=fetch)
    stub.last_vendor_filter_report = None
    stub.last_brand_census = None
    stub.last_fold_report = None
    monkeypatch.setattr(cli, "records_for_brand", stub)


ARGV = ["--domain", "k-touch.us", "--category", "beauty", "--brand", "3CE", "--only-vendor", "3CE",
        "--source-role", "retailer", "--emit-real-variants", "--plan-print-limit", "0",
        "--only-category", "beauty/makeup/lip"]


def test_the_cli_leaves_the_mislabelled_product_out_of_the_plan(monkeypatch, capsys):
    _stub(monkeypatch)
    assert cli.main(ARGV + ["--exclude-handle", "/3CE-Tone-Up-Tint-40ml/"]) == 0
    out = capsys.readouterr().out
    planned = [line for line in out.splitlines() if line.startswith("    pdp {")]
    assert len(planned) == 1 and "Velvet Lip Tint Plush" in planned[0]
    assert "TONE UP TINT" not in "".join(planned)


def test_a_handle_that_matches_nothing_refuses_the_run(monkeypatch, capsys):
    _stub(monkeypatch)
    assert cli.main(ARGV + ["--exclude-handle", "3ce-tone-up-tint-4oml"]) == 2
    assert "matched no product in this run" in capsys.readouterr().err


def test_an_empty_handle_refuses_before_any_fetch(monkeypatch, capsys):
    _stub(monkeypatch)
    assert cli.main(ARGV + ["--exclude-handle", " / "]) == 2
    assert cli.records_for_brand.await_count == 0


def test_left_out_rows_now_carry_their_url(capsys):
    cli._select_by_category([record("3CE - New Take Eyeshadow Palette", "", "new-take")] + cohort(),
                            prefix="beauty/makeup/lip", domain="k-touch.us")
    left = [l for l in capsys.readouterr().out.splitlines() if l.startswith(cli.LEFT_OUT_PDP_PREFIX)]
    assert len(left) == 1 and "https://k-touch.us/products/new-take" in left[0]

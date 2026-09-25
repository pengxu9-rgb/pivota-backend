"""Gift-with-purchase / free-sample items are dropped at crawl time, and every drop is recorded.

The rule is EXACT tag tokens plus a few title phrases, measured on a 24,523-product census of 54 US stores
(2026-09-24): it dropped 13 priced items there, all of them gifts, and no sellable product. Both halves are
pinned here -- what must be dropped AND the look-alikes that are sellable and must be kept.
"""
from unittest.mock import AsyncMock

import pytest

from services import curated_brand_feed as cbf
from services.curated_brand_feed import gift_with_purchase_reason


def _p(title="Deluxe Baby Cheeks Blush Stick", tags=(), handle="deluxe-blush", price="26.00"):
    return {"id": 1, "title": title, "handle": handle, "vendor": "Westman Atelier", "product_type": "Blush",
            "body_html": "<p>A cream blush stick for cheeks.</p>", "tags": list(tags),
            "images": [{"src": "https://cdn.example/i.jpg"}],
            "variants": [{"id": 12345678901, "price": price, "available": True}]}


MEASURED_GIFT_TAGS = [  # spelled out, NOT read from the constant: changing the rule must change this test
    "gwp", "filter::type_gwp", "filter::type_sample", "_free_gift", "_free_gift_sample", "checkout-sample",
    "motivator_hidden_product", "gwp:brand", "gwp:sitewide", "subscription gwp", "gwp-choice", "gift with purchase",
]


def test_the_tag_set_is_exactly_the_measured_one():
    assert cbf.GIFT_WITH_PURCHASE_TAGS == frozenset(MEASURED_GIFT_TAGS)


@pytest.mark.parametrize("tag", MEASURED_GIFT_TAGS)
def test_each_measured_gift_tag_drops_the_product(tag):
    assert gift_with_purchase_reason(_p(tags=[tag])) == f"tag:{tag}"


def test_tags_match_whole_and_casefolded_whether_a_list_or_a_comma_string():
    assert gift_with_purchase_reason(_p(tags=["GWP"])) == "tag:gwp"
    assert gift_with_purchase_reason({"title": "X", "tags": "new, gwp , sale"}) == "tag:gwp"


@pytest.mark.parametrize("tag", [
    # substrings of gift tokens on PAID products (census): a substring match would drop them
    "solo_gwp_elta_pca", "rationale_4_eye_cream_gwp", "excludefromgwp:sitewide",
    # related-looking tags the census found on sellable products
    "yblocklist", "searchanise_ignore", "gorgias_do_not_recommend", "brand promo",
    "freesample", "sample", "free-gifts", "free_gifts", "sets not for sale",
])
def test_look_alike_tags_on_sellable_products_are_kept(tag):
    assert gift_with_purchase_reason(_p(title="Retinol Serum 30ml", tags=[tag])) is None


@pytest.mark.parametrize("title", [
    "[FREE GIFT] K18 Molecular Repair Hair Oil 4 ml",
    "[FREE SAMPLE] Amika NORMCORE Signature Shampoo",
    "FREE GIFT - SkinMedica Neck Correct Cream Sample",
    "FREE Gifts-$500 Spend Tier",
    "[Free Samples] Mix",
    "Blush Stick Gift with Purchase",
    "Loyalty Reward - Exclusive Beauty Luxury Cosmetic Pouch",
])
def test_gift_titles_drop_the_product(title):
    assert gift_with_purchase_reason(_p(title=title)).startswith("title:")


@pytest.mark.parametrize("title", [
    "Free Random Fragrance",        # ezenzia sells it at $19.99
    "Freedom Balm", "Gift Set Duo", "Holiday Gift Box", "Fragrance-Free Moisturizer",
    "Sample Size Mascara", "The Loyalty Serum",
    # SOLD WITH a free gift (dodoskin, available, $79-$192.60): bundles, not gifts
    "[Free Gift] 1+1 SAMJIWON Pomegranate Collagen Jelly (5 Pack) + FREE medicube Mask",
    "[Free Gift Set] beaund Nmode Pro + Booster Gel 120ml",
    "[Free Gift Set] beaund Slenway + Booster Gel 120ml",
    "Free Sample Pack",   # no dash/colon after it: not the "FREE GIFT - ..." listing shape
])
def test_sellable_titles_are_kept(title):
    assert gift_with_purchase_reason(_p(title=title)) is None


@pytest.mark.asyncio
async def test_records_for_brand_drops_gifts_and_records_each_drop(monkeypatch):
    real = _p(title="Baby Cheeks Blush Stick", handle="blush-stick")
    gift = _p(title="Deluxe Baby Cheeks Blush Stick", handle="deluxe-blush", tags=["gwp", "finalsale"])
    titled = _p(title="[FREE GIFT] Mini Lip", handle="free-mini-lip", price="15.00")

    async def fake_fetch(domain, **kw):
        return cbf.ShopifyProductBatch([real, gift, titled], scanned_products=3, pages=1)

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": "USD"}))  # no network
    records = await cbf.records_for_brand(domain="westman-atelier.com", category_path="beauty/makeup")
    assert [r["pdp"]["product_name"] for r in records] == ["Baby Cheeks Blush Stick"]
    report = records.crawl_report
    assert report["gift_items_dropped"] == 2
    assert {(d["handle"], d["reason"]) for d in report["gift_items_dropped_sample"]} == {
        ("deluxe-blush", "tag:gwp"), ("free-mini-lip", "title:[FREE GIFT]")}



@pytest.mark.parametrize("ptype", ["GWP", "gwp", "Gift With Purchase", "Gift Set", "Sample"])
def test_the_merchant_product_type_alone_never_drops_a_product(ptype):
    """`GWP`-typed items include in-stock full-size products (elizabetharden.com PREVAGE set, $169)."""
    assert gift_with_purchase_reason({"title": "PREVAGE Turn Back Time Set", "product_type": ptype, "tags": []}) is None


@pytest.mark.asyncio
async def test_gifts_are_dropped_before_gtin_recovery_and_the_sample_is_capped(monkeypatch):
    many = [_p(title=f"[FREE GIFT] Mini {i}", handle=f"gift-{i}", tags=[]) for i in range(60)]
    real = _p(title="Baby Cheeks Blush Stick", handle="blush-stick")
    seen = {}

    async def fake_fetch(domain, **kw):
        return cbf.ShopifyProductBatch([real, *many], scanned_products=61, pages=1)

    async def fake_recover(products, **kw):
        seen["handles"] = [p["handle"] for p in products]
        return products, {"attempted": 0}

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": "USD"}))  # no network
    monkeypatch.setattr(cbf, "recover_missing_variant_gtins", fake_recover)
    records = await cbf.records_for_brand(domain="westman-atelier.com", category_path="beauty/makeup",
                                          enrich_missing_gtin=True)
    assert seen["handles"] == ["blush-stick"]              # no GTIN fetch spent on a gift
    assert records.crawl_report["gift_items_dropped"] == 60
    assert len(records.crawl_report["gift_items_dropped_sample"]) == 50

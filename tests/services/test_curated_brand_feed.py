"""Tests for the curated-brand-list feed mapper (pure; no network)."""

import pytest

import services.curated_brand_feed as cbf
from services.curated_brand_feed import inci_from_pdp_html, shopify_product_to_record


def _product(**over):
    base = {
        "title": "Snail Mucin Gel Cleanser",
        "handle": "snail-mucin-gel-cleanser",
        "vendor": "COSRX",
        "product_type": "Cleanser",
        "tags": ["k-beauty", "cleanser"],
        "images": [{"src": "https://cdn.x/img.jpg"}],
        "variants": [{"price": "16.00", "barcode": "8809416470016", "available": True}],
    }
    base.update(over)
    return base


def test_maps_shopify_product_to_validated_record():
    rec = shopify_product_to_record(_product(), domain="cosrx.com", category_path="beauty/skincare/cleanser")
    pdp, offers = rec["pdp"], rec["offers"]
    assert pdp["brand"] == "COSRX"
    assert pdp["product_name"] == "Snail Mucin Gel Cleanser"
    assert pdp["category_path"] == "beauty/skincare/cleanser"
    assert pdp["barcode"] == "8809416470016"  # GTIN carried (strongest deposit basis)
    assert pdp["source_domain"] == "cosrx.com"
    assert pdp["tags"] == ["k-beauty", "cleanser"]
    assert offers[0]["canonical_url"] == "https://cosrx.com/products/snail-mucin-gel-cleanser"
    assert offers[0]["merchant_inferred"] == "COSRX"
    assert offers[0]["in_stock"] is True
    assert offers[0]["price"] == 16.0  # coerced to float (numeric columns reject strings)


def test_brand_override_and_domain_cleaning():
    rec = shopify_product_to_record(
        _product(vendor=""), domain="https://Cosrx.com/", category_path="x", brand_override="COSRX"
    )
    assert rec["pdp"]["brand"] == "COSRX"
    assert rec["offers"][0]["canonical_url"].startswith("https://cosrx.com/products/")


def test_comma_string_tags():
    rec = shopify_product_to_record(_product(tags="a, b ,c"), domain="x.com", category_path="x")
    assert rec["pdp"]["tags"] == ["a", "b", "c"]


def test_skips_unactionable():
    assert shopify_product_to_record({"handle": "h", "vendor": "V"}, domain="x.com", category_path="x") is None  # no title
    assert shopify_product_to_record({"title": "T", "vendor": "V"}, domain="x.com", category_path="x") is None  # no handle
    assert shopify_product_to_record(_product(vendor=""), domain="x.com", category_path="x") is None  # no brand
    assert shopify_product_to_record(None, domain="x.com", category_path="x") is None


def test_drops_unpriced_gift_items():
    # Gift-with-purchase / $0 / unpriced items have no purchasable offer → dropped
    # entirely so they never enter the commerce index.
    assert shopify_product_to_record(_product(variants=[{"price": None}]), domain="x.com", category_path="x") is None
    assert shopify_product_to_record(_product(variants=[{"price": "0.00"}]), domain="x.com", category_path="x") is None
    assert shopify_product_to_record(_product(variants=[]), domain="x.com", category_path="x") is None


def test_picks_first_positive_priced_variant():
    # First variant unpriced, second priced → keep the product, use the priced variant.
    rec = shopify_product_to_record(
        _product(variants=[{"price": "0.00", "barcode": "Z"}, {"price": "24.00", "barcode": "8809416470016", "available": True}]),
        domain="x.com",
        category_path="x",
    )
    assert rec is not None
    assert rec["offers"][0]["price"] == 24.0
    assert rec["pdp"]["barcode"] == "8809416470016"  # GTIN from the priced variant


# --- inci_from_pdp_html: the metafield / accordion INCI source (pure; no network)
# The cohort keeps its full INCI out of /products.json body_html; these fixtures
# mirror the real rendered-PDP surfaces confirmed 2026-07-19.

_FULL_INCI = (
    "Water, Glycerin, Niacinamide, Butylene Glycol, 1,2-Hexanediol, "
    "Sodium Hyaluronate, Panthenol, Adenosine, Dipotassium Glycyrrhizate, "
    "Phenoxyethanol, Tocopherol, Citric Acid, Disodium EDTA"
)


def test_pdp_inci_from_visible_accordion():
    # axis-y / iunik / cosrx surface: the full list is a visible <p> inside a
    # "Full Ingredients" popup/modal; a short 'key ingredients' highlight (opens
    # with an active) and marketing prose must NOT be mistaken for it.
    html = (
        "<html><body>"
        '<div class="desc"><p>A brightening toner powered by Kojic Acid and Yuzu '
        "Extract that helps visibly improve your skin tone while you cleanse.</p></div>"
        '<div class="key-ingredients"><h3>Key Ingredients</h3>'
        "<p>Kojic Acid, Yuzu Extract, Niacinamide, Adenosine</p></div>"
        '<div class="more-popup__content"><h3>Full Ingredients</h3>'
        "<p>" + _FULL_INCI + "</p></div>"
        "</body></html>"
    )
    assert inci_from_pdp_html(html) == _FULL_INCI


def test_pdp_inci_from_metafield_json_island():
    # skin1004 surface: the metafield is rendered as a rich-text JSON node inside a
    # <script> data island; the list is the `value` (joined to the product name by
    # \n\n, and after an escaped <p>). The extractor reaches into the JSON.
    value = "Poremizing Cream 50ml\\n\\n\\u003cp\\u003e" + _FULL_INCI + "\\u003c/p\\u003e"
    html = (
        "<html><body>"
        '<script type="application/json" id="ProductInfo">'
        '{"tab3":{"type":"root","children":[{"type":"paragraph","children":['
        '{"type":"text","value":"' + value + '"}]}]}}'
        "</script></body></html>"
    )
    assert inci_from_pdp_html(html) == _FULL_INCI


def test_pdp_inci_strips_leading_label():
    # A label sharing the <p> with the list (cosrx) is stripped from the write.
    html = "<html><body><p>Full Ingredients: " + _FULL_INCI + "</p></body></html>"
    assert inci_from_pdp_html(html) == _FULL_INCI


def test_pdp_no_inci_returns_none():
    # beautyofjoseon / anua surface: marketing prose (mentions water) + a link to a
    # global glossary, but NO per-product list rendered — never fabricate.
    html = (
        "<html><body>"
        '<div class="desc"><p>A gentle daily sunscreen with rice bran water and '
        "panthenol for a fresh, non-greasy finish that absorbs quickly.</p></div>"
        '<a href="/pages/ingredients">See our full ingredients glossary</a>'
        "</body></html>"
    )
    assert inci_from_pdp_html(html) is None


def test_pdp_highlight_only_returns_none():
    # Only a short 'key ingredients' highlight (opens with an active, not the
    # solvent) is published — not a full INCI, so we do NOT write it.
    html = (
        "<html><body>"
        '<div class="key-ingredients"><p>Niacinamide, Centella Asiatica Extract, '
        "Sodium Hyaluronate, Panthenol, Adenosine</p></div>"
        "</body></html>"
    )
    assert inci_from_pdp_html(html) is None


def test_pdp_bundle_two_lists_is_ambiguous_none():
    # A set/bundle page renders two full lists (both open with water). Which
    # product's is the canonical? — ambiguous, so None (never guess).
    other = "Water, Dimethicone, Squalane, Cetearyl Alcohol, Stearic Acid, Glyceryl Stearate, Carbomer, Phenoxyethanol"
    html = (
        "<html><body>"
        '<div class="comp"><h3>Full Ingredients</h3><p>' + _FULL_INCI + "</p></div>"
        '<div class="comp"><h3>Full Ingredients</h3><p>' + other + "</p></div>"
        "</body></html>"
    )
    assert inci_from_pdp_html(html) is None


def test_pdp_same_list_twice_dedupes_and_returns_one():
    # The same list rendered on two surfaces (mobile + desktop DOM) must collapse
    # to one candidate, not read as an ambiguous pair.
    html = (
        "<html><body>"
        '<div class="mobile"><p>' + _FULL_INCI + "</p></div>"
        '<div class="desktop"><p>' + _FULL_INCI + "</p></div>"
        "</body></html>"
    )
    assert inci_from_pdp_html(html) == _FULL_INCI


def test_pdp_ignores_srcset_and_empty():
    # Comma-heavy non-INCI noise (an image srcset) must never be returned, and
    # empty input is None.
    html = (
        "<html><body><img srcset=\"//cdn.x/a-1.webp 136w, //cdn.x/a-2.webp 272w, "
        "//cdn.x/a-3.webp 480w, //cdn.x/a-4.webp 720w\"></body></html>"
    )
    assert inci_from_pdp_html(html) is None
    assert inci_from_pdp_html("") is None
    assert inci_from_pdp_html(None) is None


# --- records_for_brand PDP-INCI enrichment (ingest capture; no live network) ---

def _shopify_product(handle="snail-essence", *, body_html="<p>Hydrating essence.</p>"):
    return {"handle": handle, "title": "COSRX Snail Essence", "vendor": "COSRX",
            "body_html": body_html, "images": [{"src": "https://cdn.x/i.jpg"}],
            "variants": [{"price": "25.00", "available": True}]}


@pytest.mark.asyncio
async def test_records_for_brand_enrich_off_by_default(monkeypatch):
    async def _feed(domain, *, max_products=500):
        return [_shopify_product()]
    monkeypatch.setattr(cbf, "fetch_shopify_products", _feed)

    async def _boom(*a, **k):
        raise AssertionError("PDP must NOT be fetched when enrich is off")
    monkeypatch.setattr(cbf, "fetch_pdp_inci", _boom)

    recs = await cbf.records_for_brand(domain="cosrx.com", category_path="x", brand="COSRX")
    assert recs[0]["pdp"]["raw_inci"] is None  # body_html has none; no fallback taken


@pytest.mark.asyncio
async def test_records_for_brand_enrich_fills_missing_inci_from_pdp(monkeypatch):
    full = "Water, Glycerin, Niacinamide, Butylene Glycol, 1,2-Hexanediol, Panthenol"

    async def _feed(domain, *, max_products=500):
        return [_shopify_product()]  # no INCI in body_html
    monkeypatch.setattr(cbf, "fetch_shopify_products", _feed)

    seen = []

    async def _pdp(domain, handle, *, client=None, timeout_s=15.0):
        seen.append(handle)
        return full
    monkeypatch.setattr(cbf, "fetch_pdp_inci", _pdp)

    recs = await cbf.records_for_brand(domain="cosrx.com", category_path="x", brand="COSRX",
                                       enrich_missing_inci=True, pdp_delay_s=0.0)
    assert seen == ["snail-essence"]
    assert recs[0]["pdp"]["raw_inci"] == full
    assert recs[0]["pdp"]["inci_source"] == "brand_official"


@pytest.mark.asyncio
async def test_records_for_brand_enrich_skips_when_body_html_has_inci(monkeypatch):
    # body_html INCI stays the first try; the PDP fallback is not taken for it.
    body = "<p>Ingredients: Water, Glycerin, Butylene Glycol, Panthenol, Adenosine</p>"

    async def _feed(domain, *, max_products=500):
        return [_shopify_product(body_html=body)]
    monkeypatch.setattr(cbf, "fetch_shopify_products", _feed)

    async def _boom(*a, **k):
        raise AssertionError("PDP must NOT be fetched when body_html already has INCI")
    monkeypatch.setattr(cbf, "fetch_pdp_inci", _boom)

    recs = await cbf.records_for_brand(domain="cosrx.com", category_path="x", brand="COSRX",
                                       enrich_missing_inci=True, pdp_delay_s=0.0)
    assert recs[0]["pdp"]["raw_inci"].startswith("Water, Glycerin")

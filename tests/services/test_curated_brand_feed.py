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


def test_fold_replaces_a_parent_stub_with_its_shades():
    """MAC shape: the base row is a single P2000_ placeholder whose option1 is its
    own title; the shade rows are the purchasable SKUs. The base keeps one PDP and
    the shades become its variants — the stub variant is gone, not kept beside them."""
    base = _product(title="Retro Matte Lipstick", handle="retro-matte-lipstick",
                    variants=[{"id": 1, "sku": "P2000_1", "price": "24.00", "option1": "Retro Matte Lipstick", "available": True}])
    ruby = _product(title="Retro Matte Lipstick - Ruby Woo", handle="retro-matte-lipstick-ruby-woo",
                    images=[{"src": "https://cdn.x/ruby.jpg"}],
                    variants=[{"id": 2, "sku": "M0N904", "barcode": "773602049363", "price": "24.00", "available": True}])
    bronx = _product(title="Retro Matte Lipstick - Bronx", handle="retro-matte-lipstick-bronx",
                     variants=[{"id": 3, "sku": "M0N901", "price": "24.00", "available": False}])
    out, report = cbf.fold_shade_listings([ruby, base, bronx])
    assert [p["title"] for p in out] == ["Retro Matte Lipstick"]
    vs = out[0]["variants"]
    assert [(v["title"], v["sku"]) for v in vs] == [("Ruby Woo", "M0N904"), ("Bronx", "M0N901")]
    assert vs[0]["image_src"] == "https://cdn.x/ruby.jpg"
    assert vs[0][cbf.FOLDED_FROM_KEY] == "retro-matte-lipstick-ruby-woo"
    # images_adopted 0: this base carries its own image, so the fold leaves it alone
    # (the adoption path has its own test below).
    assert report == {"bases": 1, "shades": 2, "stubs_replaced": 1, "refused": [], "images_adopted": 0,
                      "folded": {"retro-matte-lipstick": ["retro-matte-lipstick-ruby-woo", "retro-matte-lipstick-bronx"]}}
    assert out[0][cbf.FOLDED_INTO_KEY] == 2  # marks the row the mapper may emit variants for


def test_fold_keeps_a_real_base_variant_and_appends_shades():
    """A base whose single variant names a real shade is not a stub: it stays and
    the folded shades join it."""
    base = _product(title="Lip Pencil", handle="lip-pencil",
                    variants=[{"id": 1, "sku": "LP01", "price": "20.00", "option1": "Whirl", "available": True}])
    shade = _product(title="Lip Pencil - Brick-O-La", handle="lip-pencil-brick-o-la",
                     variants=[{"id": 2, "sku": "LP02", "price": "20.00", "available": True}])
    out, report = cbf.fold_shade_listings([base, shade])
    assert [v["title"] for v in out[0]["variants"]] == ["Whirl", "Brick-O-La"]
    assert report["stubs_replaced"] == 0


def _multi(**over):
    return _product(variants=[
        {"id": 11, "sku": "A", "price": "24.00", "option1": "Ruby Woo", "available": True,
         "featured_image": {"src": "https://cdn.x/a.jpg"}},
        {"id": 12, "sku": "B", "price": "0.01", "option1": "Promo", "available": True},   # under the floor
        {"id": 13, "sku": "C", "price": "24.00", "option1": "Bronx", "available": False},
    ], **over)


def test_mapper_emits_variants_only_for_a_folded_row_that_asked_for_them():
    """Writing variants costs one SKU + offer each downstream, so it is opt-in AND
    limited to rows fold_shade_listings actually folded — the two other callers of
    records_for_brand must keep emitting exactly one SKU per product."""
    assert shopify_product_to_record(_multi(), domain="x.com", category_path="x")["pdp"]["variants"] == []
    assert shopify_product_to_record(_multi(), domain="x.com", category_path="x",
                                     emit_variants=True)["pdp"]["variants"] == []   # not folded
    folded = _multi(**{cbf.FOLDED_INTO_KEY: 2})
    rec = shopify_product_to_record(folded, domain="x.com", category_path="x", emit_variants=True)
    vs = rec["pdp"]["variants"]
    assert [(v["variant_id"], v["title"], v["price"], v["in_stock"]) for v in vs] == [
        ("11", "Ruby Woo", 24.0, True), ("13", "Bronx", 24.0, False)]
    # a real Shopify variant carries its swatch in featured_image; the product image is the fallback
    assert vs[0]["image_url"] == "https://cdn.x/a.jpg" and vs[1]["image_url"] == "https://cdn.x/img.jpg"
    assert rec["offers"][0]["price"] == 24.0  # primary offer unchanged: first sellable variant


def test_fold_adopts_shade_images_when_the_base_stub_has_none():
    """maccosmetics.com: 106 of 109 folded bases have an EMPTY images list while the
    shade rows carry the swatches. The product row is what the quality scorer reads,
    so an imageless base forfeits the whole images component — MAC scored 66.7
    against the 71.4 gate and every row was blocked low_quality."""
    base = _product(title="Retro Matte Lipstick", handle="rml", images=[],
                    variants=[{"id": 1, "sku": "P2000_1", "price": "24.00", "option1": "Retro Matte Lipstick", "available": True}])
    ruby = _product(title="Retro Matte Lipstick - Ruby Woo", handle="rml-ruby",
                    images=[{"src": "https://cdn.x/ruby.jpg"}],
                    variants=[{"id": 2, "sku": "M0N904", "price": "24.00", "available": True}])
    bronx = _product(title="Retro Matte Lipstick - Bronx", handle="rml-bronx",
                     images=[{"src": "https://cdn.x/bronx.jpg"}],
                     variants=[{"id": 3, "sku": "M0N901", "price": "24.00", "available": True}])
    out, report = cbf.fold_shade_listings([base, ruby, bronx])
    assert [i["src"] for i in out[0]["images"]] == ["https://cdn.x/ruby.jpg", "https://cdn.x/bronx.jpg"]
    assert report["images_adopted"] == 1
    # ...and the record the mapper builds now names a real product image
    rec = shopify_product_to_record(out[0], domain="x.com", category_path="x", emit_variants=True)
    assert rec["offers"][0]["image_url"] == "https://cdn.x/ruby.jpg"


def test_fold_never_overwrites_a_base_that_has_its_own_images():
    base = _product(title="Lip Pencil", handle="lp", images=[{"src": "https://cdn.x/own.jpg"}],
                    variants=[{"id": 1, "sku": "LP01", "price": "20.00", "option1": "Whirl", "available": True}])
    shade = _product(title="Lip Pencil - Brick-O-La", handle="lp-b", images=[{"src": "https://cdn.x/shade.jpg"}],
                     variants=[{"id": 2, "sku": "LP02", "price": "20.00", "available": True}])
    out, report = cbf.fold_shade_listings([base, shade])
    assert [i["src"] for i in out[0]["images"]] == ["https://cdn.x/own.jpg"]
    assert report["images_adopted"] == 0


def test_mapper_falls_back_to_a_variant_image_when_the_product_has_none():
    """Safety net for any imageless product row, folded or not: a variant's own
    swatch is a real image of the product and beats publishing an imageless row."""
    rec = shopify_product_to_record(
        _product(images=[], variants=[{"id": 9, "sku": "A", "price": "24.00",
                                        "featured_image": {"src": "https://cdn.x/v.jpg"}, "available": True}]),
        domain="x.com", category_path="x",
    )
    assert rec["offers"][0]["image_url"] == "https://cdn.x/v.jpg"


def test_fold_keeps_the_shade_rows_own_option1_over_the_title_suffix():
    """stila's 'Calligraphy Lip Stain - Last Chance Shade' carries option1
    'Elizabeth (Pinky Nude)'; taking the title suffix minted a second SKU for the
    same merchant code."""
    base = _product(title="Calligraphy Lip Stain", handle="cls",
                    variants=[{"id": 1, "sku": "SE08010001", "price": "24.00", "option1": "Elizabeth (Pinky Nude)", "available": True}])
    dup = _product(title="Calligraphy Lip Stain - Some Shade", handle="cls-x",
                   variants=[{"id": 2, "sku": "SE08010002", "price": "24.00", "option1": "Elizabeth (Pinky Nude)", "available": True}])
    out, _ = cbf.fold_shade_listings([base, dup])
    assert [v["title"] for v in out[0]["variants"]] == ["Elizabeth (Pinky Nude)", "Elizabeth (Pinky Nude)"]


def test_fold_refuses_accessories_and_price_mismatches():
    """tarte sells '<line> - <X> charm' as a separate $10 accessory and stila
    suffixes '- Last Chance' onto whole palettes: folding those destroys a real PDP."""
    base = _product(title="maracuja juicy loop", handle="loop",
                    variants=[{"id": 1, "price": "6.00", "option1": "multi", "available": True}])
    charm = _product(title="maracuja juicy loop - daisy charm", handle="loop-daisy",
                     variants=[{"id": 2, "price": "10.00", "available": True}])
    palette = _product(title="Pocket Play Shadow Palette", handle="pp",
                       variants=[{"id": 3, "price": "30.00", "option1": "Default Title", "available": True}])
    last = _product(title="Pocket Play Shadow Palette - Last Chance", handle="pp-lc",
                    variants=[{"id": 4, "price": "30.00", "available": True}])
    out, report = cbf.fold_shade_listings([base, charm, palette, last])
    assert [p["handle"] for p in out] == ["loop", "loop-daisy", "pp", "pp-lc"]   # nothing folded
    assert report["bases"] == 0
    assert sorted(r["reason"] for r in report["refused"]) == ["non_shade_suffix", "non_shade_suffix"]


def test_drop_shade_listings_collapses_onto_present_base():
    """maccosmetics.com shape: every shade is its own single-variant product beside
    the base listing. Only rows whose base title is IN the feed collapse."""
    base = _product(title="Retro Matte Lipstick", handle="retro-matte-lipstick")
    ruby = _product(title="Retro Matte Lipstick - Ruby Woo", handle="retro-matte-lipstick-ruby-woo")
    bronx = _product(title="Retro Matte Lipstick - Bronx", handle="retro-matte-lipstick-bronx")
    out = cbf.drop_shade_listings([ruby, base, bronx])
    assert [p["title"] for p in out] == ["Retro Matte Lipstick"]


def test_drop_shade_listings_keeps_suffixed_title_without_base_row():
    """'Lipglass / Mini M·A·C - Nymphette' with no 'Lipglass / Mini M·A·C' row in the
    feed is a real product name, not a shade of something else in the feed."""
    lone = _product(title="Cream & Chrome Eyeliner Duo - Holiday", handle="ccd-holiday")
    other = _product(title="Amplified Lipstick", handle="amplified-lipstick")
    out = cbf.drop_shade_listings([lone, other])
    assert [p["title"] for p in out] == ["Cream & Chrome Eyeliner Duo - Holiday", "Amplified Lipstick"]


def test_drop_shade_listings_handles_hyphenated_shade_names():
    """21 of MAC's collapsible rows carry a hyphen INSIDE the shade name; a
    single `[^-]+` capture kept every one of them as a duplicate PDP."""
    base = _product(title="Retro Matte Liquid Lipcolour", handle="rmll")
    lady = _product(title="Retro Matte Liquid Lipcolour - Lady-Be-Good", handle="rmll-lady-be-good")
    pencil = _product(title="Lip Pencil", handle="lip-pencil")
    brick = _product(title="Lip Pencil - Brick-O-La", handle="lip-pencil-brick-o-la")
    out = cbf.drop_shade_listings([base, lady, pencil, brick])
    assert [p["title"] for p in out] == ["Retro Matte Liquid Lipcolour", "Lip Pencil"]


def test_drop_shade_listings_compares_normalised_titles():
    """stila: 'HUGE™ …' base vs 'Huge™ … - Intense Black' shade, and a curly vs
    straight apostrophe — the same normaliser make_content_key uses decides."""
    base = _product(title="HUGE\u2122 Extreme Lash Mascara", handle="huge")
    shade = _product(title="Huge\u2122 Extreme Lash Mascara - Intense Black", handle="huge-black")
    balm = _product(title="Heaven's Dew\u2122 Honey Glow Balm", handle="balm")
    balm_shade = _product(title="Heaven\u2019s Dew\u2122 Honey Glow Balm - Golden Sun", handle="balm-golden")
    out = cbf.drop_shade_listings([base, shade, balm, balm_shade])
    assert [p["handle"] for p in out] == ["huge", "balm"]


def test_drop_shade_listings_never_drops_multi_variant_rows():
    """A multi-variant row already carries its shades as variants; a suffixed title
    on such a row is a distinct line (tarte's 'X - travel size' style), so it stays."""
    base = _product(title="Stay All Day Liquid Lipstick", handle="sad")
    multi = _product(
        title="Stay All Day Liquid Lipstick - Shimmer",
        handle="sad-shimmer",
        variants=[{"price": "25.00", "available": True}, {"price": "25.00", "available": True}],
    )
    out = cbf.drop_shade_listings([base, multi])
    assert [p["title"] for p in out] == ["Stay All Day Liquid Lipstick", "Stay All Day Liquid Lipstick - Shimmer"]


@pytest.mark.asyncio
async def test_records_for_brand_base_listings_only_is_off_by_default_and_threads(monkeypatch):
    base = _product(title="Retro Matte Lipstick", handle="retro-matte-lipstick")
    ruby = _product(title="Retro Matte Lipstick - Ruby Woo", handle="retro-matte-lipstick-ruby-woo")

    async def fake_fetch(domain, *, max_products=500, timeout_s=15.0):
        return [base, ruby]

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    default = await cbf.records_for_brand(domain="maccosmetics.com", category_path="beauty/makeup")
    assert [r["pdp"]["product_name"] for r in default] == ["Retro Matte Lipstick", "Retro Matte Lipstick - Ruby Woo"]
    filtered = await cbf.records_for_brand(
        domain="maccosmetics.com", category_path="beauty/makeup", base_listings_only=True
    )
    assert [r["pdp"]["product_name"] for r in filtered] == ["Retro Matte Lipstick"]


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


def test_drops_token_priced_promo_items():
    # stilacosmetics.com "Free Travel … (TikTok Shop)" at $0.01 cleared the old `> 0`
    # test and served as a canonical anchor. A token price is a promo, not an offer.
    assert shopify_product_to_record(_product(variants=[{"price": "0.01"}]), domain="x.com", category_path="x") is None
    assert shopify_product_to_record(_product(variants=[{"price": "0.99"}]), domain="x.com", category_path="x") is None
    # Exactly the floor is sellable.
    rec = shopify_product_to_record(_product(variants=[{"price": "1.00"}]), domain="x.com", category_path="x")
    assert rec is not None and rec["offers"][0]["price"] == 1.0


def test_price_floor_skips_to_the_first_real_variant():
    rec = shopify_product_to_record(
        _product(variants=[{"price": "0.01", "barcode": "PROMO"}, {"price": "15.00", "barcode": "8809416470016"}]),
        domain="x.com",
        category_path="x",
    )
    assert rec["offers"][0]["price"] == 15.0
    assert rec["pdp"]["barcode"] == "8809416470016"


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


# --- Under-capture fixes (each mirrors a real 2026-07-19 static-HTML MISS shape
# that inci_from_pdp_html wrongly returned None on despite the main product's INCI
# being present) — plus the precision guards that MUST still decline. -----------

# A Centella serum whose full INCI opens with the extract, not water (centellian24
# vital-capsule-serum). Water is #4; the list must still be captured.
_EXTRACT_OPENER_INCI = (
    "Centella Asiatica Extract, Glycerin, Butylene Glycol, Water, Ceteareth-20, "
    "Niacinamide, Propanediol, Hydrogenated Lecithin, 1,2-Hexanediol, Pentylene "
    "Glycol, Panthenol, Asiaticoside, Madecassic Acid, Adenosine, Xanthan Gum, "
    "Disodium EDTA, Phenoxyethanol"
)

# An all-caps sunscreen-cushion list (misshaus glow-layering-fit-cushion).
_ALLCAPS_INCI = (
    "WATER, ZINC OXIDE, TRIETHOXYCAPRYLYLSILANE, HOMOSALATE, DIMETHICONE, "
    "ETHYLHEXYL SALICYLATE, GLYCERIN, PHENYL TRIMETHICONE, TITANIUM DIOXIDE, "
    "STEARIC ACID, ALUMINA, TOCOPHEROL, NIACINAMIDE, ADENOSINE, 1,2-HEXANEDIOL, "
    "SODIUM HYALURONATE, BUTYLENE GLYCOL, ETHYLHEXYLGLYCERIN, PHENOXYETHANOL"
)


def test_pdp_inci_extract_opener_captured():
    # MISS shape #1 (centellian24): a real full INCI whose highest-concentration
    # ingredient is the hero extract, not water. The solvent-opener requirement used
    # to reject it; the length + water-present secondary path now captures it.
    html = "<html><body><div class='rte'><p><span class='metafield-multi_line_text_field'>" + _EXTRACT_OPENER_INCI + "</span></p></div></body></html>"
    assert inci_from_pdp_html(html) == _EXTRACT_OPENER_INCI


def test_pdp_inci_all_caps_captured():
    # MISS shape #2 (misshaus): an ALL-CAPS list behind a <strong>NAME</strong><br>
    # heading. The <br> line-break splits the name off; case is folded by the gate.
    html = (
        "<html><body><div class='details-content'><div>"
        "<p><strong>GLOW LAYERING FIT CUSHION (NO.17 IVORY)</strong><br>"
        + _ALLCAPS_INCI + "</p></div></div></body></html>"
    )
    assert inci_from_pdp_html(html) == _ALLCAPS_INCI


def test_pdp_inci_bracket_label_and_dual_solvent_opener_captured():
    # MISS shape #3 (barr-cosmetics): a bracketed "[INGREDIENTS]" heading joined to
    # the list inside one JSON-island string, and a "Water/Aqua" dual-name opener.
    # The label (incl. brackets) is stripped; "Water/Aqua" still reads as the solvent.
    inci = (
        "Water/Aqua, Dibutyl Adipate, Propanediol, Polymethylsilsesquioxane, "
        "Diethylamino Hydroxybenzoyl Hexyl Benzoate, Ethylhexyl Triazone, "
        "Niacinamide, Coco-Caprylate/Caprate, Caprylyl Methicone, Glycerin, "
        "Butylene Glycol, Tocopherol, Phenoxyethanol"
    )
    # Rendered as an escaped-HTML metafield string in a data island (h4 label + p).
    value = "\\u003ch4\\u003e[INGREDIENTS]\\u003c/h4\\u003e\\u003cp\\u003e" + inci + "\\u003c/p\\u003e"
    html = (
        "<html><body><script type='application/json' id='ProductJson'>"
        '{"metafield":{"type":"text","value":"' + value + '"}}'
        "</script></body></html>"
    )
    assert inci_from_pdp_html(html) == inci


def test_pdp_inci_br_wrapped_list_reassembled_captured():
    # MISS shape #4 (dasique melting-candy-balm): an anhydrous balm whose single
    # list is wrapped across many <br> tags (even mid-ingredient-name). The join-<br>
    # pass reassembles it whole; the non-water opener (a wax) is fine because the
    # list is long and contains water.
    reassembled = (
        "Hydrogenated Polyisobutene, Dipentaerythrityl Tetrahydroxystearate/"
        "Tetraisostearate, Ethylhexyl Hydroxystearate, Synthetic Wax, Paraffin, "
        "Argania Spinosa Kernel Oil, Simmondsia Chinensis (Jojoba) Seed Oil, "
        "Microcrystalline Wax, Cera Microcristallina (EU), Polyglyceryl-2 "
        "Triisostearate, Water, Butylene Glycol, Ethylhexylglycerin, "
        "Phenoxyethanol, Titanium Dioxide (CI 77891), Red 7 Lake (CI 15850)"
    )
    br = (
        "Hydrogenated Polyisobutene,<br/>Dipentaerythrityl Tetrahydroxystearate/"
        "Tetraisostearate, Ethylhexyl<br/>Hydroxystearate, Synthetic Wax, Paraffin,"
        "<br/>Argania Spinosa Kernel Oil, Simmondsia Chinensis (Jojoba) Seed Oil,"
        "<br/>Microcrystalline Wax, Cera Microcristallina (EU),<br/>Polyglyceryl-2 "
        "Triisostearate, Water, Butylene Glycol, Ethylhexylglycerin, Phenoxyethanol, "
        "Titanium Dioxide (CI 77891), Red 7 Lake (CI 15850)"
    )
    html = "<html><body><div class='accordion__content rte'><p><strong>11 Cotton Candy</strong></p><p>" + br + "</p></div></body></html>"
    assert inci_from_pdp_html(html) == reassembled


def test_pdp_shade_variants_collapse_to_one():
    # A cushion/balm PDP renders several shades whose lists are the SAME formula,
    # reordered / differing only in the pigment tail. That is one product, not a
    # bundle — collapse to the longest complete list, don't read as ambiguous.
    shade_a = _ALLCAPS_INCI + ", IRON OXIDES (CI 77492)"
    shade_b = _ALLCAPS_INCI + ", IRON OXIDES (CI 77491), MICA"
    html = (
        "<html><body>"
        "<div class='shade'><p>" + shade_a + "</p></div>"
        "<div class='shade'><p>" + shade_b + "</p></div>"
        "</body></html>"
    )
    assert inci_from_pdp_html(html) == shade_b  # longest, a real complete shade list


def test_pdp_tag_array_stays_none():
    # PRECISION GUARD (misshaus super-aqua kit): a marketing-tag JSON array
    # (`"Aqua","Women's Day Sale!","xml-pricing-feed", ...`) is NOT an INCI. Each tag
    # is its own JSON string literal, so no comma-joined ingredient candidate ever
    # forms; the only comma-bearing aqua string is prose that fails the list gate.
    html = (
        "<html><body>"
        "<script type='application/json'>"
        '{"tags":["Aqua","Women\'s Day Sale!","xml-pricing-feed","Winter Routine","hydration"]}'
        "</script>"
        "<div class='desc'><p>Experience ultimate hydration with our NEW Super Aqua "
        "Hydrating Kit! Here's to your new skincare routine, made for you.</p></div>"
        "</body></html>"
    )
    assert inci_from_pdp_html(html) is None


def test_pdp_neighbor_product_list_not_attributed():
    # PRECISION GUARD: the page's own list PLUS a recommended/related product's list
    # (a different formula) appear in the HTML. They are largely disjoint, so which
    # is the page product's is ambiguous — return None, never attribute a neighbor's.
    own = _ALLCAPS_INCI
    neighbor = (
        "Water, Sodium Cocoyl Isethionate, Cocamidopropyl Betaine, Glycerin, "
        "Sodium Chloride, Citric Acid, Houttuynia Cordata Extract, Salicylic Acid, "
        "Menthol, Sodium Benzoate, Disodium EDTA, Fragrance, Phenoxyethanol"
    )
    html = (
        "<html><body>"
        "<div class='product-main'><p>" + own + "</p></div>"
        "<div class='recommended'><script type='application/json'>"
        '{"related":{"handle":"heartleaf-low-ph-deep-cleansing","ingredients":'
        '{"type":"text","value":"' + neighbor + '"}}}'
        "</script></div>"
        "</body></html>"
    )
    assert inci_from_pdp_html(html) is None


# --- Adversarial precision guards (the recall recovery must NOT fabricate) ------
# A large shared aqueous base makes two GENUINELY DIFFERENT same-line K-beauty
# products (a Centella toner vs a Vitamin C serum) measure ingredient-set Jaccard
# ~0.73 — far above the 0.7 the first cut of this fix collapsed on. They must stay
# ambiguous. Base is 15 ingredients; each product adds 3 distinct actives.
_SHARED_BASE_15 = (
    "Water, Glycerin, Butylene Glycol, 1,2-Hexanediol, Niacinamide, Panthenol, "
    "Sodium Hyaluronate, Betaine, Allantoin, Carbomer, Tromethamine, Phenoxyethanol, "
    "Ethylhexylglycerin, Disodium EDTA, Xanthan Gum"
)
_CENTELLA_TONER = _SHARED_BASE_15 + ", Centella Asiatica Extract, Madecassoside, Asiaticoside"
_VITC_SERUM = _SHARED_BASE_15 + ", Ascorbic Acid, Ferulic Acid, Sodium Ascorbyl Phosphate"


def test_pdp_high_overlap_different_products_stay_none():
    # PRECISION GUARD (Blocker 1): two DIFFERENT products sharing a big aqueous base
    # (Jaccard ~0.73, same solvent opener, equal length) must NOT collapse — a 0.7
    # floor mis-attributed one to the other. The raised 0.85 floor keeps them apart.
    from services.curated_brand_feed import _pdp_inci_similarity
    assert 0.70 <= _pdp_inci_similarity(_CENTELLA_TONER, _VITC_SERUM) <= 0.75
    html = (
        "<html><body>"
        "<div class='a'><p>" + _CENTELLA_TONER + "</p></div>"
        "<div class='b'><p>" + _VITC_SERUM + "</p></div>"
        "</body></html>"
    )
    assert inci_from_pdp_html(html) is None


def test_pdp_neighbor_in_recommendations_island_at_high_overlap_stays_none():
    # PRECISION GUARD (Blocker 1, island path): the page's own list plus a NEIGHBOR's
    # LONGER list in a product-recommendations JSON island, at ~0.73 overlap. Picking
    # the longest would PUBLISH the neighbor's Ascorbic/Ferulic Acid for this page —
    # must stay None.
    html = (
        "<html><body>"
        "<div class='product-main'><p>" + _CENTELLA_TONER + "</p></div>"
        "<div class='recommendations'><script type='application/json'>"
        '{"rec":{"handle":"vitamin-c-serum","ingredients":{"type":"text","value":"'
        + _VITC_SERUM + '"}}}'
        "</script></div>"
        "</body></html>"
    )
    assert inci_from_pdp_html(html) is None


def test_pdp_br_adjacent_two_products_no_franken():
    # PRECISION GUARD (Blocker 2): two products' lists sit in ONE block-level element
    # separated by a bare <br> (a kit/routine metafield). The join-<br> pass would
    # concatenate them into one franken list (opens with A's Water, also carries B's
    # Mica/Iron Oxides); the two-solvent-opener guard rejects that, and the default
    # pass sees the two lists separately -> ambiguous -> None. No franken is published.
    list_a = (
        "Water, Glycerin, Niacinamide, Butylene Glycol, 1,2-Hexanediol, Panthenol, "
        "Adenosine, Sodium Hyaluronate, Carbomer, Phenoxyethanol, Tocopherol, Disodium EDTA"
    )
    list_b = (
        "Water, Dimethicone, Mica, Titanium Dioxide, Iron Oxides, Talc, Zinc Stearate, "
        "Caprylyl Glycol, Phenoxyethanol, Ethylhexylglycerin, Silica, Boron Nitride"
    )
    html = "<html><body><div class='kit-meta'>" + list_a + "<br>" + list_b + "</div></body></html>"
    assert inci_from_pdp_html(html) is None


def test_pdp_two_solvent_openers_rejected():
    # A single string carrying TWO water-opener runs is two lists concatenated (never
    # a real INCI, which has exactly one solvent entry) -> None even as a lone block.
    two = (
        "Water, Glycerin, Niacinamide, Panthenol, Adenosine, Carbomer, Phenoxyethanol, "
        "Water, Dimethicone, Mica, Iron Oxides, Talc, Silica, Ethylhexylglycerin"
    )
    html = "<html><body><p>" + two + "</p></body></html>"
    assert inci_from_pdp_html(html) is None


def test_pdp_shade_variant_captured_alongside_guards():
    # Sanity: the shade-collapse the guards are tightening must still FIRE for real
    # variants — near-identical set, same opener, near-identical length -> the longest.
    shade_a = _ALLCAPS_INCI + ", IRON OXIDES (CI 77492)"
    shade_b = _ALLCAPS_INCI + ", IRON OXIDES (CI 77491), MICA"
    html = "<html><body><div><p>" + shade_a + "</p></div><div><p>" + shade_b + "</p></div></body></html>"
    assert inci_from_pdp_html(html) == shade_b


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



def _clear_locale_cache():
    """`storefront_currency` caches per domain for the PROCESS lifetime, negatives included.

    Without this, the first test to record a None for a host fixes that answer for every later
    test -- and the assertions would then be measuring the cache, not the code.
    """
    from services import storefront_currency

    storefront_currency.clear_cache()


def _silence_politeness(monkeypatch):
    """Neutralise the shared crawl politeness gate for a unit test.

    It issues its own robots.txt GET through whatever httpx client is installed, so a stubbed
    client answers robots.txt with the meta.json body and the function under test never gets its
    turn. Stubbed rather than worked around so these tests fail for meta.json reasons only.
    """
    async def _before(*a, **kw):
        return None

    monkeypatch.setattr(cbf.crawl_politeness, "before_request", _before)
    monkeypatch.setattr(cbf.crawl_politeness, "note_response", lambda *a, **kw: None)



# -- storefront currency / market ---------------------------------------------------------------


def test_the_record_carries_the_storefronts_currency_and_market():
    """`/products.json` carries prices but never the currency they are in, so every record this
    module produced was currency-less and the ingest lane stamped USD on all of them. Measured
    2026-09-06 on jsmbeauty.sg: 170 offers, all USD, against a storefront whose /meta.json says
    SGD/SG and whose LIP-PRESSION Glowy Tint is SGD 30.00."""
    rec = shopify_product_to_record(_product(), domain="jsmbeauty.sg",
                                    category_path="beauty/makeup/lip",
                                    currency="SGD", market="SG")

    assert rec["pdp"]["currency"] == "SGD"
    assert rec["pdp"]["market"] == "SG"


def test_a_record_from_a_storefront_we_could_not_read_carries_no_currency():
    """None, NOT a USD default. The ingest lane owns the fallback, and defaulting here would make
    a storefront we failed to read indistinguishable from one that genuinely sells in USD --
    which is the difference between a known fact and a guess in a currency column."""
    rec = shopify_product_to_record(_product(), domain="x.com", category_path="x")

    assert rec["pdp"]["currency"] is None
    assert rec["pdp"]["market"] is None


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"currency": "SGD", "country": "SG"}, {"currency": "SGD", "country": "SG"}),
        ({"currency": "sgd", "country": "sg"}, {"currency": "SGD", "country": "SG"}),
        ({"currency": "USD", "country": "US"}, {"currency": "USD", "country": "US"}),
        # merchant-controlled: anything not ISO-shaped is refused, not written through
        # An unparseable currency invalidates the WHOLE record, country included:
        # `storefront_currency` returns None rather than half an answer, because it "returns None
        # when it cannot prove the answer". Asserted as its behaviour, not worked around.
        ({"currency": "dollars", "country": "SG"}, {"currency": None, "country": None}),
        ({"currency": "SGD", "country": "SGP"}, {"currency": "SGD", "country": None}),
        ({"currency": 5, "country": None}, {"currency": None, "country": None}),
        ({}, {"currency": None, "country": None}),
        ([], {"currency": None, "country": None}),
    ],
)
@pytest.mark.asyncio
async def test_meta_json_is_validated_before_it_is_believed(monkeypatch, body, expected):
    """The value lands in a currency column that is a join key for price comparison, and it comes
    from the merchant. Shape-check it at the door."""
    import httpx

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        # TEXT, not .json(). `fetch_storefront_meta`'s injected-fetch seam consumes the response
        # BODY as a string and parses it itself, so a double exposing only .json() returns None
        # for every case and the parametrisation silently tests nothing.
        @property
        def text(self):
            import json as _j

            return _j.dumps(body)

        def json(self):
            return body

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    # The politeness gate fetches robots.txt through this same client, so without stubbing it the
    # stub answers robots.txt with JSON and the function under test never runs. It has its own
    # tests; this one is about the meta.json contract.
    _silence_politeness(monkeypatch)
    _clear_locale_cache()
    assert await cbf.fetch_shopify_shop_locale("jsmbeauty.sg") == expected


@pytest.mark.asyncio
async def test_an_unreadable_meta_json_is_best_effort_not_an_exception(monkeypatch):
    """A non-Shopify host, a 404 or a hung socket must not fail the whole brand's ingest -- the
    caller keeps its default and the crawl proceeds."""
    import httpx

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Boom())
    _silence_politeness(monkeypatch)
    _clear_locale_cache()
    assert await cbf.fetch_shopify_shop_locale("x.com") == {"currency": None, "country": None}


@pytest.mark.asyncio
async def test_records_for_brand_wires_the_locale_into_every_record(monkeypatch):
    """THE SEAM. `fetch_shopify_shop_locale` can be perfect and `shopify_product_to_record` can
    stamp perfectly, and the feature is still dead in production if the caller never passes one
    to the other. Deleting that one argument left both unit files green -- the same shape as the
    whitelist drop on the ingest side, where every consumer read a field nobody ever set.

    Stubs both fetches so this asserts the WIRING and nothing else.
    """
    async def _products(domain, **kw):
        return [_product()]

    async def _locale(domain, **kw):
        return {"currency": "SGD", "country": "SG"}

    monkeypatch.setattr(cbf, "fetch_shopify_products", _products)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", _locale)

    recs = await cbf.records_for_brand(domain="jsmbeauty.sg", category_path="beauty/makeup/lip")

    assert recs, "the stub returned a product, so a record must come back"
    assert {r["pdp"]["currency"] for r in recs} == {"SGD"}
    assert {r["pdp"]["market"] for r in recs} == {"SG"}


@pytest.mark.asyncio
async def test_records_for_brand_reads_the_locale_once_per_brand_not_once_per_product(monkeypatch):
    """It is one storefront-wide setting. A per-product fetch would multiply outbound requests by
    the catalogue size against a single host -- 170 extra requests for jsmbeauty.sg alone -- which
    is exactly the shape the shared politeness gate exists to prevent."""
    calls = []

    async def _products(domain, **kw):
        return [_product(), _product(handle="second", title="Second Product")]

    async def _locale(domain, **kw):
        calls.append(domain)
        return {"currency": "SGD", "country": "SG"}

    monkeypatch.setattr(cbf, "fetch_shopify_products", _products)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", _locale)

    recs = await cbf.records_for_brand(domain="jsmbeauty.sg", category_path="beauty/makeup/lip")

    assert len(recs) >= 2, "two products in, so the per-product count is meaningful"
    assert calls == ["jsmbeauty.sg"], f"locale fetched {len(calls)} times for one brand"


@pytest.mark.parametrize(
    "status,ctype,label",
    [
        (404, "application/json", "a 404 body"),
        (500, "application/json", "an error page"),
        (200, "text/html", "an HTML soft-404"),
        (200, "", "a response with no content-type"),
    ],
)
@pytest.mark.asyncio
async def test_meta_json_refuses_anything_that_is_not_a_json_200(monkeypatch, status, ctype, label):
    """Both gates matter and neither was pinned. Storefronts that are not Shopify commonly answer
    /meta.json with a 200 HTML soft-404, and `resp.json()` on that either raises or -- worse --
    parses embedded JSON. Refuse on the status AND on the content-type, so the caller keeps its
    default instead of inheriting a currency from someone's error page."""
    import httpx

    class _Resp:
        status_code = status
        headers = {"content-type": ctype}

        @property
        def text(self):
            return '{"currency": "XXX", "country": "ZZ"}'

        def json(self):
            return {"currency": "XXX", "country": "ZZ"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    _silence_politeness(monkeypatch)
    _clear_locale_cache()

    assert await cbf.fetch_shopify_shop_locale("x.com") == {"currency": None, "country": None}, label

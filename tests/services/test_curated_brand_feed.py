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

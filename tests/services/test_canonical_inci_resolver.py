from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import json

from services.canonical_inci_resolver import (
    ResolvedInci,
    extract_inci_from_openbeautyfacts,
    extract_inci_from_shopify_json,
    extract_inci_from_text,
    open_beauty_facts_url,
    resolve_inci_from_openbeautyfacts,
    resolve_inci_from_url,
    resolve_inci_from_urls,
    shopify_product_json_url,
)


# --- extraction (the deterministic core) --------------------------------------

def test_extracts_from_plain_text_and_stops_at_next_section():
    text = (
        "A glow-boosting serum for dull skin. "
        "Ingredients: Aqua, Niacinamide, Glycerin, Butylene Glycol, Phenoxyethanol. "
        "How to use: apply morning and night."
    )
    inci = extract_inci_from_text(text)
    assert inci == "Aqua, Niacinamide, Glycerin, Butylene Glycol, Phenoxyethanol"


def test_extracts_from_html():
    html = (
        "<div><h2>Ingredients:</h2><p>Aqua, Niacinamide, Sodium Hyaluronate, "
        "Glycerin, Phenoxyethanol</p></div><div><h2>Directions</h2><p>Apply daily.</p></div>"
    )
    inci = extract_inci_from_text(html)
    assert inci == "Aqua, Niacinamide, Sodium Hyaluronate, Glycerin, Phenoxyethanol"


def test_full_ingredients_label_variant():
    inci = extract_inci_from_text("Full Ingredients: Water, Glycerin, Niacinamide, Panthenol, Carbomer")
    assert inci is not None and "Niacinamide" in inci


def test_no_ingredients_label_returns_none():
    assert extract_inci_from_text("This serum brightens and deeply hydrates the skin.") is None


def test_bare_ingredients_header_accordion_with_trailing_ui():
    # Shopify accordion: a bare "INGREDIENTS" header (no colon), the list, then
    # page/UI text -- the run must stop at the UI text.
    html = (
        "<button class='accordion'>INGREDIENTS</button>"
        "<div class='rte'>Aqua, Niacinamide, Glycerin, Sodium Hyaluronate, Phenoxyethanol</div>"
        "<button>Add to cart</button><div>Reviews</div>"
    )
    inci = extract_inci_from_text(html)
    assert inci == "Aqua, Niacinamide, Glycerin, Sodium Hyaluronate, Phenoxyethanol"


def test_inci_substring_inside_word_not_matched():
    # "inci" inside "principal" / "incision" must not trigger a match.
    assert extract_inci_from_text("Our principal incision techniques are gentle and safe.") is None


def test_skips_nav_ingredients_link_finds_real_list():
    html = (
        "<nav><a href='/pages/ingredients'>Ingredients</a> Shop About</nav>"
        "<section>Ingredients: Water, Glycerin, Niacinamide, Panthenol, Carbomer</section>"
    )
    assert "Niacinamide" in extract_inci_from_text(html)


def test_label_followed_by_prose_is_rejected():
    # "Ingredients:" but the body is marketing prose, not a list.
    assert extract_inci_from_text("Ingredients: sourced ethically from the finest Korean botanicals") is None


def test_list_without_inci_anchor_is_rejected():
    # comma-separated, but none look like real INCI -> not an ingredient list.
    assert extract_inci_from_text("Ingredients: love, care, science, nature, passion") is None


def test_empty_input():
    assert extract_inci_from_text(None) is None
    assert extract_inci_from_text("") is None


# --- resolve over candidate sources -------------------------------------------

def _fetch(mapping):
    async def _f(url):
        v = mapping.get(url)
        if isinstance(v, Exception):
            raise v
        return v
    return _f


def test_resolve_returns_first_valid_with_provenance():
    fetch = _fetch({
        "https://brand.example/pdp": "Ingredients: Aqua, Niacinamide, Glycerin, Phenoxyethanol",
    })
    res = asyncio.run(resolve_inci_from_urls(["https://brand.example/pdp"], fetch=fetch))
    assert isinstance(res, ResolvedInci)
    assert res.source_url == "https://brand.example/pdp"
    assert "Niacinamide" in res.raw_inci


def test_resolve_skips_dead_and_inci_less_sources():
    fetch = _fetch({
        "https://dead": RuntimeError("timeout"),         # raises -> skip
        "https://noinci": "Great product, no ingredients listed here.",  # no INCI -> skip
        "https://good": "Ingredients: Water, Glycerin, Niacinamide, Panthenol, Carbomer",
    })
    res = asyncio.run(
        resolve_inci_from_urls(["https://dead", "https://noinci", "https://good"], fetch=fetch)
    )
    assert res is not None and res.source_url == "https://good"


def test_resolve_returns_none_when_no_source_has_inci():
    fetch = _fetch({"https://a": "no list", "https://b": None})
    assert asyncio.run(resolve_inci_from_urls(["https://a", "https://b"], fetch=fetch)) is None


# --- Open Beauty Facts structured adapter -------------------------------------

def test_extract_inci_from_obf_json():
    body = json.dumps({
        "status": 1,
        "product": {
            "product_name": "Niacinamide Serum",
            "ingredients_text": "Aqua, Niacinamide, Glycerin, Panthenol, Phenoxyethanol",
        },
    })
    inci = extract_inci_from_openbeautyfacts(body)
    assert inci is not None and "Niacinamide" in inci


def test_obf_prefers_english_text_and_rejects_junk():
    body = json.dumps({"product": {"ingredients_text_en": "Water, Glycerin, Niacinamide, Carbomer, Parfum",
                                    "ingredients_text": "garbage"}})
    assert "Niacinamide" in extract_inci_from_openbeautyfacts(body)
    # no product / not-found OBF response -> None
    assert extract_inci_from_openbeautyfacts(json.dumps({"status": 0})) is None
    assert extract_inci_from_openbeautyfacts("not json") is None


# --- Shopify product-JSON adapter ---------------------------------------------

def test_shopify_product_json_url():
    assert shopify_product_json_url("https://brand.com/products/glow-serum?variant=1") == \
        "https://brand.com/products/glow-serum.json"
    assert shopify_product_json_url("https://brand.com/collections/all/products/handle") == \
        "https://brand.com/products/handle.json"
    assert shopify_product_json_url("https://brand.com/products/handle.json") == \
        "https://brand.com/products/handle.json"
    assert shopify_product_json_url("https://brand.com/pages/about") is None
    assert shopify_product_json_url(None) is None


def test_extract_inci_from_shopify_json_body_html():
    body = json.dumps({"product": {
        "title": "Glow Serum",
        "body_html": "<p>A glow serum.</p><p>Ingredients: Aqua, Niacinamide, Glycerin, Phenoxyethanol</p>",
    }})
    inci = extract_inci_from_shopify_json(body)
    assert inci is not None and "Niacinamide" in inci
    assert extract_inci_from_shopify_json(json.dumps({"product": {"body_html": "no list"}})) is None
    assert extract_inci_from_shopify_json("not json") is None


def test_resolve_from_url_prefers_shopify_json_over_rendered_page():
    json_url = "https://brand.com/products/glow.json"
    fetch = _fetch({
        json_url: json.dumps({"product": {"body_html": "Ingredients: Aqua, Niacinamide, Glycerin, Carbomer"}}),
        "https://brand.com/products/glow": "JS shell, no ingredients here",  # rendered page lacks INCI
    })
    res = asyncio.run(resolve_inci_from_url("https://brand.com/products/glow", fetch=fetch))
    assert res is not None and res.source_url == json_url  # used the .json endpoint
    assert "Niacinamide" in res.raw_inci


def test_resolve_from_url_falls_back_to_html_for_non_shopify():
    fetch = _fetch({"https://brand.com/p/123": "Ingredients: Water, Glycerin, Niacinamide, Panthenol, Carbomer"})
    res = asyncio.run(resolve_inci_from_url("https://brand.com/p/123", fetch=fetch))
    assert res is not None and res.source_url == "https://brand.com/p/123"


def test_resolve_from_openbeautyfacts_by_barcode():
    url = open_beauty_facts_url("8809123456789")
    fetch = _fetch({url: json.dumps({"product": {"ingredients_text": "Aqua, Niacinamide, Glycerin, Phenoxyethanol"}})})
    res = asyncio.run(resolve_inci_from_openbeautyfacts("8809123456789", fetch=fetch))
    assert isinstance(res, ResolvedInci) and "Niacinamide" in res.raw_inci
    # no barcode -> None, no fetch
    assert asyncio.run(resolve_inci_from_openbeautyfacts(None, fetch=fetch)) is None

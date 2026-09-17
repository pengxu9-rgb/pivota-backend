def test_should_overwrite_seed_variants_allows_equal_score_when_more_variants() -> None:
    from routes.employee_products import _should_overwrite_seed_variants

    existing = [
        {"variant_id": "TEF701", "title": "100 ml", "price_amount": 165.0, "price_currency": "USD"},
    ]
    incoming = [
        {"variant_id": "TEF501", "title": "10 ml", "price_amount": 39.0, "price_currency": "USD"},
        {"variant_id": "TEF601", "title": "50 ml", "price_amount": 115.0, "price_currency": "USD"},
        {"variant_id": "TEF701", "title": "100 ml", "price_amount": 165.0, "price_currency": "USD"},
    ]

    assert _should_overwrite_seed_variants(
        existing=existing,
        incoming=incoming,
        product_title="Eau d'Ombré Leather Eau de Toilette",
    )


def test_should_overwrite_seed_variants_does_not_downgrade_titles_even_if_more_variants() -> None:
    from routes.employee_products import _should_overwrite_seed_variants

    existing = [
        {"variant_id": "TEF701", "title": "100 ml"},
    ]
    incoming = [
        {"variant_id": "TEF501", "title": "T6K501"},
        {"variant_id": "TEF601", "title": "T6K601"},
        {"variant_id": "TEF701", "title": "T6K701"},
    ]

    assert not _should_overwrite_seed_variants(
        existing=existing,
        incoming=incoming,
        product_title="Eau d'Ombré Leather Eau de Toilette",
    )


# ---------------------------------------------------------------------------
# A REFRESHED PRICE MUST REACH THE VARIANTS -- WITHOUT TAKING THE CRAWL WHOLESALE,
# AND WITHOUT DISAGREEING WITH THE PRODUCT-PRICE DECISION.
#
# Measured on prod 2026-09-16: JUNGSAEMMOOL LIP-PRESSION Metal Serum Gloss held
# price_amount 28.8 SGD with all 12 seed variants and all 13 catalog_offers at 28.20.
#
# Two earlier versions of this fix were wrong, and the tests below are shaped by why:
#   * v1 assigned the crawl wholesale, dropping keys the ingestion lane writes;
#   * v2 merged prices but matched on the RAW variant_id, and the tests fed it a
#     hand-written crawl dict carrying the bare Shopify id. The real extractor emits
#     `/products/<handle>?variant=<id>#offer` for this store, so v2 matched nothing
#     and moved nothing -- while every test passed. The end-to-end tests here run the
#     REAL `_extract_from_html` on a Shopify ProductGroup page.
# ---------------------------------------------------------------------------

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

JSM_URL = "https://jsmbeauty.sg/products/lip-pression-metal-serum-gloss"
CORE_DROP = "50856826536257"
CHAI_TEA = "50865870831937"


def _stored(price=28.2, vid=CORE_DROP, currency="SGD", shade="Core Drop", **extra) -> dict:
    """A variant as the INGESTION lane writes it: fourteen keys, float price."""
    v = {
        "variant_id": vid, "id": vid, "sku": "32168999", "barcode": None,
        "title": shade, "currency": currency, "price_currency": currency,
        "price_amount": price, "price": price, "availability": "in_stock",
        "in_stock": True, "image_url": f"https://cdn.example.com/{vid}.jpg",
        "options": [{"name": "Color", "value": shade}],
        "variant_id_provenance": "shopify",
    }
    v.update(extra)
    return v


def _shopify_product_group_html(prices, currency="SGD") -> str:
    """A Shopify ProductGroup page as jsmbeauty.sg serves it: per-variant Offers with
    no `sku`, identified only by `@id`."""
    variants = []
    for vid, shade, price in prices:
        variants.append({
            "@type": "Product",
            "name": f"LIP-PRESSION Metal Serum Gloss - {shade}",
            "offers": {
                "@type": "Offer",
                "@id": f"/products/lip-pression-metal-serum-gloss?variant={vid}#offer",
                "price": price,
                "priceCurrency": currency,
                "availability": "http://schema.org/InStock",
                "url": f"{JSM_URL}?variant={vid}",
            },
        })
    ld = {"@context": "http://schema.org/", "@type": "ProductGroup",
          "name": "LIP-PRESSION Metal Serum Gloss", "sku": "32168999", "hasVariant": variants}
    return f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body></body></html>'


def _real_crawl(prices, currency="SGD") -> dict:
    from services.external_offers_service import _extract_from_html

    return _extract_from_html(JSM_URL, _shopify_product_group_html(prices, currency))


def _merge(existing, incoming, accepted="SGD"):
    from routes.employee_products import _merge_refreshed_variant_prices

    return _merge_refreshed_variant_prices(existing=existing, incoming=incoming, accepted_currency=accepted)


# --- the real extractor's output --------------------------------------------


def test_the_real_extractor_emits_the_query_string_id_shape() -> None:
    """Pins the premise. If the extractor ever starts emitting bare ids, the
    normalisation below becomes unnecessary -- and this test says so."""
    crawl = _real_crawl([(CORE_DROP, "Core Drop", "28.80")])
    assert crawl["variants"][0]["variant_id"] == (
        f"/products/lip-pression-metal-serum-gloss?variant={CORE_DROP}#offer"
    )


def test_real_crawl_output_matches_stored_bare_shopify_ids() -> None:
    """THE defect v2 shipped with: raw-id matching found 0 of 12 on the live page."""
    crawl = _real_crawl([(CORE_DROP, "Core Drop", "28.80"), (CHAI_TEA, "Chai Tea", "28.80")])
    merged = _merge([_stored(28.2, CORE_DROP), _stored(28.2, CHAI_TEA, shade="Chai Tea")], crawl["variants"])

    assert merged is not None, "the merge must recognise the extractor's id shape"
    assert merged[0]["price_amount"] == 28.8
    assert merged[1]["price_amount"] == 28.8


# --- what the merge keeps and refuses ---------------------------------------


def test_the_price_moves_and_nothing_else_is_lost() -> None:
    crawl = _real_crawl([(CORE_DROP, "Core Drop", "28.80")])
    merged = _merge([_stored(28.2)], crawl["variants"])

    v = merged[0]
    assert v["price_amount"] == 28.8 and v["price"] == 28.8
    assert v["options"] == [{"name": "Color", "value": "Core Drop"}]
    assert v["image_url"] == f"https://cdn.example.com/{CORE_DROP}.jpg"
    assert v["sku"] == "32168999"
    assert set(_stored().keys()) <= set(v.keys()), "no key the ingestion lane wrote may be dropped"


def test_the_written_price_stays_a_float() -> None:
    """The ingestion lane writes floats; a refresh must not flip the type API responses return."""
    merged = _merge([_stored(28.2)], _real_crawl([(CORE_DROP, "Core Drop", "28.80")])["variants"])
    assert isinstance(merged[0]["price_amount"], float)


def test_the_existing_array_is_not_mutated_in_place() -> None:
    existing = [_stored(28.2)]
    _merge(existing, _real_crawl([(CORE_DROP, "Core Drop", "28.80")])["variants"])
    assert existing[0]["price_amount"] == 28.2


def test_match_by_sku_when_ids_do_not_agree() -> None:
    merged = _merge(
        [_stored(28.2, vid="stored-internal-id", sku="SKU-9")],
        [{"variant_id": "some-other-shape", "sku": "SKU-9", "price_amount": 30.0, "price_currency": "SGD"}],
    )
    assert merged is not None and merged[0]["price_amount"] == 30.0


def test_gid_form_matches_the_bare_id() -> None:
    merged = _merge(
        [_stored(28.2)],
        [{"variant_id": f"gid://shopify/ProductVariant/{CORE_DROP}", "price_amount": 30.0, "price_currency": "SGD"}],
    )
    assert merged is not None and merged[0]["price_amount"] == 30.0


def test_positional_offer_ids_never_match() -> None:
    """`offer_N` is page order. A reordered page would write one shade's price onto another."""
    assert _merge(
        [_stored(20.0, vid="offer_1"), _stored(10.0, vid="offer_2")],
        [{"variant_id": "offer_1", "price_amount": 10.0, "price_currency": "SGD"},
         {"variant_id": "offer_2", "price_amount": 20.0, "price_currency": "SGD"}],
    ) is None


def test_a_crawl_key_shared_by_two_variants_is_ambiguous_and_ignored() -> None:
    assert _merge(
        [_stored(28.2, vid="A", sku="SAME")],
        [{"variant_id": "X", "sku": "SAME", "price_amount": 10.0, "price_currency": "SGD"},
         {"variant_id": "Y", "sku": "SAME", "price_amount": 99.0, "price_currency": "SGD"}],
    ) is None


def test_a_non_positive_crawl_price_is_refused() -> None:
    for bad in ("0", "0.00", "-1"):
        assert _merge([_stored(28.2)], [{"variant_id": CORE_DROP, "price_amount": bad, "price_currency": "SGD"}]) is None


def test_nan_and_infinity_are_refused_and_never_raise() -> None:
    for bad in ("NaN", "sNaN", "Infinity"):
        assert _merge([_stored(28.2)], [{"variant_id": CORE_DROP, "price_amount": bad, "price_currency": "SGD"}]) is None
    assert _merge([_stored("NaN")], [{"variant_id": CORE_DROP, "price_amount": 28.8, "price_currency": "SGD"}]) is None


def test_no_accepted_currency_means_no_merge() -> None:
    """The merge never judges currency on its own: without the product-price decision's
    accepted currency, it does nothing. Neither side carries a currency here -- a stored
    SGD would refuse the write on its own and hide a missing guard."""
    stored = _stored(28.2)
    stored.pop("currency")
    stored.pop("price_currency")
    crawl = _currencyless(_real_crawl([(CORE_DROP, "Core Drop", "28.80")]))
    assert _merge([stored], crawl["variants"], accepted="SGD") is not None, "CONTROL: it would move"
    assert _merge([stored], crawl["variants"], accepted=None) is None


def test_a_crawled_currency_other_than_the_accepted_one_is_refused() -> None:
    crawl = _real_crawl([(CORE_DROP, "Core Drop", "24")], currency="USD")
    assert _merge([_stored(24000.0, currency="KRW")], crawl["variants"], accepted="KRW") is None


def test_a_stored_currency_other_than_the_accepted_one_is_refused() -> None:
    assert _merge(
        [_stored(24000.0, currency="KRW")],
        [{"variant_id": CORE_DROP, "price_amount": 24.0}],
        accepted="USD",
    ) is None


def test_a_stored_variant_with_no_currency_key_can_still_move() -> None:
    """curated_brand_feed writes variants with no currency key at all. Absent is not a
    mismatch -- treating it as one made the merge inert for that whole lane."""
    stored = _stored(28.2)
    stored.pop("currency")
    stored.pop("price_currency")
    merged = _merge([stored], _real_crawl([(CORE_DROP, "Core Drop", "28.80")])["variants"])
    assert merged is not None and merged[0]["price_amount"] == 28.8


def test_list_price_is_neither_read_nor_written() -> None:
    """list_price is a compare-at price, not the price."""
    stored = _stored(18.0, list_price=30.0)
    merged = _merge([stored], [{"variant_id": CORE_DROP, "price_amount": 20.0, "price_currency": "SGD"}])
    assert merged[0]["price_amount"] == 20.0
    assert merged[0]["list_price"] == 30.0
    only_list = {"variant_id": CORE_DROP, "list_price": 30.0}
    assert _merge([only_list], [{"variant_id": CORE_DROP, "price_amount": 20.0}]) is None


def test_an_unchanged_price_is_not_a_write() -> None:
    assert _merge([_stored(28.8)], _real_crawl([(CORE_DROP, "Core Drop", "28.80")])["variants"]) is None


def test_a_price_that_moves_down_counts() -> None:
    merged = _merge([_stored(28.8)], _real_crawl([(CORE_DROP, "Core Drop", "19.99")])["variants"])
    assert merged is not None and merged[0]["price_amount"] == 19.99


def test_a_sub_unit_move_counts() -> None:
    merged = _merge([_stored(28.2)], _real_crawl([(CORE_DROP, "Core Drop", "28.40")])["variants"])
    assert merged is not None and merged[0]["price_amount"] == 28.4


def test_variants_the_crawl_did_not_mention_are_untouched() -> None:
    merged = _merge(
        [_stored(28.2, CORE_DROP), _stored(28.2, CHAI_TEA, shade="Chai Tea")],
        _real_crawl([(CORE_DROP, "Core Drop", "31.00")])["variants"],
    )
    assert merged[0]["price_amount"] == 31.0
    assert merged[1]["price_amount"] == 28.2


# --- the structural DECISION ------------------------------------------------


def _keep(existing, incoming, title="LIP-PRESSION Metal Serum Gloss"):
    from routes.employee_products import _should_keep_refreshed_seed_variants

    return _should_keep_refreshed_seed_variants(
        existing=existing, incoming=incoming, product_title=title, market="SG",
        previous_canonical_url=JSM_URL, refreshed_canonical_url=JSM_URL,
    )


def test_a_price_only_change_does_not_take_the_crawl_wholesale() -> None:
    assert not _keep([_stored(28.2)], _real_crawl([(CORE_DROP, "Core Drop", "28.80")])["variants"])


def test_a_structurally_better_crawl_is_still_taken_wholesale() -> None:
    existing = [{"variant_id": "A", "title": "", "price_amount": "10.0"}]
    incoming = [
        {"variant_id": "A", "title": "100 ml", "price_amount": "10.0"},
        {"variant_id": "B", "title": "50 ml", "price_amount": "8.0"},
    ]
    assert _keep(existing, incoming, title="Some Serum 100 ml")


def test_a_localisation_correction_is_still_taken_wholesale() -> None:
    """Pins the second structural arm (_should_replace_seed_variant_content)."""
    from routes.employee_products import _should_keep_refreshed_seed_variants

    existing = [{"variant_id": "A", "title": "100 ml",
                 "description": "Diese Feuchtigkeitscreme ist sehr gut fuer die Haut und wird taeglich verwendet."}]
    incoming = [{"variant_id": "A", "title": "100 ml",
                 "description": "This moisturiser is very good for the skin and is used every day."}]
    assert _should_keep_refreshed_seed_variants(
        existing=existing, incoming=incoming, product_title="Some Serum 100 ml", market="US",
        previous_canonical_url="https://example.com/de/p", refreshed_canonical_url="https://example.com/p",
    ) is True


# --- END TO END through the real refresh, fed by the real extractor ---------


def _run_refresh(stored_row: dict, crawl: dict) -> dict:
    import routes.employee_products as mod

    snapshot = SimpleNamespace(
        canonical_url=crawl.get("canonical_url") or JSM_URL, domain="jsmbeauty.sg",
        title=crawl.get("title"), image_url=crawl.get("image_url"),
        price_amount=crawl.get("price_amount"), price_currency=crawl.get("price_currency"),
        availability=crawl.get("availability") or "in_stock", fetched_at=None,
        evidence={"variants": crawl.get("variants") or []},
    )

    async def fake_fetch_one(_q, values=None):
        return stored_row if values and values.get("id") == stored_row["id"] else None

    async def fake_exec(_q, values):
        stored_row.update(values)

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
        mp.setattr(mod.database, "fetch_one", fake_fetch_one)
        mp.setattr(mod, "_execute_seed_data_stmt", fake_exec)
        mp.setattr(mod, "resolve_external_offer", AsyncMock(return_value=snapshot))
        result = asyncio.run(mod._refresh_external_seed_by_id(stored_row["id"], max_wait=0))
    finally:
        mp.undo()
    seed_data = stored_row["seed_data"]
    if isinstance(seed_data, str):
        seed_data = json.loads(seed_data)
    return {"result": result, "variants": seed_data.get("variants") or [], "row": stored_row}


def _jsm_row(column_price=28.8, variant_price=28.2, column_currency="SGD", variant_currency="SGD") -> dict:
    return {
        "id": "eps_jsm_1", "external_product_id": "ext_jsm_1", "market": "SG", "tool": "*",
        "utm_template": None, "partner_type": None, "disclosure_text": None,
        "destination_url": JSM_URL, "canonical_url": JSM_URL, "domain": "jsmbeauty.sg",
        "title": "LIP-PRESSION Metal Serum Gloss", "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": column_price, "price_currency": column_currency, "availability": "in_stock",
        "seed_data": {"title": "LIP-PRESSION Metal Serum Gloss", "snapshot": {},
                      "variants": [_stored(variant_price, currency=variant_currency),
                                   _stored(variant_price, CHAI_TEA, currency=variant_currency, shade="Chai Tea")]},
        "status": "active", "attached_product_key": None, "attached_variant_id": None,
    }


def test_the_reported_seed_heals_when_its_column_is_already_fresh() -> None:
    """The exact prod state: column already 28.8, variants stuck at 28.2. The product
    price reads `unchanged`, and the variants must still heal."""
    out = _run_refresh(_jsm_row(28.8, 28.2),
                       _real_crawl([(CORE_DROP, "Core Drop", "28.80"), (CHAI_TEA, "Chai Tea", "28.80")]))

    assert out["result"]["price_refresh"]["status"] == "unchanged"
    assert [v["price_amount"] for v in out["variants"]] == [28.8, 28.8]
    assert out["variants"][0]["options"] == [{"name": "Color", "value": "Core Drop"}]


def test_when_the_product_price_is_refused_the_variants_do_not_move() -> None:
    """Refused pair -> no variant write. The array and the column must never diverge:
    a crawl in a currency other than the stored one is refused by the scalar path, so
    the variants stay too."""
    out = _run_refresh(_jsm_row(28.8, 28.2),
                       _real_crawl([(CORE_DROP, "Core Drop", "21.00")], currency="USD"))

    assert out["result"]["price_refresh"]["status"] == "skipped_currency_mismatch"
    assert [v["price_amount"] for v in out["variants"]] == [28.2, 28.2]


def test_a_zero_crawl_moves_neither_column_nor_variants() -> None:
    crawl = _real_crawl([(CORE_DROP, "Core Drop", "0")])
    crawl["price_amount"] = 0.0
    out = _run_refresh(_jsm_row(28.8, 28.2), crawl)

    assert out["result"]["price_refresh"]["status"] == "skipped_non_positive"
    assert [v["price_amount"] for v in out["variants"]] == [28.2, 28.2]


def _currencyless(crawl: dict) -> dict:
    for v in crawl["variants"]:
        v["price_currency"] = None
    return crawl


def _row_with_currencyless_variants(**kw) -> dict:
    row = _jsm_row(**kw)
    for v in row["seed_data"]["variants"]:
        v.pop("currency")
        v.pop("price_currency")
    return row


def test_an_incomplete_price_pair_moves_no_variant_even_when_no_variant_has_a_currency() -> None:
    """THE GATE, isolated. The product price is refused `skipped_incomplete_pair` (the
    crawl read an amount but no currency). Neither the stored variants (curated lane:
    no currency key) nor the crawled ones carry a currency, so the merge's own currency
    check has nothing to object to -- only the product-price gate stops the write.
    Without it the variants move and the column does not: the seed disagrees with
    itself again, reversed."""
    crawl = _currencyless(_real_crawl([(CORE_DROP, "Core Drop", "28.80"), (CHAI_TEA, "Chai Tea", "28.80")]))
    crawl["price_currency"] = None
    out = _run_refresh(_row_with_currencyless_variants(column_price=28.2, variant_price=28.2), crawl)

    assert out["result"]["price_refresh"]["status"] == "skipped_incomplete_pair"
    assert out["row"]["price_amount"] == 28.2
    assert [v["price_amount"] for v in out["variants"]] == [28.2, 28.2]


def test_a_fabricated_currency_mismatch_moves_no_variant_either() -> None:
    """resolve_external_offer fabricates `USD` when a page shows no currency. The
    scalar path refuses that against a stored SGD column; variants without a currency
    of their own must stay put too."""
    crawl = _currencyless(_real_crawl([(CORE_DROP, "Core Drop", "21.00"), (CHAI_TEA, "Chai Tea", "21.00")]))
    crawl["price_currency"] = "USD"
    out = _run_refresh(_row_with_currencyless_variants(column_price=28.8, variant_price=28.2), crawl)

    assert out["result"]["price_refresh"]["status"] == "skipped_currency_mismatch"
    assert [v["price_amount"] for v in out["variants"]] == [28.2, 28.2]


# --- Pages this merge deliberately does NOT heal ------------------------------
#
# Two attempts to reach Shopify Dawn-theme pages (`Product.offers[]`, sku in
# `variant_id`) were withdrawn after review: a sku alias and the Offer url's
# `?variant=` each let a PRODUCT-level Offer price the one shade it named, and the
# extractor dedupe that came with them collapsed genuinely distinct sizes on
# non-Shopify stores. These tests pin that those shapes move NOTHING, through the
# real extractor, so a future attempt has to prove itself against them.

DAWN_URL = "https://shop.example.com/products/lip-gloss"


def _page(*json_ld_blocks) -> str:
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in json_ld_blocks)
    return f"<html><head>{scripts}</head></html>"


def _extract(*blocks) -> dict:
    from services.external_offers_service import _extract_from_html

    return _extract_from_html(DAWN_URL, _page(*blocks))


def _dawn_offer(sku, vid, price) -> dict:
    offer = {"@type": "Offer", "price": price, "priceCurrency": "SGD", "url": f"{DAWN_URL}?variant={vid}"}
    if sku is not None:
        offer["sku"] = sku
    return offer


def test_a_product_level_offer_beside_a_review_widget_moves_no_shade() -> None:
    """Re-review of v5: a theme's single product-level Offer (price 20.00, url naming
    shade 222) plus a review app's AggregateOffer block wrote 20.00 onto shade 222,
    stored at 35.00, end to end."""
    theme = {"@context": "http://schema.org/", "@type": "Product", "name": "Lip Gloss",
             "offers": _dawn_offer("SEL-SKU", "222", "20.00")}
    reviews = {"@context": "http://schema.org/", "@type": "Product", "name": "Lip Gloss",
               "offers": {"@type": "AggregateOffer", "lowPrice": "20.00", "priceCurrency": "SGD"}}
    crawl = _extract(theme, reviews)
    stored = [_stored(20.0, vid="111", sku="S-1"), _stored(35.0, vid="222", sku="S-2")]
    assert _merge(stored, crawl["variants"]) is None
    stored_by_sku = [_stored(20.0, vid="111", sku="SEL-SKU"), _stored(35.0, vid="222", sku="S-2")]
    assert _merge(stored_by_sku, crawl["variants"]) is None


def test_dawn_per_variant_offers_move_nothing() -> None:
    crawl = _extract({"@context": "http://schema.org/", "@type": "Product", "name": "Lip Gloss",
                      "offers": [_dawn_offer("JSM-LP-01", "111", "30.00"), _dawn_offer("", "222", "31.00")]})
    stored = [_stored(28.0, vid="111", sku="JSM-LP-01"), _stored(28.0, vid="222", sku="JSM-LP-02")]
    assert _merge(stored, crawl["variants"]) is None


def test_the_extractor_keeps_two_same_priced_sizes_on_a_non_shopify_page() -> None:
    """The withdrawn dedupe collapsed these to one: a WooCommerce/Magento permalink
    carries no `?variant=`, so two blank-sku sizes at one price shared a url and price."""
    url = "https://woo.example.com/product/lip-balm/"
    crawl = _extract({"@context": "http://schema.org/", "@type": "Product", "name": "Lip Balm", "offers": [
        {"@type": "Offer", "name": "Small", "price": "12.00", "priceCurrency": "SGD", "url": url},
        {"@type": "Offer", "name": "Large", "price": "12.00", "priceCurrency": "SGD", "url": url},
    ]})
    titles = {v.get("title") for v in crawl["variants"]}
    assert {"Small", "Large"} <= titles, crawl["variants"]


def test_one_crawled_shopify_variant_id_moves_only_its_own_shade() -> None:
    """The decision the v3 docstring states: a crawled id that IS a Shopify variant id is
    that variant's identity, so a single such Offer may move its shade and no other.
    The GID form too."""
    crawl = _real_crawl([(CORE_DROP, "Core Drop", "28.80")])
    merged = _merge([_stored(28.2, CORE_DROP), _stored(28.2, CHAI_TEA, shade="Chai Tea")], crawl["variants"])
    assert merged is not None and [v["price_amount"] for v in merged] == [28.8, 28.2]
    gid = [{"variant_id": f"gid://shopify/ProductVariant/{CHAI_TEA}", "price_amount": 30.0, "price_currency": "SGD"}]
    merged = _merge([_stored(28.2, CORE_DROP), _stored(28.2, CHAI_TEA, shade="Chai Tea")], gid)
    assert merged is not None and [v["price_amount"] for v in merged] == [28.2, 30.0]


def test_every_price_key_the_variant_carries_moves_together_and_none_is_added() -> None:
    both = _merge([_stored(28.2)], _real_crawl([(CORE_DROP, "Core Drop", "28.80")])["variants"])
    assert both[0]["price_amount"] == 28.8 and both[0]["price"] == 28.8
    only_amount = _stored(28.2)
    only_amount.pop("price")
    merged = _merge([only_amount], _real_crawl([(CORE_DROP, "Core Drop", "28.80")])["variants"])
    assert merged[0]["price_amount"] == 28.8
    assert "price" not in merged[0], "a key the ingestion lane did not write must not appear"


def test_a_structurally_better_crawl_replaces_the_variants_end_to_end() -> None:
    """The wholesale arm, observed through the refresh itself: the keep predicate is
    pinned in isolation elsewhere, but deleting the assignment it guards left every
    refresh test green."""
    row = _jsm_row(28.8, 28.8)
    row["seed_data"]["variants"] = []
    crawl = _real_crawl([(CORE_DROP, "Core Drop", "28.80"), (CHAI_TEA, "Chai Tea", "28.80")])
    out = _run_refresh(row, crawl)
    assert [v["variant_id"] for v in out["variants"]] == [v["variant_id"] for v in crawl["variants"]]


def test_a_key_two_stored_variants_hold_is_refused() -> None:
    """A product-level sku stored on every shade must not let one crawled price land on
    all of them."""
    crawl = [{"variant_id": "c1", "sku": "SHARED", "price_amount": 28.8, "price_currency": "SGD"},
             {"variant_id": "c2", "sku": "OTHER", "price_amount": 99.0, "price_currency": "SGD"}]
    assert _merge(
        [_stored(28.2, vid="x1", sku="SHARED"), _stored(30.0, vid="x2", sku="SHARED")], crawl,
    ) is None
    # CONTROL: unshared, the same skus move each shade to its own crawled price.
    merged = _merge([_stored(28.2, vid="x1", sku="SHARED"), _stored(30.0, vid="x2", sku="OTHER")], crawl)
    assert merged is not None and [v["price_amount"] for v in merged] == [28.8, 99.0]


def test_the_first_matching_key_wins() -> None:
    """A stored shade whose id names one crawled variant and whose sku names another
    takes the id's price: ids are listed first because they are the stronger identity."""
    merged = _merge(
        [_stored(28.0, vid="A", sku="S")],
        [{"variant_id": "A", "price_amount": 10.0, "price_currency": "SGD"},
         {"variant_id": "Z", "sku": "S", "price_amount": 99.0, "price_currency": "SGD"}],
    )
    assert merged is not None and merged[0]["price_amount"] == 10.0


def test_a_zero_product_price_stops_non_zero_variant_prices() -> None:
    """THE GATE, isolated from the merge's own `<= 0` refusal: the product price is 0,
    the variant prices are not, so only `skipped_non_positive` stops the write."""
    crawl = _real_crawl([(CORE_DROP, "Core Drop", "28.80"), (CHAI_TEA, "Chai Tea", "28.80")])
    crawl["price_amount"] = 0.0
    out = _run_refresh(_jsm_row(28.8, 28.2), crawl)

    assert out["result"]["price_refresh"]["status"] == "skipped_non_positive"
    assert [v["price_amount"] for v in out["variants"]] == [28.2, 28.2]

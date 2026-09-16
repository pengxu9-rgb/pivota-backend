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
# A REFRESHED PRICE MUST REACH THE VARIANTS -- WITHOUT TAKING THE CRAWL WHOLESALE.
#
# The nightly refresh crawls the correct new price and the two structural
# predicates answer "nothing new" for an unchanged shade range, so the fresh
# prices were discarded while the scalar price_amount column moved alone.
# Measured on prod 2026-09-16: JUNGSAEMMOOL LIP-PRESSION Metal Serum Gloss held
# price_amount 28.8 SGD with all 12 seed variants and all 13 catalog_offers at
# 28.20 from the 09-08 ingest.
#
# The first version of this fix ASSIGNED the crawl, which is a data-loss bug: the
# ingestion lane writes fourteen keys per variant and the crawl emits five.
# ---------------------------------------------------------------------------

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _rich(price: str = "28.2", vid: str = "50856826536257", currency: str = "SGD") -> dict:
    """A variant as the INGESTION lane writes it."""
    return {
        "variant_id": vid, "id": vid, "sku": "32168999", "barcode": None,
        "title": "Core Drop", "currency": currency, "price_currency": currency,
        "price_amount": price, "price": price, "availability": "in_stock",
        "in_stock": True, "image_url": "https://cdn.example.com/core-drop.jpg",
        "options": [{"name": "Color", "value": "Core Drop"}],
        "variant_id_provenance": "shopify",
    }


def _crawled(price: str = "28.8", vid: str = "50856826536257", currency: str = "SGD") -> dict:
    """The same variant as the CRAWL EXTRACTOR emits it -- five keys."""
    return {
        "variant_id": vid, "title": "Core Drop", "price_amount": price,
        "price_currency": currency, "availability": "in_stock",
    }


def _merge(existing, incoming):
    from routes.employee_products import _merge_refreshed_variant_prices

    return _merge_refreshed_variant_prices(existing=existing, incoming=incoming)


def test_the_price_moves_and_nothing_else_is_lost() -> None:
    """THE regression the first version shipped: assigning the crawl dropped
    `options` (shade-selector labels) and per-variant `image_url`, both of which
    services/beauty_external_ranking.py reads."""
    merged = _merge([_rich("28.2")], [_crawled("28.8")])

    assert merged is not None
    v = merged[0]
    assert v["price_amount"] == "28.8"
    assert v["price"] == "28.8", "every price key the variant carries must move together"
    assert v["options"] == [{"name": "Color", "value": "Core Drop"}]
    assert v["image_url"] == "https://cdn.example.com/core-drop.jpg"
    assert v["sku"] == "32168999"
    assert set(_rich().keys()) <= set(v.keys()), "no key the ingestion lane wrote may be dropped"


def test_the_existing_array_is_not_mutated_in_place() -> None:
    existing = [_rich("28.2")]
    _merge(existing, [_crawled("28.8")])
    assert existing[0]["price_amount"] == "28.2"


def test_a_non_positive_crawl_price_is_refused() -> None:
    """_parse_price returns 0.0 for a price glyph it could not read a number out of.
    The scalar path refuses that as skipped_non_positive; so must this."""
    assert _merge([_rich("28.2")], [_crawled("0")]) is None
    assert _merge([_rich("28.2")], [_crawled("0.00")]) is None
    assert _merge([_rich("28.2")], [_crawled("-1")]) is None


def test_a_currency_change_is_refused_not_redenominated() -> None:
    """A scraped refresh may not redenominate an offer -- the scalar path refuses
    this as skipped_currency_mismatch."""
    assert _merge([_rich("24000", currency="KRW")], [_crawled("24", currency="USD")]) is None


def test_a_currency_the_crawl_did_not_report_does_not_block_a_price_move() -> None:
    incoming = _crawled("28.8")
    incoming.pop("price_currency")
    merged = _merge([_rich("28.2")], [incoming])
    assert merged is not None and merged[0]["price_amount"] == "28.8"


def test_nan_is_refused_and_never_raises() -> None:
    """Decimal NaN compares unequal to itself, so it would rewrite the array every
    night forever; sNaN raises InvalidOperation on compare."""
    assert _merge([_rich("28.2")], [_crawled("NaN")]) is None
    assert _merge([_rich("NaN")], [_crawled("28.8")]) is None
    assert _merge([_rich("28.2")], [_crawled("sNaN")]) is None
    assert _merge([_rich("28.2")], [_crawled("Infinity")]) is None


def test_an_unchanged_price_is_not_a_write() -> None:
    """CONTROL. Without this the merge could return a new array every night."""
    assert _merge([_rich("28.2")], [_crawled("28.2")]) is None
    assert _merge([_rich("28.20")], [_crawled("28.2")]) is None, "formatting is not a price move"
    assert _merge([_rich("28")], [_crawled("28.000")]) is None


def test_a_price_that_moves_down_counts() -> None:
    """A markdown is the common case and must not be ignored."""
    merged = _merge([_rich("28.8")], [_crawled("19.99")])
    assert merged is not None and merged[0]["price_amount"] == "19.99"


def test_a_sub_unit_move_counts() -> None:
    merged = _merge([_rich("28.20")], [_crawled("28.40")])
    assert merged is not None and merged[0]["price_amount"] == "28.40"


def test_variants_the_crawl_did_not_mention_are_untouched() -> None:
    existing = [_rich("28.2", "AAA"), _rich("28.2", "BBB")]
    merged = _merge(existing, [_crawled("31.0", "AAA")])
    assert merged is not None
    assert merged[0]["price_amount"] == "31.0"
    assert merged[1]["price_amount"] == "28.2", "an id the crawl never mentioned must not move"


def test_an_id_only_the_crawl_knows_is_left_to_the_structural_predicates() -> None:
    assert _merge([_rich("28.2", "AAA")], [_crawled("99.0", "ZZZ")]) is None


# --- the DECISION and the CALL SITE, not just the parts ---------------------


def _keep(existing, incoming, title="LIP-PRESSION Metal Serum Gloss"):
    from routes.employee_products import _should_keep_refreshed_seed_variants

    return _should_keep_refreshed_seed_variants(
        existing=existing, incoming=incoming, product_title=title, market="SG",
        previous_canonical_url="https://jsmbeauty.sg/p", refreshed_canonical_url="https://jsmbeauty.sg/p",
    )


def test_a_price_only_change_does_not_take_the_crawl_wholesale() -> None:
    """The structural decision must stay structural: a price move is handled by the
    merge, not by replacing the array."""
    assert not _keep([_rich("28.2")], [_crawled("28.8")])


def test_a_structurally_better_crawl_is_still_taken_wholesale() -> None:
    existing = [{"variant_id": "A", "title": "", "price_amount": "10.0"}]
    incoming = [
        {"variant_id": "A", "title": "100 ml", "price_amount": "10.0"},
        {"variant_id": "B", "title": "50 ml", "price_amount": "8.0"},
    ]
    assert _keep(existing, incoming, title="Some Serum 100 ml")


def test_a_content_change_on_matching_ids_is_still_taken_wholesale() -> None:
    """Pins the SECOND structural arm (_should_replace_seed_variant_content), which
    no other test in this file reaches -- deleting it left every test green."""
    from routes.employee_products import _should_keep_refreshed_seed_variants

    # Arm B is LOCALISATION correction: a US-market row whose stored copy is
    # non-English and whose refresh came back in English.
    existing = [{
        "variant_id": "A", "title": "100 ml",
        "description": "Diese Feuchtigkeitscreme ist sehr gut fuer die Haut und wird taeglich verwendet.",
    }]
    incoming = [{
        "variant_id": "A", "title": "100 ml",
        "description": "This moisturiser is very good for the skin and is used every day.",
    }]
    structural = _should_keep_refreshed_seed_variants(
        existing=existing, incoming=incoming, product_title="Some Serum 100 ml",
        market="US",
        previous_canonical_url="https://example.com/de/p",
        refreshed_canonical_url="https://example.com/p",
    )
    assert structural is True


def test_the_real_refresh_moves_the_price_into_the_variants_it_already_held() -> None:
    """END TO END through `_refresh_external_seed_by_id`.

    Every assertion above drives a helper. Nothing pinned that the refresh CONSULTS
    them -- reverting the call site left the whole file green.
    """
    import routes.employee_products as mod

    stored = {
        "id": "eps_jsm_1", "external_product_id": "ext_jsm_1", "market": "SG", "tool": "*",
        "utm_template": None, "partner_type": None, "disclosure_text": None,
        "destination_url": "https://jsmbeauty.sg/products/lip-pression-metal-serum-gloss",
        "canonical_url": "https://jsmbeauty.sg/products/lip-pression-metal-serum-gloss",
        "domain": "jsmbeauty.sg", "title": "LIP-PRESSION Metal Serum Gloss",
        "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": 28.2, "price_currency": "SGD", "availability": "in_stock",
        "seed_data": {"title": "LIP-PRESSION Metal Serum Gloss", "snapshot": {},
                      "variants": [_rich("28.2")]},
        "status": "active", "attached_product_key": None, "attached_variant_id": None,
    }
    snapshot = SimpleNamespace(
        canonical_url=stored["canonical_url"], domain="jsmbeauty.sg",
        title="LIP-PRESSION Metal Serum Gloss", image_url="https://cdn.example.com/img.jpg",
        price_amount=28.8, price_currency="SGD", availability="in_stock",
        fetched_at=None, evidence={"variants": [_crawled("28.8")]},
    )

    async def fake_fetch_one(_q, values=None):
        return stored if values and values.get("id") == stored["id"] else None

    async def fake_exec(_q, values):
        stored.update(values)

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
        mp.setattr(mod.database, "fetch_one", fake_fetch_one)
        mp.setattr(mod, "_execute_seed_data_stmt", fake_exec)
        mp.setattr(mod, "resolve_external_offer", AsyncMock(return_value=snapshot))
        asyncio.run(mod._refresh_external_seed_by_id(stored["id"], max_wait=0))
    finally:
        mp.undo()

    written = stored["seed_data"]
    variants = written["variants"] if isinstance(written, dict) else []
    assert variants, "the refresh must still write a variants array"
    assert str(variants[0]["price_amount"]) == "28.8", "the crawled price must reach the variants"
    assert variants[0]["options"] == [{"name": "Color", "value": "Core Drop"}], (
        "the refresh must not replace the rich array with the crawl's five-key shape"
    )

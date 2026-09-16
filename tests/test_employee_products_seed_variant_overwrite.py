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
# A PRICE MOVE IS A REASON TO KEEP THE CRAWLED VARIANTS.
#
# The two predicates above consider titles, ids, descriptions, market and
# canonical_url -- never price. So a refresh of a product whose shades did not
# change scored the fresh crawl as "nothing new" and DISCARDED it, while the
# scalar price_amount column (written separately) moved. The seed then disagreed
# with itself and catalog_offers, built from the variants, stayed stale forever.
#
# Measured on prod 2026-09-16: JUNGSAEMMOOL LIP-PRESSION Metal Serum Gloss held
# price_amount 28.8 SGD (refreshed 09-14, matching the merchant's own door) with
# all 12 seed variants and all 13 catalog_offers still at 28.20 from 09-08.
# ---------------------------------------------------------------------------


def _jsm(price: str, vid: str = "50856826536257") -> dict:
    return {"variant_id": vid, "title": "Core Drop", "price_amount": price, "price_currency": "SGD"}


def test_a_price_move_on_a_matching_variant_is_a_change() -> None:
    from routes.employee_products import _seed_variant_prices_changed

    assert _seed_variant_prices_changed(existing=[_jsm("28.2")], incoming=[_jsm("28.8")])


def test_the_structural_predicates_would_have_thrown_that_crawl_away() -> None:
    """The control that makes the fix necessary rather than merely additive.

    Same ids, same titles, only the price moved -- so the existing overwrite
    predicate says False. Without the price predicate nothing keeps the crawl.
    """
    from routes.employee_products import _should_overwrite_seed_variants

    assert not _should_overwrite_seed_variants(
        existing=[_jsm("28.2")], incoming=[_jsm("28.8")], product_title="LIP-PRESSION Metal Serum Gloss"
    )


def test_identical_prices_are_not_a_change() -> None:
    from routes.employee_products import _seed_variant_prices_changed

    assert not _seed_variant_prices_changed(existing=[_jsm("28.2")], incoming=[_jsm("28.2")])


def test_decimal_formatting_is_not_a_change() -> None:
    """28.20 and 28.2 are the same price. Churning the array on formatting would
    rewrite good variants every single night."""
    from routes.employee_products import _seed_variant_prices_changed

    assert not _seed_variant_prices_changed(existing=[_jsm("28.20")], incoming=[_jsm("28.2")])
    assert not _seed_variant_prices_changed(existing=[_jsm("28")], incoming=[_jsm("28.000")])


def test_an_unreadable_or_missing_price_is_not_a_change() -> None:
    """Absent is not cheaper. Replacing good variants because the crawl could not
    read a price is strictly worse than keeping what we have."""
    from routes.employee_products import _seed_variant_prices_changed

    assert not _seed_variant_prices_changed(existing=[_jsm("28.2")], incoming=[{"variant_id": "50856826536257"}])
    assert not _seed_variant_prices_changed(
        existing=[_jsm("28.2")], incoming=[_jsm("not-a-price")]
    )
    assert not _seed_variant_prices_changed(existing=[], incoming=[_jsm("28.8")])


def test_ids_present_on_only_one_side_are_left_to_the_other_predicates() -> None:
    """An added or removed shade is structural churn this predicate does not
    understand -- answering True for it would fire on cases it cannot judge."""
    from routes.employee_products import _seed_variant_prices_changed

    assert not _seed_variant_prices_changed(
        existing=[_jsm("28.2", "AAA")], incoming=[_jsm("99.0", "BBB")]
    )


def test_one_moved_price_among_many_unchanged_still_counts() -> None:
    from routes.employee_products import _seed_variant_prices_changed

    existing = [_jsm("28.2", "A"), _jsm("28.2", "B"), _jsm("28.2", "C")]
    incoming = [_jsm("28.2", "A"), _jsm("28.2", "B"), _jsm("31.0", "C")]
    assert _seed_variant_prices_changed(existing=existing, incoming=incoming)


# The DECISION, not just its parts. Testing each predicate in isolation left a
# mutant alive: deleting the price arm from the refresh kept every test above
# green, because nothing asserted the refresh consults it.


def _keep(existing, incoming, title="LIP-PRESSION Metal Serum Gloss"):
    from routes.employee_products import _should_keep_refreshed_seed_variants

    return _should_keep_refreshed_seed_variants(
        existing=existing,
        incoming=incoming,
        product_title=title,
        market="SG",
        previous_canonical_url="https://jsmbeauty.sg/products/lip-pression-metal-serum-gloss",
        refreshed_canonical_url="https://jsmbeauty.sg/products/lip-pression-metal-serum-gloss",
    )


def test_the_refresh_keeps_a_crawl_whose_only_change_is_price() -> None:
    """The end-to-end property: same ids, same titles, same URL, same market --
    only the price moved, and the refresh must still take the new variants."""
    assert _keep([_jsm("28.2")], [_jsm("28.8")])


def test_the_refresh_discards_a_crawl_that_changed_nothing() -> None:
    """CONTROL. Without this, the test above would also pass if the decision
    returned True unconditionally -- rewriting every seed every night."""
    assert not _keep([_jsm("28.2")], [_jsm("28.2")])


def test_the_refresh_still_keeps_a_structurally_better_crawl() -> None:
    """The pre-existing reasons must survive the extraction."""
    existing = [{"variant_id": "A", "title": "", "price_amount": "10.0"}]
    incoming = [
        {"variant_id": "A", "title": "100 ml", "price_amount": "10.0"},
        {"variant_id": "B", "title": "50 ml", "price_amount": "8.0"},
    ]
    assert _keep(existing, incoming, title="Some Serum 100 ml")


def test_the_refresh_fills_an_empty_variant_array() -> None:
    assert _keep([], [_jsm("28.8")])


def test_the_refresh_ignores_an_empty_crawl() -> None:
    assert not _keep([_jsm("28.2")], [])

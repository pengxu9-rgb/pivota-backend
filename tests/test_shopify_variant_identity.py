"""The matching rule must refuse rather than guess.

A wrong Shopify variant id is worse than none: `shopify_cart_base_url` builds
`/cart/{id}:1` from it, so a mismatch silently adds the wrong size to the buyer's cart and
they complete a purchase we mis-specified. These tests are mostly about the cases where the
right answer is "no answer".

Fixtures are shaped from real `/products/<handle>.js` bodies observed on 2026-08-21 while
probing the K-beauty cohort (numeric ids, `price` in minor units, `options` array).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services.shopify_variant_identity import (
    match_variants,
    parse_product_js,
    product_js_url,
    stamp_variant_ids,
)


def _live(vid: str, title: str, *, options: List[str] | None = None, price: int | None = None,
          available: bool = True, sku: str | None = None) -> Dict[str, Any]:
    return {
        "id": int(vid),
        "title": title,
        "options": options if options is not None else [title],
        "price": price,
        "available": available,
        "sku": sku,
    }


# ---------------------------------------------------------------- URL derivation

@pytest.mark.parametrize(
    "given,expected",
    [
        ("https://brand.com/products/handle", "https://brand.com/products/handle.js"),
        # `?variant=` selects a variant for the RENDERER; the .js body always lists all of
        # them, so dropping the query is correct rather than lossy.
        ("https://brand.com/products/handle?variant=123", "https://brand.com/products/handle.js"),
        ("https://brand.com/products/handle/", "https://brand.com/products/handle.js"),
        # the collection prefix is presentational; .js is served from the bare product path
        ("https://brand.com/collections/serums/products/handle", "https://brand.com/products/handle.js"),
        ("https://brand.com/products/handle.js", "https://brand.com/products/handle.js"),
        ("https://brand.com/products/handle.json", "https://brand.com/products/handle.js"),
        # non-product pages must not be probed at all
        ("https://brand.com/collections/serums", None),
        ("https://brand.com/", None),
        ("not a url", None),
        ("", None),
        (None, None),
    ],
)
def test_product_js_url(given: Any, expected: Any) -> None:
    assert product_js_url(given) == expected


# ---------------------------------------------------------------- payload parsing

def test_parse_product_js_converts_minor_units_once() -> None:
    """`price` is in MINOR units in this payload. The yen-read-as-dollars class of bug
    starts with a minor-unit field crossing a boundary unconverted, so it is converted here
    and only here."""
    parsed = parse_product_js({"variants": [_live("42", "30ml", price=2240)]})

    assert parsed[0]["price_amount"] == pytest.approx(22.40)
    assert parsed[0]["shopify_variant_id"] == "42"


def test_parse_product_js_drops_variants_with_no_usable_id() -> None:
    """A variant with no numeric id cannot serve this module's purpose — a cart permalink —
    so carrying it forward would only inflate coverage numbers."""
    parsed = parse_product_js({"variants": [
        _live("42", "30ml"),
        {"id": None, "title": "50ml"},
        {"id": "not-numeric", "title": "100ml"},
        {"id": "gid://shopify/ProductVariant/77", "title": "200ml"},
    ]})

    assert [v["shopify_variant_id"] for v in parsed] == ["42", "77"]


@pytest.mark.parametrize("payload", [None, {}, {"variants": None}, {"variants": {}}, "nope", []])
def test_parse_product_js_is_total(payload: Any) -> None:
    assert parse_product_js(payload) == []


# ---------------------------------------------------------------- the matching rule

def test_a_sole_variant_on_both_sides_is_unambiguous() -> None:
    mapping, reason = match_variants([{"title": "Default Title"}], parse_product_js({"variants": [_live("11", "Default Title")]}))

    assert mapping == {0: "11"}
    assert reason == "sole_variant"


def test_labels_that_differ_only_in_separators_still_match() -> None:
    """`30 ml`, `30ML` and `30-ml` are one option on a storefront. Treating them as
    different is what turns a matchable variant into a needless refusal."""
    live = parse_product_js({"variants": [_live("11", "30 ml"), _live("22", "50 ml")]})
    mapping, reason = match_variants([{"title": "30ML"}, {"title": "50-ml"}], live)

    assert mapping == {0: "11", 1: "22"}
    assert reason == "label_match"


def test_digits_still_discriminate_after_folding() -> None:
    """The folding is aggressive on separators but must never merge 30ml with 50ml."""
    live = parse_product_js({"variants": [_live("11", "30ml"), _live("22", "50ml")]})
    mapping, _ = match_variants([{"title": "50ml"}], live)

    assert mapping == {0: "22"}


def test_two_seed_variants_claiming_one_live_variant_are_BOTH_dropped() -> None:
    """If the labels do not discriminate, we cannot tell which is which — so neither is
    safe to write. This is the case that would otherwise put the wrong size in a cart."""
    live = parse_product_js({"variants": [_live("11", "Shade 01"), _live("22", "Shade 02")]})
    seed = [{"title": "Shade"}, {"title": "Shade"}]
    mapping, reason = match_variants(seed, live)

    assert mapping == {}
    assert reason == "no_confident_match"


def test_a_label_matching_TWO_live_variants_refuses_instead_of_taking_the_first() -> None:
    """Reaches the `len(hits) == 1` guard, which nothing else did.

    Review found this: the existing "both dropped" test produced ZERO hits, not two, so it
    exercised the "nothing intersects" path and a mutant relaxing the guard to `>= 1` —
    i.e. take the first hit, i.e. GUESS — survived the whole suite. A seed that knows only
    its size, against a storefront that sells that size in two colours, is exactly the shape
    where guessing puts the wrong item in the cart.
    """
    live = parse_product_js({"variants": [
        _live("11", "Small / Red", options=["Small", "Red"]),
        _live("22", "Small / Blue", options=["Small", "Blue"]),
    ]})
    mapping, reason = match_variants([{"title": "Small"}], live)

    assert mapping == {}
    assert reason == "no_confident_match"


def test_two_seed_variants_resolving_to_the_SAME_live_variant_drop_both() -> None:
    """Reaches the double-claim filter, which nothing else did.

    Each seed variant matches exactly one live variant — so the per-seed guard is satisfied
    — but they match the SAME one. Without the claim count, both would be stamped with one
    id and two different products would share a cart URL.
    """
    live = parse_product_js({"variants": [_live("11", "Shade"), _live("22", "Deep")]})
    mapping, reason = match_variants([{"title": "Shade"}, {"title": "Shade"}], live)

    assert mapping == {}
    assert reason == "no_confident_match"


def test_an_unlabelled_seed_variant_is_refused_not_positionally_guessed() -> None:
    """Position is not identity. A storefront is free to reorder its variants, so falling
    back to index would be a coin flip dressed up as a match."""
    live = parse_product_js({"variants": [_live("11", "30ml"), _live("22", "50ml")]})
    mapping, reason = match_variants([{}, {}], live)

    assert mapping == {}
    assert reason == "no_confident_match"


def test_a_partial_match_keeps_what_is_certain_and_says_so() -> None:
    """One resolvable sibling should not be thrown away because another was ambiguous —
    but the reason code has to admit the result is partial."""
    live = parse_product_js({"variants": [_live("11", "30ml"), _live("22", "50ml"), _live("33", "100ml")]})
    mapping, reason = match_variants([{"title": "100ml"}, {}], live)

    assert mapping == {0: "33"}
    assert reason == "partial_label_match"


def test_an_empty_seed_list_never_returns_a_mapping_for_index_zero() -> None:
    """`len(seed_variants) <= 1` matched the EMPTY list too and returned `{0: id}` — a
    mapping into a list with no index 0. It was harmless only because the one caller routed
    empties elsewhere before reaching it, which is the kind of safety that disappears the
    first time a second caller appears. Asserted on `match_variants` directly, since that is
    where the trap lives.
    """
    live = parse_product_js({"variants": [_live("11", "30ml")]})
    mapping, reason = match_variants([], live)

    assert mapping == {}
    assert reason == "seed_has_no_variants"


def test_no_live_variants_is_distinguishable_from_no_match() -> None:
    """Different causes need different fixes: an unreachable/blocked page is a crawl
    problem, an unmatched label is a matching problem."""
    assert match_variants([{"title": "30ml"}], [])[1] == "no_live_variants"
    assert match_variants([], parse_product_js({"variants": [_live("11", "a"), _live("22", "b")]}))[1] == "seed_has_no_variants"


def test_a_duplicated_live_id_cannot_be_claimed_twice() -> None:
    live = parse_product_js({"variants": [_live("11", "30ml"), _live("11", "30ml")]})
    mapping, reason = match_variants([{"title": "30ml"}], live)

    assert mapping == {0: "11"}
    assert reason == "sole_variant"


# ---------------------------------------------------------------- applying to seed_data

def test_apply_never_mutates_its_argument() -> None:
    """The caller diffs before/after to decide whether to write at all; in-place mutation
    would make every row look changed."""
    seed = {"variants": [{"title": "30ml"}]}
    live = parse_product_js({"variants": [_live("11", "30ml")]})
    stamp_variant_ids(seed.get('variants', []), live)

    assert seed == {"variants": [{"title": "30ml"}]}


def test_the_existing_variant_id_field_is_left_alone() -> None:
    """`variant_id` is read by five services that match it against catalog SKU identity.
    Overwriting a SKU-shaped value with a Shopify numeric id changes what those matches
    mean, so the numeric id lands on its own key."""
    seed = {"variants": [{"title": "30ml", "variant_id": "TO-001", "sku": "TO-001"}]}
    live = parse_product_js({"variants": [_live("11", "30ml")]})
    out, _ = stamp_variant_ids(seed.get('variants', []), live)

    assert out[0]["variant_id"] == "TO-001"
    assert out[0]["shopify_variant_id"] == "11"


def test_an_existing_shopify_variant_id_is_never_overwritten() -> None:
    """A value already there was verified or hand-set; a later crawl is not better
    evidence than that."""
    seed = {"variants": [{"title": "30ml", "shopify_variant_id": "999"}]}
    live = parse_product_js({"variants": [_live("11", "30ml")]})
    out, report = stamp_variant_ids(seed.get('variants', []), live)

    assert out[0]["shopify_variant_id"] == "999"
    assert report["stamped"] == 0
    assert report["action"] == "unchanged"


def test_an_ambiguous_seed_is_left_completely_untouched() -> None:
    seed = {"variants": [{"title": "Shade"}, {"title": "Shade"}]}
    live = parse_product_js({"variants": [_live("11", "Shade 01"), _live("22", "Shade 02")]})
    out, report = stamp_variant_ids(seed.get('variants', []), live)

    assert out == seed["variants"]
    assert report["stamped"] == 0
    assert report["reason"] == "no_confident_match"


def test_the_recovered_id_is_one_the_cart_builder_will_accept() -> None:
    """End-to-end contract check. This module exists to feed
    `outbound_links_service.shopify_cart_base_url`, which refuses to fabricate a variant id
    — so a value that function would reject is worthless no matter how confidently matched."""
    from services.outbound_links_service import shopify_cart_base_url

    live = parse_product_js({"variants": [_live("41234567890123", "30ml")]})
    out, _ = stamp_variant_ids([{"title": "30ml"}], live)
    recovered = out[0]["shopify_variant_id"]

    assert shopify_cart_base_url(shop_domain="genabelle.com", variant_id=recovered, quantity=1) == (
        "https://genabelle.com/cart/41234567890123:1"
    )


# ---------------------------------------------------------------- the script's own logic
# Selection and outcome classification. Cheap to pin, and the classification is what makes
# a run's report actionable: "blocked" and "the handle is dead" need different responses.

def test_already_covered_reads_SNAPSHOT_variants_not_top_level() -> None:
    """The crawl cohort keeps its variants under `snapshot`, while the serving readers
    prefer TOP-LEVEL. Reading the wrong one made a seed with real variants look empty — and
    the earlier version then created a fabricated top-level array that SHADOWED them.

    A partially-covered row must also stay a candidate, or the first match on a multi-variant
    product permanently excludes its siblings.
    """
    from scripts.backfill_shopify_variant_ids import already_covered, snapshot_variants

    assert already_covered({}) is False
    assert already_covered({"snapshot": {"variants": []}}) is False
    assert already_covered({"snapshot": {"variants": [{"shopify_variant_id": "1"}]}}) is True
    assert already_covered({"snapshot": {"variants": [{"shopify_variant_id": "1"}, {}]}}) is False
    assert already_covered({"snapshot": {"variants": [{"shopify_variant_id": "  "}]}}) is False
    # top-level is NOT where this script looks or writes
    assert already_covered({"variants": [{"shopify_variant_id": "1"}]}) is False
    assert snapshot_variants({"variants": [{"a": 1}]}) == []


def test_seed_data_is_read_whether_the_driver_hands_back_dict_or_text() -> None:
    """JSONB comes back as a dict on asyncpg and as a string on some paths. An unparseable
    one must be SKIPPED, never coerced to an empty document and written back."""
    from scripts.backfill_shopify_variant_ids import _seed_data_of

    assert _seed_data_of({"seed_data": {"a": 1}}) == {"a": 1}
    assert _seed_data_of({"seed_data": '{"a": 1}'}) == {"a": 1}
    # None means UNREADABLE -> skip. Returning {} here was a document-destroying bug: the
    # caller stamped onto the empty dict and wrote it as the WHOLE seed_data.
    assert _seed_data_of({"seed_data": "not json"}) is None
    assert _seed_data_of({"seed_data": '["a list"]'}) is None
    assert _seed_data_of({"seed_data": None}) is None
    assert _seed_data_of({}) is None


@pytest.mark.parametrize(
    "status,content_type,body,expected",
    [
        (200, "application/json", {"variants": []}, "ok"),
        # A block and a dead handle need different responses — one is our IP, the other is
        # the seed's URL — so they must not collapse into one "failed" bucket.
        (429, "application/json", None, "rate_limited"),
        (404, "text/html", None, "dead_handle"),
        (503, "text/html", None, "http_503"),
        # The common soft-404: a themed HTML page served with status 200.
        (200, "text/html", None, "not_json"),
    ],
)
def test_fetch_outcomes_are_classified_not_lumped(status, content_type, body, expected) -> None:
    import asyncio as _asyncio

    from scripts.backfill_shopify_variant_ids import fetch_product_js

    class _Resp:
        def __init__(self):
            self.status_code = status
            self.headers = {"content-type": content_type}

        def json(self):
            if body is None:
                raise ValueError("no body")
            return body

    class _Client:
        async def get(self, *a, **k):
            return _Resp()

    payload, outcome = _asyncio.run(fetch_product_js(_Client(), "https://b.com/products/h.js"))
    assert outcome == expected
    assert (payload is not None) == (expected == "ok")


def test_a_transport_error_is_an_outcome_not_an_exception() -> None:
    """One unreachable domain must not end a 200-row sweep."""
    import asyncio as _asyncio

    from scripts.backfill_shopify_variant_ids import fetch_product_js

    class _Client:
        async def get(self, *a, **k):
            raise TimeoutError("connect timed out")

    payload, outcome = _asyncio.run(fetch_product_js(_Client(), "https://b.com/products/h.js"))
    assert payload is None
    assert outcome == "error:TimeoutError"

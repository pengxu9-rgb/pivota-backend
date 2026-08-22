"""The matching rule must refuse rather than guess.

A wrong Shopify variant id is worse than none: `shopify_cart_base_url` builds
`/cart/{id}:1` from it, so a mismatch silently adds the wrong size to the buyer's cart and
they complete a purchase we mis-specified. These tests are mostly about the cases where the
right answer is "no answer".

Fixtures are shaped from real `/products/<handle>.js` bodies observed on 2026-08-21 while
probing the K-beauty cohort (numeric ids, `price` in minor units, `options` array).

SCOPE. The decision logic, plus the consumer-side rules that shipped in #1813 — the tests at
the bottom drive the real `_external_seed_redirect_identity` and `_make_external_redirect_url`.
Nothing here touches a database or the network. The producer's SQL is covered separately, and
against a real Postgres, by tests/test_backfill_shopify_variant_ids_postgres.py.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services.shopify_variant_identity import (
    match_variants,
    parse_product_js,
    product_js_url,
    sole_stamped_variant_id,
    stamp_variant_ids,
    storefront_is_shopify,
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


# ---------------------------------------------------------------- the consumer slice
# Round 3 established the recovered id was inert: _make_external_redirect_url gates the
# cart permalink on platform == "shopify", and a crawl seed's platform is the INTAKE LANE
# ("external_seed"), not the storefront's software. These pin the evidence-gated bridge —
# and, unlike the reverted attempt, the bridge touches ONLY the redirect identity: no
# serving read (price/currency/availability) is in its blast radius by construction.

def _crawl_row(**overrides):
    row = {
        "id": "eps_1",
        "attached_product_key": "prod::external_seed::external_seed::ext_abc123",
        "attached_variant_id": None,
        "domain": "genabelle.com",
        "destination_url": "https://genabelle.com/products/melacare-jelly-touch-dual-pad",
        "seller_ref": "seller:genabelle.com",
        "seed_kind": "self",
    }
    row.update(overrides)
    return row


def _evidence_seed(*ids, platform_key: bool = True):
    snapshot = {"variants": [{"title": f"v{i}", "shopify_variant_id": vid} for i, vid in enumerate(ids)]}
    if platform_key:
        snapshot["storefront_platform"] = "shopify"
        snapshot["storefront_platform_source"] = "products_js_v1"
    return {"snapshot": snapshot}


def test_without_evidence_the_identity_is_byte_identical_to_before() -> None:
    """The no-regression control: a seed the backfill never touched must flow exactly as it
    does on main — lane label kept, no variant invented."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(row=_crawl_row(), seed_data={"snapshot": {}})

    assert identity["platform"] == "external_seed"
    assert identity["variant_id"] is None


def test_evidence_flips_the_lane_label_and_supplies_the_sole_numeric_id() -> None:
    """The recovered id rides `cart_variant_id` — a channel only the permalink reads —
    while `variant_id` (which feeds the attribution ctx) stays exactly what it was. Round 4
    found the attribution layer cross-fills product<->variant ids BOTH ways, so a numeric
    id on the old channel leaked up a grain into canonical_product_id."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(), seed_data=_evidence_seed("41234567890123")
    )

    assert identity["platform"] == "shopify"
    assert identity["cart_variant_id"] == "41234567890123"
    assert identity["variant_id"] is None, "attribution must stay byte-identical to pre-change"


def test_stamped_ids_alone_are_evidence_even_without_the_platform_key() -> None:
    """Stamped ids can only come from a successful .js parse, so they prove Shopify-ness
    by themselves — rows stamped before the producer learned to write the platform key
    must not be stranded."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(), seed_data=_evidence_seed("41234567890123", platform_key=False)
    )

    assert identity["platform"] == "shopify"
    assert identity["cart_variant_id"] == "41234567890123"


def test_two_stamped_ids_flip_the_platform_but_refuse_to_pick_a_variant() -> None:
    """Product-grain redirect, buyer has not chosen: prefilling either of two variants is
    the wrong-size hazard. The permalink is declined (non-numeric variant -> referral_only)
    while the platform evidence still stands."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(), seed_data=_evidence_seed("111", "222")
    )

    assert identity["platform"] == "shopify"
    assert identity["cart_variant_id"] is None
    assert identity["variant_id"] is None


def test_a_real_attached_platform_is_never_overridden_by_snapshot_evidence() -> None:
    """A writer-verified platform is identity; crawl evidence never outranks it."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(attached_product_key="prod::merch_1::wix::w123"),
        seed_data=_evidence_seed("41234567890123"),
    )

    assert identity["platform"] == "wix"


def test_the_stamped_id_wins_the_cart_even_when_another_numeric_id_is_present() -> None:
    """CORRECTED IN ROUND 5. This test previously asserted the opposite — "a numeric id in
    hand needs no second channel" — and that invariant was wrong.

    Being all-digits is not evidence of being a SHOPIFY variant id. `attached_variant_id`
    and the `_seed_offer_variant_id` chain both routinely carry numeric SKUs, and treating
    those as permalink-ready built carts from numbers Shopify never issued. When the platform
    label is evidence-derived, the stamped id is the only value with evidence behind it, so
    it owns the cart. `variant_id` is untouched and still owns attribution.
    """
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(attached_variant_id="40000000000001"),
        seed_data=_evidence_seed("41234567890123"),
    )

    assert identity["variant_id"] == "40000000000001", "attribution unchanged"
    assert identity["cart_variant_id"] == "41234567890123", "the cart uses the EVIDENCED id"


def _dest_of(redirect_url: str) -> str:
    """Decode the /r?token= payload and return the destination the buyer would land on."""
    import base64
    import json as _json
    from urllib.parse import parse_qs, urlparse

    token = parse_qs(urlparse(redirect_url).query)["token"][0]
    payload_b64 = token.split(".")[0]
    padded = payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4)
    return _json.loads(base64.urlsafe_b64decode(padded))["dest"]


def test_end_to_end_an_evidence_stamped_seed_redirects_into_a_prefilled_cart() -> None:
    """The whole chain, through the REAL redirect builder: identity -> is_shopify gate ->
    cart permalink -> signed token, decoded to the URL the buyer actually lands on."""
    import asyncio

    from routes.agent_shop_gateway import (
        _external_seed_redirect_identity,
        _make_external_redirect_url,
    )

    identity = _external_seed_redirect_identity(
        row=_crawl_row(), seed_data=_evidence_seed("41234567890123")
    )
    redirect_url = asyncio.run(
        _make_external_redirect_url(
            market="US",
            tool="*",
            destination_url="https://genabelle.com/products/melacare-jelly-touch-dual-pad",
            utm_template=None,
            ctx={"seedId": "eps_1"},
            allowed_domains=["genabelle.com"],
            merchant_id=identity["merchant_id"],
            product_id=identity["product_id"],
            variant_id=identity["variant_id"],
            cart_variant_id=identity["cart_variant_id"],
            shop_domain=identity["shop_domain"],
            platform=identity["platform"],
            seller_ref=identity["seller_ref"],
            seed_kind=identity["seed_kind"],
        )
    )

    dest = _dest_of(redirect_url)
    assert dest.startswith("https://genabelle.com/cart/41234567890123:1"), dest
    # THE ATTRIBUTION BOUNDARY: the numeric id must never enter the signed token ctx, where
    # commerce_attribution_service would cross-fill it into canonical_product_id.
    import base64 as _b64, json as _j
    from urllib.parse import parse_qs as _pq, urlparse as _up
    tok = _pq(_up(redirect_url).query)["token"][0].split(".")[0]
    ctx = _j.loads(_b64.urlsafe_b64decode(tok + "=" * ((4 - len(tok) % 4) % 4)))["ctx"]
    assert "41234567890123" not in _j.dumps({k: v for k, v in ctx.items() if k != "dest"}), ctx
    assert "attributes%5Bpivota_click_id%5D=" in dest or "attributes[pivota_click_id]=" in dest


def test_end_to_end_control_the_same_seed_without_evidence_stays_referral_only() -> None:
    """Proves the previous test passes BECAUSE of the evidence, not incidentally — and
    that untouched rows keep today's behaviour exactly."""
    import asyncio

    from routes.agent_shop_gateway import (
        _external_seed_redirect_identity,
        _make_external_redirect_url,
    )

    identity = _external_seed_redirect_identity(row=_crawl_row(), seed_data={"snapshot": {}})
    redirect_url = asyncio.run(
        _make_external_redirect_url(
            market="US",
            tool="*",
            destination_url="https://genabelle.com/products/melacare-jelly-touch-dual-pad",
            utm_template=None,
            ctx={"seedId": "eps_1"},
            allowed_domains=["genabelle.com"],
            merchant_id=identity["merchant_id"],
            product_id=identity["product_id"],
            variant_id=identity["variant_id"],
            cart_variant_id=identity["cart_variant_id"],
            shop_domain=identity["shop_domain"],
            platform=identity["platform"],
            seller_ref=identity["seller_ref"],
            seed_kind=identity["seed_kind"],
        )
    )

    dest = _dest_of(redirect_url)
    assert "/cart/" not in dest, dest
    assert dest.startswith("https://genabelle.com/products/melacare-jelly-touch-dual-pad")


def test_a_sku_shaped_offer_variant_id_is_preserved_for_attribution() -> None:
    """Round-4 F2: the funnel joins canonical_variant_id against catalog sku aliases, so a
    SKU-shaped id that joined before must keep joining. The cart gets the numeric id on its
    own channel; attribution keeps the SKU."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(), seed_data=_evidence_seed("41234567890123"), offer_variant_id="TO-001"
    )

    assert identity["variant_id"] == "TO-001"
    assert identity["cart_variant_id"] == "41234567890123"


def test_a_partial_stamp_on_a_multi_variant_product_declines_the_cart() -> None:
    """Round-4 F3: one recovered id is not one purchasable variant. match_variants supports
    a partial stamp, so a 3-variant product with 1 stamped id must NOT prefill — that is an
    arbitrary variant of a product the buyer has not chosen from."""
    seed = {"snapshot": {"variants": [
        {"title": "30ml", "shopify_variant_id": "11"},
        {"title": "50ml"},
        {"title": "100ml"},
    ]}}

    assert sole_stamped_variant_id(seed) is None
    assert storefront_is_shopify(seed) is True, "the evidence still stands; only the prefill declines"


def test_the_platform_key_alone_is_evidence() -> None:
    """Round-4 F5: the PRIMARY documented evidence key had zero coverage — every fixture
    also carried stamped ids, so deleting the whole storefront_platform branch survived."""
    assert storefront_is_shopify({"snapshot": {"storefront_platform": "shopify", "variants": []}}) is True
    assert storefront_is_shopify({"snapshot": {"storefront_platform": " SHOPIFY "}}) is True
    assert storefront_is_shopify({"snapshot": {"storefront_platform": "woocommerce"}}) is False


def test_junk_stamped_values_are_not_evidence() -> None:
    """Round-4 F4: scripts/recover_seed_data_from_catalog_extract lands arbitrary variant
    keys from an external extract service, unvalidated. Non-numeric junk must neither prove
    Shopify-ness nor prefill a cart."""
    junk = {"snapshot": {"variants": [{"shopify_variant_id": True}]}}
    assert storefront_is_shopify(junk) is False
    assert sole_stamped_variant_id(junk) is None


def test_a_numeric_SKU_can_never_be_used_as_a_cart_variant_id() -> None:
    """ROUND-5 P0. `variant_id` here comes from `_seed_offer_variant_id`
    (variant_id | variantId | sku | sku_id | id), and a plain NUMERIC SKU satisfies
    `extract_shopify_numeric_variant_id` by design — so the old guard skipped its branch and
    the builder's `cart_variant_id or variant_id` fallback prefilled a cart from a number
    Shopify never issued as a variant id. On a multi-variant product it did so even though
    `sole_stamped_variant_id` had deliberately declined.
    """
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(),
        seed_data=_evidence_seed("41234567890123"),
        offer_variant_id="80072940",  # an all-digit SKU, not a Shopify variant id
    )

    assert identity["cart_variant_id"] == "41234567890123", "the STAMPED id, not the SKU"
    assert identity["variant_id"] == "80072940", "attribution still sees the SKU"


def test_a_declined_prefill_is_not_rescued_by_a_numeric_sku() -> None:
    """The multi-variant case, which is the dangerous one: the guard declines, and the
    permalink must decline with it rather than fall back to whatever digits are lying around."""
    import asyncio

    from routes.agent_shop_gateway import (
        _external_seed_redirect_identity,
        _make_external_redirect_url,
    )

    seed = {"snapshot": {"storefront_platform": "shopify", "variants": [
        {"title": "30ml", "shopify_variant_id": "41234567890123"},
        {"title": "50ml"},
        {"title": "100ml"},
    ]}}
    identity = _external_seed_redirect_identity(
        row=_crawl_row(), seed_data=seed, offer_variant_id="80072940"
    )
    assert identity["cart_variant_id"] is None, "3 variants, 1 stamped -> decline"

    redirect_url = asyncio.run(
        _make_external_redirect_url(
            market="US", tool="*",
            destination_url="https://genabelle.com/products/melacare-jelly-touch-dual-pad",
            utm_template=None, ctx={"seedId": "eps_1"}, allowed_domains=["genabelle.com"],
            merchant_id=identity["merchant_id"], product_id=identity["product_id"],
            variant_id=identity["variant_id"],
            cart_variant_id=identity["cart_variant_id"],
            shop_domain=identity["shop_domain"], platform=identity["platform"],
            seller_ref=identity["seller_ref"], seed_kind=identity["seed_kind"],
        )
    )

    dest = _dest_of(redirect_url)
    assert "/cart/" not in dest, dest
    assert "80072940" not in dest, "a SKU must never reach a cart URL"


def test_a_caller_that_cannot_justify_an_id_gets_no_cart_at_all() -> None:
    """There is no fallback. `variant_id` alone — the attribution value — must never reach the
    permalink, however Shopify-shaped it looks. A caller that can justify its id passes
    `cart_variant_id` explicitly; one that cannot gets referral_only."""
    import asyncio

    from routes.agent_shop_gateway import _make_external_redirect_url

    without = asyncio.run(
        _make_external_redirect_url(
            market="US", tool="*", destination_url="https://shop.example/products/x",
            utm_template=None, ctx={"source": "connected_catalog"},
            allowed_domains=["shop.example"], merchant_id="merch_1", product_id="p1",
            variant_id="41234567890123", cart_variant_id=None,
            shop_domain="shop.example", platform="shopify",
        )
    )
    assert "/cart/" not in _dest_of(without), "variant_id alone must not build a cart"

    # the connected lane justifies its id by provenance and passes it deliberately
    with_claim = asyncio.run(
        _make_external_redirect_url(
            market="US", tool="*", destination_url="https://shop.example/products/x",
            utm_template=None, ctx={"source": "connected_catalog"},
            allowed_domains=["shop.example"], merchant_id="merch_1", product_id="p1",
            variant_id="41234567890123", cart_variant_id="41234567890123",
            shop_domain="shop.example", platform="shopify",
        )
    )
    assert "/cart/41234567890123:1" in _dest_of(with_claim)


def test_an_attached_shopify_seed_uses_catalog_identity_not_the_sku_chain() -> None:
    """The door round 5 left open: platform is writer-verified (no evidence flip), so the old
    conditional fallback still fired and built a cart from the SKU chain. The attached variant
    id is catalog identity and may be used; a SKU may not."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    sku_only = _external_seed_redirect_identity(
        row=_crawl_row(attached_product_key="prod::merch_1::shopify::p1"),
        seed_data={}, offer_variant_id="80072940",
    )
    assert sku_only["platform"] == "shopify"
    assert sku_only["cart_variant_id"] is None, "a SKU is not a Shopify variant id"

    attached = _external_seed_redirect_identity(
        row=_crawl_row(attached_product_key="prod::merch_1::shopify::p1",
                       attached_variant_id="41234567890123"),
        seed_data={}, offer_variant_id="80072940",
    )
    assert attached["cart_variant_id"] == "41234567890123"

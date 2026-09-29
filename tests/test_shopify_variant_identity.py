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

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from services.shopify_variant_identity import (
    match_variants,
    parse_product_js,
    product_js_url,
    sole_stamped_variant_id,
    sole_verified_cart_variant_id,
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


def test_cart_proof_requires_fresh_same_url_sole_live_variant() -> None:
    now = datetime.now(timezone.utc)
    seed = {"snapshot": {"variants": [{"shopify_variant_id": "11"}],
                         "shopify_cart_proof": {
                             "source": "products_js_v1",
                             "product_js_url": "https://brand.com/products/serum.js",
                             "live_variant_count": 1,
                             "variant_id": "11",
                             "checked_at": now.isoformat(),
                         }}}
    url = ["https://brand.com/products/serum"]
    assert sole_verified_cart_variant_id(seed, product_urls=url, shop_domain="brand.com", now=now) == "11"
    seed["snapshot"]["shopify_cart_proof"]["live_variant_count"] = 2
    assert sole_verified_cart_variant_id(seed, product_urls=url, shop_domain="brand.com", now=now) is None
    seed["snapshot"]["shopify_cart_proof"]["live_variant_count"] = 1
    assert sole_verified_cart_variant_id(seed, product_urls=["https://brand.com/products/other"], shop_domain="brand.com", now=now) is None
    assert sole_verified_cart_variant_id(
        seed, product_urls=["https://brand.com/products/new"],
        shop_domain="brand.com", now=now,
    ) is None
    assert sole_verified_cart_variant_id(seed, product_urls=url, shop_domain="other.com", now=now) is None
    for unsafe_url in ("http://brand.com/products/serum.js",
                       "https://brand.com:8080/products/serum.js"):
        seed["snapshot"]["shopify_cart_proof"]["product_js_url"] = unsafe_url
        assert sole_verified_cart_variant_id(
            seed, product_urls=[unsafe_url], shop_domain="brand.com", now=now,
        ) is None
    seed["snapshot"]["shopify_cart_proof"]["product_js_url"] = "https://brand.com/products/serum.js"
    assert sole_verified_cart_variant_id(seed, product_urls=url, shop_domain="brand.com", now=now + timedelta(days=8)) is None
    seed["snapshot"]["variants"].append("unknown second variant")
    assert sole_verified_cart_variant_id(seed, product_urls=url, shop_domain="brand.com", now=now) is None
    seed["snapshot"]["variants"].pop()
    seed["snapshot"].pop("shopify_cart_proof")
    assert sole_verified_cart_variant_id(seed, product_urls=url, shop_domain="brand.com", now=now) is None


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
            market_observed=True,
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
            market_observed=True,
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
            market_observed=True,
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
            market_observed=True,
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
            market_observed=True,
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


def test_the_attached_branch_requires_the_platform_to_be_shopify() -> None:
    """Round-6 F5: the platform gate on the attached branch was untested. A wix-attached seed
    on a .myshopify.com host would otherwise offer a cart id for a non-Shopify storefront."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    wix = _external_seed_redirect_identity(
        row=_crawl_row(attached_product_key="prod::merch_1::wix::p1",
                       attached_variant_id="41234567890123"),
        seed_data={},
    )
    assert wix["platform"] == "wix"
    assert wix["cart_variant_id"] is None


def test_the_production_default_attached_variant_id_is_not_a_cart_id() -> None:
    """Round-6 F5: `attach_external_seed` defaults attached_variant_id to the sentinel "∅",
    so this is the value most attached seeds actually carry. It must not reach a cart."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(attached_product_key="prod::merch_1::shopify::p1",
                       attached_variant_id="∅"),
        seed_data={},
    )
    assert identity["cart_variant_id"] is None


def test_storefront_evidence_outranks_an_operator_typed_attached_id() -> None:
    """Round-6 F5: nothing pinned which wins when BOTH exist. Evidence is the stronger
    provenance — it came from the storefront itself, not from a text field — so a seed whose
    lane label is still `external_seed` takes the stamped id even though an attached variant
    id is present."""
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(attached_variant_id="99999999999999"),
        seed_data=_evidence_seed("41234567890123"),
    )
    assert identity["cart_variant_id"] == "41234567890123"


def test_an_attached_shopify_seed_prefers_catalog_identity_over_storefront_evidence() -> None:
    """Pins a DELIBERATE choice that nothing previously constrained.

    When a seed is BOTH attached-Shopify (platform writer-verified) and carries storefront
    evidence, two ids compete: `attached_variant_id` (catalog identity, but operator-typed
    and unenforced — see the provenance note in _external_seed_redirect_identity) and the
    stamped id (proven by the storefront's own /products/x.js).

    Current behaviour: the attached id wins, because a writer-verified platform means a human
    or a sync deliberately linked this seed to a catalog row, and overriding that with a crawl
    artefact would make the attachment meaningless. The counter-argument is real — storefront
    evidence has the stronger provenance — so this is pinned rather than left to chance, and
    it is the assertion to change if that trade is ever revisited.
    """
    from routes.agent_shop_gateway import _external_seed_redirect_identity

    identity = _external_seed_redirect_identity(
        row=_crawl_row(attached_product_key="prod::merch_1::shopify::p1",
                       attached_variant_id="99999999999999"),
        seed_data=_evidence_seed("41234567890123"),
    )

    assert identity["platform"] == "shopify"
    assert identity["cart_variant_id"] == "99999999999999", "catalog identity wins here"


def test_resolve_cart_permalink_refuses_a_non_shopify_storefront() -> None:
    """The single cart decision, tested directly.

    The platform gate only bites when the platform is NOT Shopify and a numeric id IS present
    — a wix/woo seed carrying a numeric catalog variant id. Without it, `/cart/<id>:1` would be
    built for a storefront that has no such route, turning a working PDP referral into a 404.
    The offers.resolve fixtures cannot reach this combination, so it is pinned here.
    """
    from routes.agent_shop_gateway import resolve_cart_permalink

    assert resolve_cart_permalink(
        destination_url="https://brand.com/products/x", shop_domain="brand.com",
        platform="wix", cart_variant_id="41234567890123",
    ) is None
    assert resolve_cart_permalink(
        destination_url="https://brand.com/products/x", shop_domain="brand.com",
        platform="shopify", cart_variant_id="41234567890123",
    ) == "https://brand.com/cart/41234567890123:1"


def test_resolve_cart_permalink_never_fabricates_from_a_non_numeric_id() -> None:
    from routes.agent_shop_gateway import resolve_cart_permalink

    for bad in ("SKU-1", "∅", "", None, "cv_abc123"):
        assert resolve_cart_permalink(
            destination_url="https://brand.com/products/x", shop_domain="brand.com",
            platform="shopify", cart_variant_id=bad,
        ) is None, bad


def test_a_myshopify_host_is_shopify_even_without_a_platform_label() -> None:
    """The host check is the second half of the gate — a seed with no platform label but a
    *.myshopify.com destination is unambiguously Shopify."""
    from routes.agent_shop_gateway import resolve_cart_permalink

    assert resolve_cart_permalink(
        destination_url="https://shop.myshopify.com/products/x", shop_domain=None,
        platform=None, cart_variant_id="41234567890123",
    ) == "https://shop.myshopify.com/cart/41234567890123:1"


# ---------------------------------------------------------------- the NAMED-variant cart proof
#
# Option 1 (Peng, 2026-09-29): a multi-variant product is buyable through the Reap cart link when
# the seed NAMES one variant and the backfill's products.js fetch proves that variant exists and is
# available. Every case is built from REAL shapes: the live judydoll products.js (8 shades,
# trimmed), the live seed (staging epsv_38ad88d436c32e24ba7c6446, trimmed), and the proof the
# backfill's own `build_cart_proof` writes from those two -- never a hand-built proof dict.

import copy  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from scripts.backfill_shopify_variant_ids import build_cart_proof  # noqa: E402
from services.shopify_variant_identity import (  # noqa: E402
    CART_PROOF_SCOPE_NAMED,
    CART_PROOF_SCOPE_SOLE,
    named_cart_variant_id,
    named_verified_cart_variant_id,
    verified_cart_variant_id,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
JUDY_JS = json.loads((_FIXTURES / "judydoll_silky_matte_lip_ink_products_js_2026_09_29.json").read_text())
JUDY_SEED = json.loads((_FIXTURES / "judydoll_silky_matte_lip_ink_seed_2026_09_29.json").read_text())
JUDY_HOST = "judydoll.com"
JUDY_VARIANT = "49819267301653"          # 07 BURGUNDY INK, the shade the seed names
JUDY_OTHER = "49819267170581"            # 01 PETAL INK, a sibling shade
JUDY_JS_URL = "https://judydoll.com/products/silky-matte-lip-ink.js"
JUDY_STAGING_URLS = [JUDY_SEED["canonical_url"]]          # the route passes canonical or destination
JUDY_PROD_URLS = [JUDY_SEED["destination_url"]]           # prod canonical carries ?variant=
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _judy_js(**variant_overrides: Dict[str, Any]) -> Dict[str, Any]:
    payload = copy.deepcopy(JUDY_JS)
    for variant in payload["variants"]:
        variant.update(variant_overrides.get(str(variant["id"]), {}))
    return payload


def _backfilled(payload: Dict[str, Any] | None = None, *, page_url: str | None = None,
                seed_data: Dict[str, Any] | None = None, checked_at: datetime = T0) -> Dict[str, Any]:
    """What `scripts/backfill_shopify_variant_ids.run` writes into seed_data for this fetch: the
    stamped variants and the proof, both from the backfill's own functions."""
    seed = copy.deepcopy(seed_data if seed_data is not None else JUDY_SEED["seed_data"])
    payload = payload if payload is not None else JUDY_JS
    live = parse_product_js(payload)
    new_variants, _ = stamp_variant_ids(seed["snapshot"]["variants"], live)
    proof = build_cart_proof(
        seed, new_variants, payload, live, js_url=JUDY_JS_URL,
        page_url=page_url or JUDY_SEED["canonical_url"], shop_host=JUDY_HOST, checked_at=checked_at,
    )
    seed["snapshot"].update({"variants": new_variants, "storefront_platform": "shopify",
                             "storefront_platform_source": "products_js_v1",
                             "shopify_cart_proof": proof})
    return seed


def _verify(seed_data: Any, urls: List[str] = JUDY_STAGING_URLS, *, catalog: str | None = JUDY_VARIANT,
            now: datetime = T0 + timedelta(hours=1), host: str = JUDY_HOST):
    return verified_cart_variant_id(seed_data, product_urls=urls, shop_domain=host,
                                    catalog_variant_id=catalog, now=now)


def test_the_live_judydoll_fetch_writes_a_named_variant_proof() -> None:
    """The backfill's ACTUAL output on the live shapes: a label match stamps the seed's one entry
    (07 BURGUNDY INK), and the 8-variant storefront yields a named proof, never a sole one."""
    seed = _backfilled()
    assert seed["snapshot"]["variants"][0]["shopify_variant_id"] == JUDY_VARIANT
    assert seed["snapshot"]["shopify_cart_proof"] == {
        "source": "products_js_v1", "scope": "named_variant", "product_js_url": JUDY_JS_URL,
        "variant_id": JUDY_VARIANT, "variant_title": "07 BURGUNDY INK", "available": True,
        "live_variant_count": 8, "price_minor": 1399, "checked_at": T0.isoformat(),
    }


@pytest.mark.parametrize("urls", [JUDY_STAGING_URLS, JUDY_PROD_URLS], ids=["staging", "prod"])
def test_ACCEPT_a_multi_variant_product_with_the_named_variant_available(urls) -> None:
    proven = _verify(_backfilled(page_url=urls[0]), urls)
    assert proven == (JUDY_VARIANT, CART_PROOF_SCOPE_NAMED, "07 BURGUNDY INK")
    assert proven.variant_id == JUDY_VARIANT and proven.scope == "named_variant"


def test_the_sole_path_refuses_a_named_proof_and_the_named_path_refuses_a_sole_one() -> None:
    named = _backfilled()
    assert sole_verified_cart_variant_id(named, product_urls=JUDY_STAGING_URLS,
                                         shop_domain=JUDY_HOST, now=T0) is None
    # even when its informational count reads 1
    one = copy.deepcopy(named)
    one["snapshot"]["shopify_cart_proof"]["live_variant_count"] = 1
    assert sole_verified_cart_variant_id(one, product_urls=JUDY_STAGING_URLS,
                                         shop_domain=JUDY_HOST, now=T0) is None
    sole = _backfilled({"variants": [JUDY_JS["variants"][4]]})
    assert "scope" not in sole["snapshot"]["shopify_cart_proof"]
    assert named_verified_cart_variant_id(sole, product_urls=JUDY_STAGING_URLS,
                                          shop_domain=JUDY_HOST, now=T0) is None


def test_the_sole_variant_path_is_unchanged() -> None:
    """A one-variant storefront still writes the UNSCOPED proof (byte-for-byte the old keys) and is
    accepted as SOLE -- with or without a catalog variant, exactly as before."""
    seed = _backfilled({"variants": [JUDY_JS["variants"][4]]})
    assert seed["snapshot"]["shopify_cart_proof"] == {
        "source": "products_js_v1", "product_js_url": JUDY_JS_URL, "live_variant_count": 1,
        "variant_id": JUDY_VARIANT, "variant_title": "07 BURGUNDY INK", "checked_at": T0.isoformat(),
    }
    assert _verify(seed) == (JUDY_VARIANT, CART_PROOF_SCOPE_SOLE, "07 BURGUNDY INK")
    assert _verify(seed, catalog=None) == (JUDY_VARIANT, CART_PROOF_SCOPE_SOLE, "07 BURGUNDY INK")
    assert sole_verified_cart_variant_id(seed, product_urls=JUDY_STAGING_URLS,
                                         shop_domain=JUDY_HOST, now=T0) == JUDY_VARIANT


def test_a_fetch_supporting_a_sole_proof_never_writes_the_weaker_named_one() -> None:
    """A one-variant product whose seed ALSO names that variant by URL gets the sole proof."""
    seed = _backfilled({"variants": [JUDY_JS["variants"][4]]}, page_url=JUDY_SEED["destination_url"])
    assert "scope" not in seed["snapshot"]["shopify_cart_proof"]


def test_REFUSE_the_named_variant_absent_from_the_live_store() -> None:
    gone = _judy_js()
    gone["variants"] = [v for v in gone["variants"] if str(v["id"]) != JUDY_VARIANT]
    # the seed entry is stamped from an EARLIER fetch; the shade has since been delisted
    stamped = _backfilled()
    seed = _backfilled(gone, seed_data=stamped)
    assert seed["snapshot"]["shopify_cart_proof"] is None
    assert _verify(seed) is None


def test_REFUSE_the_named_variant_unavailable() -> None:
    seed = _backfilled(_judy_js(**{JUDY_VARIANT: {"available": False}}))
    assert seed["snapshot"]["shopify_cart_proof"] is None, "the backfill never writes it"
    forged = _backfilled()
    for bad in (False, None, "true", 1):
        forged["snapshot"]["shopify_cart_proof"]["available"] = bad
        assert _verify(forged) is None, bad
    del forged["snapshot"]["shopify_cart_proof"]["available"]
    assert _verify(forged) is None


def test_REFUSE_a_stale_or_future_proof() -> None:
    seed = _backfilled()
    assert _verify(seed, now=T0 + timedelta(days=7)) is not None
    assert _verify(seed, now=T0 + timedelta(days=7, seconds=1)) is None
    assert _verify(seed, now=T0 - timedelta(seconds=1)) is None
    naive = copy.deepcopy(seed)
    naive["snapshot"]["shopify_cart_proof"]["checked_at"] = "2026-09-29T12:00:00"
    assert _verify(naive) is None


def test_REFUSE_a_proof_variant_other_than_the_named_one() -> None:
    seed = _backfilled()
    seed["snapshot"]["shopify_cart_proof"]["variant_id"] = JUDY_OTHER
    assert _verify(seed) is None
    assert _verify(seed, catalog=JUDY_OTHER) is None


def test_REFUSE_a_catalog_variant_other_than_the_proven_one_or_none() -> None:
    seed = _backfilled()
    assert _verify(seed, catalog=JUDY_OTHER) is None
    assert _verify(seed, catalog=None) is None, "a placeholder-only row names no variant"


def test_REFUSE_a_seed_naming_two_variants() -> None:
    two = copy.deepcopy(JUDY_SEED["seed_data"])
    second = dict(two["snapshot"]["variants"][0], title="01 PETAL INK", option_value="01 PETAL INK",
                  display_label="Shade: 01 PETAL INK", sku="6978647800823", id=JUDY_OTHER,
                  options=[{"name": "Shade", "value": "01 PETAL INK", "axis_kind": "shade"}])
    two["snapshot"]["variants"].append(second)
    seed = _backfilled(seed_data=two)
    assert seed["snapshot"]["shopify_cart_proof"] is None
    # and a forged named proof on it is refused by the verifier too
    seed["snapshot"]["shopify_cart_proof"] = _backfilled()["snapshot"]["shopify_cart_proof"]
    assert _verify(seed) is None
    # two distinct `variant=` on the product URL(s)
    urls = [f"https://{JUDY_HOST}/products/silky-matte-lip-ink?variant={JUDY_VARIANT}&variant={JUDY_OTHER}"]
    assert named_cart_variant_id(_backfilled(), product_urls=urls, shop_domain=JUDY_HOST) is None
    assert _verify(_backfilled(), urls) is None


def test_REFUSE_a_url_variant_that_disagrees_with_the_stamped_one() -> None:
    other_url = f"https://{JUDY_HOST}/products/silky-matte-lip-ink?variant={JUDY_OTHER}"
    seed = _backfilled(page_url=other_url)
    assert seed["snapshot"]["shopify_cart_proof"] is None
    assert _verify(_backfilled(), [other_url]) is None


@pytest.mark.parametrize("url", [
    "http://judydoll.com/products/silky-matte-lip-ink",
    "https://judydoll.com:8443/products/silky-matte-lip-ink",
    "https://notjudydoll.com/products/silky-matte-lip-ink",
    "https://www.judydoll.com/products/silky-matte-lip-ink",
])
def test_REFUSE_a_proof_fetched_off_https_or_off_the_shop_host(url) -> None:
    js = product_js_url(url)
    seed = _backfilled()
    seed["snapshot"]["shopify_cart_proof"]["product_js_url"] = js
    assert _verify(seed, [url]) is None


def test_REFUSE_a_proof_for_another_products_url() -> None:
    seed = _backfilled()
    assert _verify(seed, [f"https://{JUDY_HOST}/products/other-lip"]) is None


def test_the_naming_rule_accepts_and_refuses_by_source_agreement() -> None:
    unstamped = JUDY_SEED["seed_data"]
    stamped = _backfilled()
    staging, prod = JUDY_STAGING_URLS, JUDY_PROD_URLS
    name = lambda seed, urls: named_cart_variant_id(seed, product_urls=urls, shop_domain=JUDY_HOST)  # noqa: E731
    # ACCEPT: stamped entry alone; stamped + agreeing URL; unstamped entry + URL.
    assert name(stamped, staging) == JUDY_VARIANT
    assert name(stamped, prod) == JUDY_VARIANT
    assert name(unstamped, prod) == JUDY_VARIANT
    # REFUSE: nothing names it; unreadable `variant=`; URL on another host names nothing.
    assert name(unstamped, staging) is None
    assert name(stamped, [f"https://{JUDY_HOST}/products/silky-matte-lip-ink?variant=abc"]) is None
    assert name(stamped, [f"https://{JUDY_HOST}/products/silky-matte-lip-ink?variant="]) is None
    assert name(unstamped, [f"https://other.com/products/silky-matte-lip-ink?variant={JUDY_VARIANT}"]) is None
    assert name(None, staging) is None and name({"snapshot": []}, staging) is None


def test_REFUSE_a_proof_from_any_other_source_in_either_scope() -> None:
    named = _backfilled()
    sole = _backfilled({"variants": [JUDY_JS["variants"][4]]})
    for seed in (named, sole):
        assert _verify(seed) is not None
        for source in ("products_json_v1", None, "operator"):
            forged = copy.deepcopy(seed)
            forged["snapshot"]["shopify_cart_proof"]["source"] = source
            assert _verify(forged) is None, source


def test_REFUSE_a_sole_proof_for_a_variant_other_than_the_stamped_one() -> None:
    sole = _backfilled({"variants": [JUDY_JS["variants"][4]]})
    sole["snapshot"]["shopify_cart_proof"]["variant_id"] = JUDY_OTHER
    assert _verify(sole) is None
    assert _verify(sole, catalog=JUDY_OTHER) is None


@pytest.mark.parametrize("scope", [None, "", "sole_variant", "named", "NAMED_VARIANT"])
def test_REFUSE_a_multi_variant_proof_not_scoped_named_variant(scope) -> None:
    """An 8-variant proof is accepted ONLY under the exact named_variant scope; unscoped it is a
    sole proof with the wrong count, and any other scope is unknown."""
    seed = _backfilled()
    proof = seed["snapshot"]["shopify_cart_proof"]
    if scope is None:
        del proof["scope"]
    else:
        proof["scope"] = scope
    assert _verify(seed) is None


def test_REFUSE_a_url_naming_one_variant_of_a_seed_with_several_entries() -> None:
    """A 2+ entry snapshot is a product-grain seed: it names NOTHING, whatever its URL says."""
    two = copy.deepcopy(JUDY_SEED["seed_data"])
    two["snapshot"]["variants"].append(dict(two["snapshot"]["variants"][0], title="01 PETAL INK"))
    assert named_cart_variant_id(two, product_urls=JUDY_PROD_URLS, shop_domain=JUDY_HOST) is None
    seed = _backfilled(seed_data=two, page_url=JUDY_SEED["destination_url"])
    assert seed["snapshot"]["shopify_cart_proof"] is None
    seed["snapshot"]["shopify_cart_proof"] = _backfilled(page_url=JUDY_SEED["destination_url"])["snapshot"]["shopify_cart_proof"]
    assert _verify(seed, JUDY_PROD_URLS) is None



# ---------------------------------------------------------------- #2459 review round

def test_the_proven_variant_carries_the_live_title_for_display() -> None:
    """F1: the buyer never picks the shade, so the proof records the storefront's own title and
    the verifier hands it on. Display only: the title is not part of any rule."""
    named = _verify(_backfilled())
    assert named.variant_title == "07 BURGUNDY INK" and named.scope == CART_PROOF_SCOPE_NAMED
    sole = _verify(_backfilled({"variants": [JUDY_JS["variants"][4]]}))
    assert sole.variant_title == "07 BURGUNDY INK" and sole.scope == CART_PROOF_SCOPE_SOLE
    untitled = _backfilled()
    del untitled["snapshot"]["shopify_cart_proof"]["variant_title"]
    assert _verify(untitled) == (JUDY_VARIANT, CART_PROOF_SCOPE_NAMED, None)
    junk = _backfilled()
    junk["snapshot"]["shopify_cart_proof"]["variant_title"] = {"not": "text"}
    assert _verify(junk).variant_title is None
    long = _backfilled(_judy_js(**{JUDY_VARIANT: {"title": "X" * 500}}))
    assert len(long["snapshot"]["shopify_cart_proof"]["variant_title"]) == 200


def _relabelled_as_petal(seed_data: Dict[str, Any]) -> Dict[str, Any]:
    """The reviewer's probe on the REAL seed: the entry's ids (and the snapshot's selected /
    default) still say 07 BURGUNDY INK, but every LABEL -- title, option, display label, sku --
    says 01 PETAL INK. A label match then stamps 01."""
    seed = copy.deepcopy(seed_data)
    entry = seed["snapshot"]["variants"][0]
    entry.update(title="01 PETAL INK", option_value="01 PETAL INK", display_label="Shade: 01 PETAL INK",
                 sku="6978647800823",
                 options=[{"name": "Shade", "value": "01 PETAL INK", "axis_kind": "shade"}])
    return seed


def test_REFUSE_a_label_stamp_that_contradicts_the_seeds_own_variant_id() -> None:
    """F4: the stamp lands on 01 by label, the seed's crawl-time ids say 07 -> names nothing, so
    the backfill writes no proof and a forged 01 proof is refused too."""
    probe = _relabelled_as_petal(JUDY_SEED["seed_data"])
    seed = _backfilled(seed_data=probe)
    assert seed["snapshot"]["variants"][0]["shopify_variant_id"] == JUDY_OTHER, "the label stamp"
    assert named_cart_variant_id(seed, product_urls=JUDY_STAGING_URLS, shop_domain=JUDY_HOST) is None
    assert seed["snapshot"]["shopify_cart_proof"] is None
    forged = copy.deepcopy(seed)
    forged["snapshot"]["shopify_cart_proof"] = dict(
        _backfilled()["snapshot"]["shopify_cart_proof"], variant_id=JUDY_OTHER)
    assert _verify(forged, catalog=JUDY_OTHER) is None


@pytest.mark.parametrize("where,key", [
    ("entry", "variant_id"), ("entry", "id"),
    ("snapshot", "selected_variant_id"), ("snapshot", "default_variant_id"),
    ("document", "selected_variant_id"), ("document", "default_variant_id"),
])
def test_every_variant_id_the_seed_records_must_agree_with_the_name(where, key) -> None:
    seed = _backfilled()
    assert _verify(seed) is not None
    target = {"entry": seed["snapshot"]["variants"][0], "snapshot": seed["snapshot"],
              "document": seed}[where]
    target[key] = JUDY_OTHER
    assert named_cart_variant_id(seed, product_urls=JUDY_STAGING_URLS, shop_domain=JUDY_HOST) is None
    assert _verify(seed) is None
    # a NON-numeric value claims nothing (the crawl writes SKU-shaped values in some lanes)
    target[key] = "sku-07-burgundy"
    assert _verify(seed) == (JUDY_VARIANT, CART_PROOF_SCOPE_NAMED, "07 BURGUNDY INK")
    target[key] = f"gid://shopify/ProductVariant/{JUDY_OTHER}"
    assert _verify(seed) is None


def test_the_parser_counts_only_a_boolean_true_as_available() -> None:
    """F5: `bool("false")` is True. Only the JSON boolean true is availability evidence."""
    for raw, expected in ((True, True), (False, False), ("false", False), ("true", False),
                          (1, False), (None, False)):
        parsed = parse_product_js({"variants": [{"id": 1, "title": "x", "available": raw}]})
        assert parsed[0]["available"] is expected, raw
    assert _backfilled(_judy_js(**{JUDY_VARIANT: {"available": "false"}}))["snapshot"]["shopify_cart_proof"] is None
    assert _backfilled(_judy_js(**{JUDY_VARIANT: {"available": "true"}}))["snapshot"]["shopify_cart_proof"] is None


def test_REFUSE_a_live_payload_listing_the_named_variant_twice() -> None:
    """F6: a storefront repeating the named id is not evidence of ONE purchasable variant."""
    payload = _judy_js()
    payload["variants"].append(dict(payload["variants"][4]))
    assert _backfilled(payload)["snapshot"]["shopify_cart_proof"] is None


def test_REFUSE_two_url_variants_even_with_nothing_else_to_disagree() -> None:
    """Two distinct `variant=` values name two variants, whichever the seed would otherwise back:
    an unstamped entry with no ids of its own, across one URL or two."""
    bare = {"snapshot": {"variants": [{"title": "07 BURGUNDY INK"}]}}
    base = f"https://{JUDY_HOST}/products/silky-matte-lip-ink"
    for urls in ([f"{base}?variant={JUDY_VARIANT}&variant={JUDY_OTHER}"],
                 [f"{base}?variant={JUDY_VARIANT}", f"{base}?variant={JUDY_OTHER}"]):
        assert named_cart_variant_id(bare, product_urls=urls, shop_domain=JUDY_HOST) is None, urls
    same_twice = [f"{base}?variant={JUDY_VARIANT}", f"{base}?variant={JUDY_VARIANT}&x=1"]
    assert named_cart_variant_id(bare, product_urls=same_twice, shop_domain=JUDY_HOST) == JUDY_VARIANT


# ---------------------------------------------------------------- variant title = merchant text
#
# The proof's `variant_title` is typed by the MERCHANT and reaches CartLinkItem, the purchase row
# and the 202/replay bodies. One rule (`services.text_normalization.clean_display_text`, via
# `clean_variant_title`) runs at the WRITER (build_cart_proof) and again at the READER
# (verified_cart_variant_id), so a proof written before the rule is cleaned on read.

from services.shopify_variant_identity import MAX_VARIANT_TITLE, clean_variant_title  # noqa: E402
from services.text_normalization import clean_display_text  # noqa: E402


def _written_title(title: Any) -> Any:
    """What the backfill WRITES into the proof when the storefront titles the named shade so."""
    return _backfilled(_judy_js(**{JUDY_VARIANT: {"title": title}}))["snapshot"]["shopify_cart_proof"]["variant_title"]


def _read_title(title: Any) -> Any:
    """What the verifier READS back from a proof that already holds `title` (an OLD proof)."""
    seed = _backfilled()
    seed["snapshot"]["shopify_cart_proof"]["variant_title"] = title
    return _verify(seed).variant_title


@pytest.mark.parametrize("title", [
    "07 BURGUNDY INK", "Default Title", "30 ml / Rose", "Café Crème", "Größe 2 · 50 ml",
    "樱花 01", "Shade 💄 Red", "<b>Rose</b> & \"Co\"",
])
def test_a_real_title_is_written_and_read_unchanged(title: str) -> None:
    """Real titles, HTML included (escaping is the renderer's job), pass through both ends."""
    assert _written_title(title) == title
    assert _read_title(title) == title


@pytest.mark.parametrize("dirty,clean", [
    ("07\nBURGUNDY INK", "07 BURGUNDY INK"),          # Cc whitespace -> ONE space, not glued
    ("07\tBURGUNDY  \r\n INK", "07 BURGUNDY INK"),    # a run of mixed whitespace -> one space
    ("07 BURG\x00UNDY\x7f INK\x85", "07 BURGUNDY INK"),  # Cc non-whitespace dropped; NEL folded
    ("07 \u202eBURGUNDY INK", "07 BURGUNDY INK"),     # Cf: RIGHT-TO-LEFT OVERRIDE
    ("\u202a\u202b\u202c\u202d07 BURGUNDY INK", "07 BURGUNDY INK"),  # Cf: the other embeddings
    ("\u206607 BURGUNDY INK\u2069", "07 BURGUNDY INK"),  # Cf: bidi isolates
    ("07 BURG\u200bUNDY INK", "07 BURGUNDY INK"),     # Cf: ZERO WIDTH SPACE
    ("\ufeff07 BURGUNDY INK", "07 BURGUNDY INK"),     # Cf: BOM / ZWNBSP
    ("07 BURGUNDY INK\ue000", "07 BURGUNDY INK"),     # Co: private use
    ("07 BURGUNDY INK\U000f0000", "07 BURGUNDY INK"),  # Co: supplementary private use
    ("07 BURGUNDY \ud83dINK", "07 BURGUNDY INK"),     # Cs: a lone surrogate (json "\\ud83d")
    ("  07 BURGUNDY INK  ", "07 BURGUNDY INK"),       # stripped
])
def test_every_unprintable_category_is_removed_at_both_ends(dirty: str, clean: str) -> None:
    assert _written_title(dirty) == clean
    assert _read_title(dirty) == clean


def test_the_title_is_nfc_normalised_after_the_drop() -> None:
    """Decomposed "e" + COMBINING ACUTE is stored composed, including when a zero-width space sat
    between them -- so the rule is idempotent, which the reader re-applying it relies on."""
    assert clean_variant_title("Cafe\u0301") == "Café"
    assert clean_variant_title("Cafe\u200b\u0301") == "Café"
    for raw in ("Cafe\u200b\u0301", "a\u202e \u200b b", "X" * 199 + " Y", "\n\t"):
        once = clean_variant_title(raw)
        assert clean_variant_title(once) == once


def test_a_long_title_is_capped_by_code_points_and_never_splits_an_emoji() -> None:
    assert MAX_VARIANT_TITLE == 200
    assert _written_title("X" * 300) == _read_title("X" * 300) == "X" * 200
    edge = "X" * 199 + "💄" + "Y" * 100                # the emoji is code point 200
    for got in (_written_title(edge), _read_title(edge)):
        assert got == "X" * 199 + "💄" and len(got) == 200
        assert json.loads(json.dumps(got)) == got      # a whole surrogate pair on the wire
        got.encode("utf-8")                            # no lone surrogate survives the cap
    # dropped characters never spend the budget, and a cut on a space leaves no trailing space
    assert clean_variant_title("\u202e" * 50 + "X" * 200) == "X" * 200
    assert clean_variant_title("X" * 199 + " Y") == "X" * 199


@pytest.mark.parametrize("junk", [
    "\n\t\r", "\u202e\u200b\ufeff", "\ue000", "\u2066\u2069 \x00",
    pytest.param("", id="empty"), pytest.param(" " * 3, id="three-spaces"),
])
def test_an_all_unprintable_title_is_none_and_the_proof_still_stands(junk: str) -> None:
    written = _backfilled(_judy_js(**{JUDY_VARIANT: {"title": junk}}))
    assert written["snapshot"]["shopify_cart_proof"]["variant_title"] is None
    assert _verify(written) == (JUDY_VARIANT, CART_PROOF_SCOPE_NAMED, None)
    assert _read_title(junk) is None


def test_an_old_proof_with_a_dirty_title_is_cleaned_on_read() -> None:
    """A proof the backfill wrote BEFORE the rule holds the raw title; the verifier cleans it."""
    for scope_payload in (None, {"variants": [JUDY_JS["variants"][4]]}):   # named, then sole
        seed = _backfilled(scope_payload)
        seed["snapshot"]["shopify_cart_proof"]["variant_title"] = "\u202e07\nBURGUNDY\u200b INK\ue000"
        assert _verify(seed).variant_title == "07 BURGUNDY INK"


def test_clean_display_text_refuses_non_text_and_honours_its_cap() -> None:
    for value in (None, 7, {"not": "text"}, ["07"], b"07"):
        assert clean_display_text(value, max_chars=10) is None
    assert clean_display_text("abcdef", max_chars=3) == "abc"


# ---------------------------------------------------------------- #2462 review follow-up

import random  # noqa: E402
import unicodedata  # noqa: E402

from services.text_normalization.display_text import (  # noqa: E402
    DROPPED_CATEGORIES,
    KEPT_FORMAT_CHARS,
    MAX_PRODUCT_NAME,
    clean_product_name,
)


@pytest.mark.parametrize("title", [
    "\U0001f469\u200d\U0001f4bb Coder",                 # ZWJ inside an emoji sequence
    "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",  # ZWNJ in Persian orthography
    "\u05d2\u05d5\u05d5\u05df \u200e07 ml",              # LRM placing a number after Hebrew
    "\u0644\u0648\u0646 \u200f(07)",                     # RLM before a bracket in Arabic
])
def test_the_joiners_and_direction_marks_are_kept(title: str) -> None:
    """ZWJ/ZWNJ/LRM/RLM only ever mangle display when dropped; none re-orders text beyond itself."""
    assert _written_title(title) == _read_title(title) == title


@pytest.mark.parametrize("dirty,clean", [
    # England's flag: black flag + TAG g b e n g + CANCEL TAG. The tags are invisible ASCII
    # ("ASCII smuggling"), so they go and the flag degrades to the plain black flag.
    ("Flag \U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
     "Flag \U0001f3f4"),
    ("07 BURG\u2060UNDY INK", "07 BURGUNDY INK"),       # WORD JOINER: an invisible spacer
    ("07 BUR\u00adGUNDY INK", "07 BURGUNDY INK"),       # SOFT HYPHEN
    ("07 BURGUNDY\u180e INK", "07 BURGUNDY INK"),       # MONGOLIAN VOWEL SEPARATOR
])
def test_tag_characters_and_invisible_spacers_are_still_dropped(dirty: str, clean: str) -> None:
    assert _written_title(dirty) == _read_title(dirty) == clean


@pytest.mark.parametrize("bidi", ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069"])
def test_every_bidi_embedding_override_and_isolate_is_dropped(bidi: str) -> None:
    """Each one individually: these are the characters that re-order the text AROUND them."""
    assert _written_title(f"07 {bidi}BURGUNDY INK") == _read_title(f"07 {bidi}BURGUNDY INK") \
        == "07 BURGUNDY INK"


@pytest.mark.parametrize("title", [7, 7.5, True, {"not": "text"}, ["07 BURGUNDY INK"]])
def test_a_non_string_storefront_title_is_no_title(title: Any) -> None:
    """The writer reads the RAW products.js entry: a non-string title is None, not "7"."""
    seed = _backfilled(_judy_js(**{JUDY_VARIANT: {"title": title}}))
    assert seed["snapshot"]["shopify_cart_proof"]["variant_title"] is None
    assert _verify(seed) == (JUDY_VARIANT, CART_PROOF_SCOPE_NAMED, None)
    sole = _backfilled({"variants": [dict(JUDY_JS["variants"][4], title=title)]})
    assert sole["snapshot"]["shopify_cart_proof"]["variant_title"] is None


def test_the_writer_reads_the_title_and_price_of_the_proven_variant_only() -> None:
    """The raw-entry lookup is by numeric id: a sibling's title or price never lands in the proof."""
    proof = _backfilled(_judy_js(**{JUDY_OTHER: {"title": "01 PETAL INK", "price": 1}}))[
        "snapshot"]["shopify_cart_proof"]
    assert proof["variant_title"] == "07 BURGUNDY INK" and proof["price_minor"] == 1399


_FUZZ_ALPHABET = (
    "aZ09 -/<>&\"'\u00e9e\u0301\u0327\u05d0\u0627\u6a31\U0001f469\U0001f3f4\U0001f484"
    "\t\n\r\x0b\x0c\x1c\x1f\x85\u00a0\u2028\u2029\u3000\u2003"
    "\x00\x07\x7f\x9f\u00ad\u061c\u180e\u200b\u200c\u200d\u200e\u200f\u2060\u2061\ufeff"
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufff9"
    "\ue000\uf8ff\U000f0000\U00100000" + chr(0xD800) + chr(0xDC00) + "\ud83d"
    "\U000e0001\U000e0020\U000e0067\U000e007f\u0378\uffff"
)


def test_the_rule_is_idempotent_and_leaves_nothing_it_drops_fuzzed() -> None:
    """Every output is a fixed point, is NFC, has no dropped character, no leading, trailing or
    doubled whitespace, and fits the cap -- on 20,000 random strings over the awkward alphabet."""
    rng = random.Random(2462)
    for _ in range(20000):
        cap = rng.choice((1, 2, 3, 5, 8, 200))
        raw = "".join(rng.choice(_FUZZ_ALPHABET) for _ in range(rng.randint(0, 12)))
        out = clean_display_text(raw, max_chars=cap)
        assert clean_display_text(out, max_chars=cap) == out, (raw, out)
        if out is None:
            continue
        assert out and len(out) <= cap and out == out.strip(), (raw, out)
        assert unicodedata.normalize("NFC", out) == out, (raw, out)
        assert "  " not in out and all(c == " " or not c.isspace() for c in out), (raw, out)
        assert out[-1] not in KEPT_FORMAT_CHARS, (raw, out)           # nothing dangling
        assert any(c != " " and c not in KEPT_FORMAT_CHARS for c in out), (raw, out)  # visible
        assert not any(
            c not in KEPT_FORMAT_CHARS and unicodedata.category(c) in DROPPED_CATEGORIES for c in out
        ), (raw, out)


@pytest.mark.parametrize("invisible", [
    "\u200e", "\u200f", "\u200d\u200c", " \u200f ", "\u200e \u200d\t\u200c", "\u202e\u200d\u200b",
])
def test_a_title_of_only_kept_format_characters_is_none(invisible: str) -> None:
    """Finding 3: LRM/RLM/ZWJ/ZWNJ are kept INSIDE text, but a title made of nothing else (and
    whitespace) shows nothing, so it is None -- at the writer, at the reader, and for a name."""
    assert _written_title(invisible) is None and _read_title(invisible) is None
    assert clean_product_name(invisible) is None


@pytest.mark.parametrize("dangling", ["\u200d", "\u200c", "\u200e", "\u200f", "\u200d \u200c"])
def test_the_cap_never_leaves_a_dangling_kept_format_character(dangling: str) -> None:
    """Finding 4: 199 x "X" + ZWJ + a woman emoji, capped at 200, cut between the joiner and the
    emoji it joins. The end is stripped of kept format characters too, not only of spaces."""
    raw = "X" * 199 + dangling + "\U0001f469\u200d\U0001f4bb"
    assert _written_title(raw) == _read_title(raw) == "X" * 199
    joined = "X" * 196 + "\U0001f469\u200d\U0001f4bb"                  # fits whole: kept whole
    assert _written_title(joined) == joined and len(joined) == 199
    assert clean_display_text("ab" + dangling + "c", max_chars=3) == "ab"
    assert clean_display_text("X" + dangling, max_chars=50) == "X"    # uncapped trailing too


def test_the_product_name_gets_the_same_rule_at_its_own_cap() -> None:
    assert MAX_PRODUCT_NAME == 255
    assert clean_product_name("Silky Matte Lip Ink") == "Silky Matte Lip Ink"
    assert clean_product_name("Silky\u202e Matte\nLip\u200b Ink\ue000") == "Silky Matte Lip Ink"
    assert clean_product_name("P" * 300) == "P" * 255
    assert clean_product_name("\u202e\n") is None and clean_product_name(None) is None

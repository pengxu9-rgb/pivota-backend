"""The automated stand-in for reading every row of a dry run, pinned on the rows that needed it."""
import pytest

from services import curated_brand_feed as feed
from services.retailer_ingest import detectors


def record(title, ptype, handle, *, body="<p>A lip colour.</p>", price="20.00", vendor="3CE", variants=None,
           brand="3CE"):
    return feed.shopify_product_to_record(
        {"id": abs(hash(handle)) % 10**9, "vendor": vendor, "title": title, "handle": handle,
         "product_type": ptype, "body_html": body, "images": [],
         "variants": variants or [{"id": abs(hash(handle + "v")) % 10**12, "price": price, "available": True}]},
        domain="k-touch.us", category_path="beauty", brand_override=brand, currency="USD",
        source_role="retailer", retailer_name="k-touch.us", emit_native_variants=True,
    )


# Copied from the live k-touch.us product page, 2026-09-23.
TONE_UP = record(
    "3CE - TONE UP TINT 40ml", "LIP TINT", "3ce-tone-up-tint-40ml",
    body="<p>Instantly Brighten and Refresh Your Complexion 3CE Tone Up Tint is a Korean tone-up cream "
         "designed to help improve the appearance of dull skin while creating a brighter complexion.</p>")


def rules(flags, severity=None):
    return {f["rule"] for f in flags if severity is None or f["severity"] == severity}


def test_the_mislabelled_tone_up_cream_is_held():
    flags = detectors.detect([TONE_UP])
    assert {"lip_row_copy_not_about_lips", "lip_row_implausible_size"} <= rules(flags, detectors.BLOCK)
    assert all(f["handle"] == "3ce-tone-up-tint-40ml" for f in flags)


@pytest.mark.parametrize("title,ptype", [
    ("3CE - Velvet Lip Tint Plush 4g", "LIP TINT"),
    ("BY TERRY | Rouge Opulent Lipstick", "Lipstick"),
    ("[O HUI] The First Geniture Lipstick", "Lipstick"),
    ("Lip Sleeping Balm 15g", "Lip Balm"),  # a balm tin under the 20 cut
])
def test_real_lip_products_raise_no_blocking_flag(title, ptype):
    assert detectors.blocking(detectors.detect([record(title, ptype, title.lower().replace(" ", "-"))])) == []


def test_a_title_that_names_another_class_contradicts_its_merchant_type():
    # The Wave 2 shape: a toner filed on a measured "Cleansers" shelf.
    rec = record("Pyunkang Yul Essence Toner", "Cleanser", "essence-toner", vendor="Pyunkang Yul",
                 brand="Pyunkang Yul")
    assert rec is not None
    assert "title_contradicts_category" in rules(detectors.detect([rec]), detectors.BLOCK)


# A $0.01 row never reaches here: the feed drops it (MIN_SELLABLE_PRICE). A priced test row does.
@pytest.mark.parametrize("kw", [dict(title="TEST Product", price="999999999.00"),
                                dict(title="Velvet Lip Tint", price="20.00", vendor="Lime Crime Dev",
                                     brand="Lime Crime Dev")])
def test_placeholder_rows_are_held(kw):
    title = kw.pop("title")
    rec = record(title, "LIP TINT", "x-" + title.lower().replace(" ", "-"), **kw)
    assert rec is not None
    assert "placeholder_product" in rules(detectors.detect([rec]), detectors.BLOCK)


def test_rows_the_lip_title_door_placed_hold_until_that_row_is_accepted():
    """Unattended, nothing but the title vouches for a door-placed row: it holds, per row."""
    with feed.lip_title_evidence():
        rec = record("3CE - Soft Matte Lipstick 3.5g", "", "soft-matte")
    flags = detectors.detect([rec])
    assert rules(flags) == {"placed_by_lip_title"}
    [flag] = detectors.blocking(flags)
    assert flag["key"] == "placed_by_lip_title:soft-matte"
    assert detectors.blocking(flags, accepted=[flag["key"]]) == []


def test_an_approval_accepts_exactly_the_flag_keys_it_names():
    flags = detectors.detect([TONE_UP])
    keys = [f["key"] for f in detectors.blocking(flags)]
    assert detectors.blocking(flags, accepted=keys[:1]) and not detectors.blocking(flags, accepted=keys)
    # keys are stable across runs of the same cohort
    assert keys == [f["key"] for f in detectors.blocking(detectors.detect([TONE_UP]))]


def test_non_records_are_skipped():
    assert detectors.detect([None, {}, {"pdp": None}, "x"]) == []


def test_a_lip_product_page_carrying_another_products_description_is_held():
    """k-touch.us, 2026-09-23: "3CE - Soft Matte Lipstick 3.5g - Warmish Move" was served with a rice
    squalane cleanser's description. Real lipstick copy ("flatters every skin tone", "brightens the
    complexion") is NOT flagged -- a face-word list held 4 of 10 real lipsticks and was replaced."""
    with feed.lip_title_evidence():  # the lip pass that placed these rows ran with the switch on
        wrong, right = _warmish_and_mood()
    flags = detectors.detect([wrong, right])
    copy_holds = {f["handle"] for f in detectors.blocking(flags) if f["rule"] == "lip_row_copy_not_about_lips"}
    assert copy_holds == {"3ce-soft-matte-lipstick-warmish-move"}


def _warmish_and_mood():
    wrong = record("3CE - Soft Matte Lipstick 3.5g - Warmish Move", "", "3ce-soft-matte-lipstick-warmish-move",
                   body="<p>Natural Skin-Clarity Boost From Korean Rice Germ Extract. Inspired by Korea's "
                        "rice bran cleansing rituals, this glow double cleanser leaves skin clear and bright.</p>")
    right = record("3CE - Mood Recipe Lip Color 3.5g", "", "3ce-mood-recipe-lip-color-3-5g",
                   body="<p>Universally flattering nude shades crafted to complement every skin tone and "
                        "brighten the complexion, with a creamy formula that glides over lips.</p>")
    return wrong, right


def test_an_absurd_price_alone_marks_a_placeholder():
    rec = record("Velvet Lip Tint Plush 4g", "LIP TINT", "priced-placeholder", price="1500.00")
    assert "placeholder_product" in rules(detectors.detect([rec]), detectors.BLOCK)



@pytest.mark.parametrize("title,ptype,body", [
    ("Pyunkang Yul Hand Cream", "Hand Cream", "<p>Rich cream for dry hands.</p>"),  # body/care on purpose
    ("Vaseline Lip Therapy Original 20g", "Lip Balm", "<p>Soothes chapped skin in a tin.</p>"),
    ("Healing Balm Tin 12g", "Lip Balm", "<p>Soothes and protects dry, chapped skin with pure petroleum jelly "
                                        "in a handy travel tin for everyday use.</p>"),
    ("Sugar Lip Scrub 30g", "Lip Scrub", "<p>Exfoliates dry, flaky skin.</p>"),
    ("Velvet Lip Tint 4g", "LIP TINT", "<p>" + "벨벳 립 틴트, 입술에 부드럽게 발리는 컬러. " * 3 + "</p>"),
])
def test_review_false_positives_do_not_hold(title, ptype, body):
    rec = record(title, ptype, title.lower().replace(" ", "-"), body=body)
    assert rec is not None
    assert detectors.blocking(detectors.detect([rec])) == [], detectors.detect([rec])


def test_a_set_filed_as_one_product_is_held():
    rec = record("[OHUI] Miracle Moisture Cleansing Oil Special Set", "Cleansing Oil", "cleansing-oil-set",
                 body="<p>A cleansing oil set.</p>")
    assert "set_filed_as_single_product" in rules(detectors.detect([rec]), detectors.BLOCK)



@pytest.mark.parametrize("title,ptype", [("Vitamin C Serum", "Hair"), ("Snail Mucin Essence", "Body Care")])
def test_an_area_leaf_is_exempt_only_when_its_own_title_names_the_area(title, ptype):
    rec = record(title, ptype, title.lower().replace(" ", "-"), body="<p>A serum.</p>")
    assert rec is not None and rec["pdp"]["category_path"]
    assert "title_contradicts_category" in rules(detectors.detect([rec]), detectors.BLOCK)



def test_a_single_unit_count_is_not_a_set():
    rec = record("3CE Velvet Lip Tint (1pc)", "LIP TINT", "one-pc", body="<p>Velvet colour for lips.</p>")
    assert "set_filed_as_single_product" not in rules(detectors.detect([rec]))
    two = record("3CE Velvet Lip Tint 2pcs", "LIP TINT", "two-pcs", body="<p>Velvet colour for lips.</p>")
    assert "set_filed_as_single_product" in rules(detectors.detect([two]))


# ------------------------------------------------------------------ placeholder_price_store (2026-09-26)
# headandshoulders.com priced all 120 storefront variants at 1.00 and the drain applied 74 PDPs at $1.00:
# $1.00 clears the per-row rule. The signal is store-wide, so the rule is judged over the whole cohort.

def store(prices_per_product, *, domain="headandshoulders.com"):
    """One real-producer record per product, each carrying the given variant prices (>= 1.00: the feed
    drops a variant under MIN_SELLABLE_PRICE, so a sub-dollar row is built by `hand_record`)."""
    out = []
    for i, prices in enumerate(prices_per_product):
        handle = f"p{i}"
        rec = feed.shopify_product_to_record(
            {"id": 7_000_000 + i, "vendor": "Brand", "title": f"Moisture Shampoo {i}", "handle": handle,
             "product_type": "Shampoo", "body_html": "<p>A shampoo.</p>", "images": [],
             "variants": [{"id": 40_000_000_000 + i * 100 + j, "price": f"{p:.2f}", "available": True,
                           "title": f"Size {j}"} for j, p in enumerate(prices)]},
            domain=domain, category_path="beauty", brand_override="Brand", currency="USD",
            source_role="brand_official", emit_native_variants=True,
        )
        assert rec is not None and len(rec["pdp"]["variants"]) == len(prices), (prices, rec)
        out.append(rec)
    return out


def hand_record(handle, prices):
    """The producer's shape (pdp.variants + one offer at the first variant's price), for prices the
    Shopify feed would drop before detect() sees them."""
    return {"pdp": {"product_name": f"Item {handle}", "brand": "Brand", "category_path": "beauty/haircare/shampoo",
                    "variants": [{"variant_id": f"{handle}-{j}", "price": p} for j, p in enumerate(prices)]},
            "offers": [{"canonical_url": f"https://example.com/products/{handle}", "price": prices[0]}]}


def test_the_hand_record_matches_the_producers_shape():
    [real] = store([[12.0, 14.0]])
    fake = hand_record("p0", [12.0, 14.0])
    assert detectors._variant_prices(real) == detectors._variant_prices(fake) == [12.0, 14.0]
    assert detectors._handle(real) == detectors._handle(fake) == "p0"


def _varied(n, start=5.0):
    return [round(start + 0.37 * i, 2) for i in range(n)]


def _held(flags):
    return {f["handle"] for f in flags if f["rule"] == "placeholder_price_store" and f["severity"] == detectors.BLOCK}


@pytest.mark.parametrize("name,records,expect_held", [
    # HOLD: headandshoulders.com, 2026-09-26 -- 120 variants all at 1.00.
    ("120 all at 1.00", lambda: store([[1.0]] * 120), set(range(120))),
    # HOLD: honest.com's shape -- 317 variants, 125 at 1.00 (0.39 <= 1.00 share).
    ("317 with 125 at 1.00", lambda: store([[1.0]] * 125 + [[v] for v in _varied(192)]), set(range(125))),
    # HOLD: a 0.99 token (built by hand: the feed drops sub-dollar variants).
    ("50 all at 0.99", lambda: [hand_record(f"p{i}", [0.99]) for i in range(50)], set(range(50))),
    # DO NOT HOLD: under the minimum count (the per-row rule still owns <= 0.5).
    ("15 all at 1.00", lambda: store([[1.0]] * 15), set()),
    # DO NOT HOLD: a flat-priced legitimate store -- its modal price is above 2.00.
    ("200 with 190 at 10.00", lambda: store([[10.0]] * 190 + [[v] for v in _varied(10, 20.0)]), set()),
    # DO NOT HOLD: 20 of 300 at 1.00 is under the <= 1.00 share.
    ("300 with 20 at 1.00", lambda: store([[1.0]] * 20 + [[v] for v in _varied(280)]), set()),
])
def test_store_level_placeholder_pricing(name, records, expect_held):
    flags = detectors.detect(records())
    assert _held(flags) == {f"p{i}" for i in expect_held}, name


def test_only_rows_whose_every_price_is_the_placeholder_are_held():
    # 40 variants at 1.00 of 100 (0.40: the store holds) -- but 30 of them ride on real products whose
    # other variants are real prices. Those products are not held; the ten all-1.00 rows are.
    real_with_token = [[1.0, 1.0, 1.0, 24.0, 26.0, 28.0]] * 10   # 30 at 1.00, 30 real
    all_token = [[1.0]] * 10                                       # 10 at 1.00
    rest = [[v] for v in _varied(30, 30.0)]
    flags = detectors.detect(store(real_with_token + all_token + rest))
    assert _held(flags) == {f"p{i}" for i in range(10, 20)}


def test_a_store_whose_one_price_rows_are_only_variants_of_real_products_holds_no_row():
    # Every 1.00 is a variant beside a real price (0.67 at 1.00: the store holds) -- nothing is
    # placeholder-only, so no row is held.
    flags = detectors.detect(store([[1.0, 1.0, 19.0]] * 30))
    assert _held(flags) == set()


def test_the_flag_names_the_store_evidence_and_is_keyed_per_row():
    flags = [f for f in detectors.detect(store([[1.0]] * 120)) if f["rule"] == "placeholder_price_store"]
    assert flags[0]["key"] == "placeholder_price_store:p0"
    assert "120/120 variants at 1.00" in flags[0]["detail"]
    assert len({f["key"] for f in flags}) == 120
    # an operator accepts a specific real $1 item; the rest still hold
    left = detectors.blocking(flags, accepted=["placeholder_price_store:p7"])
    assert len(left) == 119 and "placeholder_price_store:p7" not in {f["key"] for f in left}


def test_a_modal_placeholder_at_two_dollars_holds_and_the_per_row_rule_is_untouched():
    recs = store([[2.0]] * 40 + [[v] for v in _varied(5, 30.0)])  # 40/45 = 0.89 at 2.00
    flags = detectors.detect(recs)
    assert _held(flags) == {f"p{i}" for i in range(40)}
    assert "placeholder_product" not in rules(flags)  # 2.00 was never a per-row placeholder


def test_a_subset_recheck_does_not_judge_the_store():
    recs = store([[1.0]] * 30)
    assert _held(detectors.detect(recs, store_level=False)) == set()
    assert len(_held(detectors.detect(recs))) == 30


@pytest.mark.parametrize("n,held", [(19, False), (20, True)])
def test_the_minimum_count_boundary(n, held):
    assert bool(_held(detectors.detect(store([[1.0]] * n)))) is held


@pytest.mark.parametrize("price,token,rest,held", [
    (1.00, 30, 70, True),    # 0.30 at <= 1.00: holds
    (1.00, 29, 71, False),   # 0.29: does not
    (1.01, 30, 70, False),   # 1.01 is a price, not the token
])
def test_the_token_share_boundary(price, token, rest, held):
    flags = detectors.detect(store([[price]] * token + [[v] for v in _varied(rest)]))
    assert bool(_held(flags)) is held


def test_a_sub_dollar_modal_store_holds_its_one_dollar_rows_too():
    # 80 at 0.99 (the modal placeholder) and 20 at 1.00: both are token prices, every row holds.
    recs = [hand_record(f"p{i}", [0.99]) for i in range(80)] + [hand_record(f"p{i}", [1.0]) for i in range(80, 100)]
    assert _held(detectors.detect(recs)) == {f"p{i}" for i in range(100)}


def test_the_evidence_names_the_token_price_when_the_mode_ties():
    flags = [f for f in detectors.detect(store([[1.0]] * 25 + [[30.0]] * 25)) if f["rule"] == "placeholder_price_store"]
    assert len(flags) == 25 and "25/50 variants at 1.00" in flags[0]["detail"]


@pytest.mark.parametrize("price,modal,rest,held", [
    (2.00, 80, 20, True),    # 0.80 at 2.00: holds
    (2.00, 79, 21, False),   # 0.79: does not
    (2.01, 90, 10, False),   # a modal price over 2.00 is a real price list
])
def test_the_modal_share_and_ceiling_boundaries(price, modal, rest, held):
    flags = detectors.detect(store([[price]] * modal + [[v] for v in _varied(rest, 30.0)]))
    assert bool(_held(flags)) is held

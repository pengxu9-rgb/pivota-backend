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

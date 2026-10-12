"""body_html -> description cleanup.

Crawled descriptions carried damage a buyer reads: words split by inline tags, a literal "<br>"
from entity-encoded markup, store disclaimers / promo codes / social embeds kept verbatim, and
copy cut mid-sentence at the length cap. Every test here goes through the REAL producer
(`body_html_to_text`, `shopify_product_to_record`, `records_for_brand`) with Shopify-shaped
body_html, and the copy-loss cases pin what must be KEPT as firmly as what is removed. Hosts and
brands are made up.
"""

import pytest

import services.curated_brand_feed as cbf
from services.curated_brand_feed import (
    BODY_TEXT_MAX_LEN,
    DESCRIPTION_REPEAT_MIN_KEEP_CHARS,
    _split_sentences,
    body_html_to_text,
    boilerplate_class,
    description_nouns,
    drop_repeated_store_sentences,
    inci_from_body_html,
    shopify_product_to_record,
)

HOST = "lumen-botanica.example"
BRAND = "Lumen Botanica"


def _product(handle="dew-serum", title="Dew Serum", body_html="<p>A light serum.</p>", product_type="Serum"):
    return {
        "title": title, "handle": handle, "vendor": BRAND, "product_type": product_type,
        "body_html": body_html, "images": [{"src": "https://cdn.example/img.jpg"}],
        "variants": [{"id": 4400000001, "price": "28.00", "available": True}],
    }


class _Feed(list):
    """The shape `fetch_shopify_products` returns: the products plus the scan's own report."""
    crawl_report = {"status": "complete", "pages": 1}


async def _records(monkeypatch, products, *, host=HOST, brand=BRAND, category_path="beauty/skincare"):
    async def _products(domain, **kw):
        return _Feed(products)

    async def _locale(domain, **kw):
        return {"currency": "USD"}

    monkeypatch.setattr(cbf, "fetch_shopify_products", _products)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", _locale)
    return await cbf.records_for_brand(domain=host, category_path=category_path, brand=brand)


def _summaries(recs):
    return {r["pdp"]["product_name"]: r["pdp"]["attribute_summary"] for r in recs}


# --- word splitting -------------------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    # Joined: the two sides continue one word.
    ('<p><span class="dropcap">D</span>ue to its whipped texture, this balm melts.</p>',
     "Due to its whipped texture, this balm melts."),
    ('<p>A hydra<a href="/pages/glossary">ting</a> mist.</p>', "A hydrating mist."),
    ("<p><b><i>Hydra</i></b><span><a>ting</a></span> mist.</p>", "Hydrating mist."),
    ("<p>With vitamin B<sub>5</sub>, H<sub>2</sub>O and 10<sup>th</sup>-gen peptides.</p>",
     "With vitamin B5, H2O and 10th-gen peptides."),
    ("<p>Only $1,<b>000</b> units.</p>", "Only $1,000 units."),
    ("<p>Opens 10:<b>30</b></p>", "Opens 10:30"),
    ("<p><b>保湿</b>クリーム。</p>", "保湿クリーム。"),
    # Spaced: the theme relied on the tags to separate them.
    ("<p><b>Size</b>50ml</p>", "Size 50ml"),
    ("<p><strong>Skin type</strong>All</p>", "Skin type All"),
    ("<span>Vegan</span><span>Cruelty-free</span><span>Paraben-free</span>", "Vegan Cruelty-free Paraben-free"),
    ("<p><b>Vegan</b>&amp;<b>Cruelty free</b></p>", "Vegan & Cruelty free"),
    ("<p><b>Vegan</b>,<b>Cruelty free</b></p>", "Vegan, Cruelty free"),
    ("<p><strong>How to use:</strong>Smooth over damp skin.</p>", "How to use: Smooth over damp skin."),
    ("<p><b>Size:</b>50ml</p>", "Size: 50ml"),
    ("<p>Hydrates.<b>Plumps</b> too.</p>", "Hydrates. Plumps too."),
    ("<p><strong>Benefits</strong>– hydrates</p>", "Benefits – hydrates"),
])
def test_inline_tags_join_or_space_as_the_page_reads(body, expected):
    assert body_html_to_text(body) == expected


def test_block_tags_and_br_stay_boundaries():
    body = "<p>Soft focus.</p><p>Long wear.</p><ul><li>Vegan</li><li>Cruelty free</li></ul>One<br>Two<br/>Three"
    assert body_html_to_text(body) == "Soft focus. Long wear. Vegan Cruelty free One Two Three"


# --- entity-encoded markup -------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "<p>Plumps and smooths.&lt;br&gt;Wear alone or under makeup.</p>",
    "<p>Plumps and smooths.&lt;br /&gt;Wear alone or under makeup.</p>",
    "<p>Plumps and smooths.&amp;lt;br&amp;gt;Wear alone or under makeup.</p>",  # escaped twice
    "<p>Plumps and smooths.&amp;amp;lt;br&amp;amp;gt;Wear alone or under makeup.</p>",  # three times
    "&lt;p&gt;Plumps and smooths.&lt;/p&gt;&lt;p&gt;Wear alone or under makeup.&lt;/p&gt;",
])
def test_entity_encoded_markup_never_survives_as_a_literal_tag(body):
    out = body_html_to_text(body)
    assert out == "Plumps and smooths. Wear alone or under makeup."
    assert "<" not in out and "&lt;" not in out


def test_entity_encoded_inline_tags_do_not_split_words_either():
    assert body_html_to_text("<p>&lt;span&gt;D&lt;/span&gt;ewy finish.</p>") == "Dewy finish."


@pytest.mark.parametrize("body,expected", [
    ("<p>Contains &lt;5% alcohol &amp; a pH of 5.5 &gt; 4.</p>", "Contains <5% alcohol & a pH of 5.5 > 4."),
    ("<p>Results &lt;in 3 days&gt; guaranteed.</p>", "Results <in 3 days> guaranteed."),
])
def test_an_escaped_angle_bracket_in_prose_is_kept(body, expected):
    assert body_html_to_text(body) == expected


# --- sentences -------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Developed by Dr. Kim for sensitive skin. Use daily.", ["Developed by Dr. Kim for sensitive skin.", "Use daily."]),
    ("Shade No. 5 is a warm nude. Ships fast.", ["Shade No. 5 is a warm nude.", "Ships fast."]),
    ("Rich in vitamins (e.g. C and E). Use daily.", ["Rich in vitamins (e.g. C and E).", "Use daily."]),
    ("Approx. 30 uses. Contains 2.5% BHA vs. 1% in others.", ["Approx. 30 uses.", "Contains 2.5% BHA vs. 1% in others."]),
    ("Mt. Fuji water. St. Ives inspired. Made by Example Co. Ltd.", ["Mt. Fuji water.", "St. Ives inspired.", "Made by Example Co. Ltd."]),
    ("Made in the U.S. by i.e. hand. Next.", ["Made in the U.S. by i.e. hand.", "Next."]),
    ("Use 0.5ml daily. Lasts 3.5 hrs.", ["Use 0.5ml daily.", "Lasts 3.5 hrs."]),
    ("Gentle toner. free shipping applies. this calms.", ["Gentle toner.", "free shipping applies.", "this calms."]),
    ("保湿クリームです。送料無料。乾燥肌に最適！夜に？", ["保湿クリームです。", "送料無料。", "乾燥肌に最適！", "夜に？"]),
])
def test_sentence_splitting(text, expected):
    assert _split_sentences(text) == expected


def test_cjk_sentences_rejoin_without_spaces():
    assert body_html_to_text("<p>保湿クリームです。乾燥肌に最適！夜に使用？はい。</p>") == (
        "保湿クリームです。乾燥肌に最適！夜に使用？はい。")


def test_a_lowercase_continuation_loses_only_its_boilerplate_sentence():
    body = ("<p>Gentle exfoliating toner. free shipping applies only to the US. "
            "this toner balances pH and calms redness in three days.</p>")
    assert body_html_to_text(body) == (
        "Gentle exfoliating toner. this toner balances pH and calms redness in three days.")


def test_a_cjk_block_loses_only_its_boilerplate_sentence():
    body = "<p>このセラムはビタミンCを配合し、肌を明るくします。詳しくは www.example.jp をご覧ください。毎日使えます。</p>"
    assert body_html_to_text(body) == "このセラムはビタミンCを配合し、肌を明るくします。毎日使えます。"


# --- store boilerplate, one class at a time --------------------------------------------------

COPY = "<p>A cushiony balm that melts into a silky oil.</p>"
COPY_TEXT = "A cushiony balm that melts into a silky oil."


@pytest.mark.parametrize("cls,boilerplate", [
    ("colour_disclaimer", "Due to monitor settings, colours may vary slightly from the images shown."),
    ("colour_disclaimer", "Colors may differ from screen to screen."),
    ("colour_disclaimer", "Shades may look slightly different on your screen."),
    ("click_here", "Click here to purchase the set."),
    ("click_here", "Shop the full size here."),
    ("promo_code", "Use code SALON15 for 15 units or more."),
    ("promo_code", "Enter discount code WELCOME at checkout."),
    ("promo_code", "Bulk discounts available for salons."),
    ("store_link", "Shop the full range at www.lumen-botanica.example today."),
    ("store_link", "Contact us with any questions."),
    ("social_embed", "View this post on Instagram"),
    ("social_embed", "A post shared by Lumen Botanica (@lumen.example)"),
    ("social_embed", "Follow us on Instagram for tutorials."),
    ("social_embed", "Tag us @lumen.example to be featured!"),
    ("sale_banner", "NOW ON SALE 25% OFF"),
    ("sale_banner", "Limited-time offer while stocks last."),
    ("sale_banner", "Take 20% off sitewide this weekend."),
    ("sale_banner", "Was $40, now $28."),
    ("shipping_returns_policy", "This item contains hazardous materials and can only ship by ground."),
    ("shipping_returns_policy", "Free shipping on all orders over $50."),
    ("shipping_returns_policy", "Due to hygiene reasons this item is non-returnable."),
    ("shipping_returns_policy", "Please allow 3-5 business days for processing."),
    ("shipping_returns_policy", "This item is final sale."),
    ("shipping_returns_policy", "Ships within 2-3 business days."),
    ("packaging_note", "Please note: packaging may vary as we update our design."),
    ("packaging_note", "Please note that you may receive the old or the new label."),
    ("packaging_note", "Labels may vary as we use up old stock."),
])
def test_each_boilerplate_class_is_dropped_and_the_copy_kept(cls, boilerplate):
    assert boilerplate_class(boilerplate) == cls
    assert body_html_to_text(f"{COPY}<p>{boilerplate}</p>") == COPY_TEXT
    assert body_html_to_text(f"<p>{boilerplate}</p>{COPY}") == COPY_TEXT


def test_the_known_boilerplate_shapes_inside_one_body():
    body = (
        "<p>NOW ON SALE 25% OFF</p>"
        '<p><span class="dropcap">A</span> rich cream for very dry skin. It locks in moisture all day.</p>'
        "<p><em>Due to monitor settings, colours may vary.</em></p>"
        '<p>Click <a href="https://lumen-botanica.example/collections/sets">here</a> to purchase the set.</p>'
        "<p>Bulk discount: use code BULK20 at https://lumen-botanica.example</p>"
        "<blockquote>View this post on Instagram<br>A post shared by Lumen (@lumen.example)</blockquote>"
        "<p>Hazardous materials: this item ships by ground only. Returns are not accepted on opened items.</p>"
    )
    assert body_html_to_text(body) == "A rich cream for very dry skin. It locks in moisture all day."


@pytest.mark.parametrize("copy", [
    # colour: no monitor/screen in a "may vary/differ" construction
    "Displays a colour-changing effect that varies with your skin's pH.",
    "This palette has 12 shades that vary from matte to shimmer, as pictured in the image above.",
    "Shades vary on different skin tones; see the swatch photos for reference.",
    "A blue light shield that protects skin from screens and devices; the tint colour may differ on deeper skin.",
    "Colour may vary from person to person as it reacts with your skin's pH.",
    # packaging note: packaging only
    "Please note this tint changes colour on your lips to a unique shade.",
    "Please note: the colour of this balm may change over time due to natural ingredients.",
    "A gift box will change colour with heat as a surprise.",
    # promo: a case-sensitive code token in a discount context
    "Shade code: NC20 matches light skin.",
    "Colour code: Nude.",
    "Each bottle comes with the code to verify authenticity.",
    "Layer it with code red lip liner for a bolder look.",
    "Adds bulk to thin lashes without clumping.",
    # sale: needs a store sale
    "Takes 30% off the look of fine lines in 4 weeks.",
    "Wipes 99% off makeup in one swipe.",
    "The best-selling serum, finally back on sale shelves.",
    "It was $5 well spent, say our testers.",
    "Our limited-time holiday sale shade is back!",
    # social: needs a platform or a handle
    "Follow us on our journey to zero waste with a refillable case.",
    "Tag us in your glow photos!",
    # shipping: needs shipping, not a loose keyword
    "Visible results in 14 business days.",
    "Express delivery of hyaluronic acid deep into skin.",
    "Standard delivery of 2% salicylic acid.",
    "Delivery times of active ingredients are slow-release over 8 hours.",
    "A 28-day skin renewal cycle: the return period of new cells.",
    "The final sale of our limited edition holiday shade.",
    "Contains no dangerous goods or hazardous materials, unlike aerosol sprays.",
    "Ships in a recyclable glass jar.",
    "Ships in 2 sizes.",
    "Ships within 10 days of fermentation.",
    "The Free Shipping Kit includes three minis.",
    # links / click
    "Some call us the original cushion compact.",
    "A cleanser so gentle, customers email us to say thank you.",
    "Tap below the cheekbone and blend up.",
    "A balm you'll want to shop here and again.",
    # generic product copy
    "Free of fragrance, parabens and sulfates.",
    "Reduces the look of pores by 31% in four weeks.",
    "Use a pea-sized amount on clean skin.",
])
def test_product_copy_that_shares_boilerplate_vocabulary_is_kept(copy):
    assert boilerplate_class(copy) is None
    assert body_html_to_text(f"<p>{copy}</p>") == copy


@pytest.mark.parametrize("run", [
    "Hydrating hyaluronic serum for all skin types with free shipping included in the gift set edition",
    "비타민C 세럼 피부를 밝게 www.example.kr 매일 사용",
])
def test_an_unpunctuated_run_is_dropped_only_when_all_of_it_is_boilerplate(run):
    assert body_html_to_text(f"<ul><li>{run}</li></ul>") == run


def test_a_long_run_on_block_is_not_dropped_for_one_boilerplate_phrase():
    run_on = "this gel cream " * 30 + "comes with free shipping."
    assert len(run_on) > 400
    assert body_html_to_text(f"<p>{run_on}</p>") == run_on


def test_a_boilerplate_only_body_falls_back_to_product_type():
    rec = shopify_product_to_record(
        _product(body_html="<p>Due to monitor settings, colours may vary.</p><p>NOW ON SALE 25% OFF</p>"),
        domain=HOST, category_path="beauty/skincare")
    assert rec["pdp"]["attribute_summary"] == "Serum"


def test_the_record_carries_the_cleaned_description():
    body = ('<p><span class="dropcap">D</span>ew in a bottle.&lt;br&gt;'
            "Sinks in fast.</p><p>Click here to shop the set.</p>")
    rec = shopify_product_to_record(_product(body_html=body), domain=HOST, category_path="beauty/skincare")
    assert rec["pdp"]["attribute_summary"] == "Dew in a bottle. Sinks in fast."
    precomputed = shopify_product_to_record(
        _product(body_html=body), domain=HOST, category_path="beauty/skincare",
        body_sentences=cbf.body_html_sentences(body))
    assert precomputed == rec


# --- truncation ------------------------------------------------------------------------------

def test_the_cap_cuts_at_the_last_sentence_boundary():
    sentence = "This balm cushions dry lips with shea butter and squalane overnight."  # 68 chars
    body = "<p>" + " ".join([sentence] * 40) + "</p>"
    out = body_html_to_text(body)
    assert len(out) <= BODY_TEXT_MAX_LEN
    assert out == " ".join([sentence] * out.count(sentence))
    assert len(out) + 1 + len(sentence) > BODY_TEXT_MAX_LEN  # took every sentence that fits


def test_the_cap_falls_back_to_a_word_boundary_without_a_sentence_boundary():
    words = " ".join(["hydrating"] * 400)
    out = body_html_to_text(f"<p>{words}</p>")
    assert len(out) <= BODY_TEXT_MAX_LEN and out.endswith("hydrating")


def test_a_boundary_that_keeps_under_half_the_cap_falls_back_to_the_word_cut():
    long = ", ".join(["soothing centella and panthenol calm redness"] * 60)
    out = body_html_to_text(f"<h3>Details</h3><p>{long}</p>")
    assert out.startswith("Details soothing centella")
    assert BODY_TEXT_MAX_LEN - 60 < len(out) <= BODY_TEXT_MAX_LEN


# --- INCI extraction is unchanged ------------------------------------------------------------

def test_inci_extraction_is_unchanged_by_the_description_cleanup():
    body = ('<p><span class="dropcap">A</span> calming toner. Colours may vary due to screen settings.</p>'
            "<p><strong>Ingredients:</strong> Water, Glycerin, Centella Asiatica Extract, "
            "Panthenol, Allantoin</p><p>How to use: Pat onto clean skin.</p>")
    expected = "Water, Glycerin, Centella Asiatica Extract, Panthenol, Allantoin"
    assert inci_from_body_html(body) == expected
    rec = shopify_product_to_record(_product(body_html=body), domain=HOST, category_path="beauty/skincare")
    assert rec["pdp"]["raw_inci"] == expected
    assert rec["pdp"]["attribute_summary"] == (
        "A calming toner. Ingredients: Water, Glycerin, Centella Asiatica Extract, Panthenol, Allantoin "
        "How to use: Pat onto clean skin.")


# --- store-level repetition across one crawl batch ------------------------------------------

FOOTER = "Every jar is filled by hand in our small studio by the sea."  # matches no boilerplate class
HEADER = "Welcome to the studio, where every batch is poured each week."


def _own(i):
    return (f"Product number {i} is a bespoke formula with its own blend of botanicals, "
            f"made for one particular skin need and nothing else.")


def _batch(n, *, footer_on=None, header_on=0):
    footer_on = n if footer_on is None else footer_on
    out = []
    for i in range(n):
        body = f"<p>{HEADER}</p>" if i < header_on else ""
        body += f"<p>{_own(i)}</p>"
        if i < footer_on:
            body += f"<p>{FOOTER}</p>"
        out.append(_product(handle=f"item-{i}", title=f"Item {i}", body_html=body, product_type="Moisturizer"))
    return out


def test_the_repeat_floor_is_the_candidate_description_floor():
    from services.pdp_lifecycle import CANDIDATE_DESCRIPTION_MIN_LEN
    assert DESCRIPTION_REPEAT_MIN_KEEP_CHARS == CANDIDATE_DESCRIPTION_MIN_LEN


@pytest.mark.asyncio
async def test_a_store_template_footer_and_header_are_dropped(monkeypatch):
    assert boilerplate_class(FOOTER) is None and boilerplate_class(HEADER) is None
    recs = await _records(monkeypatch, _batch(20, footer_on=18, header_on=15))
    summaries = _summaries(recs)
    for i in range(20):
        assert summaries[f"Item {i}"] == _own(i)
    report = recs.crawl_report["description_repeats"]
    assert report["threshold"] == 10 and report["rows_changed"] == 18  # rows 18-19 carry neither
    assert report["sample"] == [FOOTER, HEADER]


@pytest.mark.asyncio
async def test_a_short_batch_keeps_its_repeated_sentence(monkeypatch):
    recs = await _records(monkeypatch, _batch(9))
    assert all(s.endswith(FOOTER) for s in _summaries(recs).values())
    assert "description_repeats" not in recs.crawl_report


NAIL_LINE = ("<p>Our signature long-wear nail lacquer dries in 60 seconds to a high-gloss, chip-resistant finish. "
             "The 10-free formula is vegan and made without formaldehyde, toluene or DBP. "
             "A wide, flat brush lays down even colour in two coats.</p>")
NAIL_SHADES = ["A sheer ballet pink.", "A warm brick red with gold shimmer.", "A deep navy cream.", "",
               "A soft lilac cream.", "", "A bright coral.", "An inky black.", "", "A milky white.",
               "A rose gold metallic.", "A forest green."]


@pytest.mark.asyncio
async def test_a_single_line_store_keeps_its_line_copy_above_half_the_batch(monkeypatch):
    products = [_product(handle=f"shade-{i}", title=f"Shade {i}", product_type="Nail Polish",
                         body_html=NAIL_LINE + (f"<p>{s}</p>" if s else ""))
                for i, s in enumerate(NAIL_SHADES)]
    products += [_product(handle=f"care-{i}", title=f"Care {i}", product_type="Nail Care",
                          body_html=f"<p>Treatment product {i} strengthens brittle nails with keratin "
                                    f"and biotin over four weeks.</p>") for i in range(8)]
    before = {p["title"]: body_html_to_text(p["body_html"]) for p in products}
    recs = await _records(monkeypatch, products, host="nail-lacquer.example", brand="Example Lacquer",
                          category_path="beauty/nails")
    assert recs.crawl_report["description_repeats"]["repeated_sentences"] == 3  # on 12 of 20 rows
    assert _summaries(recs) == before
    assert all(len(s) >= 50 for s in _summaries(recs).values())


@pytest.mark.asyncio
async def test_a_lipstick_line_batch_does_not_hold_the_job(monkeypatch):
    from services.retailer_ingest.detectors import detect
    line = ("<p>Our creamy satin lipstick glides on in one swipe and keeps lips soft for eight hours. "
            "Enriched with shea butter and vitamin E for comfortable all-day wear.</p>")
    shades = ["A warm brick red with a subtle gold shimmer that flatters every skin tone beautifully.",
              "A cool-toned berry with a blue undertone that brightens the whole face in seconds.",
              "A soft peachy nude inspired by golden hour light, perfect for everyday wear and travel."] * 4
    products = [_product(handle=f"s{i}", title=f"Satin Lipstick {i}", product_type="Lipstick",
                         body_html=line + f"<p>{s}</p>") for i, s in enumerate(shades)]
    recs = await _records(monkeypatch, products, host="lip-studio.example", brand="Example Lip Studio",
                          category_path="beauty/makeup/lips/lipstick")
    assert all(r["pdp"]["attribute_summary"].startswith("Our creamy satin lipstick") for r in recs)
    assert [f for f in detect(recs) if f.get("rule") == "lip_row_copy_not_about_lips"] == []


@pytest.mark.asyncio
async def test_a_line_sentence_naming_the_product_noun_survives_the_batch(monkeypatch):
    line = "Every serum in this range layers under moisturiser and sunscreen."
    products = [_product(handle=f"serum-{i}", title=f"Serum {i}", product_type="Serum",
                         body_html=f"<p>{_own(i)}</p><p>{line}</p>") for i in range(12)]
    recs = await _records(monkeypatch, products)
    assert all(s == f"{_own(int(n.split()[1]))} {line}" for n, s in _summaries(recs).items())
    assert recs.crawl_report["description_repeats"]["repeated_sentences"] == 1


# The pure pass, guard by guard. Each row: its own long sentence plus whatever the case adds.
OWN = [f"Row {i} has a long copy line of its very own, describing the formula in detail." for i in range(12)]
REPEAT = "A sentence that the store prints on many of its products."


def _pass(rows, nouns=None):
    return drop_repeated_store_sentences(rows, nouns)


def test_a_repeated_footer_is_dropped():
    out, report = _pass([[own, REPEAT] for own in OWN])
    assert out == [[own] for own in OWN] and report["rows_changed"] == 12


def test_a_repeated_sentence_between_the_rows_own_copy_is_kept():
    rows = [[own, REPEAT, f"Closing line {i} is about this product alone and nothing else."] for i, own in enumerate(OWN)]
    out, report = _pass(rows)
    assert out == rows and report["repeated_sentences"] == 1 and report["rows_changed"] == 0


def test_a_repeated_sentence_naming_the_rows_product_noun_is_kept():
    serum_line = "A sentence that every serum in the store shares with the rest."
    rows = [[own, serum_line] for own in OWN]
    out, _ = _pass(rows, [description_nouns("beauty/skincare/serum", "Serum")] * 12)
    assert out == rows
    out, _ = _pass(rows, [description_nouns("beauty/makeup/lip/lipstick", "Lipstick")] * 12)
    assert out == [[own] for own in OWN]


def test_a_pass_that_would_leave_under_half_or_under_50_chars_keeps_the_row_whole():
    long_footer = ("A sentence that the store prints on many of its products, about its studio, "
                   "its history, its founders and the sea air that fills it every single morning.")
    rows = [[own, long_footer] for own in OWN]  # dropping it would keep under half
    out, report = _pass(rows)
    assert out == rows and report["rows_changed"] == 0
    rows = [[f"Shade {i} is sheer.", REPEAT] for i in range(12)]  # would keep under 50 chars and half
    out, _ = _pass(rows)
    assert out == rows
    short_footer = "Poured by hand in our seaside studio."
    rows = [[f"Shade {i:02d} is a soft sheer pink for every day.", short_footer] for i in range(12)]
    assert all(len(r[0]) < 50 and len(r[0]) * 2 > len(" ".join(r)) for r in rows)  # over half, under 50
    out, report = _pass(rows)
    assert report["repeated_sentences"] == 1 and out == rows


def test_a_one_sentence_description_is_never_emptied():
    out, _ = _pass([[REPEAT]] * 12)
    assert out == [[REPEAT]] * 12


def test_the_repetition_threshold_is_half_the_described_rows_and_at_least_three():
    own = [[o] for o in OWN[:10]]

    def run(on):
        return _pass([r + [REPEAT] if i < on else list(r) for i, r in enumerate(own)])

    out, report = run(5)  # 5 of 10 = 50%
    assert report["threshold"] == 5 and all(REPEAT not in r for r in out)
    out, report = run(4)
    assert report["repeated_sentences"] == 0 and sum(REPEAT in r for r in out) == 4


def test_short_repeated_attributes_are_not_store_boilerplate():
    rows = [[own, "Vegan and cruelty free."] for own in OWN]
    out, report = _pass(rows)
    assert out == rows and report["repeated_sentences"] == 0


def test_description_nouns():
    assert description_nouns("beauty/makeup/lip/lipstick", "Lipstick") == {"lip", "lipstick"}
    assert description_nouns("beauty/nails", "Nail Polish") == {"nails", "nail", "polish"}
    assert description_nouns("beauty", None) == frozenset()

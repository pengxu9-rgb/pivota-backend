"""A thin brand-store description is followed by the brand's own page sections.

Measured on prod 2026-09-29: 1,849 of 8,117 served brand-store products serve under 200 chars --
mostly the store's one-line tagline (the crawl's pdp_variant_description) -- while the same row's
own seed already holds the brand's Details / Overview / Benefits sections (Fenty's accordions, Rare
Beauty, Pixi, Jurlique...). The public PDP renders them; agent_pdp_view.description never did. The
sections below are shaped like the prod ones, including the junk the rule must leave out: Rare
Beauty's "Available only on RareBeauty.com", Fenty's calls to action, Tom Ford's label lines, a
retailer menu captured under "Details", our own "positioned for..." summaries -- and every line
class review #2443 fed through the real function (shipping, reviews, promos, cross-sell, legal,
markup, non-$ prices).
"""
from __future__ import annotations

import asyncio
import json

import pytest

import services.agent_pdp_view_assembler as assembler

TAGLINE = "A contour stick in a longwear, light-as-air matte formula in a range of shades for all skin tones."
DETAILS = (
    "With Match Stix Matte Contour Skinsticks, you can contour in a range of shades fine-tuned to look good "
    "on the full spectrum of skin tones.\n"
    "Made to layer, the buildable cream-to-powder formula is weightless and easy to blend.\n"
    "Learn how to snatch ya face with our Match Stix Snatch TikTok filter .\n"
    "Fenty Beauty is 100% cruelty free."
)
DETAILS_SERVED = (
    "With Match Stix Matte Contour Skinsticks, you can contour in a range of shades fine-tuned to look good "
    "on the full spectrum of skin tones.\n"
    "Made to layer, the buildable cream-to-powder formula is weightless and easy to blend.\n"
    "Fenty Beauty is 100% cruelty free."
)
GOOD = "A longwear cream-to-powder formula that blends out easily, stays put and never creases."
SIG = "sig_" + "f" * 32
HOST = "fentybeauty.com"
# Distinct sentences (no two near-duplicates), for tests that need several sections or a long one.
PROSE = [
    "The weightless cream glides over bare skin and sets to a soft matte finish within a minute.",
    "Buildable pigment lets you sheer it out for daytime or layer it up for a sculpted evening look.",
    "A slim twist-up stick slips into any pocket, so touch-ups need no brush or sponge at all.",
    "Shades were tested across deep, medium and fair complexions to keep undertones true.",
    "The formula resists heat and humidity, holding its shape through long summer afternoons.",
    "Blend with fingertips for a diffused result or a dense brush for sharper definition.",
    "It pairs neatly under powder without pilling, streaking or lifting the base beneath it.",
    "Rounded edges follow the cheekbone, jaw and temple so placement stays quick and precise.",
    "Each stick holds enough product for months of daily contouring before it runs down.",
    "Soft focus particles blur texture while the colour itself stays clean and never muddy.",
    "Tapping it over foundation keeps the layer beneath intact rather than dragging it around.",
    "A creamy slip at first touch turns velvety once warmed by the skin in a few seconds.",
]


def _row(**over):
    row = {
        "product_key": "prod::external_seed::external_seed::ext_fenty1", "merchant_id": "merch_obs_fenty",
        "platform": "external_seed", "source_product_id": "ext_fenty1", "brand": "Fenty Beauty",
        "title": "Match Stix Contour Skinstick — Amber", "description": TAGLINE,
        "image_url": "https://cdn.example/i.jpg", "pivota_signature_id": SIG, "group_is_primary": True,
        "sync_status": "live", "suppressed_at": None, "source_domain": HOST,
        "canonical_url": f"https://{HOST}/products/match-stix", "has_brand_direct_offer": False,
    }
    row.update(over)
    return row


def _sections(*extra):
    return [
        {"heading": "Overview", "body": TAGLINE, "source_kind": "shopify_encoded_accordion_attr"},
        {"heading": "Details", "body": DETAILS, "source_kind": "shopify_encoded_accordion_attr"},
        {"heading": "How to Use", "body": "Swipe onto the hollows of your cheeks and blend it out with a brush.",
         "source_kind": "shopify_encoded_accordion_attr"},
        {"heading": "Ingredients", "body": "Dimethicone, Synthetic Wax, Silica, Octyldodecanol, Mica, Tocopherol.",
         "source_kind": "shopify_encoded_accordion_attr"},
        {"heading": "Benefits", "body": "Longwear, Matte, Buildable, Crease-proof, Easy to blend",
         "source_kind": "embedded_product_json_tags"},
        *extra,
    ]


def _seed(sections=None, origin="pdp_variant_description", reviewed=False, domain=HOST, key=None, url=None):
    return {"attached_product_key": key or _row()["product_key"], "domain": domain,
            "canonical_url": url or f"https://{HOST}/products/match-stix", "description_origin": origin,
            "description_reviewed": reviewed, "details_sections": _sections() if sections is None else sections}


def _details(*lines):
    return [{"heading": "Details", "source_kind": None, "body": "\n".join(lines)}]


class _DB:
    """Answers the own-seed read with `seeds` and the storefront repetition read with `shared`
    ({line: products}). Everything else is patched away."""

    def __init__(self, seeds=(), fail=False, shared=None, shared_fails=False):
        self.seeds, self.fail = list(seeds), fail
        self.shared, self.shared_fails = dict(shared or {}), shared_fails
        self.sql = self.params = self.shared_sql = self.shared_params = None

    async def fetch_all(self, sql, params=None):
        if "unnest(CAST(:lines AS text[]))" in sql:
            self.shared_sql, self.shared_params = sql, params
            if self.shared_fails:
                raise RuntimeError("shared-line read failed")
            return [{"line": line, "products": n} for line, n in self.shared.items() if line in params["lines"]]
        self.sql, self.params = sql, params
        if self.fail:
            raise RuntimeError("seed read failed")
        return self.seeds

    async def fetch_one(self, sql, params=None):
        return None

    async def execute(self, sql, params=None):
        return None


@pytest.fixture
def patched(monkeypatch):
    import db.product_enrichment
    import services.outcome_aggregation_service as outcomes

    state = {"rows": [_row()], "overlay": {}}

    async def _rows(*a, **kw):
        return [dict(r) for r in state["rows"]]

    async def _empty(*a, **kw):
        return []

    async def _none(*a, **kw):
        return None

    async def _no_evidence(*a, **kw):
        return {}

    async def _overlays(merchant_id, *, product_keys=None, geo_code="default"):
        return state["overlay"]

    async def _no_trust(*a, **kw):
        return {}

    monkeypatch.setattr(assembler, "fetch_products_for_key", _rows)
    monkeypatch.setattr(assembler, "fetch_skus_for_keys", _empty)
    monkeypatch.setattr(assembler, "fetch_offers_for_keys", _empty)
    monkeypatch.setattr(assembler, "fetch_external_seed_for_keys", _none)
    monkeypatch.setattr(assembler, "fetch_evidence_for_keys", _no_evidence)
    monkeypatch.setattr(db.product_enrichment, "get_enrichments_for_products", _overlays)
    monkeypatch.setattr(outcomes, "seller_trust_bulk", _no_trust)
    return state


def _build(db):
    return asyncio.run(assembler.build_agent_pdp_view_row("ck_x", refresh_source="t", db=db))


def _served(sections, **db_kw):
    return _build(_DB([_seed(sections=sections)], **db_kw))["description"]


def test_a_thin_brand_tagline_is_followed_by_the_brands_own_details(patched):
    """Fenty: the Overview repeats the tagline and adds nothing; How to Use, Ingredients and the
    storefront-tag "Benefits" are not description; the TikTok call to action is not product copy."""
    row = _build(_DB([_seed()]))
    assert row["description"] == f"{TAGLINE}\n\nDetails\n{DETAILS_SERVED}"
    assert row["pivota_signature_id"] == SIG


def test_the_own_seed_read_is_per_row_newest_active_and_reads_the_review_marks(patched):
    db = _DB([_seed()])
    _build(db)
    sql = " ".join(db.sql.split())
    assert db.params == {"keys": [_row()["product_key"]]}
    for fragment in ("DISTINCT ON (attached_product_key)", "attached_product_key = ANY(:keys)",
                     "status = 'active'", "ORDER BY attached_product_key, updated_at DESC, id DESC",
                     "domain,", "canonical_url,",
                     "seed_data->>'seed_description_origin'",
                     "seed_data->'snapshot'->>'seed_description_origin'",
                     "'pivota_description_rollback_v1'", "seed_data->'pdp_details_sections'"):
        assert fragment in sql


def test_sections_stored_as_a_json_string_are_read(patched):
    assert _build(_DB([_seed(sections=json.dumps(_sections()))]))["description"].endswith(DETAILS_SERVED)


@pytest.mark.parametrize("origin,composed", [
    ("pdp_variant_description", True),
    ("pdp_product_description", True),
    ("", True),
    # chosen by a person: served exactly as reviewed
    ("aurora_product_intel_kb_reviewed_rollback", False),
    ("aurora_product_intel_kb_reviewed_supplemental_rollback", False),
    ("reviewed_source_backed_pdp_content_patch", False),
    ("legacy_unknown", False),
])
def test_only_a_description_the_crawl_took_from_the_page_is_extended(patched, origin, composed):
    row = _build(_DB([_seed(origin=origin)]))
    assert (row["description"] != TAGLINE) is composed


def test_a_reviewed_rollback_record_blocks_it_whatever_the_origin_says(patched):
    assert _build(_DB([_seed(reviewed=True)]))["description"] == TAGLINE


def test_without_an_own_seed_nothing_changes(patched):
    assert _build(_DB([]))["description"] == TAGLINE


def test_a_failed_own_seed_read_serves_the_description_as_before(patched):
    row = _build(_DB([_seed()], fail=True))
    assert row is not None and row["description"] == TAGLINE


@pytest.mark.parametrize("over,composed", [
    ({}, True),
    # a retailer's page sections are the retailer's copy
    ({"product_key": "ext:retailer:0123", "source_domain": "dermavenue.com",
      "canonical_url": "https://dermavenue.com/products/x"}, False),
    # a known retailer host, even with a brand_direct offer on the row
    ({"source_domain": "ulta.com", "canonical_url": "https://ulta.com/p/x", "has_brand_direct_offer": True}, False),
    # not the brand's domain and no brand_direct offer
    ({"source_domain": "beautyshop.example", "canonical_url": "https://beautyshop.example/x"}, False),
    # the ingest pipeline's own seller verdict makes it the brand's store
    ({"source_domain": "beautyshop.example", "canonical_url": "https://beautyshop.example/x",
      "has_brand_direct_offer": True}, True),
])
def test_only_the_brands_own_store_row_is_extended(patched, over, composed):
    patched["rows"] = [_row(**over)]
    row = patched["rows"][0]
    seed = _seed(key=row["product_key"], domain=row["source_domain"], url=row["canonical_url"])
    assert (_build(_DB([seed]))["description"] != TAGLINE) is composed


@pytest.mark.parametrize("domain,url,composed", [
    (HOST, None, True),
    ("www.fentybeauty.com", None, True),                    # same storefront, normalised
    ("fentybeauty.co.uk", None, False),                     # another storefront's seed on this row
    (None, "https://fentybeauty.com/products/x", False),    # no domain: nothing to count repetition in
    (None, "https://other.example/products/x", False),
])
def test_the_seed_must_be_on_the_rows_own_storefront(patched, domain, url, composed):
    seed = _seed(domain=domain, url=url or "https://other.example/x")
    if url:
        seed["canonical_url"] = url
    assert (_build(_DB([seed]))["description"] != TAGLINE) is composed


@pytest.mark.parametrize("length,composed", [(199, True), (200, False)])
def test_only_a_description_under_200_chars_is_extended(patched, length, composed):
    base = ("A creamy contour stick. " * 20)[:length].strip()
    base = base + "x" * (length - len(base))
    patched["rows"] = [_row(description=base)]
    assert (_build(_DB([_seed()]))["description"] != base) is composed


@pytest.mark.parametrize("overlay", ["Curated copy.", TAGLINE])
def test_an_overlay_description_is_served_as_written(patched, overlay):
    """Even an overlay that repeats the row's own tagline word for word: it is curated copy."""
    row = _row()
    patched["overlay"] = {(row["platform"], row["source_product_id"]): {"description_markdown": overlay}}
    assert _build(_DB([_seed()]))["description"] == overlay


def test_a_seed_fallback_description_is_not_extended(patched, monkeypatch):
    """The row has no description of its own; the cluster seed supplies one, and which seed that is
    depends on the caller. Nothing is appended to it."""
    patched["rows"] = [_row(description="")]

    async def _cluster_seed(*a, **kw):
        return {"id": "eps_1", "seed_data": {"description": TAGLINE}}

    monkeypatch.setattr(assembler, "fetch_external_seed_for_keys", _cluster_seed)
    assert _build(_DB([_seed()]))["description"] == TAGLINE


# ----------------------------------------------------------------- storefront repetition

def test_a_line_other_products_of_the_storefront_carry_is_dropped(patched):
    """"Fenty Beauty is 100% cruelty free." sits on every Fenty product: the store's line."""
    db = _DB([_seed()], shared={"Fenty Beauty is 100% cruelty free.": 2})
    assert _build(db)["description"] == f"{TAGLINE}\n\nDetails\n" + DETAILS_SERVED.rsplit("\n", 1)[0]
    params = db.shared_params
    assert params["domains"] == [HOST, f"www.{HOST}"]  # seed domains are stored both ways
    assert params["keys"] == [_row()["product_key"]]
    assert params["own_base"] == "match stix contour skinstick"  # its own shades are not "other products"
    assert "Fenty Beauty is 100% cruelty free." in params["lines"]


def test_a_line_one_other_product_carries_is_kept(patched):
    assert _build(_DB([_seed()], shared={"Fenty Beauty is 100% cruelty free.": 1}))["description"].endswith(
        "Fenty Beauty is 100% cruelty free.")


def test_a_failed_repetition_check_appends_nothing(patched):
    assert _build(_DB([_seed()], shared_fails=True))["description"] == TAGLINE


def test_a_seed_without_a_domain_is_not_checked_and_appends_nothing(patched, caplog):
    seed = _seed(domain=None)
    seed["canonical_url"] = f"https://{HOST}/products/match-stix"
    with caplog.at_level("WARNING"):
        assert _build(_DB([seed]))["description"] == TAGLINE
    assert "shared section line check failed" not in caplog.text


def test_only_lines_that_could_be_served_are_sent_to_the_repetition_check(patched):
    row = dict(_row(), **{assembler._OWN_SEED_COPY: _seed(sections=_details(
        GOOD, "skin care", "body care and bath", "Rated 4.8 out of 5 stars by our customers today."))})
    assert assembler.brand_section_candidate_lines(row, None) == [GOOD]


def test_the_repetition_query_counts_other_products_by_base_title_on_the_storefront(patched):
    db = _DB([_seed()])
    _build(db)
    sql = " ".join(db.shared_sql.split())
    for fragment in ("s.domain = ANY(CAST(:domains AS text[]))", "s.status = 'active'",
                     "s.attached_product_key <> ALL(:keys)", "WITH seeds AS MATERIALIZED", "bodies AS MATERIALIZED",
                     "LIMIT :seed_limit",
                     "count(DISTINCT b.base)", "b.base <> :own_base", "unnest(CAST(:lines AS text[]))",
                     "strpos(b.body, l.line) > 0"):
        assert fragment in sql


def test_a_www_seed_domain_is_checked_as_the_bare_host(patched):
    db = _DB([_seed(domain="www.fentybeauty.com")])
    _build(db)
    assert db.shared_params["domains"] == [HOST, f"www.{HOST}"]


def test_lines_past_the_check_cap_are_not_appended(patched, monkeypatch):
    monkeypatch.setattr(assembler, "_SHARED_LINE_CHECK_MAX", 1)
    db = _DB([_seed(sections=_details(PROSE[0], PROSE[1]))])
    assert _build(db)["description"] == f"{TAGLINE}\n\nDetails\n{PROSE[0]}"
    assert db.shared_params["lines"] == [PROSE[0]]  # page order: the first line is the one checked


def test_the_candidate_lines_keep_page_order(patched):
    row = dict(_row(), **{assembler._OWN_SEED_COPY: _seed(sections=_details(PROSE[3], PROSE[0], PROSE[3]))})
    assert assembler.brand_section_candidate_lines(row, None) == [PROSE[3], PROSE[0]]


def test_no_repetition_read_without_a_candidate_line(patched):
    db = _DB([_seed(origin="aurora_product_intel_kb_reviewed_rollback")])
    _build(db)
    assert db.shared_sql is None


def test_the_edition_suffix_is_cut_the_same_way_as_in_sql():
    assert assembler._product_base_title("Pro Filt'r Soft Matte Longwear Foundation — #265") == \
        "pro filt'r soft matte longwear foundation"
    assert assembler._product_base_title("Find Comfort Claw Clip – Awaken Confidence") == "find comfort claw clip"
    assert assembler._product_base_title("Glow Mist - Mini") == "glow mist"
    assert assembler._product_base_title("Hydrium Green-Tea Gel") == "hydrium green-tea gel"


# ----------------------------------------------------------------- what a line may be

@pytest.mark.parametrize("line,kind", [
    ("A <p>deeply hydrating <b>mist</b></p> for dry skin and dull complexions.", "markup"),
    ("A hydrating mist&nbsp;that refreshes dry skin all day long.", "markup"),
    ("See the full routine at https://brand.example/routine for more ideas.", "link"),
    ("Find the whole range at www.brand.example today and every day.", "link"),
    ("Available only at the flagship store on RareBeauty.com online.", "link"),
    ("The full size costs $25 and lasts about three months of use.", "price"),
    ("The full size costs 25 USD and lasts about three months of use.", "price"),
    ("The full size costs ₩25,000 and lasts about three months of use.", "price"),
    ("The full size costs RM150 and lasts about three months of use.", "price"),
    ("The full size costs 4,500円 and lasts about three months of use.", "price"),
    ("Take 20% off your first order when you join the list today.", "promo"),
    ("Save 20 percent on the full routine when you buy it together.", "promo"),
    ("Buy one get one half off across the whole lip collection.", "promo"),
    ("Limited edition: while supplies last, so grab yours soon.", "promo"),
    ("Complimentary standard shipping on all orders over fifty dollars.", "promo"),
    ("Standard shipping on all orders over fifty dollars, every day.", "shipping"),
    ("Free returns within 30 days of delivery on every order placed.", "promo"),
    ("Ships within 2-3 business days from our warehouse in New Jersey.", "shipping"),
    ("Rated 4.8 out of 5 stars by over 2,000 verified customers.", "reviews"),
    ("Pair it with Gloss Bomb for the full signature lip look tonight.", "cross_sell"),
    ("Ingredients: Water, Glycerin, Butylene Glycol, Niacinamide, Panthenol.", "legal"),
    ("Warning: For external use only. Avoid contact with eyes at all times.", "legal"),
    ("Water, Glycerin, Butylene Glycol, Niacinamide, Panthenol, Allantoin, Betaine, Squalane, Ceramide NP",
     "ingredient_list"),
    ("Join our rewards program to earn points on every purchase you make.", "store"),
    ("Members of our Beauty Rewards club get early access to every launch.", "store"),
    ("Available only while this collection lasts, in selected colours.", "store"),
    ("Learn more about the Icon case and its refills in our guide.", "call_to_action"),
    ("Watch the full tutorial on our TikTok channel this week.", "call_to_action"),
    ("Want to protect your skin the right way? Tap into our blog: Mastering Moisture.", "call_to_action"),
    # evasions from review #2443 round 2
    ("Enjoy frее ѕhipping on every order over fifty dollars today.", "promo"),        # Cyrillic е, ѕ
    ("The full size is ＄ 25 and lasts for months of daily use.", "price"),            # fullwidth
    ("Read the [full routine guide](/pages/routine) before you start.", "link"),
    ("Visit brand.shop to see the complete range of shades.", "link"),
    ("Receive a complimentary deluxe mini with every purchase today.", "promo"),
    ("As seen on the runways of Paris and in every magazine.", "promo"),
    ("Join our loyalty club and earn points on every single purchase.", "store"),
    ("Pay in 4 interest-free installments with Shop Pay at checkout.", "store"),
    ("Questions? Call our team at (800) 555-0199 any weekday.", "contact"),
    ("Write to hello@brand.example for help with any order.", "contact"),
    ("Write to hello at brand dot com for help with any order.", "contact"),
    ("Tag your look with @brand for a chance to be featured.", "contact"),
    ("These statements have not been evaluated by the Food and Drug Administration.", "legal"),
])
def test_every_class_of_non_product_line_is_named(line, kind):
    assert assembler._not_product_copy(line) == kind


@pytest.mark.parametrize("line", [
    "A <p>deeply hydrating <b>mist</b></p> for dry skin and dull complexions.",
    "See the full routine at https://brand.example/routine for more ideas.",
    "Available only at the flagship store on RareBeauty.com online.",
    "The full size costs ₩25,000 and lasts about three months of use.",
    "The full size costs 4,500円 and lasts about three months of use.",
    "Take 20% off your first order when you join the list today.",
    "Complimentary standard shipping on all orders over fifty dollars.",
    "Rated 4.8 out of 5 stars by over 2,000 verified customers.",
    "Pair it with Gloss Bomb for the full signature lip look tonight.",
    "Warning: For external use only. Avoid contact with eyes at all times.",
    "Learn more about the Icon case and its refills in our guide.",
])
def test_a_non_product_line_is_dropped_from_the_section_through_the_producer(patched, line):
    assert _served(_details(GOOD, line)) == f"{TAGLINE}\n\nDetails\n{GOOD}"


@pytest.mark.parametrize("line", [
    "Formulated with squalane and vitamin E to keep lips soft and comfortable.",
    "Made to layer, the buildable formula is weightless and easy to blend.",
    "Dermatologist tested and suitable for sensitive skin types.",
    "It returns skin to a calm, balanced feel after cleansing.",
    "Apply to the high points of your face for a lit, lifted glow.",
    "Complementary shades that flatter every undertone and skin type.",
    "Contains 2% niacinamide and 10% squalane for a softer feel.",
])
def test_product_copy_is_not_mistaken_for_store_text(line):
    assert assembler._not_product_copy(line) is None


@pytest.mark.parametrize("marker", ["*", "†", "‡", "§", "¹"])
def test_a_footnote_line_is_dropped_whatever_its_marker(patched, marker):
    footnote = f"{marker}In an 8-week clinical study on 40 people, results were measured by an independent lab."
    assert _served(_details(GOOD, footnote)) == f"{TAGLINE}\n\nDetails\n{GOOD}"


def test_a_section_with_a_script_is_dropped_whole(patched):
    assert _served(_details(GOOD, "<script>alert('x')</script> and more words here")) == TAGLINE


def test_store_footnote_and_label_lines_are_dropped_and_a_menu_adds_nothing(patched):
    sections = [
        # Rare Beauty: product copy, then a store line
        {"heading": "Details", "source_kind": None, "body":
            "Smooth, lightweight design with a firm yet comfortable hold.\n"
            "Embossed Rare Beauty monogram logo on both sides.\n"
            "Available only on RareBeauty.com"},
        # a retailer menu captured under a second "Details" (beautyofjoseon.com): short lines and store lines
        {"heading": "Details", "source_kind": "accordion_control", "body":
            "Community\nAbout Us\nOhlolly Rewards\nRefer a Friend and earn rewards today\nJournal"},
        # Tom Ford: label lines with no sentence
        {"heading": "Benefits", "source_kind": "details_summary", "body":
            "Key Notes\nSkin Type\nWhat Else You Need To Know\nBlack Truffle, Ylang Ylang, Black Orchid, Black Plum"},
        # a claim footnote
        {"heading": "Overview", "source_kind": None, "body":
            "A high-shine gloss that leaves lips looking fuller, smoother and softer all day long.\n"
            "*Based on 1st half of 2025 Gloss Bomb Universal Lip Luminizer sales"},
    ]
    assert _served(sections) == (
        f"{TAGLINE}\n\nDetails\nSmooth, lightweight design with a firm yet comfortable hold.\n"
        "Embossed Rare Beauty monogram logo on both sides.\n\n"
        "Overview\nA high-shine gloss that leaves lips looking fuller, smoother and softer all day long."
    )


@pytest.mark.parametrize("body", [
    # a customer review captured beside the product copy
    "I love this contour stick so much and my skin looks snatched and sculpted every single day of the week.",
    # our own generated summary, not the brand's copy
    "A contour stick positioned for sculpting routines, built around a cream-to-powder texture and longwear.",
    "Elasticity-focused gel mask care with collagen and peptide positioning, built for bouncy elastic skin.",
    "A gel mask for elasticity and bounce, summarised here from reviewed source evidence and notes.",
])
def test_a_review_or_our_own_summary_is_not_brand_copy(patched, body):
    assert _served(_details(body)) == TAGLINE


@pytest.mark.parametrize("heading", [
    "How to Use", "Ingredients", "Key Ingredients", "What's In It", "Claims", "Clinical Results",
    "About The Brand", "Product Type", "Features", "Shade Finding Tips", "FAQ",
])
def test_only_descriptive_headings_are_used(patched, heading):
    assert _served([{"heading": heading, "source_kind": None, "body": GOOD}]) == TAGLINE


@pytest.mark.parametrize("heading", [
    "Details", "DETAILS", "Product Details", "Overview", "Description", "Product Description", "Benefits",
    "Key Benefits", "What it is", "What it does", "Why we made it", "Why you'll love it", "Highlights",
    "Details\n\nDetails",
])
def test_descriptive_headings_are_used(patched, heading):
    assert _served([{"heading": heading, "source_kind": None, "body": GOOD}]) != TAGLINE


@pytest.mark.parametrize("kind", ["embedded_product_json_tags", "shopify_product_tag",
                                  "embedded_product_json_media_alt"])
def test_tag_and_alt_text_sections_are_not_prose(patched, kind):
    assert _served([{"heading": "Details", "source_kind": kind, "body": GOOD}]) == TAGLINE


def test_each_line_filter_decides_alone(patched):
    """One section per filter, each body long enough to be served if its filter were missing."""
    sections = [
        # a claim footnote in sentence case: only the "*" rule catches it
        {"heading": "Details", "source_kind": None, "body":
            "*In an 8-week clinical study on 40 people, results were measured by an independent lab."},
        # label lines, long enough together to pass the section minimum
        {"heading": "Overview", "source_kind": None, "body":
            "What Else You Need To Know About This Scent\n"
            "Black Orchid Eau de Parfum 50mLBlack Orchid Eau de Parfum Travel Spray 10mL"},
        # a lowercase menu: every entry under five words
        {"heading": "Description", "source_kind": None, "body":
            "skin care\nbody care and bath\nhair care for all\nsets and gift kits\nnew arrivals this week\n"
            "best sellers of the month\ntravel size minis"},
        # prose under the section minimum
        {"heading": "Benefits", "source_kind": None, "body": "A soft brush for blending powder and cream products."},
    ]
    assert _served(sections) == TAGLINE


@pytest.mark.parametrize("line,label", [
    ("Rich Black Truffle And Warm Amber Notes", True),          # every word capitalised
    ("Rich Black Truffle And Warm amber notes", False),         # 4 of 6 is under 75%
    ("Rich Black Truffle And amber Notes", True),               # 5 of 6
    ("Rich Black Truffle And Warm Amber Notes.", False),        # a sentence, whatever its case
])
def test_a_label_line_is_mostly_capitalised_and_ends_no_sentence(line, label):
    assert assembler._is_label_line(line) is label


# ----------------------------------------------------------------- repeats

def test_a_tagline_inside_a_longer_line_is_not_served_twice(patched):
    body = f"{TAGLINE} {PROSE[0]}"
    assert _served(_details(body)) == f"{TAGLINE}\n\nDetails\n{PROSE[0]}"


def test_a_near_duplicate_sentence_is_served_once(patched):
    sections = [
        {"heading": "Details", "source_kind": None, "body": PROSE[0]},
        {"heading": "Benefits", "source_kind": None, "body":
            PROSE[0].replace("within a minute", "in about a minute") + "\n" + PROSE[1]},
    ]
    assert _served(sections) == f"{TAGLINE}\n\nDetails\n{PROSE[0]}\n\nBenefits\n{PROSE[1]}"


def test_a_line_served_by_an_earlier_section_is_not_repeated(patched):
    sections = [
        {"heading": "Details", "source_kind": None, "body": PROSE[2]},
        {"heading": "Benefits", "source_kind": None, "body": f"{PROSE[2]}\n{PROSE[3]}"},
    ]
    description = _served(sections)
    assert description.count(PROSE[2]) == 1
    assert description.endswith(f"Benefits\n{PROSE[3]}")


# ----------------------------------------------------------------- shape and caps

def test_an_all_caps_heading_is_served_capitalised(patched):
    assert "\n\nDetails\nA longwear" in _served([{"heading": "DETAILS", "source_kind": None, "body": GOOD}])


def test_a_heading_is_used_once_and_at_most_three_sections(patched):
    sections = [{"heading": h, "source_kind": None, "body": PROSE[i]}
                for i, h in enumerate(["Details", "Details", "Overview", "Benefits", "What it is"])]
    description = _served(sections)
    assert [b.split("\n")[0] for b in description.split("\n\n")[1:]] == ["Details", "Overview", "Benefits"]
    assert PROSE[1] not in description  # the second "Details"


def test_the_composed_description_is_capped_and_each_section_ends_on_a_sentence(patched):
    one_long_line = " ".join(PROSE)  # over _BRAND_SECTION_MAX_CHARS on its own
    assert len(one_long_line) > assembler._BRAND_SECTION_MAX_CHARS
    description = _served([{"heading": "Details", "source_kind": None, "body": one_long_line},
                           {"heading": "Overview", "source_kind": None, "body": GOOD}])
    assert len(description) <= assembler._COMPOSED_DESCRIPTION_MAX_CHARS
    details = description.split("\n\n")[1].split("\n", 1)[1]
    assert len(details) <= assembler._BRAND_SECTION_MAX_CHARS and details.endswith(".")
    assert details.startswith(PROSE[0]) and PROSE[-1] not in details


def test_a_section_that_would_break_the_total_cap_ends_the_description(patched, monkeypatch):
    """The cap stops composition at the first section that does not fit; a smaller one after it,
    which would fit, is not squeezed in (the page's order is kept)."""
    cap = len(TAGLINE) + (2 + len("Details\n") + len(PROSE[0])) + (2 + len("Benefits\n") + len(PROSE[6]))
    monkeypatch.setattr(assembler, "_COMPOSED_DESCRIPTION_MAX_CHARS", cap)
    sections = [
        {"heading": "Details", "source_kind": None, "body": PROSE[0]},
        {"heading": "Overview", "source_kind": None, "body": " ".join(PROSE[1:5])},
        {"heading": "Benefits", "source_kind": None, "body": PROSE[6]},
    ]
    assert _served(sections) == f"{TAGLINE}\n\nDetails\n{PROSE[0]}"


def test_a_long_section_is_cut_at_a_sentence_not_mid_word(monkeypatch):
    text = "x" * 50 + ". " + "y" * 300
    assert assembler._clip_at_sentence(text, 200) == ""                       # the only end is before limit // 3
    text = "x" * 80 + ". " + "y" * 300
    assert assembler._clip_at_sentence(text, 200) == "x" * 80 + "."


def test_a_run_together_label_is_split_onto_its_own_line(patched):
    """sigmabeauty.com: "…trouble spotsUnique Feature: …" -- the crawl ran the labels together."""
    sections = [{"heading": "Details", "source_kind": "description_delimited_section", "body":
                 "Function: Correct dark circles or cover tattoos, spots, and scarsUnique Feature: "
                 "Features two blendable shades for a wide spectrum of correction"}]
    assert _served(sections).endswith("Details\nFunction: Correct dark circles or cover tattoos, spots, and scars\n"
                                      "Unique Feature: Features two blendable shades for a wide spectrum of correction")


def test_the_pick_is_the_same_with_and_without_the_sections(patched):
    """The sections extend the served description only; they never move the served row."""
    with_sections = _build(_DB([_seed()]))
    without = _build(_DB([]))
    assert with_sections["pivota_signature_id"] == without["pivota_signature_id"] == SIG
    assert {k for k in with_sections if with_sections[k] != without[k]} == {"description"}


def test_the_content_bar_does_not_read_the_sections(patched):
    """A row whose own column fails the bar still loses to a sibling that passes, whatever its
    sections: they only lengthen whichever row already won."""
    thin = _row(description="Contour stick.")
    sibling = _row(product_key="prod::external_seed::external_seed::ext_fenty2", source_product_id="ext_fenty2",
                   pivota_signature_id="sig_" + "e" * 32, description=TAGLINE, title="Match Stix — Truffle")
    patched["rows"] = [thin, sibling]
    row = _build(_DB([_seed(key=thin["product_key"])]))
    assert row["pivota_signature_id"] == "sig_" + "e" * 32

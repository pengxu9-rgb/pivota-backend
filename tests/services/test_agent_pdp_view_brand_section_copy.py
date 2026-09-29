"""A thin brand-store description is followed by the brand's own page sections.

Measured on prod 2026-09-29: 1,849 of 8,117 served brand-store products serve under 200 chars --
mostly the store's one-line tagline (the crawl's pdp_variant_description) -- while the same row's
own seed already holds the brand's Details / Overview / Benefits sections (Fenty's accordions, Rare
Beauty, Pixi, Jurlique...). The public PDP renders them; agent_pdp_view.description never did. The
sections below are shaped like the prod ones, including the junk the rule must leave out: Rare
Beauty's "Available only on RareBeauty.com", Fenty's calls to action, Tom Ford's label lines, a
retailer menu captured under "Details", our own "positioned for..." summaries.
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
SIG = "sig_" + "f" * 32


def _row(**over):
    row = {
        "product_key": "prod::external_seed::external_seed::ext_fenty1", "merchant_id": "merch_obs_fenty",
        "platform": "external_seed", "source_product_id": "ext_fenty1", "brand": "Fenty Beauty",
        "title": "Match Stix Contour Skinstick", "description": TAGLINE, "image_url": "https://cdn.example/i.jpg",
        "pivota_signature_id": SIG, "group_is_primary": True, "sync_status": "live", "suppressed_at": None,
        "source_domain": "fentybeauty.com", "canonical_url": "https://fentybeauty.com/products/match-stix",
        "has_brand_direct_offer": False,
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


def _seed(sections=None, origin="pdp_variant_description", reviewed=False):
    return {"attached_product_key": _row()["product_key"], "description_origin": origin,
            "description_reviewed": reviewed, "details_sections": _sections() if sections is None else sections}


class _DB:
    """Answers the own-seed read with `seeds`; records it. Nothing else reaches it (fetchers patched)."""

    def __init__(self, seeds=(), fail=False):
        self.seeds, self.fail, self.sql, self.params = list(seeds), fail, None, None

    async def fetch_all(self, sql, params=None):
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
                     "status = 'active'", "ORDER BY attached_product_key, updated_at DESC",
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
    seed = dict(_seed(), attached_product_key=patched["rows"][0]["product_key"])
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
    row = _build(_DB([_seed(sections=sections)]))
    assert row["description"] == (
        f"{TAGLINE}\n\nDetails\nSmooth, lightweight design with a firm yet comfortable hold.\n"
        "Embossed Rare Beauty monogram logo on both sides.\n\n"
        "Overview\nA high-shine gloss that leaves lips looking fuller, smoother and softer all day long."
    )


@pytest.mark.parametrize("body", [
    # a customer review captured beside the product copy
    "I love this contour stick so much and my skin looks snatched and sculpted every single day of the week.",
    # our own generated summary, not the brand's copy
    "A contour stick positioned for sculpting routines, built around a cream-to-powder texture and longwear.",
    "Elasticity-focused gel mask care with collagen and peptide positioning from reviewed source evidence.",
])
def test_a_review_or_our_own_summary_is_not_brand_copy(patched, body):
    sections = [{"heading": "Details", "source_kind": None, "body": body}]
    assert _build(_DB([_seed(sections=sections)]))["description"] == TAGLINE


@pytest.mark.parametrize("heading", [
    "How to Use", "Ingredients", "Key Ingredients", "What's In It", "Claims", "Clinical Results",
    "About The Brand", "Product Type", "Features", "Shade Finding Tips", "FAQ",
])
def test_only_descriptive_headings_are_used(patched, heading):
    sections = [{"heading": heading, "source_kind": None,
                 "body": "A longwear cream-to-powder formula that blends out easily, stays put and never creases."}]
    assert _build(_DB([_seed(sections=sections)]))["description"] == TAGLINE


@pytest.mark.parametrize("heading", [
    "Details", "DETAILS", "Product Details", "Overview", "Description", "Product Description", "Benefits",
    "Key Benefits", "What it is", "What it does", "Why we made it", "Why you'll love it", "Highlights",
    "Details\n\nDetails",
])
def test_descriptive_headings_are_used(patched, heading):
    sections = [{"heading": heading, "source_kind": None,
                 "body": "A longwear cream-to-powder formula that blends out easily, stays put and never creases."}]
    assert _build(_DB([_seed(sections=sections)]))["description"] != TAGLINE


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
        # image alt text is captured as a "section"; it is not the brand's prose
        {"heading": "What it is", "source_kind": "embedded_product_json_media_alt", "body":
            "Swatch of the creamy, rich texture of the contour stick. Great for dry and mature skin types alike."},
    ]
    assert _build(_DB([_seed(sections=sections)]))["description"] == TAGLINE


def test_a_line_served_by_an_earlier_section_is_not_repeated(patched):
    shared = "The buildable cream-to-powder formula is weightless, easy to blend and stays put all day."
    sections = [
        {"heading": "Details", "source_kind": None, "body": shared},
        {"heading": "Benefits", "source_kind": None, "body":
            f"{shared}\nComes in twelve shades tested on the full spectrum of skin tones and undertones."},
    ]
    description = _build(_DB([_seed(sections=sections)]))["description"]
    assert description.count(shared) == 1
    assert description.endswith("Benefits\nComes in twelve shades tested on the full spectrum of skin tones and undertones.")


def test_an_all_caps_heading_is_served_capitalised(patched):
    sections = [{"heading": "DETAILS", "source_kind": None,
                 "body": "A longwear cream-to-powder formula that blends out easily, stays put and never creases."}]
    assert "\n\nDetails\nA longwear" in _build(_DB([_seed(sections=sections)]))["description"]


def test_a_heading_is_used_once_and_at_most_three_sections(patched):
    prose = "A longwear cream-to-powder formula that blends out easily, stays put and never creases {}."
    sections = [{"heading": h, "source_kind": None, "body": prose.format(i)}
                for i, h in enumerate(["Details", "Details", "Overview", "Benefits", "What it is"])]
    description = _build(_DB([_seed(sections=sections)]))["description"]
    assert [b.split("\n")[0] for b in description.split("\n\n")[1:]] == ["Details", "Overview", "Benefits"]


def test_the_composed_description_is_capped_and_ends_on_a_sentence(patched):
    sentence = "This buildable formula blends out easily and stays put all day long without creasing. "
    sections = [{"heading": h, "source_kind": None, "body": (sentence.replace("This", w) * 14).strip()}
                for h, w in [("Details", "This"), ("Overview", "Our"), ("Benefits", "The")]]
    description = _build(_DB([_seed(sections=sections)]))["description"]
    assert len(description) <= assembler._COMPOSED_DESCRIPTION_MAX_CHARS
    for block in description.split("\n\n")[1:]:
        body = block.split("\n", 1)[1]
        assert len(body) <= assembler._BRAND_SECTION_MAX_CHARS and body.endswith(".")


def test_a_run_together_label_is_split_onto_its_own_line(patched):
    """sigmabeauty.com: "…trouble spotsUnique Feature: …" -- the crawl ran the labels together."""
    sections = [{"heading": "Details", "source_kind": "description_delimited_section", "body":
                 "Function: Correct dark circles or cover tattoos, spots, and scarsUnique Feature: "
                 "Features two blendable shades for a wide spectrum of correction"}]
    description = _build(_DB([_seed(sections=sections)]))["description"]
    assert description.endswith("Details\nFunction: Correct dark circles or cover tattoos, spots, and scars\n"
                                 "Unique Feature: Features two blendable shades for a wide spectrum of correction")


def test_the_pick_is_the_same_with_and_without_the_sections(patched):
    """The sections extend the served description only; they never move the served row."""
    with_sections = _build(_DB([_seed()]))
    without = _build(_DB([]))
    assert with_sections["pivota_signature_id"] == without["pivota_signature_id"] == SIG
    assert {k for k in with_sections if with_sections[k] != without[k]} == {"description"}

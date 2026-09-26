"""A false-lash or nail product's own title, where the merchant type says nothing -- and nowhere else.

OPT-IN (options.lash_nail_title_evidence): the queue worker, the brand-official lane and the repair
planner share the resolver and must not change unless a job asks. Measured 2026-09-26: kissusa.com
types 1,395 of 1,464 products "Physical Products" (39 blank), so its brand-official crawl kept 3 rows
although the titles name the lash and nail leaves #2364 added. Titles below are copied from
kissusa.com, unitedbeautysupply.com and mybeautyexchange.com on 2026-09-26.
"""
from __future__ import annotations

import pytest

from services import curated_brand_feed as feed
from services.retailer_ingest import detectors

LASHES = "beauty/makeup/eye/false-lashes"
PRESS_ON = "beauty/makeup/nails/press-on-nails"
POLISH = "beauty/makeup/nails/nail-polish"
DOOR = feed.CATEGORY_CONFIDENCE_LASH_NAIL_TITLE


def resolve(title, ptype=None, flag="beauty"):
    return feed._resolve_category(product_type=ptype, title=title, flag_path=flag)


@pytest.fixture
def evidence():
    with feed.lash_nail_title_evidence():
        yield


ACCEPTED = [
    ("Kiss Lash Couture Faux Mink False Eyelashes Multipack - Jubilee | 4 Pairs, Strip Lashes, 10mm",
     "Physical Products", LASHES),
    ("Impress Falsies Pre-Glued False Eyelashes Minipack - Authentic | 12 Lash Clusters, 12mm-14mm",
     "Physical Products", LASHES),
    ("Falscara Soft Natural Mega Multipack | DIY False Lash Extensions, Cluster Lash, Wispy Lashes, 70 Wisps",
     "", LASHES),
    ("Kiss Gel Fantasy Allure Press On Glue Nails - Fancy You Dearly", "Physical Products", PRESS_ON),
    ("Kiss Core French Press On Fake Glue Nails - If You Dare | French Tips, White, Real Short, Squoval",
     "Physical Products", PRESS_ON),
    ("Kiss Gel Fantasy Press On Glue Toenails - Chase It", "Physical Products", PRESS_ON),
    ("Kiss Salon X-tend Color Press On Soft Gel Nails - Brick Road", "Physical Products", PRESS_ON),
    ("Impress Color No Glue Fake Press On Nails - Ballroom | Solid, White, Medium, Almond, Manicure",
     "Physical Products", PRESS_ON),
    # a stockist's lash or nail shelf, written as a path, vouches for its own family
    ("KISS i.Envy Press & Go Waterproof No Glue Pre-Glued Cluster Lash Refill Pack",
     "COSMETICS - EYELASH - CLUSTERS", LASHES),
    ("Ruby Kisses High Shine Nail Polish - Shades 01-10", "Nail Styling", POLISH),
    ("KISS Gel Strong Nail Polish(#91-#103)", "ACCESSORIES", POLISH),
]


@pytest.mark.parametrize("title,ptype,want", ACCEPTED)
def test_an_explicit_lash_or_nail_title_resolves_where_the_type_says_nothing(title, ptype, want, evidence):
    assert resolve(title, ptype) == (want, DOOR)


@pytest.mark.parametrize("title,ptype,want", ACCEPTED)
def test_the_door_is_off_unless_the_job_asks(title, ptype, want):
    path, confidence = resolve(title, ptype)
    assert confidence != DOOR and path != want


@pytest.mark.parametrize("title,ptype", [
    # two leaves named: ambiguity, never a guess
    ("Kiss Magnetic Eyeliner & False Eyelashes - Charm | 1 Pair, Strip Lashes, 12mm", "Physical Products"),
    ("Kiss Lash Couture False Eyelashes The Muses Collection - Aura | 1 Pair, Strip Lashes", "Physical Products"),
    ("Kiss Classy Press On Glue Nails - Silk Dress", "Physical Products"),
    ("Falscara Bold Wispy Essentials Kit | DIY False Lash Extensions, Cluster Lash", "Physical Products"),
    # the one leaf named is not a lash or nail leaf -- the lip door's leaves included
    ("Kiss Colors & Care Lace Bond Spray | Extreme Hold, Wig Adhesive Spray, 6 oz, Hair Care", "Physical Products"),
    ("3CE - Soft Matte Lipstick 3.5g", ""),
    ("Kiss PowerFlex Brush-On Fake Nail Glue - 0.17 oz.", "Physical Products"),  # names a brush
    # no leaf named: accessories and tools
    ("Kiss Glow-In-The-Dark Halloween Press On Fake Nail Art Stickers - Creepin | 3 Sheets", "Physical Products"),
    ("Kiss Glue OFF Press On Fake Nails Remover", "Physical Products"),
    ("Kiss Lash Glue Remover Pads | Pre-Soaked Oil Pads, 30 Pieces", "Physical Products"),
    # not a product: merch, cards, cases, other audiences
    ("Kiss Press On Nails Gift Card", ""),
    ("False Eyelashes Storage Case", ""),
    ("Nail Polish for Dogs", ""),
    ("Nail Polish Socks", ""),
    ("Nail Polish Wall Art Print", ""),
    # a tool or accessory FOR a lash or nail product (review of #2377)
    ("Individual Lash Tweezers", ""),
    ("Falscara Tweezer Applicator", "Physical Products"),
    ("Lash Cluster Storage Tray", ""),
    ("Press On Nails Cuticle Pusher", ""),
    ("Kiss Press On Nails File Buffer 2 Pack", ""),
    ("Kiss imPRESS Press-On Manicure Prep Pads", ""),
    ("Nail Polish Rack Holder", "Accessories"),
    ("Nail Polish Bottle Opener", ""),
    # the merchant's shelf names ANOTHER family or a tool shelf
    ("Kiss Gel Fantasy Press On Glue Nails - Chase It", "Eye lashes"),
    ("Kiss Lash Couture Faux Mink False Eyelashes - Jubilee", "NAILS"),
    ("i Envy Super Strong Waterproof Clear Eyelash Adhesive 0.17oz", "BROW & LASH TOOLS"),
    ("Kiss Gel Fantasy Press On Glue Nails - Chase It", "NAIL TOOLS"),
    ("Kiss Gel Fantasy Press On Glue Nails - Chase It", "Hair Care"),
    ("Kiss Gel Fantasy Press On Glue Nails - Chase It", "Toys"),
    ("Kiss Gel Fantasy Press On Glue Nails - Chase It", "Nails, Hair"),
])
def test_what_the_door_refuses_stays_unresolved(title, ptype, evidence):
    assert resolve(title, ptype)[1] != DOOR


@pytest.mark.parametrize("title,ptype", [
    ("OPI Nail Lacquer - Big Apple Red", "Nail Polish"),
    ("Kiss Lash Couture Faux Mink False Eyelashes - Jubilee", "False Eyelashes"),
    ("Kiss Colors & Care Edge Fixer Gel - Sweet Peach", "Hair Care"),
])
def test_a_row_the_evidence_policy_resolves_is_untouched(title, ptype):
    """The door fills only what everything else left unresolved: a typed row keeps its own answer."""
    without = resolve(title, ptype)
    with feed.lash_nail_title_evidence():
        assert resolve(title, ptype) == without


def test_the_lip_door_is_not_widened():
    """The lip door stays lip-only: turning it on places no lash or nail row, and this door no lip row."""
    lash = "Kiss Lash Couture Faux Mink False Eyelashes - Jubilee"
    with feed.lip_title_evidence():
        assert resolve(lash, "Physical Products")[1] != feed.CATEGORY_CONFIDENCE_LIP_TITLE
        assert resolve(lash, "")[0] != LASHES
    with feed.lash_nail_title_evidence():
        assert not resolve("3CE - Soft Matte Lipstick 3.5g", "")[0].startswith("beauty/makeup/lip/")


def test_the_confidence_is_distinct_from_every_other_writer():
    """Read from the source, not a hand list: every CATEGORY_CONFIDENCE_* constant any writer declares."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[2]
    declared = re.compile(r"^\s*(CATEGORY_CONFIDENCE_[A-Z_]+)\s*(?::[^=]*)?=\s*([0-9.]+)", re.M)
    values = {}
    for path in [*root.joinpath("services").rglob("*.py"), *root.joinpath("scripts").rglob("*.py")]:
        for name, value in declared.findall(path.read_text(encoding="utf-8", errors="ignore")):
            values.setdefault(float(value), set()).add(name)
    assert values[DOOR] == {"CATEGORY_CONFIDENCE_LASH_NAIL_TITLE"}
    assert len(values) >= 8  # the scan found the other writers (0.3 ... 1.0), not nothing


def _record(title, ptype, handle):
    return feed.shopify_product_to_record(
        {"id": abs(hash(handle)) % 10**9, "vendor": "Kiss", "title": title, "handle": handle,
         "product_type": ptype, "body_html": "<p>Salon-quality press-on nails in minutes.</p>",
         "images": [{"src": "https://www.kissusa.com/i.jpg"}],
         "variants": [{"id": abs(hash(handle + "v")) % 10**12, "price": "9.99", "available": True, "sku": handle}]},
        domain="www.kissusa.com", category_path="beauty", brand_override="KISS", currency="USD",
        source_role="brand_official", emit_native_variants=True,
    )


def test_rows_the_door_placed_hold_until_that_row_is_accepted():
    """Built by the real producer: only the title vouches for a door-placed row, so it holds, per row."""
    with feed.lash_nail_title_evidence():
        rec = _record("Kiss Gel Fantasy Press On Glue Toenails - Chase It", "Physical Products", "chase-it")
    assert rec["pdp"]["category_path"] == PRESS_ON
    flags = detectors.detect([rec])
    held = [f for f in detectors.blocking(flags) if f["rule"] == "placed_by_lash_nail_title"]
    assert [f["key"] for f in held] == ["placed_by_lash_nail_title:chase-it"]
    assert detectors.blocking(flags, accepted=[f["key"] for f in detectors.blocking(flags)]) == []


def test_without_the_option_the_same_row_is_not_placed_and_not_flagged():
    rec = _record("Kiss Gel Fantasy Press On Glue Toenails - Chase It", "Physical Products", "chase-it")
    assert rec is None or rec["pdp"]["category_path"] != PRESS_ON
    if rec is not None:
        assert "placed_by_lash_nail_title" not in {f["rule"] for f in detectors.detect([rec])}

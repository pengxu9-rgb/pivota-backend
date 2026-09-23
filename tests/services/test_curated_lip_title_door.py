"""A lip product's own title, where the merchant type says nothing -- and nowhere else.

OPT-IN: the door is off unless a manual run passes --lip-title-evidence. The queue worker, the
brand-official lane and the repair planner share the resolver and must not change.

Measured 2026-09-23 on the UCP-ready Meitu retailers: k-touch.us (3CE), belleandblush.com
(BY TERRY) and openthebeauty.com (HERA) file lipsticks under a blank type, "Cosmetics", or a
comma tag list, so every lipstick at those stores landed `category_unresolved` and blocked its
cohort while the same stores' mascaras resolved. Every lip row was exactly an unresolved row.
"""
import itertools

import pytest

from scripts.onboard_curated_brands import LEFT_OUT_PDP_PREFIX, _select_by_category
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan


def resolve(title, ptype=None, flag="beauty"):
    return feed._resolve_category(product_type=ptype, title=title, flag_path=flag)


@pytest.fixture
def evidence():
    with feed.lip_title_evidence():
        yield


# Titles copied from the 2026-09-23 dry runs (k-touch.us, belleandblush.com, shoppalacebeauty.com).
@pytest.mark.parametrize("title,ptype,want", [
    ("3CE - Soft Matte Lipstick 3.5g - Warmish Move", None, "beauty/makeup/lip/lipstick"),
    ("3CE - Soft Matte Lipstick 3.5g", "", "beauty/makeup/lip/lipstick"),
    ("3CE - Mood Recipe Lip Color 3.5g", None, "beauty/makeup/lip/lipstick"),
    ("3CE - Velvet Lip Tint 4g", "Cosmetics", "beauty/makeup/lip/tint"),
    ("BY TERRY | Rouge Opulent Lipstick", "Cosmetics", "beauty/makeup/lip/lipstick"),
    ("BY TERRY | Hyaluronic Lip Liner", "Cosmetics", "beauty/makeup/lip/liner"),
    ("Velvet Lipstick", "Makeup", "beauty/makeup/lip/lipstick"),
    ("Velvet Lipstick", "HERA,Lip,Makeup,matte", "beauty/makeup/lip/lipstick"),
])
def test_an_explicit_lip_title_resolves_where_the_type_says_nothing(title, ptype, want, evidence):
    assert resolve(title, ptype) == (want, feed.CATEGORY_CONFIDENCE_EXPLICIT_TITLE)


@pytest.mark.parametrize("title,ptype", [
    ("3CE - Soft Matte Lipstick 3.5g - Warmish Move", None),
    ("3CE - Velvet Lip Tint 4g", "Cosmetics"),
    ("BY TERRY | Hyaluronic Lip Liner", "Cosmetics"),
    ("Velvet Lipstick", "HERA,Lip,Makeup,matte"),
])
def test_the_main_route_is_unchanged_when_nobody_opts_in(title, ptype):
    """Default OFF: the worker / brand-official / repair-planner callers never set the switch."""
    assert resolve(title, ptype) == ("beauty", feed.CATEGORY_CONFIDENCE_FEED_DEFAULT)
    rec = feed.shopify_product_to_record(
        {"title": title, "product_type": ptype, "handle": "h", "vendor": "3CE",
         "variants": [{"price": "20", "available": True}], "images": []},
        domain="k-touch.us", category_path="beauty")
    assert rec["pdp"]["category_resolution_status"] == "unresolved"
    assert feed.product_category_path(title=title, product_type=ptype, fallback="beauty") == "beauty"


def test_the_switch_is_scoped_to_its_block():
    assert resolve("Soft Matte Lipstick")[0] == "beauty"
    with feed.lip_title_evidence():
        assert resolve("Soft Matte Lipstick")[0] == "beauty/makeup/lip/lipstick"
    assert resolve("Soft Matte Lipstick")[0] == "beauty"


def test_the_cli_flag_enables_it_for_that_run_only(monkeypatch):
    import scripts.onboard_curated_brands as cli
    seen = []

    async def fake_run(args):
        seen.append(feed._LIP_TITLE_EVIDENCE.get())
        return 0
    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["--domain", "k-touch.us", "--lip-title-evidence"]) == 0
    assert cli.main(["--domain", "k-touch.us"]) == 0
    assert seen == [True, False]
    assert feed._LIP_TITLE_EVIDENCE.get() is False


@pytest.mark.parametrize("title,ptype", [
    # no lip pattern at all: marketing names stay unresolved
    ("BY TERRY | Rouge Expert Click Stick", "Cosmetics"),
    ("HERA Sensual Fitting Glow Tint", None),
    ("3CE - Bouncy Blur Balm 4.8g", None),
    # two patterns: PR #2158's ambiguity guard, and the Powder Kiss negatives
    ("HERA Sensual Powder Matte Lipstick", None),
    ("Powder Kiss Lipstick", None),
    ("BY TERRY | Baume de Rose Lip Oil Serum", "Cosmetics"),
    # multi-use
    ("Lip & Cheek Tint Stick", None),
    ("Lip and Cheek Lip Tint", None),
    ("Lip/Cheek Lip Tint", None),
    ("Eye and Lip Liner", None),
    # sets, kits, bundles
    ("Lipstick Special 3pcs Set", None),
    ("Lip Tint Kit", None),
    ("Lip Liner Trio", None),
    ("Lipstick Holiday Gift", None),
    ("Mini Lip Tint 5 ea", None),
    # tools and accessories for a lip product
    ("Lip Pencil Sharpener", None),
    ("Lip Tint Keychain", None),
    ("Lipstick Case", None),
    ("Lip Liner Applicator", None),
    # another body area
    ("Lip Balm for Hands", None),
    # "Lip Color" is a family word; with a form word it is that form, not a lipstick
    ("Glossy Lip Color", None),
    ("Lip Color Balm", None),
    ("Lip Color Crayon", None),
    # the merchant type names another class, or is a multi-use label
    ("Velvet Lipstick", "HERA,Face,Makeup,Powder"),
    # ...even when it also carries a lip tag, or names two classes (so the type itself resolved nothing)
    ("Velvet Lipstick", "HERA,Lip,Makeup,Powder,Primer"),
    ("Lip Tint", "Mascara Eyeliner"),
    ("Lip Tint", "Blush"),
    ("Lip Tint", "Lip & Cheek"),
    ("Lip Tint", "Lip/Cheek"),
    # a comma tag list that never names the lip area
    ("Velvet Lipstick", "HERA,Face,Makeup,Cushion"),
])
def test_everything_short_of_one_explicit_lip_product_stays_unresolved(title, ptype, evidence):
    path, confidence = resolve(title, ptype)
    assert not path.startswith("beauty/makeup/lip/"), (title, ptype, path)


def test_a_non_beauty_or_leaf_flag_is_never_second_guessed(evidence):
    assert resolve("Soft Matte Lipstick", None, "beauty/skincare/moisturize/cream")[0] == "beauty/skincare/moisturize/cream"
    assert resolve("Soft Matte Lipstick", None, "home/kitchen")[0] == "home/kitchen"


def test_a_merchant_type_leaf_keeps_its_own_confidence(evidence):
    # Resolved above the door: the door never sees it.
    assert resolve("Velvet Lipstick", "Lipstick") == ("beauty/makeup/lip/lipstick", feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE)


_TITLES = ["Soft Matte Lipstick", "Velvet Lip Tint", "Lip Liner", "Glow Gloss", "Lip Balm", "Lip Oil",
           "Powder Kiss Lipstick", "Rich Curling Mascara", "Hydro Reflecting Toner", "Lip & Cheek",
           "Wash Off Mask", "Hand Cream", "Lipstick Brush", "Lip Duo Gift Set", "Cushion Foundation"]
_TYPES = [None, "", "Cosmetics", "Makeup", "Lipstick", "Lip Care", "Lip & Cheek", "Mascara",
          "Cleansers", "wash off mask", "HERA,Face,Makeup,Cushion", "HERA,Lip,Makeup", "Blush"]
_FLAGS = ["beauty", "beauty/makeup", "beauty/skincare", "beauty/skincare/moisturize/cream"]
_HOSTS = [None, "ohlolly.com", "sokoglam.com"]


def test_structural_no_regression_only_unresolved_rows_change(monkeypatch):
    """Off: byte-identical to no door at all. On: every row that resolved, or was deliberately
    refused (""), before this door is unchanged."""
    from services.category_path_aliases import resolve as leaf_of
    grid = list(itertools.product(_TITLES, _TYPES, _FLAGS, _HOSTS))
    run = lambda: {g: feed._resolve_category(title=g[0], product_type=g[1], flag_path=g[2], domain=g[3]) for g in grid}
    off = run()
    with feed.lip_title_evidence():
        after = run()
    monkeypatch.setattr(feed, "_explicit_lip_title_leaf", lambda **_: None)
    before = run()
    assert off == before
    with feed.lip_title_evidence():
        assert run() == before  # the patched-out door: the switch alone changes nothing
    changed = [g for g in grid if after[g] != before[g]]
    assert changed, "the door never fired on the grid: the test is not exercising it"
    for g in changed:
        assert before[g][0] != "" and not leaf_of(before[g][0]), (g, before[g], after[g])
        assert after[g][0].startswith("beauty/makeup/lip/"), (g, after[g])


# --- the ingest script's category filter -----------------------------------------------------

def record(title, ptype, handle):
    return feed.shopify_product_to_record(
        {"id": abs(hash(handle)) % 10**9, "vendor": "3CE", "title": title, "handle": handle,
         "product_type": ptype, "body_html": "<p>x</p>", "images": [{"src": "https://cdn.example/i.jpg"}],
         "variants": [{"id": abs(hash(handle + "v")) % 10**12, "price": "20.00", "available": True, "sku": handle}]},
        domain="k-touch.us", category_path="beauty", brand_override="3CE", currency="USD",
        source_role="retailer", retailer_name="k-touch.us", emit_native_variants=True,
    )


def _cohort():
    return [
        record("3CE - Soft Matte Lipstick 3.5g", "", "soft-matte"),
        record("3CE - Slim Fix Waterproof Mascara", "Mascara", "slim-fix"),
        record("3CE - New Take Eyeshadow Palette", "", "new-take"),  # unresolved
    ]


def test_lip_pass_keeps_only_lip_rows_and_prints_every_row_it_left_out(capsys, evidence):
    kept = _select_by_category(_cohort(), prefix="beauty/makeup/lip")
    assert [r["pdp"]["category_path"] for r in kept] == ["beauty/makeup/lip/lipstick"]
    out = capsys.readouterr().out
    left = [line for line in out.splitlines() if line.startswith(LEFT_OUT_PDP_PREFIX)]
    assert len(left) == 2
    assert '"reason": "outside_category_filter"' in out and '"reason": "category_unresolved"' in out
    assert "3 -> 1 products (2 left out)" in out


def test_resolved_pass_unblocks_the_cohort_without_resolving_anything(evidence):
    records = _cohort()
    blocked = inspect_primary_plan(ingest_validated_jsonl(records))
    assert "category_unresolved" in blocked["reasons"]
    kept = _select_by_category(records, prefix=None)
    assert len(kept) == 2
    report = inspect_primary_plan(ingest_validated_jsonl(kept))
    assert "category_unresolved" not in report["reasons"]
    assert report["unresolved_category_count"] == 0


def test_prefix_is_a_path_boundary_not_a_substring(evidence):
    with pytest.raises(ValueError, match="kept none"):
        _select_by_category(_cohort(), prefix="beauty/makeup/li")


def test_a_filter_that_keeps_nothing_is_an_error():
    only_unresolved = [record("3CE - New Take Eyeshadow Palette", "", "new-take")]
    with pytest.raises(ValueError, match="kept none"):
        _select_by_category(only_unresolved, prefix=None)

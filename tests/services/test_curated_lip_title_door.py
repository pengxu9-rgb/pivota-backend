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

from scripts.onboard_curated_brands import CATEGORY_FILTER_MARKER, LEFT_OUT_PDP_PREFIX, _select_by_category
from scripts import curated_apply_gate as gate
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
    ("HERA Velvet Matte Lipstick", "HERA,Lip,Makeup,matte", "beauty/makeup/lip/lipstick"),
    ("Velvet Lipstick", "Lips", "beauty/makeup/lip/lipstick"),
    # thisisbeauty.us types every product "Misc"
    ("ILLAMASQUA Lipstick HOWL 0.14oz - Imperfect Box", "Misc", "beauty/makeup/lip/lipstick"),
    ("ILLAMASQUA Loaded Lip Polish FIZZ 0.05oz - New", "Other", None),
    # sizes and SPF are not joiners
    ("3CE - Soft Matte Lipstick 3.5g/0.12oz", None, "beauty/makeup/lip/lipstick"),
])
def test_an_explicit_lip_title_resolves_where_the_type_says_nothing(title, ptype, want, evidence):
    if want is None:  # a neutral type, but the title names no lip leaf ("Lip Polish") -> still unresolved
        assert not resolve(title, ptype)[0].startswith("beauty/makeup/lip/")
        return
    assert resolve(title, ptype) == (want, feed.CATEGORY_CONFIDENCE_LIP_TITLE)


@pytest.mark.parametrize("title,ptype", [
    ("3CE - Soft Matte Lipstick 3.5g - Warmish Move", None),
    ("3CE - Velvet Lip Tint 4g", "Cosmetics"),
    ("BY TERRY | Hyaluronic Lip Liner", "Cosmetics"),
    ("HERA Velvet Matte Lipstick", "HERA,Lip,Makeup,matte"),
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
    # --- adversarial review of PR #2257 (each of these was placed in a lip leaf by the first cut) ---
    # a merchant type no pattern reads is still the merchant saying what it is: allowlist, not denylist
    ("Lip Care Gummies", "Supplements"), ("Lipstick Pretend Play", "Toys"), ("Lipstick", "Sets"),
    ("Lipstick", "Gift Sets"), ("Lipstick", "Samples"), ("Lip Liner", "Kits"), ("Lip Liner", "Tools"),
    ("Lip Liner", "Brushes"), ("Lip Liner", "Eye"), ("Lip Liner", "Eyes"), ("Lip Balm", "Pet"),
    ("Lip Balm", "Masks"), ("Lip Gloss Tube", "Packaging"), ("Lipstick Rose", "Fragrance"),
    # generic, but generic for ANOTHER area
    ("Glossy Lip Oil", "Hair Care"), ("Lip Tint", "Skin Care"), ("Lip Liner", "Face Care"), ("Lip Oil", "HERA,Lip,Hair Care"),
    # a tag list with ANY tag the title does not say
    ("Lip Tint", "Lip,Cheek"), ("Lip Liner", "Eye,Lip"), ("Lip Liner", "Lips,Eyes"), ("Lipstick", "Makeup,Lips,Sets"),
    # another area named anywhere, not only beside "lip"
    ("Lip Tint & Cheek", None), ("Lip Tint + Cheek", None), ("Lipstick for Lips, Cheeks & Eyes", None),
    ("Lip Balm and Cuticle Balm", None), ("Lip Stain for Cheeks Too", None), ("Lip Liner Eye Pencil", None),
    ("Eye Lip Liner Pencil", None), ("Lip Tint & Cheek Balm", "Cosmetics"),
    # more set words
    ("Lip Combo - Nude", None), ("Lipstick Quad", None), ("Lipstick Palette", None), ("Lip Liner Wardrobe", None),
    ("Mini Lipstick 3 Pack", None),
    ("ILLAMASQUA The Antimatter Lipstick Vault 5 pieces - Imperfect Box", "Misc"), ("Lip Liner Twin Pack", None), ("Lipstick Collection", None), ("Lipstick x3", None),
    # not a lip product at all
    ("Lipstick Charm", None), ("Lipstick Earrings", None), ("Lipstick Candle", None), ("Lipstick Lighter", None),
    ("Lipstick Socks", None), ("Lipstick USB Drive", None), ("Lipstick Power Bank", None), ("Lip Gloss Keyring", None),
    ("Lip Gloss Phone Charm", None), ("Lip Balm Lanyard", None), ("Lipstick Plush Toy", None),
    ("Toy Lipstick for Girls", None), ("Lip Gloss Squishy Toy", None), ("Empty Lip Gloss Tubes", None),
    ("Lip Balm Tin (Empty)", None), ("Lip Gloss Base", None), ("Lip Balm Base Beeswax", None), ("Lipstick Mold", None),
    ("Lip Liner Stencil", None), ("Lip Balm Dispenser", None), ("Lip Balm Display Stand", None),
    ("Lip Balm for Dogs", None), ("Lipstick Sample Card", None), ("Lip Gloss Tester", None),
    # a multi-use joiner with no area word ("Liner" names no pattern on its own)
    ("Lipstick & Liner", None), ("Lip Tint + Liner", None),
    # an area only _NON_FACE_TITLE names
    ("Lip Balm Beard Care", None), ("Lash Lip Tint", None),
    # two LIP leaves at once: which one is not the door's to choose
    ("Lip Gloss Lip Oil", None), ("Lip Liner Lipstick", None),
    # a tag list of words the title says, none of them the lip area
    ("HERA Velvet Lipstick", "HERA,Makeup"),
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
    kept = _select_by_category(_cohort(), prefix="beauty/makeup/lip", domain="k-touch.us")
    assert [r["pdp"]["category_path"] for r in kept] == ["beauty/makeup/lip/lipstick"]
    out = capsys.readouterr().out
    left = [line for line in out.splitlines() if line.startswith(LEFT_OUT_PDP_PREFIX)]
    assert len(left) == 2
    assert '"reason": "outside_category_filter"' in out and '"reason": "category_unresolved"' in out
    assert "3 -> 1 products (2 left out)" in out
    report = [line for line in out.splitlines() if line.startswith(CATEGORY_FILTER_MARKER)]
    assert len(report) == 1 and '"left_out": 2' in report[0] and '"domain": "k-touch.us"' in report[0]


def test_a_record_without_a_pdp_is_left_for_the_plan_to_refuse(evidence):
    malformed = {"offers": []}
    kept = _select_by_category(_cohort() + [malformed], prefix=None)
    assert malformed in kept


def test_a_domain_that_keeps_nothing_is_named_in_the_error():
    only_unresolved = [record("3CE - New Take Eyeshadow Palette", "", "new-take")]
    with pytest.raises(ValueError, match="^k-touch.us: category filter"):
        _select_by_category(only_unresolved, prefix=None, domain="k-touch.us")


def test_the_gate_reads_the_same_marker_the_cli_prints():
    assert gate.FILTER_MARKER == CATEGORY_FILTER_MARKER


def test_the_gate_reports_what_a_filter_kept_out_without_failing_on_it():
    applied = ('primary ingestion: {"applied": {"primary_readiness": {"status": "complete", "products": '
               '[{"canonical_url": "https://k-touch.us/products/a", "product_key": "k"}]}}, '
               '"missing": {}, "status": "applied"}')
    log = "\n".join([
        CATEGORY_FILTER_MARKER + '{"domain": "k-touch.us", "filter": "beauty/makeup/lip", "kept": 3, "left_out": 21, "selected": 24}',
        applied, "JOB=oneoff-1 RC=0"])
    verdict = gate.evaluate_apply_log(log, domain="k-touch.us")
    assert verdict["ok"] is True
    assert verdict["category_filter"]["left_out"] == 21 and verdict["category_filter"]["kept"] == 3
    assert gate.evaluate_apply_log(applied + "\nJOB=oneoff-1 RC=0", domain="k-touch.us")["category_filter"] is None


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


def test_the_cli_runs_the_door_and_the_filter_together(monkeypatch, capsys):
    """End to end through main(): records are built INSIDE the run, as records_for_brand does, so
    the switch must be on there; and the filter must drop exactly the unresolved rows."""
    from unittest.mock import AsyncMock
    import scripts.onboard_curated_brands as cli

    async def fetch(**_):
        return feed.ShopifyProductBatch(_cohort(), scanned_products=3, pages=1)
    stub = AsyncMock(side_effect=fetch)
    stub.last_vendor_filter_report = None
    stub.last_brand_census = None
    stub.last_fold_report = None
    monkeypatch.setattr(cli, "records_for_brand", stub)
    argv = ["--domain", "k-touch.us", "--category", "beauty", "--brand", "3CE", "--only-vendor", "3CE",
            "--source-role", "retailer",
            "--emit-real-variants", "--plan-print-limit", "0"]

    assert cli.main(argv + ["--lip-title-evidence", "--only-category", "beauty/makeup/lip"]) == 0
    out = capsys.readouterr().out
    assert '"kept": 1' in out and '"left_out": 2' in out
    assert '"category_path": "beauty/makeup/lip/lipstick"' in out
    assert '"unresolved_category_count": 0' in out
    placed = [line for line in out.splitlines() if line.startswith("    lip title pdp ")]
    assert len(placed) == 1 and "Soft Matte Lipstick" in placed[0]

    # Without the switch the lipstick is unresolved again, so a lip-only pass keeps nothing:
    # main() reports the refusal and exits 2, the same as an --only-gtin that matches nothing.
    assert cli.main(argv + ["--only-category", "beauty/makeup/lip"]) == 2
    captured = capsys.readouterr()
    assert '"kept": 0' in captured.out
    assert "k-touch.us: category filter beauty/makeup/lip kept none" in captured.err

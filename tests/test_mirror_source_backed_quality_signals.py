"""The mirror ingest path must hand the quality scorer the signals it already has.

`scripts/mirror_external_seeds_to_catalog_products` was the only one of the three
`build_servable_quality_payload` call sites passing neither `category` nor
`raw_inci` nor `pdp_details_sections`. Every row it mirrored was therefore scored
with an empty ATTRIBUTES component and — whenever product_type was null, which is
the common case for crawled seeds — an empty BRAND_CATEGORY one, capping it at
4-of-7 (57.1) or 3-of-7 (42.9).

Measured on prod 2026-07-28: those two scores accounted for 3,456 of the 5,114
`low_quality` rows. The content was never missing — `pdp_content_depth`, which
reads the DB, passes the same rows the attributes component scored 0 on.

Two layers of test here, deliberately:

  * the behaviour tests below prove the signals, once passed, do lift the score;
  * `test_the_call_site_still_passes_the_signals` proves the ingest path actually
    passes them.

The second is not redundant. The defect was never "the helper is wrong" — it was
"the call site omits arguments the builder accepts", and every behaviour test here
would still pass with the call site reverted, because they call the builder
directly. The same lesson as PIVOTA-Agent#1833: fixing a builder does nothing if
the caller never reaches it. Driving the real mirror function needs a live DB, so
the call site is pinned structurally instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.mirror_external_seeds_to_catalog_products import (  # noqa: E402
    _extract_source_backed_signals,
)
from services.external_seed_servability import (  # noqa: E402
    build_servable_quality_payload,
)
from services.product_quality_service import preview_quality  # noqa: E402

SECTIONS = [
    {"title": "How to use", "body": "Apply morning and night to cleansed skin."},
    {"title": "Ingredients", "body": "Water, Glycerin, Centella Asiatica Extract."},
]
INCI = "Water, Glycerin, Centella Asiatica Extract, Niacinamide"

# Shaped like a real mirror row: crawled seeds routinely have a null product_type
# and carry their content under seed_data.
MIRROR_ROW = dict(
    title="Centella Calming Gel Cream",
    description="A soothing gel cream with centella for sensitive, dehydrated skin.",
    price=24.0,
    image_url="https://example.com/p.jpg",
    brand="ExampleBeauty",
    product_type=None,
)


def _payload(seed_data, *, category="skincare", with_fix=True):
    extra = _extract_source_backed_signals(seed_data) if with_fix else {}
    return build_servable_quality_payload(
        **MIRROR_ROW,
        category=category if with_fix else None,
        **extra,
    )


def _score(seed_data, **kw):
    # score_source_backed_components is passed explicitly, as the sibling test
    # test_servable_quality_payload_sections does: the source-backed components
    # are env-flag gated in production, and a test that reads ambient env passes
    # for the wrong reason when the flag is unset.
    return preview_quality(
        _payload(seed_data, **kw), score_source_backed_components=True
    )["content_quality_score"]


# --- extraction ------------------------------------------------------------

def test_reads_raw_ingredients_string_and_sections():
    out = _extract_source_backed_signals(
        {"pdp_ingredients_raw": INCI, "pdp_details_sections": SECTIONS}
    )
    assert out["raw_inci"] == INCI
    assert out["pdp_details_sections"] == SECTIONS


def test_falls_back_to_inci_list_joined_for_the_builder_to_resplit():
    out = _extract_source_backed_signals({"inci_list": ["Water", " Glycerin ", ""]})
    assert out["raw_inci"] == "Water, Glycerin"
    assert "pdp_details_sections" not in out


def test_reads_from_the_snapshot_nesting_too():
    # Matches the COALESCE in backfill_external_seed_quality_rescore.FETCH.
    out = _extract_source_backed_signals(
        {"snapshot": {"pdp_ingredients_raw": INCI, "pdp_details_sections": SECTIONS}}
    )
    assert out["raw_inci"] == INCI
    assert out["pdp_details_sections"] == SECTIONS


def test_sections_arriving_as_a_json_string_are_decoded():
    import json

    out = _extract_source_backed_signals(
        {"pdp_details_sections": json.dumps(SECTIONS)}
    )
    assert out["pdp_details_sections"] == SECTIONS


def test_absent_malformed_or_empty_yields_nothing():
    # Must answer "no" as cleanly as it answers "yes" — otherwise the payload
    # gains an empty seed_data key and the assertions below prove nothing.
    for seed_data in (None, {}, "not-a-dict", [], {"inci_list": []},
                      {"pdp_details_sections": "{{not json"},
                      {"pdp_details_sections": []},
                      {"pdp_ingredients_raw": "   "}):
        assert _extract_source_backed_signals(seed_data) == {}


# --- the effect the defect was actually about ------------------------------

def test_signals_reach_the_scorer_as_seed_data():
    payload = _payload({"pdp_ingredients_raw": INCI, "pdp_details_sections": SECTIONS})
    assert payload["seed_data"]["inci_list"]
    assert payload["seed_data"]["pdp_details_sections"] == SECTIONS
    # category must reach global_category_id via the product_type fallback,
    # which is the whole reason brand_category scored 0 on null-product_type rows.
    assert payload.get("global_category_id")


def test_the_attributes_lift_is_isolated_from_the_category_lift():
    """Pin the INCI/sections half on its own.

    Necessary because `category` alone already clears the bar (see the test
    below it): an assertion that merely compares "fix" to "no fix" is satisfied
    by category and would still pass if the source-backed signals were dropped
    again. Hold category constant and vary only seed_data.
    """
    no_signals = _score({})
    with_inci = _score({"pdp_ingredients_raw": INCI})
    with_both = _score({"pdp_ingredients_raw": INCI, "pdp_details_sections": SECTIONS})
    assert with_inci > no_signals, (
        f"INCI must raise the attributes component (got {no_signals} -> {with_inci})"
    )
    assert with_both >= with_inci


def test_a_content_bearing_row_clears_the_serving_bar_only_with_the_signals():
    from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD

    seed_data = {"pdp_ingredients_raw": INCI, "pdp_details_sections": SECTIONS}
    without = _score(seed_data, with_fix=False)
    with_fix = _score(seed_data)
    assert with_fix > without, (
        f"passing the source-backed signals must lift the score "
        f"(got {without} -> {with_fix})"
    )
    assert with_fix >= QUALITY_SCORE_THRESHOLD, (
        f"a row with real INCI, sections, description, image, price and category "
        f"must clear the serving bar; got {with_fix}"
    )


def test_category_alone_lifts_a_null_product_type_row_over_the_bar():
    """The cheaper half of the fix, pinned separately because it is the bigger one.

    A crawled seed with title + description + image + price + brand and a null
    product_type scores 4-of-7 = 57.1 without `category` (brand_category = 0) and
    5-of-7 = 71.4 with it — clearing the bar on content it already had, with no
    INCI involved at all. 57.1 was the single most common blocked score in prod
    (1,794 rows), so this line is doing most of the work.
    """
    from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD

    assert _score({}, with_fix=False) < QUALITY_SCORE_THRESHOLD
    assert _score({}) >= QUALITY_SCORE_THRESHOLD


def test_the_call_site_still_passes_the_signals():
    """The regression guard for the actual defect.

    Structural rather than behavioural: the mirror path that calls
    `build_servable_quality_payload` sits inside an async function that writes to
    catalog_products, external_product_seeds and agent_pdp_view, so exercising it
    for real needs a live Postgres. Asserting on the call node is the cheap way to
    make a silent reversion fail CI. If this test ever becomes awkward, replace it
    with a Postgres integration test — do not simply delete it, or the omission it
    guards becomes invisible again.
    """
    import ast

    src = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "mirror_external_seeds_to_catalog_products.py"
    ).read_text()

    calls = [
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_servable_quality_payload"
    ]
    assert len(calls) == 1, (
        f"expected exactly one build_servable_quality_payload call site, "
        f"found {len(calls)} — update this guard to cover all of them"
    )
    call = calls[0]

    named = {kw.arg for kw in call.keywords if kw.arg}
    assert "category" in named, (
        "the mirror call site must pass `category`; without it product_type is "
        "null on crawled seeds and the brand_category component scores 0"
    )

    unpacked = [
        kw.value
        for kw in call.keywords
        if kw.arg is None  # i.e. **something
    ]
    assert any(
        isinstance(v, ast.Call)
        and isinstance(v.func, ast.Name)
        and v.func.id == "_extract_source_backed_signals"
        for v in unpacked
    ), (
        "the mirror call site must expand _extract_source_backed_signals(...) so "
        "raw_inci / pdp_details_sections reach the ATTRIBUTES component"
    )


def test_a_genuinely_contentless_row_still_fails():
    # Guards against "the fix passes everything". Strip the real content away:
    # no description, no image, no price. Category and INCI must NOT rescue it.
    from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD

    payload = build_servable_quality_payload(
        title="Some Product",
        description=None,
        price=None,
        image_url=None,
        brand="ExampleBeauty",
        product_type=None,
        category="skincare",
        **_extract_source_backed_signals({"pdp_ingredients_raw": INCI}),
    )
    score = preview_quality(payload)["content_quality_score"]
    assert score < QUALITY_SCORE_THRESHOLD, (
        f"a row with no description, image or price must stay blocked; got {score}"
    )

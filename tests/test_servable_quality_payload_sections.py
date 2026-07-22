"""`build_servable_quality_payload` must carry crawled PDP detail sections through.

Both `score_source_backed_summary` and `source_backed_attribute_signal_count` read
`seed_data.pdp_details_sections`, but nothing used to pass them into the servable
payload — so an external-seed product whose crawl DID capture its detail sections
was scored as if it had none, forfeiting the summary and attribute components it
had already earned. Measured on 250 live rows carrying sections (2026-07-22): 98%
clear the 65 serving bar once passed, vs 50% without.
"""

from services.external_seed_servability import build_servable_quality_payload
from services.product_quality_service import preview_quality

SECTIONS = [
    {"title": "Description", "body": "A lightweight gel moisturizer for dehydrated skin."},
    {"title": "How to use", "body": "Apply morning and night to cleansed skin."},
    {"title": "Ingredients", "body": "Water, Glycerin, Centella Asiatica Extract."},
]

BASE = dict(
    title="Centella Calming Gel Cream",
    description="A soothing gel cream with centella for sensitive, dehydrated skin.",
    price=24.0,
    image_url="https://example.com/p.jpg",
    brand="ExampleBeauty",
    product_type=None,
    category="skincare",
)


def _score(payload):
    return preview_quality(payload, score_source_backed_components=True)["content_quality_score"]


def test_sections_are_carried_into_seed_data():
    payload = build_servable_quality_payload(**BASE, pdp_details_sections=SECTIONS)
    assert payload["seed_data"]["pdp_details_sections"] == SECTIONS


def test_sections_lift_the_quality_score():
    without = _score(build_servable_quality_payload(**BASE))
    with_sections = _score(build_servable_quality_payload(**BASE, pdp_details_sections=SECTIONS))
    assert with_sections > without, (
        f"passing detail sections must raise the score (got {without} -> {with_sections})"
    )


def test_sections_coexist_with_inci():
    payload = build_servable_quality_payload(
        **BASE, raw_inci="Water, Glycerin, Centella Asiatica Extract",
        pdp_details_sections=SECTIONS,
    )
    seed = payload["seed_data"]
    assert seed["pdp_details_sections"] == SECTIONS
    assert seed["inci_list"] == ["Water", "Glycerin", "Centella Asiatica Extract"]
    assert seed["pdp_ingredients_raw"]


def test_no_seed_data_key_when_nothing_to_attach():
    # Flag-off equivalence: a product with neither INCI nor sections must produce
    # exactly the payload shape it did before this change (no empty seed_data).
    payload = build_servable_quality_payload(**BASE)
    assert "seed_data" not in payload


def test_empty_or_malformed_sections_are_ignored():
    for value in ([], None, "not-a-list", {}):
        payload = build_servable_quality_payload(**BASE, pdp_details_sections=value)
        assert "seed_data" not in payload, f"{value!r} must not create seed_data"

"""P1 — assemble_row E2 publish bridge: brand-attested title_override +
description_markdown reach the SERVED agent_pdp_view row (pure, no DB).

Closes the review's Finding A for the two fields the assembler serves: an
attested title now overrides the storefront title (it previously only did so for
description).
"""

from services.agent_pdp_view_assembler import assemble_row


def _products(title="Storefront Title 50ml"):
    return [
        {
            "title": title,
            "description": "raw storefront description",
            "product_key": "m1|url_audit|x",
        }
    ]


def test_attested_title_and_description_override_storefront():
    row = assemble_row(
        content_key="ck_x",
        products=_products(),
        skus=[],
        offers=[],
        external_seed=None,
        enrichment={
            "title_override": "Anua Heartleaf 77% Soothing Toner",
            "description_markdown": "Brand-attested soothing toner copy.",
        },
    )
    assert row is not None
    # brand-attested copy reaches the SERVED row (E2 bridge), outranking storefront
    assert "Anua Heartleaf" in row["title"]
    assert row["description"] == "Brand-attested soothing toner copy."


def test_no_enrichment_falls_back_to_storefront():
    row = assemble_row(
        content_key="ck_x",
        products=_products(title="Storefront Title 50ml"),
        skus=[],
        offers=[],
        external_seed=None,
        enrichment=None,
    )
    assert row is not None
    assert "Storefront Title" in row["title"]
    assert row["description"] == "raw storefront description"


def test_empty_title_override_does_not_blank_title():
    row = assemble_row(
        content_key="ck_x",
        products=_products(title="Storefront Title 50ml"),
        skus=[],
        offers=[],
        external_seed=None,
        enrichment={"title_override": "   "},  # strip-aware: falls through
    )
    assert row is not None
    assert "Storefront Title" in row["title"]

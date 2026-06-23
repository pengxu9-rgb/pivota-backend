"""P1 — assemble_row E2 publish bridge: brand-attested content reaches the SERVED
agent_pdp_view row (pure, no DB).

Covers title_override + description_markdown (Finding A) AND the richer fields
bullet_points + usage_scenarios, including the write->serve round trip
(row_to_upsert_params JSONB-encode -> agent_pdp_v1 _row_to_dict decode).
"""

from routes.agent_pdp_v1 import _row_to_dict
from services.agent_pdp_view_assembler import assemble_row, row_to_upsert_params


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


def test_attested_bullets_and_usage_reach_served_row():
    row = assemble_row(
        content_key="ck_x",
        products=_products(),
        skus=[],
        offers=[],
        external_seed=None,
        enrichment={
            "bullet_points": ["77% heartleaf extract", "pH 5.5"],
            "usage_scenarios": ["Apply AM/PM after cleansing"],
        },
    )
    assert row is not None
    assert row["bullet_points"] == ["77% heartleaf extract", "pH 5.5"]
    assert row["usage_scenarios"] == ["Apply AM/PM after cleansing"]


def test_bullets_and_usage_survive_write_then_serve_roundtrip():
    # The riskiest path: the assembled row is JSONB-encoded for the upsert bind,
    # then the agent_pdp_v1 read path decodes it back. Prove the new fields make
    # the full round trip (encode -> decode) as lists.
    row = assemble_row(
        content_key="ck_x",
        products=_products(),
        skus=[],
        offers=[],
        external_seed=None,
        enrichment={"bullet_points": ["a", "b"], "usage_scenarios": ["u1"]},
    )
    params = row_to_upsert_params(row)
    assert isinstance(params["bullet_points"], str)  # JSONB-encoded for the bind
    assert isinstance(params["usage_scenarios"], str)
    served = _row_to_dict(params)  # agent_pdp_v1 decode
    assert served["bullet_points"] == ["a", "b"]
    assert served["usage_scenarios"] == ["u1"]


def test_no_enrichment_leaves_bullets_and_usage_absent():
    row = assemble_row(
        content_key="ck_x",
        products=_products(),
        skus=[],
        offers=[],
        external_seed=None,
        enrichment=None,
    )
    assert row is not None
    assert row["bullet_points"] is None
    assert row["usage_scenarios"] is None

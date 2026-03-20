from readiness.models import CapabilityStatus, ReadyProduct, ReadyVariant
from services.product_exposure_service import (
    AGENT_PUSH_STATUS_ELIGIBLE,
    AGENT_PUSH_STATUS_EXCLUDED,
    build_agent_push_projection_from_ready_product,
    build_agent_push_projection_from_standard_product,
    summarize_agent_push_projections,
)


def test_standard_product_projection_excludes_only_unsellable_variants() -> None:
    projection = build_agent_push_projection_from_standard_product(
        {
            "id": "prod_1",
            "platform": "shopify",
            "merchant_id": "merch_test",
            "title": "Glow Serum",
            "currency": "USD",
            "variants": [
                {"id": "var_live", "price": 28.0, "currency": "USD", "inventory_quantity": 12},
                {"id": "var_oos", "price": 28.0, "currency": "USD", "inventory_quantity": 0},
                {"id": "var_noprice", "price": None, "currency": "USD", "inventory_quantity": 6},
            ],
        },
        checked_at="2026-03-20T00:00:00Z",
    )

    assert projection["agent_push_status"] == AGENT_PUSH_STATUS_ELIGIBLE
    assert projection["eligible_variant_count"] == 1
    assert projection["excluded_variant_count"] == 2
    assert "out_of_stock" in projection["agent_push_reason_codes"]
    assert "missing_price" in projection["agent_push_reason_codes"]


def test_ready_product_projection_marks_fully_unsellable_product_excluded() -> None:
    product = ReadyProduct(
        product_id="prod_2",
        platform="shopify",
        title="Night Cream",
        variants=[
            ReadyVariant(
                variant_id="var_1",
                title="Default",
                price={"amount": None, "currency": "USD"},
                inventory={"quantity": 0, "availability": "out_of_stock"},
                freshness={},
                provenance=[],
                source_of_truth={},
                blockers={"discovery": [], "checkout": ["out_of_stock", "missing_price"]},
                warnings={"discovery": [], "checkout": []},
                discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                checkout=CapabilityStatus(capability="checkout", status="blocked", score=20),
                channel_coverage={"ucp": "blocked"},
            )
        ],
    )

    projection = build_agent_push_projection_from_ready_product(
        product,
        checked_at="2026-03-20T00:00:00Z",
    )

    assert projection["agent_push_status"] == AGENT_PUSH_STATUS_EXCLUDED
    assert projection["eligible_variant_count"] == 0
    assert projection["excluded_variant_count"] == 1
    assert set(projection["agent_push_reason_codes"]) >= {"out_of_stock", "missing_price"}


def test_summarize_agent_push_projections_tracks_counts() -> None:
    summary = summarize_agent_push_projections(
        [
            {
                "agent_push_status": AGENT_PUSH_STATUS_ELIGIBLE,
                "agent_push_reason_codes": [],
                "eligible_variant_count": 2,
                "excluded_variant_count": 1,
                "store_data_last_checked_at": "2026-03-20T00:00:00Z",
            },
            {
                "agent_push_status": AGENT_PUSH_STATUS_EXCLUDED,
                "agent_push_reason_codes": ["out_of_stock", "missing_price"],
                "eligible_variant_count": 0,
                "excluded_variant_count": 2,
                "store_data_last_checked_at": "2026-03-20T01:00:00Z",
            },
        ],
        active_blocked_variants=3,
    )

    assert summary["total_products"] == 2
    assert summary["eligible_products"] == 1
    assert summary["excluded_products"] == 1
    assert summary["eligible_variants"] == 2
    assert summary["excluded_variants"] == 3
    assert summary["active_blocked_variants"] == 3
    assert summary["top_reason_codes"][0]["code"] in {"out_of_stock", "missing_price"}

from services.product_quality_service import (
    QUALITY_SOURCE_PREVIEW,
    QUALITY_SOURCE_SNAPSHOT,
    build_quality_payload_from_cache_row,
    summarize_quality_coverage,
)


def test_build_quality_payload_from_cache_row_prefers_existing_enrichment() -> None:
    payload = build_quality_payload_from_cache_row(
        {
            "product_data": {
                "title": "Glow Serum",
                "description": "Hydrating serum for daily use.",
                "price": 29.0,
                "currency": "USD",
                "vendor": "Pivota Beauty",
                "product_type": "Serum",
                "image_url": "https://example.com/serum.jpg",
            }
        },
        {
            "title_override": "Glow Serum +",
            "summary_short": "Daily hydration for dry skin.",
            "bullet_points": ["Hydrating", "Daily use", "Fast absorbing"],
        },
    )

    assert payload is not None
    assert payload["title_local"] == "Glow Serum +"
    assert payload["summary_short"] == "Daily hydration for dry skin."
    assert payload["bullet_points"] == ["Hydrating", "Daily use", "Fast absorbing"]
    assert payload["brand"] == "Pivota Beauty"
    assert payload["global_category_id"] == "Serum"


def test_summarize_quality_coverage_tracks_snapshot_preview_and_unscored() -> None:
    summary = summarize_quality_coverage(
        [("shopify", "1"), ("shopify", "2"), ("shopify", "3")],
        projections_by_key={
            ("shopify", "1"): {
                "content_quality_score": 81.0,
                "model_readiness_score": 72.0,
                "conversion_potential_score": None,
                "last_evaluated_at": "2026-03-19T00:00:00Z",
                "quality_source": QUALITY_SOURCE_SNAPSHOT,
            },
            ("shopify", "2"): {
                "content_quality_score": 66.0,
                "model_readiness_score": 58.0,
                "conversion_potential_score": None,
                "last_evaluated_at": None,
                "quality_source": QUALITY_SOURCE_PREVIEW,
            },
            ("shopify", "3"): {
                "content_quality_score": None,
                "model_readiness_score": None,
                "conversion_potential_score": None,
                "last_evaluated_at": None,
                "quality_source": "none",
            },
        },
        snapshot_rows_by_key={
            ("shopify", "1"): {
                "content_quality_score": 81.0,
                "model_readiness_score": 72.0,
                "conversion_potential_score": None,
                "snapshot_date": "2026-03-19T00:00:00Z",
            }
        },
        active_backfill_job={"job_id": "qbf_123", "status": "running"},
    )

    assert summary["total_products"] == 3
    assert summary["snapshot_scored_products"] == 1
    assert summary["effective_scored_products"] == 2
    assert summary["preview_only_products"] == 1
    assert summary["unscored_products"] == 1
    assert summary["coverage_state"] == "partial"
    assert summary["latest_snapshot_at"] == "2026-03-19T00:00:00Z"
    assert summary["backfill_recommended"] is True
    assert summary["active_backfill_job"]["job_id"] == "qbf_123"

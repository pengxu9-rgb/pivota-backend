from __future__ import annotations

from datetime import datetime, timezone

from readiness.source_of_truth import build_field_family_status


def test_build_field_family_status_marks_stale_and_blocked():
    status, freshness, provenance = build_field_family_status(
        family="inventory",
        source="shopify_cache.inventory.v1",
        fallback_source="shopify_admin.inventory.v2025-10",
        observed_at="2026-03-15T00:00:00Z",
        reference_time=datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc),
        blockers=["out_of_stock"],
        warnings=[],
        notes=["inventory fallback in use"],
    )

    assert status.family == "inventory"
    assert status.status == "blocked"
    assert status.stale is True
    assert "out_of_stock" in status.blockers
    assert freshness.stale is True
    assert provenance.fallback_source == "shopify_admin.inventory.v2025-10"


def test_build_field_family_status_ready_when_fresh():
    status, freshness, provenance = build_field_family_status(
        family="price",
        source="shopify_cache.variant_offer.v1",
        observed_at="2026-03-17T11:30:00Z",
        reference_time=datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc),
        blockers=[],
        warnings=[],
        notes=["fresh cached price"],
    )

    assert status.status == "ready"
    assert status.stale is False
    assert freshness.stale is False
    assert provenance.source == "shopify_cache.variant_offer.v1"


def test_build_field_family_status_supports_reviews_confidence():
    status, freshness, provenance = build_field_family_status(
        family="reviews_confidence",
        source="reviews_center.review_group.v1",
        observed_at="2026-03-16T12:00:00Z",
        reference_time=datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc),
        blockers=[],
        warnings=[],
        notes=["reviews center aggregate"],
    )

    assert status.status == "ready"
    assert status.stale is False
    assert freshness.source == "reviews_center.review_group.v1"
    assert provenance.source_of_truth is True

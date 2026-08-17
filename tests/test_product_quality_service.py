import pytest

from services import product_quality_service as svc
from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD
from services.product_quality_service import (
    QUALITY_SOURCE_PREVIEW,
    QUALITY_SOURCE_SNAPSHOT,
    SOURCE_BACKED_COMPONENTS_RULES_VERSION,
    build_quality_payload_from_cache_row,
    preview_quality,
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


def _source_backed_quality_payload(**overrides):
    payload = {
        "title_local": "Barrier Serum",
        "description_local": (
            "A source-backed serum description with enough detail to clear the "
            "full deterministic description component."
        ),
        "main_image_url": "https://example.test/serum.jpg",
        "image_list": ["https://example.test/serum.jpg"],
        "brand": "Pivota Test",
        "global_category_id": "Serum",
        "price_local_value": 24.0,
        "seed_data": {
            "summary": (
                "A concise source-backed product summary that is long enough "
                "to score as a complete summary component."
            ),
            "pdp_details_sections": [
                {"heading": "Benefits", "content": "Hydrates and supports the skin barrier."}
            ],
            "pdp_how_to_use_raw": "Apply after cleansing.",
            "ingredient_intel": {"inci_list": ["water"]},
        },
    }
    payload.update(overrides)
    return payload


def _component_score(result, name):
    return next(item["score"] for item in result["components"] if item["name"] == name)


def test_source_backed_components_remain_zero_by_default() -> None:
    result = preview_quality(_source_backed_quality_payload())

    # 83.3 = 5 of 6 components (the 7th, `summary`, was removed 2026-07-28 —
    # it scored 0.0 for 100% of prod rows, so it was a flat 14.3-point penalty
    # rather than a signal). `summary` is therefore no longer a scored component.
    assert result["content_quality_score"] == 83.3
    assert not any(c["name"] == "summary" for c in result["components"])
    assert _component_score(result, "attributes") == 0.0
    assert "source_backed_fields" not in result


def test_scores_source_backed_summary_and_attributes_when_enabled() -> None:
    result = preview_quality(
        _source_backed_quality_payload(),
        score_source_backed_components=True,
    )

    assert result["content_quality_score"] == 100.0
    # `summary` is still COMPUTED (it feeds model_readiness_score) but is no
    # longer one of the scored components — see product_quality_service.
    assert not any(c["name"] == "summary" for c in result["components"])
    assert _component_score(result, "attributes") == 100.0
    assert result["source_backed_fields"] == {
        "optional_components_enabled": True,
        "summary_length": 101,
        "attribute_signal_count": 3,
        # This payload supplies a real source document, so a snapshot written
        # from it was genuinely evaluated under the source-backed rules and the
        # rescore's resumability filter may skip it.
        "source_roots_present": True,
    }
    assert {item["code"] for item in result["problems"]} == {"insufficient_bullets"}


def test_source_backed_attribute_signal_can_clear_quality_threshold() -> None:
    payload = _source_backed_quality_payload(
        description_local="",
        seed_data={
            "pdp_details_sections": [
                {"heading": "Texture", "content": "Lightweight gel serum."}
            ],
        },
    )

    historical = preview_quality(payload, score_source_backed_components=False)
    lifted = preview_quality(payload, score_source_backed_components=True)

    # 6-component scale (summary dropped 2026-07-28). The test's point is
    # sharper than before: without the source-backed signal this row sits at
    # 4-of-6 = 66.7, BELOW the 71.4 floor; the attributes lift carries it over.
    assert historical["content_quality_score"] == 66.7
    assert lifted["content_quality_score"] == 78.3
    assert _component_score(lifted, "attributes") == 70.0
    assert historical["content_quality_score"] < QUALITY_SCORE_THRESHOLD
    assert lifted["content_quality_score"] >= QUALITY_SCORE_THRESHOLD


@pytest.mark.asyncio
async def test_full_quality_eval_bumps_rules_version_for_source_backed_scoring(monkeypatch) -> None:
    captured = {}

    async def fake_execute(query, *_args, **_kwargs):
        captured["params"] = query.compile().params
        return None

    async def fake_fetch_one(*_args, **_kwargs):
        return None

    monkeypatch.setattr(svc.database, "execute", fake_execute)
    monkeypatch.setattr(svc.database, "fetch_one", fake_fetch_one)

    result = await svc.full_quality_eval(
        merchant_id="merchant",
        platform="shopify",
        platform_product_id="prod_1",
        geo_code="default",
        payload=_source_backed_quality_payload(),
        score_source_backed_components=True,
    )

    assert result["content_quality_score"] == 100.0
    assert captured["params"]["rules_version"] == SOURCE_BACKED_COMPONENTS_RULES_VERSION

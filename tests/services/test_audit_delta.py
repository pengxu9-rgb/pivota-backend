from __future__ import annotations

from services.audit_delta import build_reaudit_delta


def _report(
    *,
    visibility: int = 60,
    attribution: int = 45,
    category_visibility: int = 55,
    primary_gap: str = "retailer_route_leak",
    controller_strategy: str = "leading_retailer_competition",
    top_controller: str = "walmart.com",
    verdict: str = "VISIBLE VIA RETAILERS",
    tracking_metrics: list[str] | None = None,
) -> dict:
    return {
        "verdict": {
            "label": verdict,
            "visibility_score": visibility,
            "attribution_score": attribution,
            "category_visibility_score": category_visibility,
        },
        "merchant_view": {
            "headline": {
                "verdict_label": verdict,
                "scores": {
                    "visibility": visibility,
                    "attribution": attribution,
                    "category_visibility": category_visibility,
                },
            },
            "receipts": {
                "top_cited_hosts": [top_controller],
            },
            "next_best_action": {
                "primary_gap": primary_gap,
                "tracking_metrics": tracking_metrics or [
                    "First-party citation rate on the same failed buyer questions.",
                    "Share of cited buying paths going to the merchant-owned page versus walmart.com.",
                    "Checkout starts or orders from the owned agent-ready path once instrumented.",
                ],
                "canonical_page_play": {
                    "controllers": [top_controller, "target.com"],
                    "controller_profile": {"strategy": controller_strategy},
                },
            },
        },
    }


def _movement(delta: dict, signal: str) -> dict:
    return next(m for m in delta["movements"] if m["signal"] == signal)


def _metric(delta: dict, prefix: str) -> dict:
    return next(
        row for row in delta["tracked_metric_results"]
        if row["metric"].startswith(prefix)
    )


def test_probe_noise_does_not_create_material_movement():
    prior = _report(visibility=60, attribution=45, category_visibility=55)
    current = _report(visibility=68, attribution=52, category_visibility=60)

    delta = build_reaudit_delta(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=14,
    )

    assert delta["is_first_audit"] is False
    assert delta["headline"].startswith("No material change since your last audit 14 days ago")
    assert all(
        m["direction"] not in {"improved", "regressed", "changed"}
        for m in delta["movements"]
    )
    assert all(not m["is_material"] for m in delta["movements"])


def test_band_boundary_jitter_is_not_material():
    """A tiny score move that merely straddles a band boundary (e.g. 38->41
    across the 40 blocked/partial line) is probe noise, not improvement. The
    band must describe a move, never trigger materiality on its own."""
    prior = _report(attribution=38)
    current = _report(attribution=41)

    delta = build_reaudit_delta(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=30,
    )

    movement = _movement(delta, "attribution")
    assert movement["from"] == 38
    assert movement["to"] == 41
    assert movement["direction"] == "stable"
    assert movement["is_material"] is False
    assert delta["headline"].startswith("No material change")


def test_within_band_real_improvement_is_material():
    """A genuine >=15 move within a single band (50->66, both 'partial') is
    material even though no band boundary is crossed."""
    prior = _report(attribution=50)
    current = _report(attribution=66)

    delta = build_reaudit_delta(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=30,
    )

    movement = _movement(delta, "attribution")
    assert movement["direction"] == "improved"
    assert movement["is_material"] is True


def test_band_crossing_score_improvement_is_material():
    prior = _report(attribution=35)
    current = _report(attribution=55)

    delta = build_reaudit_delta(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=30,
    )

    movement = _movement(delta, "attribution")
    assert movement["from"] == 35
    assert movement["to"] == 55
    assert movement["direction"] == "improved"
    assert movement["is_material"] is True
    assert "improved: First-party citation" in delta["headline"]


def test_controller_archetype_flip_is_neutral_material_change():
    prior = _report(controller_strategy="leading_retailer_competition")
    current = _report(controller_strategy="source_authority_gap")

    delta = build_reaudit_delta(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=21,
    )

    movement = _movement(delta, "controller_archetype")
    assert movement["direction"] == "changed"
    assert movement["is_material"] is True
    assert "changed: Controller archetype" in delta["headline"]
    assert "improved" not in delta["headline"]


def test_first_audit_establishes_baseline():
    delta = build_reaudit_delta(
        current_report=_report(),
        prior_report=None,
        prior_row=None,
        days_since=None,
    )

    assert delta == {
        "is_first_audit": True,
        "days_since_last": None,
        "headline": "Baseline established — re-audit in ~30 days to see movement.",
        "movements": [],
        # W2 pinned basis: a first audit declares itself the measurement
        # baseline (no prompt_basis in the legacy _report() fixture → id None).
        "measurement_basis": {
            "same": None,
            "prompt_set_id": None,
            "note": "This run establishes the measurement basis for future comparisons.",
        },
        "tracked_metric_results": [
            {
                "metric": "First-party citation rate on the same failed buyer questions.",
                "status": "not_measurable",
                "note": "No prior audit exists yet; this run is the baseline.",
            },
            {
                "metric": "Share of cited buying paths going to the merchant-owned page versus walmart.com.",
                "status": "not_measurable",
                "note": "No prior audit exists yet; this run is the baseline.",
            },
            {
                "metric": "Checkout starts or orders from the owned agent-ready path once instrumented.",
                "status": "not_measurable",
                "note": "No prior audit exists yet; this run is the baseline.",
            },
        ],
    }


def test_tracked_metric_results_map_only_stored_signals():
    prior = _report(attribution=35, top_controller="walmart.com")
    current = _report(attribution=55, top_controller="target.com")

    delta = build_reaudit_delta(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=30,
    )

    assert _metric(delta, "First-party citation")["status"] == "moved"
    assert _metric(delta, "Share of cited buying paths")["status"] == "moved"
    checkout = _metric(delta, "Checkout starts")
    assert checkout["status"] == "not_measurable"
    assert "not stored" in checkout["note"]


def test_full_brand_report_shape_uses_first_per_product_report():
    prior = {"per_product": [_report(attribution=35)]}
    current = {"per_product": [_report(attribution=55)]}

    delta = build_reaudit_delta(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=10,
    )

    assert _movement(delta, "attribution")["direction"] == "improved"

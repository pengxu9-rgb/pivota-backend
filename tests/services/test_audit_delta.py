from __future__ import annotations

from services.audit_delta import build_reaudit_delta, measurement_basis_between as _measurement_basis_between


def _basis():
    return {"methodology_version": "2", "providers_and_models": {"gemini": {"model_id": "g"}},
            "primary_destination_version": 1, "prompt_set_id": "p", "selected_set_id": "s",
            "official_domains": ["brand.com"], "tier_mix": {"category": 10}, "market": "US", "language": "en"}


def compare_pinned_runs(**kwargs):
    # Numerical movement tests have a full recorded basis; missing-basis
    # behavior is covered independently in test_audit_delta_basis.
    return build_reaudit_delta(**kwargs, current_basis=_basis(), prior_basis=_basis())


def measurement_basis_between(current, prior):
    return _measurement_basis_between(current, prior, _basis(), _basis())



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
    prompt_basis: dict | None = None,
) -> dict:
    report = {
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
    report["prompt_basis"] = {"selected_set_id": "default"} if prompt_basis is None else prompt_basis
    return report


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

    delta = compare_pinned_runs(
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

    delta = compare_pinned_runs(
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

    delta = compare_pinned_runs(
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

    delta = compare_pinned_runs(
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


def test_same_basis_does_not_buy_a_threshold_below_the_measured_floor():
    """The regression. This test used to assert the opposite — that an 8-point
    move on a pinned basis IS material, because MATERIAL_SCORE_DELTA_SAME_BASIS
    was 5.

    8 points is inside the measured noise floor. Three replicate runs at
    temperature 0 over the SAME queries spread 63.6 / 52.3 / 53.5 — 11.3 points
    from noise alone, 95% CI ±8.5, smallest resolvable change ~11.9
    (docs/revenue-recovery-joy-spike-2026-08-31.md §4). That study WAS the
    pinned-basis condition: identical queries, temperature 0. Pinning removes
    the variance from which questions got asked; it does not touch the response
    variance that produced the spread.

    So the tightening claimed detection at under half our smallest detectable
    change, and claimed it specifically on the runs we tell the merchant are
    the most comparable.
    """
    from services.audit_delta import (
        MATERIAL_SCORE_DELTA, MATERIAL_SCORE_DELTA_SAME_BASIS,
    )

    assert MATERIAL_SCORE_DELTA_SAME_BASIS >= 12, (
        "the same-basis threshold is back inside the ±8.5-14.6 measured floor"
    )
    assert MATERIAL_SCORE_DELTA_SAME_BASIS == MATERIAL_SCORE_DELTA

    pinned = {"selected_set_id": "sel_abc"}
    delta = compare_pinned_runs(
        current_report=_report(attribution=53, prompt_basis=pinned),
        prior_report=_report(attribution=45, prompt_basis=pinned),
        prior_row={"run_id": "prior"},
        days_since=14,
    )

    # Same basis is still stamped — it licenses "the questions did not change",
    # which is a real and useful thing to say. It just does not license calling
    # an 8-point move.
    assert delta["measurement_basis"]["same"] is True
    movement = _movement(delta, "attribution")
    assert movement["from"] == 45 and movement["to"] == 53
    assert movement["is_material"] is False


def test_a_sub_threshold_move_is_reported_as_undetectable_not_as_stable():
    """`direction: "stable"` is a positive claim — "this did not move". For a
    12-point drop that is false; what is true is that the move is smaller than
    this basis can resolve. The distinction is the whole point of the floor."""
    pinned = {"selected_set_id": "sel_abc"}
    delta = compare_pinned_runs(
        current_report=_report(attribution=40, prompt_basis=pinned),
        prior_report=_report(attribution=52, prompt_basis=pinned),
        prior_row={"run_id": "prior"},
        days_since=14,
    )

    detection = _movement(delta, "attribution")["detection"]
    assert detection["observed_delta"] == 12
    assert detection["threshold"] == 15
    assert detection["verdict"] == "below_detection_floor"
    assert "not evidence of stability" in detection["basis_note"]


def test_a_move_clear_of_the_floor_is_reported_as_resolved():
    """The positive counterpart: the floor must not swallow everything."""
    pinned = {"selected_set_id": "sel_abc"}
    delta = compare_pinned_runs(
        current_report=_report(attribution=70, prompt_basis=pinned),
        prior_report=_report(attribution=45, prompt_basis=pinned),
        prior_row={"run_id": "prior"},
        days_since=14,
    )

    movement = _movement(delta, "attribution")
    assert movement["is_material"] is True
    assert movement["direction"] == "improved"
    assert movement["detection"]["verdict"] == "resolved"
    assert movement["detection"]["observed_delta"] == 25


def test_a_signal_missing_on_one_side_is_not_called_stable():
    """No prior value is not a zero move."""
    pinned = {"selected_set_id": "sel_abc"}
    delta = compare_pinned_runs(
        current_report=_report(attribution=45, prompt_basis=pinned),
        prior_report=_report(attribution=None, prompt_basis=pinned),
        prior_row={"run_id": "prior"},
        days_since=14,
    )

    detection = _movement(delta, "attribution")["detection"]
    assert detection["observed_delta"] is None
    assert detection["verdict"] == "not_comparable"


def test_different_basis_keeps_loose_threshold():
    """W2: when the prompt set changed between runs, the same 8-point move is NOT
    material — it could be the new questions, not real movement. Loose 15 holds."""
    prior = _report(attribution=45, prompt_basis={"selected_set_id": "sel_old"})
    current = _report(attribution=53, prompt_basis={"selected_set_id": "sel_new"})

    delta = compare_pinned_runs(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=14,
    )

    assert delta["measurement_basis"]["same"] is False
    movement = _movement(delta, "attribution")
    assert movement["direction"] == "unknown"
    assert movement["is_material"] is False


def test_unknown_basis_keeps_loose_threshold():
    """W2: if one side predates prompt-basis pinning (same=None), stay conservative
    — an 8-point move is not asserted as material."""
    prior = _report(attribution=45, prompt_basis={})  # no prompt basis
    current = _report(attribution=53, prompt_basis={"selected_set_id": "sel_new"})

    delta = compare_pinned_runs(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=14,
    )

    assert delta["measurement_basis"]["same"] is None
    assert _movement(delta, "attribution")["is_material"] is False


def test_controller_archetype_flip_is_neutral_material_change():
    prior = _report(controller_strategy="leading_retailer_competition")
    current = _report(controller_strategy="source_authority_gap")

    delta = compare_pinned_runs(
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
    delta = compare_pinned_runs(
        current_report=_report(prompt_basis={}),
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

    delta = compare_pinned_runs(
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


def test_measurement_basis_between_matches_build_reaudit_delta():
    """The standalone basis wrapper must agree with build_reaudit_delta for
    the same report pair — it exists so per-SKU consumers can gate query-level
    claims without the score-delta layer."""
    prior = _report(prompt_basis={"selected_set_id": "sel_abc"})
    current = _report(prompt_basis={"selected_set_id": "sel_abc"})

    delta = compare_pinned_runs(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=7,
    )

    assert measurement_basis_between(current, prior) == delta["measurement_basis"]
    assert measurement_basis_between(current, prior)["same"] is True


def test_measurement_basis_between_reads_per_sku_reports_shape():
    """Per-SKU brand reports carry prompt_basis on per_sku_reports rows, not
    at the root — the wrapper must resolve it there (same tolerance as
    build_reaudit_delta's _prompt_set_id)."""
    prior = {
        "per_sku_reports": [{"prompt_basis": {"selected_set_id": "sel_w2"}}]
    }
    current = {
        "per_sku_reports": [{"prompt_basis": {"selected_set_id": "sel_w2"}}]
    }

    assert measurement_basis_between(current, prior)["same"] is True
    changed = {
        "per_sku_reports": [{"prompt_basis": {"selected_set_id": "sel_new"}}]
    }
    assert measurement_basis_between(changed, prior)["same"] is False


def test_measurement_basis_between_none_prior_is_baseline():
    basis = measurement_basis_between(_report(), None)
    assert basis["same"] is None
    assert "establishes the measurement basis" in basis["note"]


def test_full_brand_report_shape_uses_first_per_product_report():
    prior = {"per_product": [_report(attribution=35)]}
    current = {"per_product": [_report(attribution=55)]}

    delta = compare_pinned_runs(
        current_report=current,
        prior_report=prior,
        prior_row={"run_id": "prior"},
        days_since=10,
    )

    assert _movement(delta, "attribution")["direction"] == "improved"


# ---------------------------------------------------------------------------
# A not-comparable pair must not be narrated as a definite verdict.
#
# The degrade block turns every score movement into direction="unknown" /
# verdict="not_comparable" — and then `_headline`, which has no
# not-comparable branch, saw nothing material and said "No material change
# since your last audit N days ago — keep the current plan running." Two
# claims we cannot support (nothing moved; your plan is working), persisted
# into report_jsonb, on the pair we had just refused to compare.
# ---------------------------------------------------------------------------


def _not_comparable_delta(**overrides):
    """visibility 41 -> 63 (a 22-point move, well over the 15-point floor) on
    a pair with NO recorded measurement basis."""
    kwargs = {
        "current_report": _report(visibility=63),
        "prior_report": _report(visibility=41),
        "prior_row": {"run_id": "prior"},
        "days_since": 30,
        "current_basis": None,
        "prior_basis": None,
    }
    kwargs.update(overrides)
    return build_reaudit_delta(**kwargs)


def test_not_comparable_pair_headline_says_not_comparable():
    delta = _not_comparable_delta()

    assert delta["measurement_basis"]["same"] is None
    assert all(m["direction"] == "unknown" for m in delta["movements"]
               if m["signal"] in ("visibility", "attribution",
                                  "category_visibility"))
    headline = delta["headline"]
    assert headline.startswith("Not comparable to your last audit 30 days ago")
    # The exact sentence the pair cannot support, in either direction.
    assert "keep the current plan running" not in headline
    assert "No material change" not in headline
    assert "Material change" not in headline


def test_not_comparable_headline_names_the_reason_when_one_is_known():
    from services.audit_delta import NOT_COMPARABLE_REASONS

    missing = _not_comparable_delta()
    assert NOT_COMPARABLE_REASONS["measurement_basis_missing"] in missing["headline"]

    prompt_set_changed = _not_comparable_delta(
        current_report=_report(visibility=63,
                               prompt_basis={"selected_set_id": "sel_new"}),
        prior_report=_report(visibility=41,
                             prompt_basis={"selected_set_id": "sel_old"}),
        current_basis=_basis(),
        prior_basis=_basis(),
    )
    assert prompt_set_changed["measurement_basis"]["same"] is False
    assert (NOT_COMPARABLE_REASONS["prompt_set_changed"]
            in prompt_set_changed["headline"])

    unpinned = _not_comparable_delta(
        current_report=_report(visibility=63, prompt_basis={}),
        prior_report=_report(visibility=41, prompt_basis={}),
        current_basis=_basis(),
        prior_basis=_basis(),
    )
    assert unpinned["measurement_basis"]["same"] is None
    assert NOT_COMPARABLE_REASONS["prompt_basis_missing"] in unpinned["headline"]


def test_not_comparable_pair_tracking_read_is_not_measurable_not_unchanged():
    """The headline branch above is only half the claim. `tracked_metric_results`
    is derived from the SAME movements the degrade block stamped
    `not_comparable`, and `_tracked_metric_result` only knows "moved" /
    "unchanged": with nothing material left every metric came back
    status="unchanged", note "…; no material movement". Both renderers print
    that verbatim — "Tracking read: AI visibility rate: unchanged" — directly
    beneath a "Not comparable" headline. Same false no-change claim, one layer
    down."""
    from services.audit_delta import NOT_COMPARABLE_REASONS

    delta = _not_comparable_delta()
    tracked = delta["tracked_metric_results"]

    assert tracked, "the fixture's tracking metrics must survive the branch"
    assert {row["status"] for row in tracked} == {"not_measurable"}
    for row in tracked:
        assert NOT_COMPARABLE_REASONS["measurement_basis_missing"] in row["note"]
        assert "no material movement" not in row["note"]

    # And the reason travels: a changed prompt set says so.
    changed = _not_comparable_delta(
        current_report=_report(visibility=63,
                               prompt_basis={"selected_set_id": "sel_new"}),
        prior_report=_report(visibility=41,
                             prompt_basis={"selected_set_id": "sel_old"}),
        current_basis=_basis(),
        prior_basis=_basis(),
    )
    assert all(
        NOT_COMPARABLE_REASONS["prompt_set_changed"] in row["note"]
        and row["status"] == "not_measurable"
        for row in changed["tracked_metric_results"]
    )

    # A COMPARABLE pair is untouched: the ordinary tracking read still reads
    # "unchanged" with the mapped-signal note.
    flat = compare_pinned_runs(
        current_report=_report(),
        prior_report=_report(),
        prior_row={"run_id": "prior"},
        days_since=30,
    )
    citation = _metric(flat, "First-party citation rate")
    assert citation["status"] == "unchanged"
    assert "no material movement" in citation["note"]


def test_a_comparable_pair_still_gets_the_ordinary_headline():
    """The guard above must not swallow the real verdicts."""
    moved = compare_pinned_runs(
        current_report=_report(attribution=55),
        prior_report=_report(attribution=35),
        prior_row={"run_id": "prior"},
        days_since=30,
    )
    assert "improved: First-party citation" in moved["headline"]

    flat = compare_pinned_runs(
        current_report=_report(),
        prior_report=_report(),
        prior_row={"run_id": "prior"},
        days_since=30,
    )
    assert flat["headline"].startswith("No material change")

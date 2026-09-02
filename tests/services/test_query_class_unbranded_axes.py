"""Pipeline sprint, check 1: the branded/unbranded classifier.

`query_class_for_axis` was a one-value whitelist — anything but the literal
"category" was `branded_navigational`. The live unbranded axes the generator
actually emits (sidewalk, attribute, custom) were therefore COUNTED AS BRANDED
by `_query_class_coverage`, whose docstring says it exists so the report
"never conflates 'found when shoppers name you' with 'found when shoppers ask
the category question'". The classifier now derives from `intent_axis_for`,
the same partition `_scan_mode_for_query_spec` uses to choose the scan mode a
probe actually runs under, so report and probe agree by construction.

Motivating measurement (2026-09-01, 840 grounded responses): unbranded
discovery mention runs 0.0-8.9% against 100% branded across 7 cohorts. Note
those cohort tiers were measured by query TEXT, not by a stamped axis — the
in-repo defect this file pins is the axis-keyed one above.

Positive counterpart (feedback_a_negative_assertion_needs_a_positive_counterpart):
`category` must still route to category_discovery, and `intent` to branded.
"""
import pytest

from services.audit_facts import (
    QUERY_CLASS_BRANDED,
    QUERY_CLASS_CATEGORY,
    query_class_for_axis,
    run_query_class,
)

# "sidewalk" and "attribute" are live-emitted axes; "head"/"problem"/"dupe" are
# NOT axis values any producer stamps (they are output classes of
# sku_opportunity._query_class and tier labels from the 2026-09-01 cohort
# measurement). They are kept here as fall-through coverage, not as a claim
# that the generator emits them.
UNBRANDED_DISCOVERY_AXES = ["sidewalk", "attribute", "head", "problem", "dupe"]


@pytest.mark.parametrize("axis", UNBRANDED_DISCOVERY_AXES)
def test_unbranded_discovery_axes_are_not_counted_as_branded(axis):
    assert query_class_for_axis(axis) != QUERY_CLASS_BRANDED, (
        f"axis={axis!r} is an unbranded discovery shape; filing it as "
        f"{QUERY_CLASS_BRANDED} inflates branded coverage and hides the gap"
    )


@pytest.mark.parametrize("axis", [None, "", "   ", "unknown", "custom"])
def test_missing_or_unknown_axis_is_not_silently_branded(axis):
    assert query_class_for_axis(axis) != QUERY_CLASS_BRANDED


def test_run_without_axis_metadata_is_not_branded():
    assert run_query_class({}) != QUERY_CLASS_BRANDED
    assert run_query_class({"axis_metadata": {}}) != QUERY_CLASS_BRANDED


# --- positive counterparts: the classifier must still classify -----------
def test_category_axis_still_routes_to_category_discovery():
    assert query_class_for_axis("category") == QUERY_CLASS_CATEGORY


@pytest.mark.parametrize("axis", ["intent", "price", "brand", "identity"])
def test_branded_axes_still_route_to_branded(axis):
    assert query_class_for_axis(axis) == QUERY_CLASS_BRANDED


# --- consumer level: the number that reaches RunFacts and the report --------
def _payload(axis, query="best contour palette under $15"):
    run = {"query": query, "provider": "gemini"}
    if axis is not None:
        run["axis_metadata"] = {"axis": axis}
    return [{"provider": "gemini", "probe_run_id": "p", "raw_runs": [run]}]


@pytest.mark.parametrize("axis", ["sidewalk", "attribute", "head", "problem", "dupe"])
def test_query_class_coverage_does_not_count_discovery_shapes_as_branded(axis):
    from services.agent_center_bd_report_service import _query_class_coverage

    cov = _query_class_coverage(_payload(axis))
    assert cov[QUERY_CLASS_BRANDED] == 0, (
        f"axis={axis!r} was probed under the DISCOVERY scan mode "
        f"(_scan_mode_for_query_spec) but is reported as branded coverage"
    )


def test_query_class_coverage_agrees_with_the_scan_mode_partition():
    """The report must describe the partition the probe actually ran under."""
    from services.agent_center_bd_report_service import (
        _PER_SKU_DISCOVERY_SCAN_MODE,
        _query_class_coverage,
        _scan_mode_for_query_spec,
    )

    for axis in ["sidewalk", "attribute", "custom", "category"]:
        assert _scan_mode_for_query_spec("best cream blush", axis) == _PER_SKU_DISCOVERY_SCAN_MODE
        assert _query_class_coverage(_payload(axis))[QUERY_CLASS_BRANDED] == 0, axis


# --- the stamp defaults: a record with NO axis must not be PROBED as branded ---
# These are the half of the fix the classifier tests cannot reach. Before the
# fix an axis-less record was stamped "intent" *before* partitioning, and
# _scan_mode_for_query_spec("intent") selects the BRANDED scan mode — so the
# query was mis-probed, not merely mis-reported. Reverting the three
# AXIS_UNCLASSIFIED defaults to "intent" left 130 tests green, which is why
# these exist.
def test_unstamped_selected_spec_defaults_to_unclassified_not_intent():
    from services.audit_facts import AXIS_UNCLASSIFIED
    from services.prompt_basis import clean_selected_specs

    [spec] = clean_selected_specs([{"query": "best cream blush under $15"}])
    assert spec["axis"] == AXIS_UNCLASSIFIED


def test_unstamped_spec_is_probed_under_the_discovery_scan_mode():
    from services.agent_center_bd_report_service import (
        _PER_SKU_DISCOVERY_SCAN_MODE,
        _scan_mode_for_query_spec,
    )
    from services.audit_facts import AXIS_UNCLASSIFIED
    from services.prompt_basis import clean_selected_specs

    [spec] = clean_selected_specs([{"query": "best cream blush under $15"}])
    assert (
        _scan_mode_for_query_spec(spec["query"], spec["axis"])
        == _PER_SKU_DISCOVERY_SCAN_MODE
    ), "an unstamped query must be probed on its own merit, not as branded"
    assert _scan_mode_for_query_spec("q", AXIS_UNCLASSIFIED) == _PER_SKU_DISCOVERY_SCAN_MODE


def test_a_genuinely_branded_spec_still_probes_under_the_branded_mode():
    """Positive counterpart: the default changed, the branded path did not."""
    from services.agent_center_bd_report_service import (
        _PER_SKU_BRANDED_SCAN_MODE,
        _scan_mode_for_query_spec,
    )
    from services.prompt_basis import clean_selected_specs

    [spec] = clean_selected_specs([{"query": "where to buy Judydoll X", "axis": "intent"}])
    assert spec["axis"] == "intent"
    assert (
        _scan_mode_for_query_spec(spec["query"], spec["axis"])
        == _PER_SKU_BRANDED_SCAN_MODE
    )


# --- F4: deep-tier comparison runs are internal-first everywhere ------------
def _comparison_run(query="Brand vs Rival"):
    return {
        "query": query,
        "axis_metadata": {"axis": "comparison", "prompt_source": "deep_tier"},
    }


def test_compute_run_facts_excludes_internal_comparison_by_default():
    from services.audit_facts import compute_run_facts

    facts = compute_run_facts([_comparison_run()], merchant_host="brand.com")
    assert facts.run_count == 0, (
        "deep-tier comparison probes are internal-first; every RunFacts rollup "
        "feeds merchant-facing surfaces"
    )


def test_compute_run_facts_counts_a_normal_run():
    """Positive counterpart: the exclusion is targeted, not a blanket drop."""
    from services.audit_facts import compute_run_facts

    run = {"query": "best cream blush", "axis_metadata": {"axis": "category"}}
    assert compute_run_facts([run], merchant_host="brand.com").run_count == 1


def test_the_internal_rollup_can_opt_back_in():
    from services.audit_facts import compute_run_facts

    facts = compute_run_facts(
        [_comparison_run()], merchant_host="brand.com", include_internal_comparison=True
    )
    assert facts.run_count == 1

"""Pipeline sprint, check 1: the branded/unbranded classifier.

`query_class_for_axis` is a one-value whitelist — anything but the literal
"category" is `branded_navigational`. The unbranded discovery shapes the
prompt generator emits (sidewalk / attribute / head / problem / dupe) are
therefore COUNTED AS BRANDED by `_query_class_coverage`, whose docstring says
it exists so the report "never conflates 'found when shoppers name you' with
'found when shoppers ask the category question'".

Measured 2026-09-01 on 840 grounded responses: dupe queries are 0.0% mention
in 6 of 7 cohorts — the most complete failure mode — and the classifier files
them as branded, where the neighbouring rate is 100%.

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

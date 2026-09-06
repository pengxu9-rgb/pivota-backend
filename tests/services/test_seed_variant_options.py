"""One reader for two stored shapes of the same fact."""

from __future__ import annotations

import pytest

from services.seed_variant_options import (
    normalize_seed_variant_options,
    seed_variant_options_as_mapping,
)


def test_the_enrichment_lanes_list_shape():
    assert normalize_seed_variant_options([{"name": "Shade", "value": "Ruby Woo"}]) == [
        {"name": "Shade", "value": "Ruby Woo"}
    ]


def test_the_employee_csv_lanes_mapping_shape():
    """`routes/employee_products.py` writes `{option_name: option_value}`."""
    assert normalize_seed_variant_options({"Shade": "Ruby Woo"}) == [
        {"name": "Shade", "value": "Ruby Woo"}
    ]


@pytest.mark.parametrize("raw", [None, "", 0, [], {}, "Shade: Ruby Woo", ["Ruby Woo"], [None]])
def test_nothing_usable_yields_nothing(raw):
    assert normalize_seed_variant_options(raw) == []


def test_a_half_formed_pair_is_dropped_not_forwarded():
    """A pair missing either half reaches the page as a selector entry naming no
    choice — worse than the bare list it replaces."""
    assert normalize_seed_variant_options(
        [{"name": "Shade", "value": ""}, {"value": "Bronx"}, {"name": "  Shade  ",
                                                              "value": "  Ruby Woo  "}]
    ) == [{"name": "Shade", "value": "Ruby Woo"}]


def test_a_repeated_pair_is_collapsed():
    assert normalize_seed_variant_options(
        [{"name": "Shade", "value": "Ruby Woo"}, {"name": "Shade", "value": "Ruby Woo"}]
    ) == [{"name": "Shade", "value": "Ruby Woo"}]


def test_the_mapping_form_keeps_the_type_its_lane_is_declared_for():
    """`agent_shop_gateway` has always emitted a mapping here. Forwarding the
    enrichment lane's list verbatim would flip a public field's type as a side
    effect of a writer-side change."""
    assert seed_variant_options_as_mapping([{"name": "Shade", "value": "Ruby Woo"}]) == {
        "Shade": "Ruby Woo"
    }
    assert seed_variant_options_as_mapping({"Shade": "Ruby Woo"}) == {"Shade": "Ruby Woo"}
    assert seed_variant_options_as_mapping(None) == {}


def test_the_mapping_form_keeps_the_first_value_on_a_repeated_axis():
    """A mapping cannot hold both; dropping the earlier one would silently
    reorder what the page shows."""
    assert seed_variant_options_as_mapping(
        [{"name": "Shade", "value": "Ruby Woo"}, {"name": "Shade", "value": "Bronx"}]
    ) == {"Shade": "Ruby Woo"}


@pytest.mark.parametrize("raw", [
    {"Shade": {"value": "Ruby Woo", "hex": "#f00"}},
    {"Shade": ["Ruby", "Woo"]},
    [{"name": "Shade", "value": {"hex": "#f00"}}],
    [{"name": ["Shade"], "value": "Ruby Woo"}],
    {"Shade": True},
])
def test_a_container_is_not_a_label(raw):
    """`str()` of a dict or list is NON-EMPTY, so coercing before testing for
    emptiness let "{'hex': '#f00'}" through to the page as a selector entry."""
    assert normalize_seed_variant_options(raw) == []


def test_a_numeric_shade_code_is_a_label():
    """The positive counterpart — a shade named `01` is a real label, and the
    scalar guard must not take it with the containers."""
    assert normalize_seed_variant_options({"Shade": 1}) == [{"name": "Shade", "value": "1"}]


@pytest.mark.parametrize("raw", [
    {"": "Ruby Woo"},
    {"   ": "Ruby Woo"},
    [{"name": "  ", "value": "Bronx"}],
    [{"name": "", "value": "Bronx"}],
])
def test_a_pair_with_no_axis_name_is_dropped(raw):
    """The missing-NAME half of the well-formedness rule. The existing case used
    `{"value": "Bronx"}`, where `name` is None and the upstream scalar guard
    rejects it first — so the `not name` clause itself had no coverage."""
    assert normalize_seed_variant_options(raw) == []

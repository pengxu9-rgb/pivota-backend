"""The third seed-variant builder — the one whose shape DIFFERS from the others.

`routes/agent_shop_gateway.py::_normalize_seed_variants` feeds the invoke lane,
and it is the only one of the three that hands on a `{name: value}` MAPPING
rather than a list of pairs. It had no test at all: reverting it to its old raw
passthrough, or hardcoding `options` to `{}`, left the entire suite green.

The mapping is what this lane has always emitted, and the point is that the
enrichment lane's new list shape does not silently change it. The blob test
below pins one dict-consuming reader; it is a demonstration that the shape
matters to a real reader, not a claim that this lane feeds that reader.
"""

from __future__ import annotations

import pytest


def _variants(raw_options, title="v1"):
    from routes.agent_shop_gateway import _normalize_seed_variants

    return _normalize_seed_variants(
        {"variants": [{"variant_id": "v1", "title": title, "price_amount": 24.0,
                       "price_currency": "USD", "availability": "in_stock",
                       "image_url": "https://cdn.x/rw.jpg", "options": raw_options}]}
    )


def test_the_enrichment_lanes_list_becomes_this_lanes_mapping():
    assert _variants([{"name": "Shade", "value": "Ruby Woo"}])[0]["options"] == {
        "Shade": "Ruby Woo"
    }


def test_the_employee_csv_lanes_mapping_is_passed_through():
    assert _variants({"Shade": "Ruby Woo"})[0]["options"] == {"Shade": "Ruby Woo"}


@pytest.mark.parametrize("raw", [None, [], {}, "Shade: Ruby Woo", [{"name": "Shade"}]])
def test_nothing_usable_yields_an_empty_mapping(raw):
    """Empty MAPPING, not an empty list — a consumer that guards
    `isinstance(options, dict)` would skip the field otherwise."""
    out = _variants(raw)[0]["options"]
    assert out == {} and isinstance(out, dict)


def test_the_axis_survives_into_the_search_blob():
    """A real dict-consuming reader, to show the shape is load-bearing.
    `_pivot_multi_search_text_blob` reads `options` only when it is a dict, so a
    list passed through verbatim contributes nothing to the text it builds.

    The variant TITLE deliberately carries none of the option text. The blob
    appends the title unconditionally, so a fixture whose title repeats the shade
    would pass whatever `options` holds — this assertion has to be able to fail,
    and its negative counterpart below proves it can."""
    from routes.agent_shop_gateway import _pivot_multi_search_text_blob

    product = {"title": "Retro Matte Lipstick",
               "variants": _variants([{"name": "Shade", "value": "Ruby Woo"}], title="v1")}
    blob = _pivot_multi_search_text_blob(product).lower()
    assert "ruby woo" in blob
    assert "shade" in blob


def test_the_search_blob_assertion_can_fail():
    """Its negative counterpart: with no axis, the same fixture must NOT carry
    the shade — otherwise the test above proves nothing."""
    from routes.agent_shop_gateway import _pivot_multi_search_text_blob

    product = {"title": "Retro Matte Lipstick", "variants": _variants(None, title="v1")}
    assert "ruby woo" not in _pivot_multi_search_text_blob(product).lower()

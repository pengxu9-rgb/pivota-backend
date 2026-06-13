"""SkuNode.visible_attributes coercion.

External_seed records + the variant promoter (Dict[str, str]) emit scalar
attribute labels (e.g. {"Format": "Garden Gift Set"}), but the contract type is
Dict[str, List[str]]. Strict validation rejected the scalar shape and dropped
the whole SkuNode — silently erroring real SKUs out of the pivot/eval assembly
(the K-beauty decision-grade eval errored 4/5 Ownist SKUs on this). The
validator normalizes scalars to single-element lists instead of failing.
"""

from models.catalog import SkuNode


def test_scalar_string_value_coerced_to_list() -> None:
    node = SkuNode(visible_attributes={"Format": "Garden Gift Set"})
    assert node.visible_attributes == {"Format": ["Garden Gift Set"]}


def test_mixed_scalar_and_list_values() -> None:
    node = SkuNode(
        visible_attributes={
            "Size": "14 Servings, 2-Week Routine",  # scalar (was the crash)
            "Shade": ["Grape", "Orange"],  # already a list
        }
    )
    assert node.visible_attributes == {
        "Size": ["14 Servings, 2-Week Routine"],
        "Shade": ["Grape", "Orange"],
    }


def test_none_and_empty_values_become_empty_lists() -> None:
    node = SkuNode(visible_attributes={"A": None, "B": "", "C": "  "})
    assert node.visible_attributes == {"A": [], "B": [], "C": []}


def test_list_values_are_string_normalized_and_pruned() -> None:
    node = SkuNode(visible_attributes={"K": ["x", None, "", 3]})
    assert node.visible_attributes == {"K": ["x", "3"]}


def test_non_dict_visible_attributes_becomes_empty() -> None:
    assert SkuNode(visible_attributes=None).visible_attributes == {}
    assert SkuNode(visible_attributes="nope").visible_attributes == {}


def test_default_is_empty_dict() -> None:
    assert SkuNode().visible_attributes == {}

"""Structured-category gate on the strict agent surface (agent_api).

The category/attribute gate matched only STRUCTURED visible_attributes, with text
fallback disabled on strict — but essentially no products carry those structured
attributes, so it blocked EVERY beauty category query ("green tea toner" → 0).
Mirror the ingredient text-recall #1659 already enabled for beauty: allow category
/attribute words to match product TITLE/TYPE/TAGS text on strict too, flag-gated
(STRICT_BEAUTY_CATEGORY_TEXT_RECALL), matching title/type/tags only (never
description) for precision.
"""
from __future__ import annotations

import inspect
import os

from routes import agent_shop_gateway


def _src() -> str:
    return inspect.getsource(agent_shop_gateway._handle_find_products_multi_inner)


def test_flag_default_off_and_env_gated():
    prev = os.environ.pop("STRICT_BEAUTY_CATEGORY_TEXT_RECALL", None)
    try:
        src = _src()
        # The strict recall is gated on the env flag (default OFF ⇒ byte-identical
        # to the pre-change structured-only behaviour on the strict surface).
        assert "STRICT_BEAUTY_CATEGORY_TEXT_RECALL" in src
        assert "beauty_category_text_recall_enabled" in src
        # Off-strict behaviour is unchanged: the flag is True either off-strict OR
        # when the env flag is set.
        assert "not strict_serving_mode" in src
    finally:
        if prev is not None:
            os.environ["STRICT_BEAUTY_CATEGORY_TEXT_RECALL"] = prev


def test_category_and_attribute_gates_consult_the_flag():
    src = _src()
    # Both the category and attribute text-fallbacks must OR-in the new flag (so
    # the fallback runs on strict when the flag is on), mirroring the ingredient
    # precedent's `non_strict... or beauty_ingredient_text_recall_enabled`.
    assert src.count(
        "non_strict_beauty_text_recall_enabled or beauty_category_text_recall_enabled"
    ) >= 2


def test_strict_blob_excludes_description_for_precision():
    src = _src()
    # The strict text-recall blob is title/type/tags only — NEVER description
    # (blob_for_filters, which includes description, is used only off-strict).
    assert "title_type_tag_blob" in src
    i = src.index("elif beauty_category_text_recall_enabled:")
    assert "beauty_text_blob = title_type_tag_blob" in src[i:i + 200]
    # title_type_tag_blob is assembled from title/product_type/tags, not description.
    j = src.index("title_type_tag_blob = ")
    block = src[j:j + 400]
    assert "product.title" in block and "product.product_type" in block
    assert "product.description" not in block

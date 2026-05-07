from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.mirror_external_seeds_to_catalog_products import (  # noqa: E402
    CATEGORY_CONFIDENCE_REGEX_AT_MIRROR,
    CATEGORY_LABEL_SOURCE_AT_MIRROR,
    resolve_mirror_category_metadata,
)


def test_mirror_insert_classifies_category_path() -> None:
    meta = resolve_mirror_category_metadata(
        category=None,
        product_type="Lip Gloss",
        title="Fenty Beauty Gloss Bomb Lip Gloss",
    )

    assert meta["category_path"] == "beauty/makeup/lip/lipstick"
    assert meta["category_confidence"] == CATEGORY_CONFIDENCE_REGEX_AT_MIRROR
    assert meta["category_label_source"] == CATEGORY_LABEL_SOURCE_AT_MIRROR
    assert meta["category_label"] == "Lipstick"


def test_mirror_insert_leaves_unknown_category_null() -> None:
    meta = resolve_mirror_category_metadata(
        category=None,
        product_type="Accessory",
        title="Travel Makeup Bag",
    )

    assert meta["category_path"] is None
    assert meta["category_confidence"] is None
    assert meta["category_label_source"] is None
    assert meta["category_label"] is None

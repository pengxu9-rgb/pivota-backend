"""
Tests for recommendation_meta helpers.
"""

from catalog.recommendation_meta import (
    parse_tags,
    derive_facets,
    derive_recommendation_meta,
)
from models.standard_product import StandardProduct


class TestParseTags:
    def test_parse_tags_from_string(self):
        tags_raw, tags_norm = parse_tags("Group-123, Cat:Brush ,  ,  area:Face")
        assert tags_raw == ["Group-123", "Cat:Brush", "area:Face"]
        assert tags_norm == ["group-123", "cat:brush", "area:face"]

    def test_parse_tags_with_semicolon_separator(self):
        tags_raw, tags_norm = parse_tags("Group-123; Cat:Brush ; area:Face")
        assert tags_raw == ["Group-123", "Cat:Brush", "area:Face"]
        assert tags_norm == ["group-123", "cat:brush", "area:face"]

    def test_parse_tags_from_list(self):
        tags_raw, tags_norm = parse_tags(["Group-123", "Cat:Brush", "Cat:Brush"])
        assert tags_raw == ["Group-123", "Cat:Brush", "Cat:Brush"]
        # Deduplicated, normalized
        assert tags_norm == ["group-123", "cat:brush"]

    def test_parse_tags_none(self):
        tags_raw, tags_norm = parse_tags(None)
        assert tags_raw == []
        assert tags_norm == []


class TestDeriveFacets:
    def test_group_and_structured_facets(self):
        tags_norm = [
            "group-651785399260",
            "cat:brush",
            "area:face",
            "use:blush",
            "mat:synthetic",
            "hair:goat",
            "shape:round",
            "feature:soft",
            "series:pro",
            "color:pink",
            "ships-48h",
        ]
        group_id, facets = derive_facets(tags_norm)

        assert group_id == "651785399260"
        assert facets["cat"] == "brush"
        assert facets["area"] == ["face"]
        assert facets["use"] == ["blush"]
        assert facets["material"] == ["synthetic"]
        assert facets["hair"] == ["goat"]
        assert facets["shape"] == ["round"]
        assert facets["feature"] == ["soft"]
        assert facets["series"] == ["pro"]
        assert facets["color"] == ["pink"]
        assert facets["ships"] == ["48h"]

    def test_multiple_values_and_dedup(self):
        tags_norm = [
            "area:face",
            "area:eyes",
            "area:face",
            "use:blush",
            "use:foundation",
            "material:synthetic",
            "mat:synthetic",
        ]
        group_id, facets = derive_facets(tags_norm)

        assert group_id is None
        assert facets["area"] == ["face", "eyes"]
        # material should be deduplicated and unified
        assert facets["material"] == ["synthetic"]
        assert facets["use"] == ["blush", "foundation"]

    def test_unsupported_tags_do_not_create_facets(self):
        tags_norm = ["foo", "bar:baz", "other"]
        group_id, facets = derive_facets(tags_norm)

        assert group_id is None
        assert facets == {}


class TestDeriveRecommendationMeta:
    def test_meta_from_raw_shopify_tags(self):
        product = StandardProduct(
            id="651785399260",
            platform="shopify",
            merchant_id="m1",
            title="Brush",
            price=10.0,
        )
        raw = {
            "id": 651785399260,
            "tags": "Group-651785399260, Cat:Brush, Area:Face, Use:Blush",
        }

        meta = derive_recommendation_meta(product, raw)

        assert meta["version"] == 1
        assert meta["group_id"] == "651785399260"
        # tags_raw preserve original form (trimmed)
        assert "Group-651785399260" in meta["tags_raw"]
        assert "Cat:Brush" in meta["tags_raw"]
        # tags normalized and deduplicated
        assert "group-651785399260" in meta["tags"]
        assert "cat:brush" in meta["tags"]
        assert meta["facets"]["cat"] == "brush"
        assert meta["facets"]["area"] == ["face"]
        assert meta["facets"]["use"] == ["blush"]

    def test_meta_without_group_tag(self):
        product = StandardProduct(
            id="p1",
            platform="shopify",
            merchant_id="m1",
            title="Product",
            price=10.0,
        )
        raw = {"id": 1, "tags": "cat:brush, area:face"}

        meta = derive_recommendation_meta(product, raw)

        assert meta["group_id"] is None
        assert meta["facets"]["cat"] == "brush"
        assert meta["facets"]["area"] == ["face"]

    def test_meta_falls_back_to_standard_product_tags(self):
        product = StandardProduct(
            id="p1",
            platform="shopify",
            merchant_id="m1",
            title="Product",
            price=10.0,
            tags=["Group-42", "Area:Face"],
        )

        meta = derive_recommendation_meta(product, raw_shopify_product=None)

        assert meta["group_id"] == "42"
        assert "group-42" in meta["tags"]
        assert meta["facets"]["area"] == ["face"]

    def test_group_id_with_colon_prefix(self):
        product = StandardProduct(
            id="p1",
            platform="shopify",
            merchant_id="m1",
            title="Product",
            price=10.0,
        )
        raw = {"id": 1, "tags": "group:999, cat:brush"}

        meta = derive_recommendation_meta(product, raw)

        assert meta["group_id"] == "999"
        assert meta["facets"]["cat"] == "brush"

    def test_meta_empty_when_no_tags(self):
        product = StandardProduct(
            id="p1",
            platform="shopify",
            merchant_id="m1",
            title="Product",
            price=10.0,
        )
        raw = {"id": 1, "tags": ""}

        meta = derive_recommendation_meta(product, raw)

        assert meta["group_id"] is None
        assert meta["tags_raw"] == []
        assert meta["tags"] == []
        assert meta["facets"] == {}

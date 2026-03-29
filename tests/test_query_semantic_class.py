from __future__ import annotations

from services.query_semantic_class import classify_query_semantic_class


def test_classify_query_semantic_class_defaults_to_default() -> None:
    assert classify_query_semantic_class("linen sheet set") == "default"
    assert classify_query_semantic_class("") == "default"


def test_classify_query_semantic_class_detects_fragrance() -> None:
    assert classify_query_semantic_class("rose eau de parfum") == "fragrance"
    assert classify_query_semantic_class("travel perfume spray") == "fragrance"


def test_classify_query_semantic_class_detects_beauty_and_lingerie() -> None:
    assert classify_query_semantic_class("fragrance free barrier moisturizer") == "beauty"
    assert classify_query_semantic_class("lingerie set") == "lingerie"

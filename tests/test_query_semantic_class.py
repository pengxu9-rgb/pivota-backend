from __future__ import annotations

from services.query_semantic_class import classify_query_semantic_class


def test_classify_query_semantic_class_defaults_to_default() -> None:
    assert classify_query_semantic_class("linen sheet set") == "default"
    assert classify_query_semantic_class("") == "default"


def test_classify_query_semantic_class_detects_fragrance() -> None:
    assert classify_query_semantic_class("rose eau de parfum") == "fragrance"
    assert classify_query_semantic_class("travel perfume spray") == "fragrance"
    assert classify_query_semantic_class("le labo santal 33") == "fragrance"


def test_classify_query_semantic_class_detects_beauty_and_lingerie() -> None:
    assert classify_query_semantic_class("fragrance free barrier moisturizer") == "beauty"
    assert classify_query_semantic_class("sunscreen") == "beauty"
    assert classify_query_semantic_class("spf 50") == "beauty"
    assert classify_query_semantic_class("lingerie set") == "lingerie"


def test_classify_query_semantic_class_catches_beauty_regression_queries() -> None:
    assert classify_query_semantic_class("body acne treatment spray") == "beauty"
    assert classify_query_semantic_class("eye cream for dark circles") == "beauty"
    assert classify_query_semantic_class("overnight mask for dry skin") == "beauty"
    assert classify_query_semantic_class("retinal night cream") == "beauty"
    assert classify_query_semantic_class("clean makeup remover balm") == "beauty"


def test_classify_query_semantic_class_detects_additional_beauty_categories() -> None:
    # Regression: these unambiguous beauty categories previously classified as "default",
    # which blocked external-seed recall (semantic_class_blocked) and returned 0 results
    # despite real eligible supply.
    for query in (
        "nail polish",
        "lip balm",
        "lip liner",
        "mascara",
        "eyeliner",
        "eye liner",
        "concealer",
        "bronzer",
        "eyeshadow",
        "eye shadow",
        "setting spray",
        "cuticle oil",
        "vitamin c ampoule",
        "body wash",
        "body lotion",
        "hair mask",
        "shampoo",
        "deodorant",
    ):
        assert classify_query_semantic_class(query) == "beauty", query


def test_classify_query_semantic_class_additions_do_not_overreach() -> None:
    # The additions must not pull clearly non-beauty queries into "beauty".
    assert classify_query_semantic_class("linen sheet set") == "default"
    assert classify_query_semantic_class("cordless drill") == "default"
    assert classify_query_semantic_class("running shoes") == "default"

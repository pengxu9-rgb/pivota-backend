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


def test_classify_query_semantic_class_kbeauty_forms_and_actives() -> None:
    """K-beauty forms/actives are real high-count inventory (essence, ampoule,
    emulsion, cica, pdrn, centella, tea tree, ceramide, ...). Missing them
    misclassified these as 'default', blocking the external-seed beauty leg and
    zeroing legitimate results on the agent tool."""
    for q in [
        "snail mucin essence",
        "hydrating essence",
        "centella ampoule",
        "propolis ampoule",
        "pdrn essence",
        "cica cream",
        "ceramide emulsion",
        "tea tree toner",
        "collagen ampoule",
        "rice water toner",
        "mugwort cleanser",
        "heartleaf calming mist",
    ]:
        assert classify_query_semantic_class(q) == "beauty", q


def test_classify_query_semantic_class_kbeauty_additions_do_not_over_capture() -> None:
    """The additions are word-boundary / skincare-clear only (no bare
    cream/oil/mask/gel), so genuinely non-beauty queries stay 'default' and the
    junk guards keep protecting them."""
    for q in [
        "leather crossbody bag",
        "dog food",
        "running shoes",
        "wireless earbuds",
        "ice cream maker",
        "olive oil",
        "notepad",
    ]:
        assert classify_query_semantic_class(q) == "default", q

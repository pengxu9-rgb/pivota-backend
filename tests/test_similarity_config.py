import os

from services.similarity_config import (
    get_similarity_scoring_weights,
    default_similarity_scoring_weights,
)


def test_weights_default(monkeypatch):
    # Clear env
    for key in [
        "SIMILARITY_WEIGHT_SIMILARITY",
        "SIMILARITY_WEIGHT_PRICE",
        "SIMILARITY_WEIGHT_MERCHANT",
        "SIMILARITY_WEIGHT_PERSONALIZATION",
    ]:
        monkeypatch.delenv(key, raising=False)

    weights = get_similarity_scoring_weights()
    assert weights == default_similarity_scoring_weights


def test_weights_env_override_and_normalize(monkeypatch):
    monkeypatch.setenv("SIMILARITY_WEIGHT_SIMILARITY", "1")
    monkeypatch.setenv("SIMILARITY_WEIGHT_PRICE", "1")
    monkeypatch.setenv("SIMILARITY_WEIGHT_MERCHANT", "1")
    monkeypatch.setenv("SIMILARITY_WEIGHT_PERSONALIZATION", "1")

    weights = get_similarity_scoring_weights()
    # All equal -> normalized to 0.25 each
    assert round(weights["similarity"], 2) == 0.25
    assert round(weights["price"], 2) == 0.25
    assert round(weights["merchant"], 2) == 0.25
    assert round(weights["personalization"], 2) == 0.25

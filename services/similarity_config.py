"""
Similarity scoring configuration

Allows runtime tuning of scoring weights via environment variables.
"""
import os
from typing import Dict


class SimilarityScoringWeights(Dict[str, float]):
    similarity: float
    price: float
    merchant: float
    personalization: float


default_similarity_scoring_weights: SimilarityScoringWeights = {
    "similarity": 0.6,
    "price": 0.2,
    "merchant": 0.1,
    "personalization": 0.1,
}


def _read_weight(env_key: str) -> float:
    try:
        return float(os.getenv(env_key, ""))
    except Exception:
        return float("nan")


def get_similarity_scoring_weights() -> SimilarityScoringWeights:
    """Return normalized similarity scoring weights, falling back to defaults."""
    env_weights = {
        "similarity": _read_weight("SIMILARITY_WEIGHT_SIMILARITY"),
        "price": _read_weight("SIMILARITY_WEIGHT_PRICE"),
        "merchant": _read_weight("SIMILARITY_WEIGHT_MERCHANT"),
        "personalization": _read_weight("SIMILARITY_WEIGHT_PERSONALIZATION"),
    }

    # Use defaults when env not provided or invalid
    weights = {}
    for key, val in env_weights.items():
        if val != val:  # NaN check
            weights[key] = default_similarity_scoring_weights[key]
        else:
            weights[key] = val

    total = sum(weights.values())
    if total <= 0:
        return default_similarity_scoring_weights

    normalized = {k: v / total for k, v in weights.items()}
    return normalized  # type: ignore

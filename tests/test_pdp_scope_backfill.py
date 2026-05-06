"""Pin the pdp_scope classifier rule.

The classifier is pure — given (category_label_source, seller_count) it
returns one of three scope strings. These tests lock the priority order
defined in services/pdp_scope_classifier.py: enrichment_agent_v1 always
beats seller_count, and a single-merchant row is merchant_owned by
default.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pdp_scope_classifier import (  # noqa: E402
    LABEL_SOURCE_ENRICHMENT,
    SCOPE_CANONICAL,
    SCOPE_MERCHANT_OWNED,
    ScopeSignals,
    classify,
)


def _signals(*, category_label_source=None, seller_count=1) -> ScopeSignals:
    return ScopeSignals(category_label_source=category_label_source, seller_count=seller_count)


def test_enrichment_agent_always_canonical():
    """An enrichment_agent_v1 row is canonical even with one validated merchant.
    The scope reflects the PDP's NATURE, not the offer-count snapshot."""
    out = classify(_signals(category_label_source=LABEL_SOURCE_ENRICHMENT, seller_count=1))
    assert out == SCOPE_CANONICAL


def test_enrichment_agent_canonical_with_zero_sellers():
    out = classify(_signals(category_label_source=LABEL_SOURCE_ENRICHMENT, seller_count=0))
    assert out == SCOPE_CANONICAL


def test_multi_seller_canonical_without_enrichment_label():
    out = classify(_signals(category_label_source="merchant_payload", seller_count=3))
    assert out == SCOPE_CANONICAL


def test_two_sellers_is_threshold():
    """seller_count >= 2 → canonical; the PDP is offered by ≥2 distinct merchants."""
    out = classify(_signals(category_label_source=None, seller_count=2))
    assert out == SCOPE_CANONICAL


def test_single_seller_default_merchant_owned():
    """The most common case for the long-tail merchant catalog: 1 merchant
    sells the product, no canonical sibling exists. MOYU brushes look like
    this."""
    out = classify(_signals(category_label_source="regex_backfill", seller_count=1))
    assert out == SCOPE_MERCHANT_OWNED


def test_zero_seller_default_merchant_owned():
    """A PDP with no offer rows yet (just inserted, no seeds attached) is
    treated as merchant_owned for matcher safety. seller_count is clamped
    to >=1 by the SQL but defending the rule explicitly."""
    out = classify(_signals(category_label_source=None, seller_count=0))
    assert out == SCOPE_MERCHANT_OWNED


def test_unknown_label_source_does_not_short_circuit():
    """Only enrichment_agent_v1 grants automatic canonical status.
    A made-up label_source falls through to seller_count."""
    out = classify(_signals(category_label_source="something_made_up", seller_count=1))
    assert out == SCOPE_MERCHANT_OWNED


def test_unknown_label_source_with_multi_seller_is_canonical():
    out = classify(_signals(category_label_source="something_made_up", seller_count=2))
    assert out == SCOPE_CANONICAL


def test_high_seller_count_canonical():
    out = classify(_signals(category_label_source=None, seller_count=20))
    assert out == SCOPE_CANONICAL


def test_negative_seller_count_falls_through_to_merchant_owned():
    """Defensive: a negative count (shouldn't happen but guard anyway)
    must not flip to canonical."""
    out = classify(_signals(category_label_source=None, seller_count=-1))
    assert out == SCOPE_MERCHANT_OWNED


def test_enrichment_agent_overrides_low_seller_count():
    """The MOYU lipsticks scenario: the agent authored 15 lipstick PDPs
    but only 18 offers came back from web validation (~1.2 per PDP).
    Most have seller_count=1, but they're all canonical industry products.
    The label_source override ensures they're scored as canonical, which
    matches the agent's intent."""
    for n in (0, 1, 2):
        out = classify(_signals(category_label_source=LABEL_SOURCE_ENRICHMENT, seller_count=n))
        assert out == SCOPE_CANONICAL, f"seller_count={n} should still be canonical for agent rows"

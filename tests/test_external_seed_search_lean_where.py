"""Lean WHERE for long many-token seed queries (SEED_QUERY_LEAN_WHERE_MIN_TOKENS).

Even with the #1369 fast path, a long natural-language query ("gentle foaming
cleanser for oily skin") still timed stage-A out to 0: each per-token arm ORs in
the trgm-indexed seed_data->derived->recall->>retrieval_summary column, GIN trgm
is lossy, and a common token ("skin"/"cream") flags thousands of rows whose
recheck detoasts the whole seed_data blob. ~2.4s → stage-A timeout → 0 results.

The lean WHERE drops the seed_data->recall JSON arms and matches ONLY the four
cheap inline columns (destination_url/canonical_url/domain/title) — no detoast,
worst-case ~0.06s, higher-precision title matches. It is applied per-query only
at/above a token-count threshold, so short ingredient queries keep the full
recall-rich path. Gated behind SEED_QUERY_FAST_MULTITERM ⇒ off = byte-identical.
"""
from __future__ import annotations

import pytest

from services.external_seed_search import build_external_seed_text_clause


LONG = "gentle foaming cleanser for oily skin"
SHORT = "niacinamide serum"


class _FakeDatabase:
    def __init__(self):
        self.last_query = ""
        self.last_values = {}

    async def fetch_all(self, query: str, values=None):
        self.last_query = str(query)
        self.last_values = dict(values or {})
        return []


def test_lean_columns_drops_all_seed_data_recall_arms_from_where():
    clause, values = build_external_seed_text_clause(
        raw_query=LONG, skip_phrase_arms=True, lean_columns=True
    )
    # No seed_data path anywhere in the WHERE — the detoast source is gone.
    assert "seed_data" not in clause
    # Per-token arms retained on the four cheap inline columns.
    assert [k for k in values if k.startswith("q_term_")]
    assert "LOWER(title) LIKE" in clause
    assert "LOWER(domain) LIKE" in clause
    assert "LOWER(destination_url) LIKE" in clause
    assert "LOWER(canonical_url) LIKE" in clause


def test_non_lean_keeps_seed_data_recall_arms():
    """lean_columns=False (default) is the recall-rich path with seed_data arms."""
    clause, _ = build_external_seed_text_clause(raw_query=LONG, skip_phrase_arms=True)
    assert "seed_data" in clause
    assert "retrieval_summary" in clause


def test_lean_columns_default_off_is_recall_rich():
    """Omitting lean_columns must not change the clause (byte-identical guard)."""
    a, _ = build_external_seed_text_clause(raw_query=LONG, skip_phrase_arms=True)
    b, _ = build_external_seed_text_clause(
        raw_query=LONG, skip_phrase_arms=True, lean_columns=False
    )
    assert a == b


@pytest.mark.asyncio
async def test_fetch_applies_lean_at_or_above_threshold():
    from services.external_seed_search import fetch_external_seed_rows

    db = _FakeDatabase()
    result = await fetch_external_seed_rows(
        database=db,
        market=None,
        query=LONG,  # 6 tokens ≥ threshold
        limit=20,
        include_seed_data_text_match=False,
        query_timeout_seconds=0.5,
        fast_multiterm=True,
        lean_where_min_tokens=4,
    )
    assert result.get("lean_where_applied") is True
    # The seed_data recall arms must be absent from the emitted WHERE.
    assert "retrieval_summary" not in db.last_query
    assert "LOWER(title) LIKE" in db.last_query


@pytest.mark.asyncio
async def test_fetch_keeps_full_path_below_threshold():
    from services.external_seed_search import fetch_external_seed_rows

    db = _FakeDatabase()
    result = await fetch_external_seed_rows(
        database=db,
        market=None,
        query=SHORT,  # 2 tokens < threshold
        limit=20,
        include_seed_data_text_match=False,
        query_timeout_seconds=0.5,
        fast_multiterm=True,
        lean_where_min_tokens=4,
    )
    assert result.get("lean_where_applied") is False
    # Below the threshold the recall-rich seed_data arms are retained.
    assert "retrieval_summary" in db.last_query


@pytest.mark.asyncio
async def test_fetch_lean_disabled_when_min_tokens_none_or_zero():
    from services.external_seed_search import fetch_external_seed_rows

    for disabled in (None, 0):
        db = _FakeDatabase()
        result = await fetch_external_seed_rows(
            database=db,
            market=None,
            query=LONG,
            limit=20,
            include_seed_data_text_match=False,
            query_timeout_seconds=0.5,
            fast_multiterm=True,
            lean_where_min_tokens=disabled,
        )
        assert result.get("lean_where_applied") is False
        assert "retrieval_summary" in db.last_query

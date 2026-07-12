"""Fast multi-term external-seed query (SEED_QUERY_FAST_MULTITERM).

A broad multi-term query ("brightening vitamin c serum") ran ~4s because the
whole-phrase + compact OR arms bloated the filter enough that the planner
abandoned the trgm GIN indexes for a parallel seq scan that detoasts seed_data
per row, and the ORDER BY re-extracted seed_data JSON. The fast path drops the
redundant phrase arms (a strict subset of the per-token OR — recall-neutral) and
ranks on cheap indexed columns (no seed_data detoast). Prod-measured 3.9s → 0.44s.
"""
from __future__ import annotations

from services.external_seed_search import (
    build_external_seed_text_clause,
    build_external_seed_prefer_terms_rank_sql,
)

Q = "brightening vitamin c serum"


def test_default_where_is_byte_identical():
    """Flag off (default) must be unchanged: whole-phrase + compact + per-token."""
    clause, values = build_external_seed_text_clause(raw_query=Q)
    assert "q_like" in values and values["q_like"] == f"%{Q}%"          # whole-phrase
    assert "q_compact_like" in values                                   # compact
    assert any(k.startswith("q_term_") for k in values)                 # per-token
    # seed_data recall paths present in the WHERE (recall unchanged).
    assert "seed_data" in clause


def test_fast_where_drops_phrase_arms_keeps_per_token_and_recall_paths():
    clause, values = build_external_seed_text_clause(raw_query=Q, skip_phrase_arms=True)
    # whole-phrase + compact dropped (redundant subset of per-token OR)...
    assert "q_like" not in values and "q_compact_like" not in values
    # ...per-token arms retained (recall-neutral: per-token is a superset)...
    assert [k for k in values if k.startswith("q_term_")]
    # ...and the seed_data recall paths are STILL in the WHERE (recall intact).
    assert "seed_data" in clause


def test_fast_rank_drops_seed_data_detoast_keeps_cheap_columns():
    rank_default, _ = build_external_seed_prefer_terms_rank_sql(prefer_terms=["serum", "vitamin"])
    rank_fast, _ = build_external_seed_prefer_terms_rank_sql(prefer_terms=["serum", "vitamin"], columns_only=True)
    assert "seed_data" in rank_default          # default ranks over seed_data JSON (the detoast)
    assert "seed_data" not in rank_fast         # fast ranks on cheap columns only
    assert "title" in rank_fast and "domain" in rank_fast


def test_single_token_fast_still_recalls():
    # A single-token query keeps a per-token arm even in fast mode (no recall loss).
    _, values = build_external_seed_text_clause(raw_query="acropass", skip_phrase_arms=True)
    assert any(k.startswith("q_term_") for k in values)

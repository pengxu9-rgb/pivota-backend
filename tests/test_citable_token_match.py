"""Tests for the citable token-overlap match in _fetch_citable_canonical_rows.

Captures the compiled SQL + bind params handed to database.fetch_all to assert
the additive token clause is present only when CITABLE_TOKEN_MATCH is on and the
query has >=2 significant tokens; byte-identical (no ctok params/clause) otherwise.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import services.pivot_query_service as pqs


class _CaptureDb:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def fetch_all(self, sql: Any, params: Any = None) -> List[Any]:
        self.calls.append({"sql": str(sql), "params": dict(params or {})})
        return []


def test_tokens_drop_stopwords_short_and_dedup():
    assert pqs._citable_query_tokens("damaged hair treatment") == [
        "damaged",
        "hair",
        "treatment",
    ]
    assert pqs._citable_query_tokens("the best hair oil for you") == ["hair", "oil"]
    assert pqs._citable_query_tokens("hair hair hair") == ["hair"]
    assert pqs._citable_query_tokens("a an of") == []
    assert len(pqs._citable_query_tokens("alpha beta gamma delta epsilon zeta eta")) == 6


@pytest.mark.asyncio
async def test_token_clause_absent_when_flag_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CITABLE_TOKEN_MATCH", raising=False)
    db = _CaptureDb()
    monkeypatch.setattr(pqs, "database", db)
    await pqs._fetch_citable_canonical_rows(
        query="damaged hair treatment", merchant_id=None, limit=20
    )
    call = db.calls[0]
    assert "ctok0" not in call["params"]
    assert "ctok_min" not in call["params"]
    assert ":ctok" not in call["sql"]


@pytest.mark.asyncio
async def test_token_clause_added_when_flag_on_multitoken(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CITABLE_TOKEN_MATCH", "1")
    db = _CaptureDb()
    monkeypatch.setattr(pqs, "database", db)
    await pqs._fetch_citable_canonical_rows(
        query="damaged hair treatment", merchant_id=None, limit=20
    )
    call = db.calls[0]
    # 3 tokens → ctok0..ctok2 + ctok_min = ceil(3/2) = 2
    assert call["params"]["ctok0"] == "%damaged%"
    assert call["params"]["ctok2"] == "%treatment%"
    assert call["params"]["ctok_min"] == 2
    # the overlap clause appears in BOTH the WHERE and the rank
    assert ":ctok0" in call["sql"]
    assert ">= :ctok_min" in call["sql"]
    assert "* 25" in call["sql"]


@pytest.mark.asyncio
async def test_token_clause_skipped_for_single_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CITABLE_TOKEN_MATCH", "1")
    db = _CaptureDb()
    monkeypatch.setattr(pqs, "database", db)
    await pqs._fetch_citable_canonical_rows(query="anuko", merchant_id=None, limit=20)
    call = db.calls[0]
    assert "ctok0" not in call["params"]  # single token → no overlap clause
    assert ":ctok" not in call["sql"]

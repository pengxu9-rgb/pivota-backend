import re

import pytest


class _FakeDatabase:
    def __init__(self):
        self.last_query = ""
        self.last_values = {}

    async def fetch_all(self, query: str, values=None):
        self.last_query = str(query)
        self.last_values = dict(values or {})
        return []


@pytest.mark.asyncio
async def test_fetch_external_seed_rows_uses_lower_like_predicates() -> None:
    from services.external_seed_search import fetch_external_seed_rows

    db = _FakeDatabase()
    result = await fetch_external_seed_rows(
        database=db,
        market="US",
        query="IPSA Toner",
        limit=20,
        offset=0,
        include_seed_data_text_match=False,
        query_timeout_seconds=0.5,
    )

    assert result.get("query_timeout") is False
    assert "LOWER(title) LIKE :q_like" in db.last_query
    assert "LOWER(domain) LIKE :q_like" in db.last_query
    assert "LOWER(destination_url) LIKE :q_like" in db.last_query
    assert "ILIKE" not in db.last_query.upper()
    assert db.last_values.get("q_like") == "%ipsa toner%"


@pytest.mark.asyncio
async def test_fetch_external_seed_rows_optionally_matches_seed_data_text() -> None:
    from services.external_seed_search import fetch_external_seed_rows

    db = _FakeDatabase()
    await fetch_external_seed_rows(
        database=db,
        market="US",
        query="retinol",
        limit=10,
        offset=0,
        include_seed_data_text_match=True,
        query_timeout_seconds=0.5,
    )

    assert "LOWER(CAST(seed_data AS TEXT)) LIKE :q_like" in db.last_query


@pytest.mark.asyncio
async def test_fetch_external_seed_rows_supports_brand_terms_and_rank_ordering() -> None:
    from services.external_seed_search import fetch_external_seed_rows

    db = _FakeDatabase()
    await fetch_external_seed_rows(
        database=db,
        market="US",
        query="fenty beauty",
        limit=24,
        offset=0,
        include_seed_data_text_match=True,
        required_terms=["fenty"],
        prefer_terms=["fenty beauty"],
        scope="brand_strict",
        query_timeout_seconds=0.5,
    )

    assert "AS brand_term_hit" in db.last_query
    assert "ORDER BY brand_term_hit DESC, updated_at DESC, created_at DESC" in db.last_query
    assert "required_0" not in db.last_query
    assert "prefer_0" in db.last_query
    assert "required_term_" not in db.last_query
    assert "prefer_term_" not in db.last_query
    assert db.last_values.get("required_0") is None
    assert db.last_values.get("prefer_0") == "%fenty beauty%"
    placeholders = set(re.findall(r":([a-zA-Z_][a-zA-Z0-9_]*)", db.last_query))
    missing = sorted(name for name in placeholders if name not in db.last_values)
    assert not missing


@pytest.mark.asyncio
async def test_fetch_external_seed_rows_can_disable_required_terms_filter() -> None:
    from services.external_seed_search import fetch_external_seed_rows

    db = _FakeDatabase()
    await fetch_external_seed_rows(
        database=db,
        market="US",
        query="kylie cosmetics",
        limit=24,
        offset=0,
        include_seed_data_text_match=True,
        required_terms=["kylie"],
        prefer_terms=["kylie cosmetics"],
        scope="brand_strict",
        use_required_terms_filter=False,
        query_timeout_seconds=0.5,
    )

    assert "required_0" not in db.last_query
    assert "prefer_0" in db.last_query
    assert db.last_values.get("required_0") is None
    assert db.last_values.get("prefer_0") == "%kylie cosmetics%"

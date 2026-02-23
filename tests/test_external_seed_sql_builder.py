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

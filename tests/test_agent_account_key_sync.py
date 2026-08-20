import pytest


@pytest.mark.asyncio
async def test_sync_new_agent_api_key_prefers_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_account as module
    import db.agents as agents_db

    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")

    executed = []

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "to_regclass('public.api_keys')" in q:
            return {"api_keys_table": "api_keys", "agent_api_keys_table": "agent_api_keys"}
        return None

    async def fake_execute(query: str, values=None):
        executed.append((str(query), values))
        return None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)

    source = await module._sync_new_agent_api_key(
        agent_id="agent_test",
        api_key="ak_live_" + ("a" * 64),
        api_key_hash="hash_sha256",
    )

    assert source == "api_keys"
    assert any("INSERT INTO api_keys" in q for q, _ in executed)
    assert all("INSERT INTO agent_api_keys" not in q for q, _ in executed)


@pytest.mark.asyncio
async def test_sync_new_agent_api_key_uses_agent_api_keys_when_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_account as module
    import db.agents as agents_db

    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")

    executed = []

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "to_regclass('public.api_keys')" in q:
            return {"api_keys_table": None, "agent_api_keys_table": "agent_api_keys"}
        return None

    async def fake_execute(query: str, values=None):
        executed.append((str(query), values))
        return None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)

    source = await module._sync_new_agent_api_key(
        agent_id="agent_test",
        api_key="ak_live_" + ("b" * 64),
        api_key_hash="hash_sha256",
    )

    assert source == "agent_api_keys"
    agent_insert_queries = [q for q, _ in executed if "INSERT INTO agent_api_keys" in q]
    assert len(agent_insert_queries) == 1
    assert "key_name" not in agent_insert_queries[0]
    assert "created_by" in agent_insert_queries[0]
    assert all("INSERT INTO api_keys" not in q for q, _ in executed)


@pytest.mark.asyncio
async def test_sync_new_agent_api_key_falls_back_to_legacy_when_no_key_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_account as module
    import db.agents as agents_db

    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")

    executed = []

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "to_regclass('public.api_keys')" in q:
            return {"api_keys_table": None, "agent_api_keys_table": None}
        return None

    async def fake_execute(query: str, values=None):
        executed.append((str(query), values))
        return None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)

    source = await module._sync_new_agent_api_key(
        agent_id="agent_test",
        api_key="ak_live_" + ("c" * 64),
        api_key_hash="hash_sha256",
    )

    assert source == "legacy"
    assert executed == []

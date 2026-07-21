import hashlib
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_get_agent_by_key_prefers_hash_lookup_without_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.agents as agents_module

    agents_module._AGENT_AUTH_CACHE.clear()
    agents_module._clear_auth_key_table_cache()
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK", False)
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_KEY_TABLE_MODE", "api_keys")

    api_key = "ak_" + ("a" * 64)
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    calls = {"hash": 0, "legacy": 0}

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "JOIN api_keys" in q:
            calls["hash"] += 1
            assert values is not None
            assert values.get("key_hash") == key_hash
            return {
                "agent_id": "agent_hash",
                "agent_name": "Hash First",
                "allowed_merchants": None,
                "is_active": True,
            }
        if "WHERE api_key = :api_key" in q:
            calls["legacy"] += 1
        return None

    monkeypatch.setattr(agents_module.database, "fetch_one", fake_fetch_one)

    metrics = {}
    agent = await agents_module.get_agent_by_key(api_key, metrics_out=metrics)

    assert agent is not None
    assert agent.get("agent_id") == "agent_hash"
    assert calls["hash"] == 1
    assert calls["legacy"] == 0
    assert metrics.get("auth_source") == "api_keys"


@pytest.mark.asyncio
async def test_get_agent_by_key_negative_cache_skips_second_db_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.agents as agents_module

    agents_module._AGENT_AUTH_CACHE.clear()
    agents_module._clear_auth_key_table_cache()
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK", False)
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_KEY_TABLE_MODE", "api_keys")

    fetch_one = AsyncMock(return_value=None)
    monkeypatch.setattr(agents_module.database, "fetch_one", fetch_one)

    api_key = "ak_" + ("b" * 64)
    first = await agents_module.get_agent_by_key(api_key)
    second = await agents_module.get_agent_by_key(api_key)

    assert first is None
    assert second is None
    # second call should hit negative cache
    assert fetch_one.await_count == 1


@pytest.mark.asyncio
async def test_get_agent_by_key_legacy_fallback_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.agents as agents_module

    agents_module._AGENT_AUTH_CACHE.clear()
    agents_module._clear_auth_key_table_cache()
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK", True)
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_KEY_TABLE_MODE", "api_keys")

    api_key = "ak_" + ("c" * 64)
    calls = {"hash": 0, "legacy": 0}

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "JOIN api_keys" in q:
            calls["hash"] += 1
            return None
        if "WHERE api_key = :api_key" in q:
            calls["legacy"] += 1
            return {
                "agent_id": "agent_legacy",
                "agent_name": "Legacy Path",
                "allowed_merchants": None,
                "is_active": True,
            }
        return None

    monkeypatch.setattr(agents_module.database, "fetch_one", fake_fetch_one)

    metrics = {}
    agent = await agents_module.get_agent_by_key(api_key, metrics_out=metrics)

    assert agent is not None
    assert agent.get("agent_id") == "agent_legacy"
    assert calls["hash"] == 1
    assert calls["legacy"] == 1
    assert metrics.get("auth_source") == "legacy_fallback"


@pytest.mark.asyncio
async def test_get_agent_by_key_uses_agent_api_keys_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.agents as agents_module

    agents_module._AGENT_AUTH_CACHE.clear()
    agents_module._clear_auth_key_table_cache()
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK", False)
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")
    # "auto" key-table detection only runs on Postgres; force the flag so the
    # test doesn't depend on the process-wide DATABASE_URL (fetch_one is faked).
    monkeypatch.setattr(agents_module, "IS_POSTGRES", True)

    api_key = "ak_" + ("d" * 64)
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    calls = {"detect": 0, "agent_api_keys": 0, "legacy": 0}

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "to_regclass('public.api_keys')" in q:
            calls["detect"] += 1
            return {"api_keys_table": None, "agent_api_keys_table": "agent_api_keys"}
        if "JOIN agent_api_keys" in q:
            calls["agent_api_keys"] += 1
            assert values is not None
            assert values.get("key_hash_sha256") == key_hash
            return {
                "agent_id": "agent_hash_table",
                "agent_name": "Agent API Keys Path",
                "allowed_merchants": None,
                "is_active": True,
                "_auth_hash_alg": "sha256",
            }
        if "WHERE api_key = :api_key" in q:
            calls["legacy"] += 1
        return None

    monkeypatch.setattr(agents_module.database, "fetch_one", fake_fetch_one)

    metrics = {}
    agent = await agents_module.get_agent_by_key(api_key, metrics_out=metrics)

    assert agent is not None
    assert agent.get("agent_id") == "agent_hash_table"
    assert calls["detect"] == 1
    assert calls["agent_api_keys"] == 1
    assert calls["legacy"] == 0
    assert metrics.get("auth_source") == "agent_api_keys_sha256"


@pytest.mark.asyncio
async def test_get_agent_by_key_auto_legacy_when_no_key_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.agents as agents_module

    agents_module._AGENT_AUTH_CACHE.clear()
    agents_module._clear_auth_key_table_cache()
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK", False)
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")
    # "auto" key-table detection only runs on Postgres; force the flag so the
    # test doesn't depend on the process-wide DATABASE_URL (fetch_one is faked).
    monkeypatch.setattr(agents_module, "IS_POSTGRES", True)

    calls = {"detect": 0, "legacy": 0}
    api_key = "ak_" + ("e" * 64)

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "to_regclass('public.api_keys')" in q:
            calls["detect"] += 1
            return {"api_keys_table": None, "agent_api_keys_table": None}
        if "WHERE api_key = :api_key" in q:
            calls["legacy"] += 1
            return {
                "agent_id": "agent_legacy_auto",
                "agent_name": "Legacy Auto",
                "allowed_merchants": None,
                "is_active": True,
            }
        return None

    monkeypatch.setattr(agents_module.database, "fetch_one", fake_fetch_one)

    metrics = {}
    agent = await agents_module.get_agent_by_key(api_key, metrics_out=metrics)

    assert agent is not None
    assert agent.get("agent_id") == "agent_legacy_auto"
    assert calls["detect"] == 1
    assert calls["legacy"] == 1
    assert metrics.get("auth_source") == "legacy_auto"


@pytest.mark.asyncio
async def test_get_agent_by_key_raises_transient_error_on_pool_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.agents as agents_module

    agents_module._AGENT_AUTH_CACHE.clear()
    agents_module._clear_auth_key_table_cache()
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK", False)
    monkeypatch.setattr(agents_module, "_AGENT_AUTH_KEY_TABLE_MODE", "api_keys")

    async def fake_fetch_one(_query: str, values=None):
        raise RuntimeError("pool is closing")

    monkeypatch.setattr(agents_module.database, "fetch_one", fake_fetch_one)

    metrics = {}
    with pytest.raises(agents_module.AgentAuthLookupTransientError):
        await agents_module.get_agent_by_key("ak_" + ("f" * 64), metrics_out=metrics)

    assert metrics.get("auth_source") == "error_transient"

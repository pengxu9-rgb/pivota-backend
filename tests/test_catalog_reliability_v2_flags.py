from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_catalog_local_fallback_guarded_by_flag_and_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "CATALOG_RELIABILITY_V2_ENABLED", False)
    monkeypatch.setattr(gateway, "CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL", True)
    assert gateway._allow_local_fallback_after_delegate_fail({}) is False

    monkeypatch.setattr(gateway, "CATALOG_RELIABILITY_V2_ENABLED", True)
    monkeypatch.setattr(gateway, "CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL", True)
    monkeypatch.setattr(gateway, "CATALOG_UPSTREAM_V2_LOCAL_FALLBACK_MIN_BUDGET_SECONDS", 0.4)
    assert gateway._allow_local_fallback_after_delegate_fail({}) is True
    assert gateway._allow_local_fallback_after_delegate_fail({"remaining_budget_seconds": 0.8}) is True
    assert gateway._allow_local_fallback_after_delegate_fail({"remaining_budget_seconds": 0.2}) is False


@pytest.mark.asyncio
async def test_catalog_v2_circuit_threshold_open_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "CATALOG_RELIABILITY_V2_ENABLED", True)
    monkeypatch.setattr(gateway, "CATALOG_UPSTREAM_V2_CIRCUIT_FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(gateway, "CATALOG_UPSTREAM_V2_CIRCUIT_WINDOW_SECONDS", 60.0)
    monkeypatch.setattr(gateway, "CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_SECONDS", 30.0)
    monkeypatch.setattr(gateway, "_MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS", [])
    monkeypatch.setattr(gateway, "_MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL", 0.0)

    gateway._multi_upstream_record_outcome(False)
    assert gateway._multi_upstream_circuit_is_open() is False
    gateway._multi_upstream_record_outcome(False)
    assert gateway._multi_upstream_circuit_is_open() is False
    gateway._multi_upstream_record_outcome(False)
    assert gateway._multi_upstream_circuit_is_open() is True


@pytest.mark.asyncio
async def test_catalog_v2_timeout_opens_circuit_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "CATALOG_RELIABILITY_V2_ENABLED", True)
    monkeypatch.setattr(gateway, "CATALOG_UPSTREAM_V2_CIRCUIT_FAILURE_THRESHOLD", 99)
    monkeypatch.setattr(gateway, "CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_ON_TIMEOUT", True)
    monkeypatch.setattr(gateway, "CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_SECONDS", 30.0)
    monkeypatch.setattr(gateway, "_MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS", [])
    monkeypatch.setattr(gateway, "_MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL", 0.0)

    gateway._multi_upstream_record_outcome(False, timeout=True)
    assert gateway._multi_upstream_circuit_is_open() is True


@pytest.mark.asyncio
async def test_catalog_legacy_threshold_unchanged_when_v2_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "CATALOG_RELIABILITY_V2_ENABLED", False)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_UPSTREAM_CIRCUIT_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_UPSTREAM_CIRCUIT_WINDOW_SECONDS", 60.0)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_SECONDS", 20.0)
    monkeypatch.setattr(gateway, "_MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS", [])
    monkeypatch.setattr(gateway, "_MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL", 0.0)

    gateway._multi_upstream_record_outcome(False)
    assert gateway._multi_upstream_circuit_is_open() is True

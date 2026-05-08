"""
Concurrency caps for LLM probes — Phase C prerequisite.

Per feedback_llm_call_multipliers.md: PR #278 took the backend down
when uncapped concurrent probes saturated the upstream LLM provider.
Phase C multi-market would multiply this 3-6x, so caps must be in
place first.

Two semaphores guard `services.agent_center_llm_client.probe`:

  - global cap: total in-flight probes backend-wide
  - per-merchant cap: in-flight probes for a single merchant_id

Tests verify both serialize correctly under contention without
deadlocking, and that monkeypatched cap settings actually take
effect (regression guard against forgetting to call the test reset).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_caps_before_after():
    """Each test starts + ends with fresh semaphore state. Without
    this, a previous test's cap (e.g. =1) persists into the next
    test and produces confusing failures."""
    from services.agent_center_llm_client import _reset_concurrency_caps_for_test
    _reset_concurrency_caps_for_test()
    yield
    _reset_concurrency_caps_for_test()


@pytest.fixture
def configured_api_key(monkeypatch):
    """Set the upstream API key so probe() takes the real-network
    branch (not the local-mock fallback)."""
    from config import settings as settings_module
    monkeypatch.setattr(
        settings_module.settings, "pivota_agent_internal_api_key", "test-key",
    )


def _ok_response(merchant_id: str = "m") -> MagicMock:
    """Synthetic httpx response matching the V1 wire shape — the
    `ok: True, result: {...}` envelope. Without the envelope the
    probe raises AgentCenterLlmClientError("response not ok")."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={
        "ok": True,
        "result": {
            "scan_mode": "open_product_visibility_test",
            "provider": "gemini",
            "runs_count": 1,
            "scores": {"visibility_score": 100},
            "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "raw_runs": [],
        },
    })
    return resp


# -----------------------------------------------------------------
# Global cap
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_cap_serializes_concurrent_calls(
    configured_api_key, monkeypatch,
):
    """When global cap = 2 and 5 probes fire concurrently across
    different merchants, only 2 hit httpx at a time — the other 3
    wait their turn."""
    from config import settings as settings_module
    from services import agent_center_llm_client as llm_client

    # Force a low global cap.
    monkeypatch.setattr(
        settings_module.settings, "llm_probe_global_max_concurrent", 2,
    )
    monkeypatch.setattr(
        settings_module.settings, "llm_probe_per_merchant_max_concurrent", 10,
    )
    llm_client._reset_concurrency_caps_for_test()

    in_flight = 0
    max_in_flight = 0
    enter = asyncio.Event()

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            enter.set()
            # Hold the slot briefly so concurrent waiters pile up.
            await asyncio.sleep(0.05)
            in_flight -= 1
            return _ok_response()

    with patch("httpx.AsyncClient", _Client):
        results = await asyncio.gather(*[
            llm_client.probe(
                scan_mode="open_product_visibility_test",
                scan_target_id=f"t{i}",
                merchant_id=f"merch_{i}",  # different merchants
                store_id=f"s{i}",
                provider="gemini",
                max_runs=1,
            )
            for i in range(5)
        ])

    assert len(results) == 5
    assert all(r["scan_mode"] == "open_product_visibility_test" for r in results)
    # Global cap = 2 → never more than 2 simultaneous.
    assert max_in_flight == 2, f"max_in_flight={max_in_flight}, expected 2"


# -----------------------------------------------------------------
# Per-merchant cap
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_merchant_cap_serializes_one_merchant_only(
    configured_api_key, monkeypatch,
):
    """Per-merchant cap = 1 + global cap = 10: when 5 probes for the
    SAME merchant fire concurrently, only 1 hits httpx at a time. But
    a probe from a DIFFERENT merchant interleaved should NOT be
    blocked — proves per-merchant cap is per-merchant, not global."""
    from config import settings as settings_module
    from services import agent_center_llm_client as llm_client

    monkeypatch.setattr(
        settings_module.settings, "llm_probe_global_max_concurrent", 10,
    )
    monkeypatch.setattr(
        settings_module.settings, "llm_probe_per_merchant_max_concurrent", 1,
    )
    llm_client._reset_concurrency_caps_for_test()

    per_merchant_in_flight: Dict[str, int] = {}
    per_merchant_max: Dict[str, int] = {}

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs):
            body = kwargs.get("json") or {}
            mid = body.get("merchant_id") or "?"
            per_merchant_in_flight[mid] = per_merchant_in_flight.get(mid, 0) + 1
            per_merchant_max[mid] = max(
                per_merchant_max.get(mid, 0), per_merchant_in_flight[mid],
            )
            await asyncio.sleep(0.03)
            per_merchant_in_flight[mid] -= 1
            return _ok_response()

    with patch("httpx.AsyncClient", _Client):
        # Mix: 4 probes for merchant_a, 4 probes for merchant_b
        await asyncio.gather(
            *[
                llm_client.probe(
                    scan_mode="open_product_visibility_test",
                    scan_target_id=f"t_a_{i}",
                    merchant_id="merchant_a",
                    store_id="s",
                    provider="gemini",
                    max_runs=1,
                )
                for i in range(4)
            ],
            *[
                llm_client.probe(
                    scan_mode="open_product_visibility_test",
                    scan_target_id=f"t_b_{i}",
                    merchant_id="merchant_b",
                    store_id="s",
                    provider="gemini",
                    max_runs=1,
                )
                for i in range(4)
            ],
        )

    # Per-merchant cap = 1 → each merchant never exceeds 1 simultaneous
    assert per_merchant_max.get("merchant_a") == 1
    assert per_merchant_max.get("merchant_b") == 1


@pytest.mark.asyncio
async def test_one_merchant_does_not_starve_others(
    configured_api_key, monkeypatch,
):
    """The whole point of per-merchant caps: merchant A flooding the
    backend with audits doesn't block merchant B's audit. Verify
    merchant B's probe completes even while A has many in flight."""
    from config import settings as settings_module
    from services import agent_center_llm_client as llm_client

    monkeypatch.setattr(
        settings_module.settings, "llm_probe_global_max_concurrent", 10,
    )
    monkeypatch.setattr(
        settings_module.settings, "llm_probe_per_merchant_max_concurrent", 2,
    )
    llm_client._reset_concurrency_caps_for_test()

    started: Dict[str, int] = {}

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs):
            body = kwargs.get("json") or {}
            mid = body.get("merchant_id") or "?"
            started[mid] = started.get(mid, 0) + 1
            await asyncio.sleep(0.02)
            return _ok_response()

    with patch("httpx.AsyncClient", _Client):
        # 8 from merchant_a (saturates per-merchant cap=2 → 4 batches),
        # 1 from merchant_b kicked off mid-flight.
        a_tasks = [
            llm_client.probe(
                scan_mode="open_product_visibility_test",
                scan_target_id=f"a_{i}",
                merchant_id="merchant_a",
                store_id="s",
                provider="gemini",
                max_runs=1,
            )
            for i in range(8)
        ]
        b_tasks = [
            llm_client.probe(
                scan_mode="open_product_visibility_test",
                scan_target_id="b_1",
                merchant_id="merchant_b",
                store_id="s",
                provider="gemini",
                max_runs=1,
            )
        ]
        await asyncio.gather(*a_tasks, *b_tasks)

    # Both merchants' probes ran (no starvation)
    assert started.get("merchant_a") == 8
    assert started.get("merchant_b") == 1


# -----------------------------------------------------------------
# Settings respected on init
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_caps_pick_up_settings_at_first_use(monkeypatch):
    """Settings read on first semaphore-init, not at module import.
    Test verifies monkeypatched cap actually applies (regression guard
    for accidentally caching cap at module-load time)."""
    from config import settings as settings_module
    from services.agent_center_llm_client import (
        _get_global_semaphore,
        _reset_concurrency_caps_for_test,
    )

    monkeypatch.setattr(
        settings_module.settings, "llm_probe_global_max_concurrent", 7,
    )
    _reset_concurrency_caps_for_test()
    sem = _get_global_semaphore()
    # asyncio.Semaphore exposes its initial counter via _value (private
    # but stable across CPython versions used here).
    assert sem._value == 7


@pytest.mark.asyncio
async def test_per_merchant_cap_minimum_one(monkeypatch):
    """Defensive: zero or negative caps clamp to 1 (don't deadlock
    every probe by setting cap=0)."""
    from config import settings as settings_module
    from services.agent_center_llm_client import (
        _get_per_merchant_semaphore,
        _reset_concurrency_caps_for_test,
    )

    monkeypatch.setattr(
        settings_module.settings, "llm_probe_per_merchant_max_concurrent", 0,
    )
    _reset_concurrency_caps_for_test()
    sem = await _get_per_merchant_semaphore("m")
    assert sem._value == 1


@pytest.mark.asyncio
async def test_global_cap_minimum_one(monkeypatch):
    from config import settings as settings_module
    from services.agent_center_llm_client import (
        _get_global_semaphore,
        _reset_concurrency_caps_for_test,
    )

    monkeypatch.setattr(
        settings_module.settings, "llm_probe_global_max_concurrent", -5,
    )
    _reset_concurrency_caps_for_test()
    sem = _get_global_semaphore()
    assert sem._value == 1

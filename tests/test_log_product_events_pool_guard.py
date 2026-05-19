"""Regression test for db/agent_product_events.log_product_events.

PR #560 moved log_product_events from an awaited-inline call to a
FastAPI background_task. Background tasks run *after* the response is
sent, which means under TestClient (and any other lifecycle where the
DB pool tears down before the task completes) the underlying
`databases.Database._pool` can be None when execute_many is invoked.
That raised `AssertionError: DatabaseBackend is not running` and broke
test_agent_products_search_cross_merchant_injects_external_seeds_by_domain
+ test_agent_products_search_external_seed_compacts_spaced_query.

The function is documented best-effort. If the pool isn't up, there's
nowhere to write to anyway — so a clean no-op is correct.

This test pins the guard so future refactors don't regress.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import agent_product_events as ape  # noqa: E402


@pytest.mark.asyncio
async def test_returns_clean_when_pool_disconnected(monkeypatch):
    """The PR #560 race: pool is torn down before the background task
    fires. Function must return cleanly without raising."""
    # Force is_connected to False
    monkeypatch.setattr(ape.database, "is_connected", False, raising=False)
    # execute_many must NOT be called
    spy = AsyncMock(side_effect=AssertionError("DatabaseBackend is not running"))
    monkeypatch.setattr(ape.database, "execute_many", spy)

    rows = [{"event_type": "impression", "merchant_id": "m1"}]
    # Should NOT raise
    await ape.log_product_events(rows)
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_returns_clean_when_execute_many_raises(monkeypatch):
    """Belt-and-suspenders: if is_connected lies (returns True) but
    execute_many still raises (e.g. mid-teardown race), the function
    must swallow and return — it's best-effort."""
    monkeypatch.setattr(ape.database, "is_connected", True, raising=False)

    async def _raise(*_a, **_kw):
        raise AssertionError("DatabaseBackend is not running")

    monkeypatch.setattr(ape.database, "execute_many", _raise)

    rows = [{"event_type": "impression", "merchant_id": "m1"}]
    await ape.log_product_events(rows)  # must not raise


@pytest.mark.asyncio
async def test_writes_when_pool_alive(monkeypatch):
    """Sanity: when the pool is up, execute_many gets called with the
    cleaned rows. Guard doesn't change happy-path behavior."""
    monkeypatch.setattr(ape.database, "is_connected", True, raising=False)
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(ape.database, "execute_many", spy)

    rows = [
        {"event_type": "impression", "merchant_id": "m1"},
        {"event_type": "impression", "merchant_id": "m2"},
    ]
    await ape.log_product_events(rows)
    assert spy.await_count == 1
    # Second positional arg is the rows list
    assert spy.call_args.args[1] == rows


@pytest.mark.asyncio
async def test_empty_rows_short_circuit(monkeypatch):
    """Pre-existing behavior: empty input → no call, no error."""
    spy = AsyncMock()
    monkeypatch.setattr(ape.database, "execute_many", spy)
    await ape.log_product_events([])
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_rows_without_event_type_filtered(monkeypatch):
    """Pre-existing behavior: rows missing event_type get dropped; if all
    rows are filtered, no DB call happens."""
    monkeypatch.setattr(ape.database, "is_connected", True, raising=False)
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(ape.database, "execute_many", spy)
    await ape.log_product_events([{"merchant_id": "m1"}, {"merchant_id": "m2"}])
    spy.assert_not_called()

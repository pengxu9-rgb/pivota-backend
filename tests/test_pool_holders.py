"""The holder registry must name WHERE a still-checked-out connection was taken.

2026-09-16: four web instances x 13 connections, all plain `idle` for up to 12h, and
/__pool_health's `tasks_by_frame` had nothing to say because no task was parked on them. The only
recoverable fact about a leaked connection is its acquisition site. These tests pin that the site
is the APPLICATION caller (not the pool plumbing it passed through), that a released connection
leaves no record, and that an overdue one is reported once — not once per request.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest

from db.pool_holders import HolderRegistry, acquisition_site

THIS_FILE = os.path.basename(__file__)


async def _query_helper() -> tuple:
    # Stands in for the pool layer's caller chain: the site is taken while this coroutine RUNS.
    return acquisition_site(start_depth=1)


async def _route_handler() -> tuple:
    return await _query_helper()


_HELPER_LINE = _query_helper.__code__.co_firstlineno + 2
_ROUTE_LINE = _route_handler.__code__.co_firstlineno + 1


def test_the_site_walks_through_awaiting_coroutines_to_the_route() -> None:
    """A running coroutine's f_back chain reaches its awaiters. If it did not, every leak would be
    attributed to the innermost helper and the route that caused it would stay anonymous."""
    site = asyncio.run(_route_handler())
    assert site[0] == f"{THIS_FILE}:{_HELPER_LINE}"
    assert f"{THIS_FILE}:{_ROUTE_LINE}" in site


def test_pool_plumbing_is_not_reported_as_the_site() -> None:
    """db/database.py and installed libraries sit on every checkout's stack; naming them would make
    every leak look identical."""
    site = asyncio.run(_route_handler())
    assert not any(frame.startswith(("database.py:", "pool_holders.py:")) for frame in site)
    assert all(":" in frame and os.sep not in frame for frame in site), "FILE:LINE only, no paths"


def test_a_released_checkout_leaves_nothing_behind() -> None:
    registry = HolderRegistry()
    conn = object()
    registry.record(conn, ("route.py:10",), now=100.0)
    assert registry.snapshot(now=101.0)["checked_out"] == 1
    registry.forget(conn)
    assert registry.snapshot(now=101.0) == {"checked_out": 0, "by_site": []}


def test_snapshot_groups_by_site_oldest_first() -> None:
    registry = HolderRegistry()
    registry.record(object(), ("seed_search.py:528", "agent_api.py:4139"), now=0.0)
    registry.record(object(), ("seed_search.py:528", "agent_api.py:4139"), now=50.0)
    registry.record(object(), ("offers.py:5760",), now=90.0)
    snap = registry.snapshot(now=100.0)
    assert snap["checked_out"] == 3
    assert snap["by_site"] == [
        {"site": "seed_search.py:528 <- agent_api.py:4139", "count": 2, "oldest_seconds": 100.0},
        {"site": "offers.py:5760", "count": 1, "oldest_seconds": 10.0},
    ]


def test_forgetting_an_unknown_connection_is_harmless() -> None:
    HolderRegistry().forget(object())


def test_an_overdue_checkout_is_logged_once_with_its_site(caplog: pytest.LogCaptureFixture) -> None:
    """Once, not per checkout: the warning is evaluated on every acquire, and a leaked connection
    stays leaked — repeating it would bury the one line that matters under thousands."""
    registry = HolderRegistry()
    registry.record(object(), ("external_seed_search.py:528", "agent_api.py:4139"), now=0.0)
    registry.record(object(), ("fresh.py:1",), now=290.0)
    with caplog.at_level(logging.WARNING, logger="db.pool_holders"):
        assert registry.warn_overdue(300.0, now=301.0) == 1
        assert registry.warn_overdue(300.0, now=900.0) == 1  # the second one only, now overdue
        assert registry.warn_overdue(300.0, now=5000.0) == 0
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "external_seed_search.py:528 <- agent_api.py:4139" in messages[0]
    assert "held 301s" in messages[0]


def test_below_the_threshold_nothing_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    registry = HolderRegistry()
    registry.record(object(), ("route.py:1",), now=0.0)
    with caplog.at_level(logging.WARNING, logger="db.pool_holders"):
        assert registry.warn_overdue(300.0, now=299.0) == 0
    assert not caplog.records


def test_pool_health_serves_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    import db.pool_holders as holders
    from routes.pool_health import _holders

    registry = HolderRegistry()
    registry.record(object(), ("agent_api.py:4139",))
    monkeypatch.setattr(holders, "REGISTRY", registry)
    out = _holders()
    assert out["tracking"] is True
    assert out["checked_out"] == 1
    assert out["by_site"][0]["site"] == "agent_api.py:4139"


def test_pool_health_says_when_tracking_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import db.database as dbmod
    from routes.pool_health import _holders

    monkeypatch.setattr(dbmod, "DB_POOL_HOLDER_TRACKING", False)
    assert _holders() == {"tracking": False}

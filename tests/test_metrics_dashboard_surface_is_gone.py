"""The unused metrics dashboard surface must stay deleted.

SEVEN endpoints (five REST — `/api/ws/status` is one of them — and two
WebSocket) served an in-memory metrics store to anonymous callers. They came
from a bulk scaffold commit and were never developed: `routes/dashboard_routes.py`
had TWO commits before this one, `bc4d01e02` (a bulk "add all missing backend
modules" dump) and `eab05fc8a` (#1950), of which one was a security fix.

Nothing called them. Cloud Run request logs for the prod `web` service show no
organic requests to any of the seven — over the ~10 days of logs that EXIST,
which is the honest window: the oldest entry for that service is 2026-08-20,
the GCP cutover, and Railway before it is decommissioned. The only hits are a
handful of `curl` probes from this investigation on 2026-08-30.

They cost a Cloud Run concurrency-slot DoS (#1950), an unauthenticated POST that
wiped the store, and an anonymous read of raw enrichment events.

This file is a ratchet, not a unit test: it fails if any of them comes back
without someone deliberately changing it here.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

REMOVED_MODULES = [
    "routes.dashboard_routes",
    "routes.simple_ws_routes",
    "realtime.ws_manager",
    "realtime.ws_guard",
]

REMOVED_PATHS = [
    "/api/snapshot",
    "/api/recent-events",
    "/api/connection-stats",
    "/api/reset-metrics",
    "/api/ws/status",
    "/api/ws/simple",
    "/api/ws/metrics",
]


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_the_removed_modules_are_not_importable(module):
    """Pinned to the module that is actually missing.

    A bare `pytest.raises(ModuleNotFoundError)` passes for the WRONG reason on a
    partial revert: restore `routes/dashboard_routes.py` alone and it still
    raises, because its own `from realtime.ws_manager import ...` fails — so the
    file is back on disk with its routes defined and this test stays green.
    Checking `.name` makes the assertion about THIS module.
    """
    with pytest.raises(ModuleNotFoundError) as excinfo:
        importlib.import_module(module)
    assert excinfo.value.name == module, (
        f"{module} imported far enough to fail on {excinfo.value.name} — it is "
        "back on disk"
    )


@pytest.mark.parametrize("path", REMOVED_PATHS)
def test_no_removed_route_is_mounted_on_the_app(path):
    """Asserted against the REAL app, not a test-local router.

    Import-level absence is not enough on its own: a route could be
    re-registered from somewhere else entirely and this is the only check that
    would notice.
    """
    import main

    assert path not in {getattr(r, "path", None) for r in main.app.routes}


def test_no_websocket_route_survives_anywhere():
    """The concurrency-slot exposure was a property of holding a WebSocket open,
    not of these two routes specifically — a WS handler occupies a Cloud Run
    slot for the life of the socket while `--concurrency 20` bounds the
    instance. A new one needs its own bound; failing here is the prompt.
    """
    from starlette.routing import WebSocketRoute

    import main

    survivors = [
        r.path for r in main.app.routes if isinstance(r, WebSocketRoute)
    ]
    assert survivors == [], f"a WebSocket route needs a concurrency bound: {survivors}"


# --- the one surviving reader of the store ----------------------------------

def test_the_snapshot_still_refuses_to_guess_a_role():
    """`get_snapshot` kept a `role="admin"` default and its only remaining
    caller took it, so a caller that had not decided whose data this is
    received everyone's."""
    from realtime import metrics_store

    with pytest.raises(TypeError):
        metrics_store.snapshot()
    with pytest.raises(TypeError):
        metrics_store.get_metrics_store().get_snapshot()


@pytest.mark.parametrize("role,entity", [("", None), ("nonsense", None), ("merchant", None)])
def test_an_unscopeable_caller_sees_nothing(role, entity):
    """"Cannot be scoped" must not mean "sees everything" — which is what the
    old if/elif fall-through did when a caller had no entity_id."""
    from realtime import metrics_store

    store = metrics_store.get_metrics_store()
    store.reset_metrics()
    store.record_event(
        {"type": "p", "status": "succeeded", "psp": "stripe",
         "agent": "agent_a", "merchant": "merchant_a", "latency_ms": 5}
    )

    body = metrics_store.snapshot(role=role, entity_id=entity)
    assert body["psp"] == body["agent"] == body["merchant"] == {}
    assert body["summary"] == {"total": 0, "success": 0, "fail": 0, "retries": 0}
    assert body["total_events"] == 0


def test_a_scoped_caller_sees_only_its_own_dimension():
    from realtime import metrics_store

    store = metrics_store.get_metrics_store()
    store.reset_metrics()
    for suffix in ("a", "b"):
        store.record_event(
            {"type": "p", "status": "succeeded", "psp": "stripe",
             "agent": f"agent_{suffix}", "merchant": f"merchant_{suffix}", "latency_ms": 5}
        )

    body = metrics_store.snapshot(role="merchant", entity_id="merchant_a")
    assert set(body["merchant"]) == {"merchant_a"}
    assert body["agent"] == {}, "the other dimension was left whole"
    assert body["psp"] == {}
    assert body["total_events"] == 1, "platform event count leaked past the filter"

    full = metrics_store.snapshot(role="admin")
    assert set(full["merchant"]) == {"merchant_a", "merchant_b"}
    assert full["total_events"] == 2


def test_recording_an_event_no_longer_reaches_a_websocket():
    """event_publisher's WS branch is gone along with the manager it called.
    Recording must still work — it is what /api/operations/dashboard-summary
    reads — and must not reach for a module that no longer exists.
    """
    import utils.event_publisher as ep

    assert not hasattr(ep, "publish_event_to_ws")
    assert ep.record_event is not None

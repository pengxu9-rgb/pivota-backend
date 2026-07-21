import os
import sys
from collections import defaultdict
from pathlib import Path

from starlette.routing import Match


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app  # noqa: E402


def test_runtime_routes_have_unique_method_path_pairs():
    seen = defaultdict(list)

    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or []
        endpoint = getattr(route, "endpoint", None)

        for method in methods:
            if method in {"HEAD", "OPTIONS"} or not path:
                continue
            seen[(method, path)].append(
                f"{getattr(endpoint, '__module__', '?')}.{getattr(endpoint, '__name__', '?')}"
            )

    duplicates = {
        (method, path): owners
        for (method, path), owners in seen.items()
        if len(owners) > 1
    }

    assert not duplicates, f"duplicate mounted routes: {duplicates}"


def test_agent_order_events_route_precedes_dynamic_order_route():
    scope = {"type": "http", "method": "GET", "path": "/agent/v1/orders/events"}
    matches = []

    for route in app.routes:
        match, _ = route.matches(scope) if hasattr(route, "matches") else (Match.NONE, {})
        if match == Match.FULL:
            matches.append(route)

    assert matches, "GET /agent/v1/orders/events should match a registered route"
    endpoint = getattr(matches[0], "endpoint", None)
    assert getattr(endpoint, "__name__", None) == "agent_list_order_events"


def test_merchant_payout_routes_are_not_mounted():
    payout_routes = [
        getattr(route, "path", "")
        for route in app.routes
        if str(getattr(route, "path", "")).startswith("/merchant/payouts")
    ]

    assert payout_routes == []

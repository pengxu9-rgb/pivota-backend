from __future__ import annotations

import json
from datetime import datetime, timezone

from readiness.order_sync import _json_safe


def test_json_safe_normalizes_nested_datetimes() -> None:
    observed_at = datetime(2026, 3, 17, 14, 7, 17, 483796, tzinfo=timezone.utc)
    payload = {
        "merchant_connection": {
            "psp": {
                "provider": "adyen",
                "connected_at": observed_at,
            }
        },
        "events": [
            {"event_type": "checkout_created", "at": observed_at},
        ],
    }

    normalized = _json_safe(payload)

    assert normalized["merchant_connection"]["psp"]["connected_at"] == "2026-03-17T14:07:17Z"
    assert normalized["events"][0]["at"] == "2026-03-17T14:07:17Z"
    json.dumps(normalized)

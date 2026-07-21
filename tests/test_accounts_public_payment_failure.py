from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from routes.accounts_orders_api import (  # noqa: E402
    _build_resumable_payment_payload,
    _build_tracking_events,
    _compute_permissions,
    _derive_order_status,
    _map_fulfillment_status,
    _map_payment_status,
)


class _Principal:
    primary_role = "customer"
    email_normalized = "buyer@example.com"


def test_payment_failed_maps_to_terminal_public_status() -> None:
    payment_status = _map_payment_status("payment_failed")
    fulfillment_status = _map_fulfillment_status(None)

    assert payment_status == "payment_failed"
    assert _derive_order_status(payment_status, fulfillment_status, cancelled=False, refunded=False) == "payment_failed"


def test_payment_failed_permissions_disable_pay() -> None:
    permissions = _compute_permissions(
        {
            "payment_status": "payment_failed",
            "fulfillment_status": None,
        },
        _Principal(),
    )

    assert permissions["can_pay"] is False
    assert permissions["can_cancel"] is False


def test_tracking_events_include_payment_failed_terminal_event() -> None:
    events = _build_tracking_events(
        {
            "payment_status": "payment_failed",
            "status": "payment_failed",
            "created_at": datetime(2026, 4, 22, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 4, 22, 1, tzinfo=timezone.utc),
        }
    )

    assert [event["status"] for event in events] == ["ordered", "payment_failed"]


def test_public_order_resume_suppresses_payment_current_for_failed_orders() -> None:
    current = asyncio.run(
        _build_resumable_payment_payload(
            {
                "payment_status": "payment_failed",
                "psp_used": "adyen",
                "payment_intent_id": "adyen_session_123",
                "client_secret": "session_data_123",
            },
            payment_status="payment_failed",
        )
    )

    assert current is None

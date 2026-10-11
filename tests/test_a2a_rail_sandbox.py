"""Live contract check of the A2A rail client against the provider's SANDBOX. Opt-in, read-only.

Skipped unless A2A_RAIL_SANDBOX=1. The sandbox origin comes from A2A_RAIL_SANDBOX_BASE_URL (it must
be on the shipped allowlist). The key is read from the file A2A_RAIL_SANDBOX_KEY_FILE, from the line
named by A2A_RAIL_SANDBOX_KEY_NAME (default `A2A_RAIL_API_KEY`), and never printed. Only
read-only actions are sent, so the test can run any number of times without creating anything.
Member and expectations come from A2A_RAIL_SANDBOX_MEMBER (default `pivota-sandbox-buyer-1`).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.payment_orchestration.a2a_rail as a2a  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.getenv("A2A_RAIL_SANDBOX") != "1", reason="live rail sandbox check; set A2A_RAIL_SANDBOX=1"
)


def _env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        pytest.fail(f"{name} is not set")
    return value


def _key() -> str:
    name = os.getenv("A2A_RAIL_SANDBOX_KEY_NAME") or "A2A_RAIL_API_KEY"
    for line in Path(_env("A2A_RAIL_SANDBOX_KEY_FILE")).expanduser().read_text().splitlines():
        if line.strip().startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip("'\"")
    pytest.fail(f"no {name} line in the sandbox key file")


async def test_the_live_card_list_parses_into_the_documented_card():
    client = a2a.A2ARailClient(_env("A2A_RAIL_SANDBOX_BASE_URL"), _key())
    member = os.getenv("A2A_RAIL_SANDBOX_MEMBER") or "pivota-sandbox-buyer-1"
    result = await client.send_action(
        member_id=member, context_id=client.context_for(f"contract_{uuid.uuid4().hex[:12]}"),
        component="payment.method-view", action="submit", payload={"list_statuses": "ACTIVE"},
    )
    assert result.ok, (result.kind, result.http_status, result.error_code)
    card = result.card("payment.method-list")
    assert card is not None, [c.component for c in result.cards]
    assert set(card.action_ids()) >= {"select-method", "add-method"}
    methods = card.data.get("methods")
    assert isinstance(methods, list)
    for method in methods:
        # The two fields the payment contract guarantees, plus what an ACTIVE card shows.
        assert {"id", "payment_brand", "status"} <= set(method)
        assert method["status"] == "ACTIVE"
        assert len(str(method.get("last4") or "")) == 4
    assert result.credential_fields_stripped == 0

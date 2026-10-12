"""Live check of the A2A rail worker's first step against the provider's SANDBOX. Opt-in, read-only.

Skipped unless A2A_RAIL_SANDBOX=1 (same variables as tests/test_a2a_rail_sandbox.py). The purchase is
pointed at the sandbox member (A2A_RAIL_SANDBOX_MEMBER), which already holds an ACTIVE card, so the one
step taken is a card-list READ and a local transition to `quoting`: nothing is written on the rail.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.a2a_rail_ledger as ledger  # noqa: E402
from services.payment_orchestration import a2a_rail, a2a_rail_worker as worker  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.getenv("A2A_RAIL_SANDBOX") != "1" or IS_POSTGRES, reason="live rail sandbox check; set A2A_RAIL_SANDBOX=1"
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


async def test_the_first_step_reads_the_live_card_list_and_moves_to_quoting():
    if not database.is_connected:
        await database.connect()
    await ensure_required_schema_light()
    member = os.getenv("A2A_RAIL_SANDBOX_MEMBER") or "pivota-sandbox-buyer-1"
    row = await ledger.create_purchase(
        agent_id="agent_sandbox", agent_user_ref_hash="s" * 64, merchant_domain="brand.example",
        checkout_url="https://brand.example/cart/1:1", currency="USD", our_price_minor=100,
        buyer_email=None)  # no email: if the step tried to bind a card it would refuse, not write
    await database.execute("UPDATE a2a_rail_purchases SET member_id = :m WHERE id = :i", {"m": member, "i": row["id"]})
    [claimed] = [r for r in await ledger.claim_due_purchases("sandbox-w", limit=50) if r["id"] == row["id"]]
    client = a2a_rail.A2ARailClient(_env("A2A_RAIL_SANDBOX_BASE_URL"), _key())
    assert await worker.step(client, claimed, "sandbox-w") == "method_ready"
    after = await ledger.get_purchase_internal(row["id"])
    assert after["state"] == "quoting" and after["payment_method_id"] and after["pending_write"] is None



@pytest.mark.skipif(os.getenv("A2A_RAIL_SANDBOX_WRITES") != "1",
                    reason="writes test data on the sandbox; set A2A_RAIL_SANDBOX_WRITES=1")
async def test_the_card_list_filter_the_worker_sends_lists_a_pending_card():
    """Pins the measurement the binding step depends on: under the worker's own `list_statuses`, a
    card whose binding has just started is listed as PENDING. Starts one binding (no money; the page
    is never opened and lapses)."""
    import uuid

    member = os.getenv("A2A_RAIL_SANDBOX_MEMBER") or "pivota-sandbox-buyer-1"
    client = a2a_rail.A2ARailClient(_env("A2A_RAIL_SANDBOX_BASE_URL"), _key())
    added = await client.send_action(
        member_id=member, context_id=client.context_for(member, f"contract_{uuid.uuid4().hex[:12]}"),
        component=worker.ADD_METHOD[0], action=worker.ADD_METHOD[1],
        payload={"user_email": _env("A2A_RAIL_SANDBOX_EMAIL")})
    card = added.card("payment.method-add-required-card")
    assert card is not None, added.kind
    new_id = card.data["paymentMethodId"]
    listed = await client.send_action(
        member_id=member, context_id=client.context_for(member, f"contract_{uuid.uuid4().hex[:12]}"),
        component=worker.VIEW_METHODS[0], action=worker.VIEW_METHODS[1],
        payload={"list_statuses": worker._LIST_ALL})
    methods = listed.card("payment.method-list").data["methods"]
    assert any(m.get("id") == new_id and m.get("status") == "PENDING" for m in methods)

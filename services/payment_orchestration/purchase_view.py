"""The unified purchase view: the ONE body every reader of a rail purchase gets.

`GET /agent/v2/commerce/purchases/{id}` returns it, and the `purchase.*` webhook events carry it, so
an agent sees the same shape whether it polls or is told. It is built from the rail route's own
owner-scoped, redacted view (Reap: routes/agent_commerce_reap._owner_view; the A2A token rail:
services/payment_orchestration/a2a_purchase_view), never from a raw row,
so it carries no PII and only a hosted URL the rail route has already vetted.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from services.payment_orchestration.rails import BUYER_ACTION_KIND, unified_state


def unified_body(parent: Mapping[str, Any], detail: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The rail-neutral shape. None when the rail state has no unified mapping, which a caller must
    treat as "say nothing" rather than tell an agent something made up about the purchase."""
    rail_state = detail.get("state")
    state = unified_state(parent["rail"], rail_state)
    if state is None:
        return None
    body: Dict[str, Any] = {
        "purchase_id": parent["id"],
        "rail": parent["rail"],
        "executor": parent["executor"],
        "rail_purchase_id": parent["rail_purchase_id"],
        "state": state,
        "rail_state": rail_state,
        "created_at": parent.get("created_at"),
        "totals": detail.get("totals"),
        "poll_after_seconds": detail.get("poll_after_seconds"),
    }
    # The rail route has already vetted the hosted URL against its allowlist and its deadline and
    # dropped it when it fails; an absent `hosted_url` therefore means "nowhere to send the buyer".
    kind = BUYER_ACTION_KIND.get(state)
    if kind and detail.get("hosted_url"):
        body["next_action"] = {
            "type": "open_url",
            "kind": kind,
            "url": detail["hosted_url"],
            "expires_at": detail.get("approval_deadline") or detail.get("hosted_url_expires_at"),
        }
    if state == "completed" and detail.get("order_reference"):
        body["order_reference"] = detail["order_reference"]
    body["detail"] = dict(detail)
    return body


async def unified_purchase(
    parent: Mapping[str, Any], *, agent_id: str, agent_user_ref_hash: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(body, None), or (None, "not_found" | "state_unmapped"). Owner-scoped: the rail read
    conjuncts on the agent AND the buyer."""
    if parent["rail"] == "a2a":
        from services.payment_orchestration import a2a_purchase_view

        detail = await a2a_purchase_view.owner_view(
            purchase_id=str(parent["rail_purchase_id"]), agent_id=agent_id,
            agent_user_ref_hash=agent_user_ref_hash,
        )
    elif parent["rail"] == "reap":
        # Imported here: the rail route module imports the web stack, and this module is also used
        # by the worker's event job.
        import routes.agent_commerce_reap as reap_routes

        detail = await reap_routes._owner_view(
            purchase_id=str(parent["rail_purchase_id"]), agent_id=agent_id,
            agent_user_ref_hash=agent_user_ref_hash,
        )
    else:
        return None, "state_unmapped"
    if not detail:
        return None, "not_found"
    body = unified_body(parent, detail)
    if body is None:
        return None, "state_unmapped"
    return body, None

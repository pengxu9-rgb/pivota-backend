"""ADR-030 D11's run condition for a payment partner's checkout agent, read on the create path.

A partner's checkout agent may run on a merchant only while BOTH hold:

  * the merchant publishes an agentic checkout protocol: a fresh stored fact, and no negative since
    (db/merchant_agent_checkout_facts.meets_run_condition, migration 294);
  * no challenge or block on THAT rail is open for the merchant
    (db/partner_agent_merchant_blocks.is_clear, migration 295). An event on one rail says nothing
    about the other.

Either record unreadable (a missing table, a failed or slow read) is "not met": both readers answer
False then, and this module passes that on. It never raises.

DARK BY DEFAULT, PER RAIL. `PARTNER_AGENT_RUN_CONDITION_RAILS` (services/payment_orchestration/router.py)
names the rails it applies to. A rail not on it is never read for, and behaves exactly as before.
Only the web service reads it; the token rail worker does not (yet).

WHERE IT RUNS, AND WHAT A FAILURE ANSWERS:
  * the token rail, in the rail-neutral create door, BEFORE the router chooses: a failure is the router's
    drop reason `agent_run_condition_unmet`, so a request with no rail choice goes to Reap (and meets
    Reap's own filter there), and an explicit `a2a` choice is refused like every other token-rail drop;
  * Reap, inside the Reap route's create, after the key lookup, the pilot list and the merchant checks,
    and before any buyer row or purchase is written: a failure is the existing refusal
    `merchant_not_eligible` (409), the same answer as the Tier B verdict and the variant-lane allowlist
    beside it. The specific cause goes to a WARNING log line, never to the caller: a new code would be
    read by the gateway as an unknown outcome, and would tell an agent which merchants are cleared.

THE KEY is the host the request names (`merchant_host`, lowercased), the spelling the census stores its
fact under; both readers fold it themselves.

CREATE ONLY. A replay of an existing purchase is never refused by this, purchases already open are not
re-checked, and the Reap resume door does not call it. The worker's own re-check before it dispatches,
and its challenge write, are a later change.

READ ORDER AND TIMING. Read outside any transaction and before the request fans out, as both readers'
docstrings require. The two reads run one after the other (each is bounded at 2 s), never together: the
reads' timeouts cancel a query in flight, and with databases 0.7 a request's tasks can share a connection.
"""

from __future__ import annotations

import logging
from typing import Optional

import db.merchant_agent_checkout_facts as checkout_facts
import db.partner_agent_merchant_blocks as agent_blocks
from services.payment_orchestration import router as rail_router

logger = logging.getLogger(__name__)

#: Why the condition is not met, for the log only.
NO_CHECKOUT_FACT = "no_checkout_fact"
OPEN_BLOCK = "open_block_or_unreadable"


async def unmet_cause(rail: str, merchant_host: str) -> Optional[str]:
    """None when this rail's agent may run on this merchant, else the cause. Reads both records
    whatever the flag says (the callers check `rail_router.run_condition_enforced` first). Never raises.

    The fact first: it is the broader condition (no protocol means no partner may run), and a
    merchant without one is refused without asking about challenges."""
    try:
        if not await checkout_facts.meets_run_condition(merchant_host):
            return NO_CHECKOUT_FACT
        if not await agent_blocks.is_clear(rail, merchant_host):
            return OPEN_BLOCK
    except Exception as exc:  # noqa: BLE001 - both readers never raise; this is the backstop
        logger.warning("agent_run_condition: read failed rail=%s error_type=%s", rail, type(exc).__name__)
        return NO_CHECKOUT_FACT
    return None


async def agent_may_run(rail: str, merchant_host: str, *, agent_id: str) -> bool:
    """True when the rail's filter is off, or the condition is met. A failure is logged with its cause."""
    if not rail_router.run_condition_enforced(rail):
        return True
    cause = await unmet_cause(rail, merchant_host)
    if cause is None:
        return True
    # WARNING, not INFO: prod drops module-logger INFO, and an operator needs to see why.
    logger.warning(
        "agent_run_condition: not met rail=%s merchant=%s agent=%s cause=%s", rail, merchant_host, agent_id, cause)
    return False

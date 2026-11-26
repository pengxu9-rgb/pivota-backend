"""ADR-030 D11's run condition for a payment partner's checkout agent, read on the create path.

A partner's checkout agent may run on a merchant only while BOTH hold:

  * the merchant publishes an agentic checkout protocol: a fresh stored fact, and no negative since
    (db/merchant_agent_checkout_facts.run_condition_answer, migration 294);
  * no challenge or block on THAT rail is open for the merchant
    (db/partner_agent_merchant_blocks.clear_answer, migration 295). An event on one rail says nothing
    about the other.

Either record unreadable (a missing table, a failed or slow read) is "not met" on the create path.
The cause says which it was: `no_checkout_fact` or `open_block` for a DEFINITE no from a record, and
`record_unreadable` when neither record said no but one could not be read. A definite no wins: an open
block beside an unreadable fact is `open_block`. The token rail worker's relay waits out
`record_unreadable` instead of ending the purchase (`enforced_unmet_cause`). This module never raises.

DARK BY DEFAULT, PER RAIL. `PARTNER_AGENT_RUN_CONDITION_RAILS` (services/payment_orchestration/router.py)
names the rails it applies to. A rail not on it is never read for, and behaves exactly as before.
Both the web service and the token rail worker read it, each in its own environment.

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
re-checked here, and the Reap resume door does not call it. The token rail worker asks again
(`enforced_unmet_cause`) right before each send to the merchant, and records a challenge it meets
(services/payment_orchestration/a2a_rail_worker, "ADR-030 D11's run condition"). Reap's purchases are
not re-checked after create, and a Reap challenge is recorded by an operator.

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

#: Why the condition is not met: for the log, and for the token rail worker's relay, which waits out
#: RECORD_UNREADABLE. Never sent to a caller.
NO_CHECKOUT_FACT = "no_checkout_fact"
OPEN_BLOCK = "open_block"
RECORD_UNREADABLE = "record_unreadable"


async def unmet_cause(rail: str, merchant_host: str) -> Optional[str]:
    """None when this rail's agent may run on this merchant, else the cause. Reads both records
    whatever the flag says (the callers check `rail_router.run_condition_enforced` first). Never raises.

    The fact first: it is the broader condition (no protocol means no partner may run), and a
    merchant with a definite no there is refused without asking about challenges. A fact that
    cannot be read is not a no: the block record is read too, and its definite no wins."""
    reader = "checkout_fact"
    try:
        fact = await checkout_facts.run_condition_answer(merchant_host)
        if fact is False:
            return NO_CHECKOUT_FACT
        reader = "merchant_block"
        clear = await agent_blocks.clear_answer(rail, merchant_host)
    except Exception as exc:  # noqa: BLE001 - both readers never raise; this is the backstop
        # A reader that broke its promise: named, since the worker's relay now waits this out (to the page's
        # deadline) instead of failing the purchase.
        logger.warning("agent_run_condition: read failed rail=%s reader=%s error_type=%s",
                       rail, reader, type(exc).__name__)
        return RECORD_UNREADABLE
    if clear is False:
        return OPEN_BLOCK
    if fact is not True or clear is not True:
        return RECORD_UNREADABLE
    return None


async def enforced_unmet_cause(rail: str, merchant_host: str, *, agent_id: str) -> Optional[str]:
    """None when the rail's filter is off (nothing is read) or the condition is met, else the cause,
    logged at WARNING. For a caller that treats RECORD_UNREADABLE apart (the token rail worker's relay);
    every other caller asks `agent_may_run`."""
    if not rail_router.run_condition_enforced(rail):
        return None
    cause = await unmet_cause(rail, merchant_host)
    if cause is not None:
        # WARNING, not INFO: prod drops module-logger INFO, and an operator needs to see why.
        logger.warning(
            "agent_run_condition: not met rail=%s merchant=%s agent=%s cause=%s", rail, merchant_host, agent_id, cause)
    return cause


async def agent_may_run(rail: str, merchant_host: str, *, agent_id: str) -> bool:
    """True when the rail's filter is off, or the condition is met. A failure is logged with its cause;
    an unreadable record is a failure here."""
    return await enforced_unmet_cause(rail, merchant_host, agent_id=agent_id) is None

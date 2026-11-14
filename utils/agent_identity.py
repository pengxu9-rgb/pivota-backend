"""Who an agent id stands for when the question is "is this the same caller?".

One rule, used by everything that must agree on it: quote ownership (services/quote_service.py),
the one-order-per-quote claim and the order-create replay lookup (db/orders.py). If two of them
disagreed, a caller that ownership lets use a quote could be refused its own order, or, worse, be
given a second one.

Ids of the form `agent_internal_trusted_<key hash>` are all this service's own internal credential
(the commerce kernel previews and creates with it), so they are ONE caller: otherwise a key
rotation, with gateway revisions on both keys, would split one checkout between two callers.
Every other id stands for itself.
"""

from __future__ import annotations

from typing import Any

INTERNAL_TRUSTED_AGENT_PREFIX = "agent_internal_trusted_"


def caller_identity(agent_id: Any) -> str:
    """The identity `agent_id` acts as: the shared prefix for an internal trusted key, else the id."""
    value = str(agent_id or "").strip()
    if value.startswith(INTERNAL_TRUSTED_AGENT_PREFIX):
        return INTERNAL_TRUSTED_AGENT_PREFIX
    return value


def is_internal_trusted_identity(identity: Any) -> bool:
    """Is `identity` (a `caller_identity` result) the shared internal one?"""
    return identity == INTERNAL_TRUSTED_AGENT_PREFIX

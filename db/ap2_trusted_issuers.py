"""
AP2 trusted-issuer registry (ADR-012 authority layer, migration 184).

The mandate authority check (`services/ap2_mandate.verify_mandate_chain`) only
honors a mandate whose ISSUER DID — the user who authorized the agent — is
registered as trusted for the presenting agent. This module is the storage +
read path for that binding.

Keyed by ``agent_id``: the issuer DIDs allowed to authorize a given agent. For
the pilot the binding is **admin-provisioned** (an operator calls
``add_trusted_issuer`` / SQL); self-serve proof-of-control enrollment is a later
slice. Revocation flips ``status`` (the audit row is kept, not deleted).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from db.database import database


async def add_trusted_issuer(
    agent_id: str,
    issuer_did: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Register (or re-activate) ``issuer_did`` as trusted to authorize ``agent_id``.
    Idempotent: re-granting a previously revoked pair reactivates it.
    """
    await database.execute(
        """
        INSERT INTO ap2_trusted_issuers (agent_id, issuer_did, status, created_at)
        VALUES (:agent_id, :issuer_did, 'active', NOW())
        ON CONFLICT (agent_id, issuer_did)
        DO UPDATE SET status = 'active', revoked_at = NULL
        """,
        {"agent_id": agent_id, "issuer_did": issuer_did},
    )


async def revoke_trusted_issuer(agent_id: str, issuer_did: str) -> None:
    """Revoke a previously trusted issuer for an agent (keeps the audit row)."""
    await database.execute(
        """
        UPDATE ap2_trusted_issuers
        SET status = 'revoked', revoked_at = NOW()
        WHERE agent_id = :agent_id AND issuer_did = :issuer_did
        """,
        {"agent_id": agent_id, "issuer_did": issuer_did},
    )


async def get_trusted_issuers(agent_id: str) -> Set[str]:
    """
    Return the set of **active** issuer DIDs trusted to authorize ``agent_id``.

    This is what the wiring layer passes as ``trusted_issuers`` to
    ``verify_mandate_chain``. An agent with no registered issuers gets an empty
    set → every mandate fails closed (deny by default).
    """
    if not agent_id:
        return set()
    rows: List[Any] = await database.fetch_all(
        """
        SELECT issuer_did FROM ap2_trusted_issuers
        WHERE agent_id = :agent_id AND status = 'active'
        """,
        {"agent_id": agent_id},
    )
    # fetch_all returns Record rows — subscript access, never .get.
    return {row["issuer_did"] for row in rows}

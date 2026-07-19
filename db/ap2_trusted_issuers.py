"""
AP2 trusted-issuer registry (ADR-012 authority layer, migration 184).

The mandate authority check (`services/ap2_mandate.verify_mandate_chain`) only
honors a mandate whose ISSUER DID — the user who authorized the agent — is
registered as trusted for the presenting agent. This module is the storage +
read path for that binding.

Keyed by ``agent_id``: the issuer DIDs allowed to authorize a given agent, PLUS a
platform-**global** tier (#1495) stored under the sentinel ``agent_id = '*'`` — a
global issuer is trusted to authorize ANY agent's mandate. This is how a frontier /
app platform is trusted once and covers its whole agent fleet ("approve issuers,
not agents"); per-agent rows remain for user-as-issuer / personal-agent bindings.
``get_trusted_issuers(agent_id)`` returns the **union** (per-agent ∪ global), so the
verify primitive and the transaction route are untouched. Enrollment is admin-
provisioned with DID-resolution proof-of-control (``routes/ap2_trusted_issuers_admin``).
Revocation flips ``status`` (the audit row is kept, not deleted).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set

from db.database import database

# Sentinel agent_id for platform-GLOBAL trusted issuers (trusted for ANY agent).
# Real agent ids are ``agent_<hex>`` (db.agents.create_agent), so '*' can never
# collide; the ``(agent_id, issuer_did)`` unique index keeps one row per global
# issuer. No schema change — this is a keying convention on the existing table.
GLOBAL_SCOPE_AGENT_ID = "*"


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
        INSERT INTO ap2_trusted_issuers (agent_id, issuer_did, status, created_at, metadata)
        VALUES (:agent_id, :issuer_did, 'active', NOW(), :metadata)
        ON CONFLICT (agent_id, issuer_did)
        DO UPDATE SET status = 'active', revoked_at = NULL
        """,
        {
            "agent_id": agent_id,
            "issuer_did": issuer_did,
            "metadata": json.dumps(metadata) if metadata is not None else None,
        },
    )


async def add_global_trusted_issuer(
    issuer_did: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Register (or re-activate) ``issuer_did`` as a PLATFORM-GLOBAL trusted issuer
    — trusted to authorize ANY agent's mandate (#1495). Use for a frontier / app
    platform issuer whose whole fleet you trust with one enrollment."""
    await add_trusted_issuer(GLOBAL_SCOPE_AGENT_ID, issuer_did, metadata)


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


async def revoke_global_trusted_issuer(issuer_did: str) -> None:
    """Revoke a platform-global trusted issuer (#1495). Effective immediately — the
    transaction route reads the trusted set live per request."""
    await revoke_trusted_issuer(GLOBAL_SCOPE_AGENT_ID, issuer_did)


async def get_trusted_issuers(agent_id: str) -> Set[str]:
    """
    Return the set of **active** issuer DIDs trusted to authorize ``agent_id`` —
    the **union** of this agent's own trusted issuers AND the platform-global tier
    (#1495). This is what the wiring layer passes as ``trusted_issuers`` to
    ``verify_mandate_chain``. An agent with no per-agent AND no global issuers gets
    an empty set → every mandate fails closed (deny by default).
    """
    if not agent_id:
        return set()
    rows: List[Any] = await database.fetch_all(
        """
        SELECT issuer_did FROM ap2_trusted_issuers
        WHERE agent_id IN (:agent_id, :global_key) AND status = 'active'
        """,
        {"agent_id": agent_id, "global_key": GLOBAL_SCOPE_AGENT_ID},
    )
    # fetch_all returns Record rows — subscript access, never .get.
    return {row["issuer_did"] for row in rows}


async def list_trusted_issuers(agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List trusted-issuer rows for ops/audit. With ``agent_id`` → that agent's rows
    plus the global tier; without → every row. Each entry is
    ``{issuer_did, scope, agent_id, status}`` (``scope`` is 'global' | 'agent')."""
    if agent_id:
        rows: List[Any] = await database.fetch_all(
            """
            SELECT agent_id, issuer_did, status FROM ap2_trusted_issuers
            WHERE agent_id IN (:agent_id, :global_key)
            ORDER BY created_at DESC
            """,
            {"agent_id": agent_id, "global_key": GLOBAL_SCOPE_AGENT_ID},
        )
    else:
        rows = await database.fetch_all(
            "SELECT agent_id, issuer_did, status FROM ap2_trusted_issuers ORDER BY created_at DESC"
        )
    out: List[Dict[str, Any]] = []
    for row in rows:  # Record rows — subscript access, never .get
        aid = row["agent_id"]
        is_global = aid == GLOBAL_SCOPE_AGENT_ID
        out.append({
            "issuer_did": row["issuer_did"],
            "scope": "global" if is_global else "agent",
            "agent_id": None if is_global else aid,
            "status": row["status"],
        })
    return out

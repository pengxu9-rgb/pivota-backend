"""
Unified AP2 agent-identity resolution (ADR-012).

Single entrypoint the consent/transaction flow calls to turn a stored identity
into a verification key. Dispatches by DID method:
- ``did:key`` — offline, deterministic (services/ap2_did.py).
- ``did:web`` — fetches the agent's DID document (services/ap2_did_web.py):
  network, cached, SSRF-guarded, fail-closed.

A raw PEM (the legacy/pilot path) is NOT a DID and is handled by the caller
directly; ``is_did`` returns False for it.
"""
from __future__ import annotations

from typing import Optional, Tuple

from services.ap2_did import is_did_key, resolve_did_key
from services.ap2_did_web import is_did_web, resolve_did_web


def is_did(value: object) -> bool:
    """True iff ``value`` is a DID method we can resolve."""
    return is_did_key(value) or is_did_web(value)


async def resolve_agent_identity(
    identity: str, *, kid: Optional[str] = None
) -> Tuple[str, str]:
    """
    Resolve a DID identity to ``(public_key_pem, algorithm)``.

    The algorithm is taken from the DID / DID document — the identity, not the
    caller, is authoritative about the key type. Raises ValueError on anything
    unresolvable (the caller maps that to a fail-closed 401).
    """
    if is_did_key(identity):
        return resolve_did_key(identity)
    if is_did_web(identity):
        return await resolve_did_web(identity, kid=kid)
    raise ValueError("not a supported DID method")

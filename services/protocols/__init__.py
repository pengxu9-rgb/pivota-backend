"""Protocol identifiers for the agentic-commerce dimension on the decision-layer event store.

Reserve new identifiers here when adding adapters; do not invent values inline
at call sites.
"""

from __future__ import annotations

from typing import Optional

DEFAULT_PROTOCOL = "pdp_direct"
KNOWN_PROTOCOLS: frozenset[str] = frozenset({
    "pdp_direct",
    "ucp_session",
    "acp_session",
    "mcp_session",
    "creator_token",
    "resume_order",
})


def validate_protocol(value: Optional[str]) -> str:
    return value if isinstance(value, str) and value in KNOWN_PROTOCOLS else DEFAULT_PROTOCOL


def derive_protocol_for_surface(source: Optional[str]) -> str:
    """Best-effort protocol identifier from a serving surface/source/channel label.

    Phase 0 of the convergence plan: the decision-layer ``protocol`` dimension
    was 100% ``pdp_direct`` because every writer hardcoded the default. This
    maps only labels that UNAMBIGUOUSLY identify an agentic-commerce session
    type (mcp/acp/ucp appearing in the surface label); anything else stays
    DEFAULT_PROTOCOL so the dimension never guesses.
    """
    token = str(source or "").strip().lower()
    if not token:
        return DEFAULT_PROTOCOL
    if "mcp" in token:
        return "mcp_session"
    if "acp" in token:
        return "acp_session"
    if "ucp" in token:
        return "ucp_session"
    return DEFAULT_PROTOCOL

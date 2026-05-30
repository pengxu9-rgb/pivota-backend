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

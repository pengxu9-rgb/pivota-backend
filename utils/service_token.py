"""
Service-to-service token validation (#1296).

During the ACP canary both sides' service tokens were set to the literal string
``$TOK`` — an **unexpanded shell variable**. Because they matched, auth "worked",
so a broken placeholder silently became the shared secret. These guards make that
class of misconfiguration fail LOUDLY (at the point the token is used) instead of
silently authenticating: reject an unexpanded shell var, and in production reject a
too-short (weak) token. Dev/staging fallbacks like ``test`` stay allowed (non-prod).
"""
from __future__ import annotations

import os

# A real service secret (e.g. `openssl rand -hex 32` = 64 chars) is well above this;
# it only rejects obviously-weak values in production. Dev fallbacks are exempt.
_MIN_PROD_TOKEN_LEN = 16


def _is_production() -> bool:
    return (
        os.getenv("ENVIRONMENT", "").strip().lower() == "production"
        or os.getenv("RAILWAY_ENVIRONMENT", "").strip().lower() == "production"
    )


def validate_service_token(token: str, *, label: str) -> None:
    """Raise ``ValueError`` if ``token`` is an obviously-broken service secret.

    - empty / whitespace,
    - an **unexpanded shell variable** (``$TOK`` / ``${TOK}`` — the #1296 bug),
    - in production, shorter than ``_MIN_PROD_TOKEN_LEN`` (a weak secret).

    ``label`` names the env var for a clear error. Callers map the ValueError to a
    loud 5xx so the misconfiguration surfaces instead of silently authenticating.
    """
    t = (token or "").strip()
    if not t:
        raise ValueError(f"{label} is empty")
    if t.startswith("$"):
        raise ValueError(
            f"{label} looks like an unexpanded shell variable ({t[:8]}…) — set a real secret"
        )
    if _is_production() and len(t) < _MIN_PROD_TOKEN_LEN:
        raise ValueError(
            f"{label} is too short for production (< {_MIN_PROD_TOKEN_LEN} chars) — use a strong secret"
        )

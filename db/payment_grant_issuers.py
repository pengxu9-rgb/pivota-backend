"""Persistence for `payment_grant_issuers` — which PSPs may authorize money (migration 203).

Validation primitives are IMPORTED from db.agent_identity_issuers, not copied: the https/JWKS/
public-resolution rules and the asymmetric-alg allowlist are one security posture, and a copy
is a fork that drifts. What is deliberately NOT shared is the write path: identity rows are
agent self-service; these rows move money and are admin-only (enforced in the route, stated
here because the absence of an agent_id column is the schema-level half of that decision).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from db.agent_identity_issuers import (
    ALLOWED_ALG_PATTERN,
    DEFAULT_ALGS,
    IssuerValidationError,
    dereference_jwks,
    host_resolves_public_only,
)
from db._ddl_guard import apply_ddl_statements
from db.database import database

logger = logging.getLogger(__name__)

ALLOWED_METHODS = ("signed_grant", "ap2_mandate")

# Runtime DDL backstop, same as the identity store: prod applies db/migrations/203 directly,
# but a deploy that skipped migrations must not turn the gateway's registry pull into a 500 —
# this table gates Antom checkout at request time.
_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS payment_grant_issuers (
        id               BIGSERIAL PRIMARY KEY,
        issuer           TEXT        NOT NULL,
        jwks_uri         TEXT        NOT NULL,
        audience         TEXT        NOT NULL,
        algs             TEXT[]      NOT NULL DEFAULT ARRAY['RS256', 'ES256']::TEXT[],
        authorized_party TEXT        NULL,
        methods          TEXT[]      NOT NULL DEFAULT ARRAY['signed_grant']::TEXT[],
        expected_vct     TEXT        NULL,
        status           TEXT        NOT NULL DEFAULT 'active',
        registered_by    TEXT        NOT NULL,
        last_jwks_ok_at  TIMESTAMPTZ NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT payment_grant_issuers_status_check CHECK (status IN ('active', 'disabled')),
        CONSTRAINT payment_grant_issuers_jwks_https CHECK (jwks_uri LIKE 'https://%'),
        CONSTRAINT payment_grant_issuers_methods_check CHECK (
            methods <@ ARRAY['signed_grant', 'ap2_mandate']::TEXT[]
            AND array_length(methods, 1) >= 1
        )
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS payment_grant_issuers_active_issuer_uidx
        ON payment_grant_issuers (issuer) WHERE status = 'active'
    """,
]
_DDL_LOCK = asyncio.Lock()
_DDL_READY = False


async def ensure_payment_grant_issuers_table() -> None:
    """Per-statement-tolerant DDL backstop (prod applies the .sql migration directly)."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_payment_grant_issuers_table",
            logger=logger,
            execute=database.execute,
        )


@dataclass(frozen=True)
class PaymentIssuerRegistration:
    issuer: str
    jwks_uri: str
    audience: str
    algs: List[str]
    authorized_party: Optional[str]
    methods: List[str]
    expected_vct: Optional[str]


def _clean(value: Any, field: str, *, required: bool, max_len: int = 512) -> Optional[str]:
    if value is None:
        if required:
            raise IssuerValidationError(field, f"{field} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise IssuerValidationError(field, f"{field} must be a non-blank string")
    s = value.strip()
    if len(s) > max_len:
        raise IssuerValidationError(field, f"{field} must be at most {max_len} characters")
    return s


def normalize_registration(body: Dict[str, Any]) -> PaymentIssuerRegistration:
    """Shape-validate a registration body. Pure; no network (the JWKS dereference is the
    route's second step, so tests can cover shape rules without sockets)."""
    if not isinstance(body, dict):
        raise IssuerValidationError("body", "body must be a JSON object")

    issuer = _clean(body.get("issuer"), "issuer", required=True)
    if any(ch.isspace() for ch in issuer):
        raise IssuerValidationError("issuer", "issuer must not contain whitespace")

    jwks_uri = _clean(body.get("jwks_uri"), "jwks_uri", required=True)
    parsed = urlparse(jwks_uri)
    if parsed.scheme != "https" or not parsed.netloc:
        raise IssuerValidationError("jwks_uri", "jwks_uri must be an https URL")
    if parsed.username or parsed.password:
        raise IssuerValidationError("jwks_uri", "jwks_uri must not carry credentials")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "::1"} or host.endswith((".local", ".internal", ".localhost")):
        raise IssuerValidationError("jwks_uri", "jwks_uri must be publicly reachable")
    if not host_resolves_public_only(host):
        raise IssuerValidationError("jwks_uri", "jwks_uri must resolve to a public address")

    audience = _clean(body.get("audience"), "audience", required=True)

    raw_algs = body.get("algs")
    if raw_algs is None:
        algs = list(DEFAULT_ALGS)
    else:
        if not isinstance(raw_algs, list) or not raw_algs:
            raise IssuerValidationError("algs", "algs must be a non-empty array")
        algs = []
        for a in raw_algs:
            if not isinstance(a, str) or not ALLOWED_ALG_PATTERN.match(a.strip()):
                raise IssuerValidationError(
                    "algs", "algs must be asymmetric algorithms only (RS*/PS*/ES*/EdDSA)"
                )
            if a.strip() not in algs:
                algs.append(a.strip())

    raw_methods = body.get("methods")
    if raw_methods is None:
        methods = ["signed_grant"]
    else:
        if not isinstance(raw_methods, list) or not raw_methods:
            raise IssuerValidationError("methods", "methods must be a non-empty array")
        methods = []
        for m in raw_methods:
            if not isinstance(m, str) or m.strip() not in ALLOWED_METHODS:
                raise IssuerValidationError(
                    "methods", f"each method must be one of {', '.join(ALLOWED_METHODS)}"
                )
            if m.strip() not in methods:
                methods.append(m.strip())

    expected_vct = _clean(body.get("expected_vct"), "expected_vct", required=False)
    if expected_vct is not None and "ap2_mandate" not in methods:
        # A pinned credential type on an issuer that may not mint mandates is a config that
        # LOOKS like AP2 trust without being it — refuse the ambiguity at the door.
        raise IssuerValidationError("expected_vct", "expected_vct requires methods to include ap2_mandate")

    return PaymentIssuerRegistration(
        issuer=issuer, jwks_uri=jwks_uri, audience=audience, algs=algs,
        authorized_party=_clean(body.get("authorized_party"), "authorized_party", required=False),
        methods=methods, expected_vct=expected_vct,
    )


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("["):
            try:
                return [str(v) for v in json.loads(s)]
            except Exception:  # noqa: BLE001
                return []
        if s.startswith("{") and s.endswith("}"):
            return [p.strip().strip('"') for p in s[1:-1].split(",") if p.strip()]
    return []


def _row_public(row: Any) -> Dict[str, Any]:
    d = dict(row)
    return {
        "id": d.get("id"),
        "issuer": d.get("issuer"),
        "jwks_uri": d.get("jwks_uri"),
        "audience": d.get("audience"),
        "algs": _as_list(d.get("algs")),
        "authorized_party": d.get("authorized_party"),
        "methods": _as_list(d.get("methods")),
        "expected_vct": d.get("expected_vct"),
        "status": d.get("status"),
        "registered_by": d.get("registered_by"),
        "last_jwks_ok_at": d.get("last_jwks_ok_at"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }


async def list_all() -> List[Dict[str, Any]]:
    await ensure_payment_grant_issuers_table()
    rows = await database.fetch_all(
        "SELECT * FROM payment_grant_issuers ORDER BY status = 'active' DESC, updated_at DESC, id DESC"
    )
    return [_row_public(r) for r in rows]


async def list_active() -> List[Dict[str, Any]]:
    """Every ACTIVE row, in registration order — the shape the gateway registry consumes."""
    rows = await database.fetch_all(
        "SELECT * FROM payment_grant_issuers WHERE status = 'active' ORDER BY id"
    )
    return [_row_public(r) for r in rows]


async def upsert_issuer(reg: PaymentIssuerRegistration, *, registered_by: str, jwks_ok: bool) -> Dict[str, Any]:
    """Create or replace the row for this issuer (re-activating a disabled one). Same
    read-then-write shape as the identity store: the partial unique index on active issuers is
    the arbiter for a concurrent duplicate."""
    await ensure_payment_grant_issuers_table()
    existing = await database.fetch_one(
        "SELECT id FROM payment_grant_issuers WHERE issuer = :issuer ORDER BY status = 'active' DESC, id DESC LIMIT 1",
        {"issuer": reg.issuer},
    )
    params = {
        "issuer": reg.issuer,
        "jwks_uri": reg.jwks_uri,
        "audience": reg.audience,
        "algs": reg.algs,
        "authorized_party": reg.authorized_party,
        "methods": reg.methods,
        "expected_vct": reg.expected_vct,
        "registered_by": registered_by,
        "jwks_ok": bool(jwks_ok),
    }
    if existing:
        await database.execute(
            """
            UPDATE payment_grant_issuers
            SET jwks_uri = :jwks_uri, audience = :audience, algs = :algs,
                authorized_party = :authorized_party, methods = :methods,
                expected_vct = :expected_vct, registered_by = :registered_by,
                status = 'active',
                last_jwks_ok_at = CASE WHEN CAST(:jwks_ok AS boolean) THEN NOW() ELSE last_jwks_ok_at END,
                updated_at = NOW()
            WHERE id = :id
            """,
            # exactly the binds the statement names — databases' text() REFUSES extras
            # (sqlalchemy ArgumentError), it does not ignore them.
            {k: params[k] for k in (
                "jwks_uri", "audience", "algs", "authorized_party", "methods",
                "expected_vct", "registered_by", "jwks_ok",
            )} | {"id": dict(existing)["id"]},
        )
    else:
        await database.execute(
            """
            INSERT INTO payment_grant_issuers (
                issuer, jwks_uri, audience, algs, authorized_party, methods, expected_vct,
                registered_by, status, last_jwks_ok_at
            ) VALUES (
                :issuer, :jwks_uri, :audience, :algs, :authorized_party, :methods, :expected_vct,
                :registered_by, 'active',
                CASE WHEN CAST(:jwks_ok AS boolean) THEN NOW() ELSE NULL END
            )
            """,
            params,
        )
    row = await database.fetch_one(
        "SELECT * FROM payment_grant_issuers WHERE issuer = :issuer AND status = 'active' LIMIT 1",
        {"issuer": reg.issuer},
    )
    return _row_public(row)


async def disable_issuer(issuer_id: int) -> bool:
    """Disable, never delete: a payment issuer that WAS trusted is audit evidence. Returns
    whether a row changed — disabling an already-disabled or unknown id is False, so the route
    can 404 instead of lying."""
    await ensure_payment_grant_issuers_table()
    row = await database.fetch_one(
        """
        UPDATE payment_grant_issuers
        SET status = 'disabled', updated_at = NOW()
        WHERE id = :id AND status = 'active'
        RETURNING id
        """,
        {"id": issuer_id},
    )
    return row is not None


__all__ = [
    "ALLOWED_METHODS",
    "IssuerValidationError",
    "PaymentIssuerRegistration",
    "dereference_jwks",
    "disable_issuer",
    "ensure_payment_grant_issuers_table",
    "list_active",
    "list_all",
    "normalize_registration",
    "upsert_issuer",
]

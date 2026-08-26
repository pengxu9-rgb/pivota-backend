"""
agent_identity_issuers — per-agent FEDERATED buyer-identity issuers.

A buyer agent registers its own token issuer (iss + JWKS + aud) from the developer portal; a
user token minted by that issuer is then accepted on the checkout path ONLY when presented
together with that agent's API key. See db/migrations/193_agent_identity_issuers.sql for the
model and the security reasoning; this module is the accessor layer plus the registration-time
validation (shape + a live JWKS dereference) that keeps an unusable issuer from being stored.

Consumers:
- routes/agent_identity_issuers.py   portal self-serve CRUD (+ the internal registry read the
                                     gateway polls)
- services/agent_user_jwt.py         REST-side verification for a specific agent
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from db._ddl_guard import apply_ddl_statements
from db.database import database

logger = logging.getLogger(__name__)

ALLOWED_ALG_PATTERN = re.compile(r"^(?:RS|PS|ES)\d{3}$|^EdDSA$")
DEFAULT_ALGS = ["RS256", "ES256"]
DEFAULT_REQUIRED_SCOPES = ["pivota.checkout"]
JWKS_FETCH_TIMEOUT_SECONDS = 5.0
JWKS_OPAQUE_FAILURE = "JWKS could not be fetched as a usable key set (https, 2xx, {keys:[…]} with an RSA/EC/OKP key)"
ASYMMETRIC_KTYS = {"RSA", "EC", "OKP"}


class IssuerValidationError(ValueError):
    """A registration body that must be refused; `field` names what to fix."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _env_list(name: str) -> List[str]:
    raw = (os.getenv(name) or "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else []


def reserved_issuers() -> set:
    """Issuer strings NO agent may register: the platform's own identity issuers.

    An agent registering one of these with its own JWKS would be claiming to mint tokens for an
    issuer the platform already trusts elsewhere. Sources: the global agent-user issuer(s)
    (AGENT_USER_JWT_ISSUERS / AGENT_USER_JWT_ISSUER), Pivota's own OAuth AS (MCP_OAUTH_AS_ISSUER),
    and an ops-maintained list (AGENT_IDENTITY_RESERVED_ISSUERS) for gateway-side issuers this
    service cannot see (IDENTITY_ISSUERS_JSON / MCP_OAUTH_ISSUERS_JSON live on the gateway).
    Compared case-insensitively with trailing slashes stripped.
    """
    out = set()
    for name in ("AGENT_USER_JWT_ISSUERS", "AGENT_USER_JWT_ISSUER", "AGENT_IDENTITY_RESERVED_ISSUERS"):
        for v in _env_list(name):
            out.add(_issuer_key(v))
    own = (os.getenv("MCP_OAUTH_AS_ISSUER") or "").strip()
    if own:
        out.add(_issuer_key(own))
    return out


def _issuer_key(value: str) -> str:
    return str(value or "").strip().rstrip("/").lower()


def is_reserved_issuer(issuer: str) -> bool:
    return _issuer_key(issuer) in reserved_issuers()


def is_global_issuer(issuer: str) -> bool:
    """True when the GLOBAL env verifier owns this issuer — it must be verified there, never via a binding."""
    key = _issuer_key(issuer)
    for name in ("AGENT_USER_JWT_ISSUERS", "AGENT_USER_JWT_ISSUER"):
        if any(_issuer_key(v) == key for v in _env_list(name)):
            return True
    return False


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # `is_global` is the authoritative check (covers CGNAT 100.64/10, which is_private misses);
    # the explicit flags are belt-and-braces for older Python semantics.
    if not getattr(addr, "is_global", True):
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified or getattr(addr, "is_site_local", False)
    )


def host_resolves_public_only(host: str) -> bool:
    """Every address the host resolves to must be globally routable (SSRF: no RFC1918/CGNAT/link-local)."""
    try:
        literal = ipaddress.ip_address(host)
        return _is_public_ip(str(literal))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception:  # noqa: BLE001
        return False
    addrs = {info[4][0] for info in infos if info and info[4]}
    return bool(addrs) and all(_is_public_ip(a) for a in addrs)


_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS agent_identity_issuers (
        id               BIGSERIAL PRIMARY KEY,
        agent_id         TEXT        NOT NULL,
        issuer           TEXT        NOT NULL,
        jwks_uri         TEXT        NOT NULL,
        audience         TEXT        NOT NULL,
        algs             TEXT[]      NOT NULL DEFAULT ARRAY['RS256', 'ES256']::TEXT[],
        authorized_party TEXT        NULL,
        required_scopes  TEXT[]      NULL,
        status           TEXT        NOT NULL DEFAULT 'active',
        last_jwks_ok_at  TIMESTAMPTZ NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT agent_identity_issuers_status_check CHECK (status IN ('active', 'disabled')),
        CONSTRAINT agent_identity_issuers_issuer_no_pipe CHECK (position('|' in issuer) = 0),
        CONSTRAINT agent_identity_issuers_jwks_https CHECK (jwks_uri LIKE 'https://%'),
        CONSTRAINT agent_identity_issuers_agent_issuer_unique UNIQUE (agent_id, issuer)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS agent_identity_issuers_active_issuer_uidx
        ON agent_identity_issuers (issuer) WHERE status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS agent_identity_issuers_agent_idx
        ON agent_identity_issuers (agent_id, status)
    """,
]
_DDL_LOCK = asyncio.Lock()
_DDL_READY = False


async def ensure_agent_identity_issuers_table() -> None:
    """Per-statement-tolerant DDL backstop (prod applies the .sql migration directly)."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_agent_identity_issuers_table",
            logger=logger,
            execute=database.execute,
        )


# ── validation ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IssuerRegistration:
    issuer: str
    jwks_uri: str
    audience: str
    algs: List[str]
    authorized_party: Optional[str]
    required_scopes: Optional[List[str]]


def _clean_str(value: Any, field: str, *, required: bool, max_len: int = 512) -> Optional[str]:
    if value is None:
        if required:
            raise IssuerValidationError(field, f"{field} is required")
        return None
    if not isinstance(value, str):
        raise IssuerValidationError(field, f"{field} must be a string")
    s = value.strip()
    if not s:
        if required:
            raise IssuerValidationError(field, f"{field} is required")
        return None
    if len(s) > max_len:
        raise IssuerValidationError(field, f"{field} is too long")
    return s


def normalize_registration(body: Dict[str, Any]) -> IssuerRegistration:
    """Shape-validate a registration body. Pure; no network."""
    if not isinstance(body, dict):
        raise IssuerValidationError("body", "body must be a JSON object")
    issuer = _clean_str(body.get("issuer"), "issuer", required=True)
    if "|" in issuer:
        raise IssuerValidationError("issuer", "issuer must not contain '|'")
    if any(ch.isspace() for ch in issuer):
        raise IssuerValidationError("issuer", "issuer must not contain whitespace")

    if is_reserved_issuer(issuer):
        raise IssuerValidationError("issuer", "this issuer is reserved by the platform and cannot be registered")

    jwks_uri = _clean_str(body.get("jwks_uri"), "jwks_uri", required=True)
    parsed = urlparse(jwks_uri)
    if parsed.scheme != "https" or not parsed.netloc:
        raise IssuerValidationError("jwks_uri", "jwks_uri must be an https URL")
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password:
        raise IssuerValidationError("jwks_uri", "jwks_uri must not carry credentials")
    if host in {"localhost", "::1"} or host.endswith(".local") or host.endswith(".internal") or host.endswith(".localhost"):
        raise IssuerValidationError("jwks_uri", "jwks_uri must be publicly reachable")
    if not host_resolves_public_only(host):
        raise IssuerValidationError("jwks_uri", "jwks_uri must resolve to a public address")

    audience = _clean_str(body.get("audience"), "audience", required=True)

    raw_algs = body.get("algs")
    if raw_algs is None:
        algs = list(DEFAULT_ALGS)
    else:
        if not isinstance(raw_algs, list) or not raw_algs:
            raise IssuerValidationError("algs", "algs must be a non-empty array")
        algs = []
        for a in raw_algs:
            if not isinstance(a, str) or not ALLOWED_ALG_PATTERN.match(a.strip()):
                raise IssuerValidationError("algs", "algs must be asymmetric algorithms only (RS*/PS*/ES*/EdDSA)")
            if a.strip() not in algs:
                algs.append(a.strip())

    authorized_party = _clean_str(body.get("authorized_party"), "authorized_party", required=False)

    # Scope enforcement is fail-closed by default: without required_scopes, ANY token the issuer
    # mints for the registered audience is checkout-grade identity (a plain session token, not a
    # checkout grant). Opting out must be the explicit, named act below — an empty array alone is
    # refused so a form serializing an empty multi-select can never silently disable the check.
    allow_unscoped = body.get("allow_unscoped_tokens")
    if allow_unscoped is not None and not isinstance(allow_unscoped, bool):
        raise IssuerValidationError("allow_unscoped_tokens", "allow_unscoped_tokens must be a boolean")

    raw_scopes = body.get("required_scopes")
    required_scopes: Optional[List[str]] = None
    if raw_scopes is not None:
        if not isinstance(raw_scopes, list):
            raise IssuerValidationError("required_scopes", "required_scopes must be an array of strings")
        required_scopes = []
        for s in raw_scopes:
            if not isinstance(s, str) or not s.strip() or any(ch.isspace() for ch in s.strip()):
                raise IssuerValidationError("required_scopes", "each required scope must be a non-blank token")
            if s.strip() not in required_scopes:
                required_scopes.append(s.strip())
        if not required_scopes:
            required_scopes = None

    if required_scopes:
        if allow_unscoped is True:
            raise IssuerValidationError(
                "allow_unscoped_tokens",
                "allow_unscoped_tokens cannot be combined with required_scopes; drop one",
            )
    elif allow_unscoped is True:
        required_scopes = None
    elif raw_scopes is None:
        # Omitted (or JSON null) → the default. IdPs that cannot mint a scope claim (e.g. plain
        # OIDC ID tokens) must opt out explicitly with allow_unscoped_tokens: true.
        required_scopes = list(DEFAULT_REQUIRED_SCOPES)
    else:
        raise IssuerValidationError(
            "required_scopes",
            "an empty required_scopes disables scope checks; set allow_unscoped_tokens: true to confirm, or provide scopes",
        )

    return IssuerRegistration(
        issuer=issuer, jwks_uri=jwks_uri, audience=audience, algs=algs,
        authorized_party=authorized_party, required_scopes=required_scopes,
    )


def _jwks_has_usable_key(doc: Any) -> bool:
    if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
        return False
    for k in doc["keys"]:
        if isinstance(k, dict) and k.get("kty") in ASYMMETRIC_KTYS:
            return True
    return False


async def dereference_jwks(jwks_uri: str) -> Dict[str, Any]:
    """Fetch the JWKS once; refuse anything that is not a usable asymmetric key set."""
    try:
        async with httpx.AsyncClient(timeout=JWKS_FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = await client.get(jwks_uri, headers={"Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001 — surfaced as a validation error, never a 500
        logger.info("jwks dereference failed: %s", type(exc).__name__)
        raise IssuerValidationError("jwks_uri", JWKS_OPAQUE_FAILURE) from exc
    # ONE opaque message for every failure mode: status, JSON, shape. Distinct messages turned
    # this endpoint into an internal-network oracle (status codes of whatever the URL reached).
    if resp.status_code < 200 or resp.status_code >= 300:
        raise IssuerValidationError("jwks_uri", JWKS_OPAQUE_FAILURE)
    try:
        doc = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise IssuerValidationError("jwks_uri", JWKS_OPAQUE_FAILURE) from exc
    if not _jwks_has_usable_key(doc):
        raise IssuerValidationError("jwks_uri", JWKS_OPAQUE_FAILURE)
    return doc


# ── accessors ─────────────────────────────────────────────────────────────────

def _row_to_public(row: Any) -> Dict[str, Any]:
    d = dict(row)
    required_scopes = _as_list(d.get("required_scopes")) or None
    return {
        "id": d.get("id"),
        "agent_id": d.get("agent_id"),
        "issuer": d.get("issuer"),
        "jwks_uri": d.get("jwks_uri"),
        "audience": d.get("audience"),
        "algs": _as_list(d.get("algs")),
        "authorized_party": d.get("authorized_party"),
        "required_scopes": required_scopes,
        # A binding with no scope check accepts ANY of the issuer's tokens for the audience;
        # surface that loudly rather than leaving a null to be read as "default applies".
        "unscoped_tokens_allowed": required_scopes is None,
        "status": d.get("status"),
        "last_jwks_ok_at": d.get("last_jwks_ok_at"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        # SQLite / text fallback: JSON array or {a,b} pg-literal
        s = value.strip()
        if s.startswith("["):
            try:
                return [str(v) for v in json.loads(s)]
            except Exception:  # noqa: BLE001
                return []
        if s.startswith("{") and s.endswith("}"):
            return [p.strip().strip('"') for p in s[1:-1].split(",") if p.strip()]
    return []


async def list_issuers_for_agent(agent_id: str) -> List[Dict[str, Any]]:
    await ensure_agent_identity_issuers_table()
    rows = await database.fetch_all(
        """
        SELECT * FROM agent_identity_issuers
        WHERE agent_id = :agent_id
        ORDER BY status = 'active' DESC, updated_at DESC, id DESC
        """,
        {"agent_id": agent_id},
    )
    return [_row_to_public(r) for r in rows]


async def get_active_issuer(agent_id: str, issuer: str) -> Optional[Dict[str, Any]]:
    """The ACTIVE row for (agent, iss), or None. This is the binding the verifier enforces."""
    await ensure_agent_identity_issuers_table()
    row = await database.fetch_one(
        """
        SELECT * FROM agent_identity_issuers
        WHERE agent_id = :agent_id AND issuer = :issuer AND status = 'active'
        LIMIT 1
        """,
        {"agent_id": agent_id, "issuer": issuer},
    )
    return _row_to_public(row) if row else None


async def find_active_owner(issuer: str) -> Optional[str]:
    await ensure_agent_identity_issuers_table()
    row = await database.fetch_one(
        "SELECT agent_id FROM agent_identity_issuers WHERE issuer = :issuer AND status = 'active' LIMIT 1",
        {"issuer": issuer},
    )
    return str(dict(row)["agent_id"]) if row else None


async def upsert_issuer(agent_id: str, reg: IssuerRegistration, *, jwks_ok: bool) -> Dict[str, Any]:
    """Create or replace the agent's row for this issuer (re-activating a disabled one)."""
    await ensure_agent_identity_issuers_table()
    params = {
        "agent_id": agent_id,
        "issuer": reg.issuer,
        "jwks_uri": reg.jwks_uri,
        "audience": reg.audience,
        "algs": reg.algs,
        "authorized_party": reg.authorized_party,
        "required_scopes": reg.required_scopes,
        "jwks_ok": bool(jwks_ok),
    }
    existing = await database.fetch_one(
        "SELECT id FROM agent_identity_issuers WHERE agent_id = :agent_id AND issuer = :issuer LIMIT 1",
        {"agent_id": agent_id, "issuer": reg.issuer},
    )
    if existing:
        await database.execute(
            """
            UPDATE agent_identity_issuers
            SET jwks_uri = :jwks_uri, audience = :audience, algs = :algs,
                authorized_party = :authorized_party, required_scopes = :required_scopes,
                status = 'active',
                last_jwks_ok_at = CASE WHEN :jwks_ok THEN NOW() ELSE last_jwks_ok_at END,
                updated_at = NOW()
            WHERE id = :id
            """,
            {**params, "id": dict(existing)["id"]},
        )
    else:
        await database.execute(
            """
            INSERT INTO agent_identity_issuers
                (agent_id, issuer, jwks_uri, audience, algs, authorized_party, required_scopes, status, last_jwks_ok_at)
            VALUES
                (:agent_id, :issuer, :jwks_uri, :audience, :algs, :authorized_party, :required_scopes, 'active',
                 CASE WHEN :jwks_ok THEN NOW() ELSE NULL END)
            """,
            params,
        )
    row = await database.fetch_one(
        "SELECT * FROM agent_identity_issuers WHERE agent_id = :agent_id AND issuer = :issuer LIMIT 1",
        {"agent_id": agent_id, "issuer": reg.issuer},
    )
    return _row_to_public(row) if row else {**params, "status": "active"}


async def disable_issuer(agent_id: str, issuer_id: int) -> bool:
    await ensure_agent_identity_issuers_table()
    row = await database.fetch_one(
        "SELECT id FROM agent_identity_issuers WHERE id = :id AND agent_id = :agent_id AND status = 'active' LIMIT 1",
        {"id": issuer_id, "agent_id": agent_id},
    )
    if not row:
        return False
    await database.execute(
        "UPDATE agent_identity_issuers SET status = 'disabled', updated_at = NOW() WHERE id = :id",
        {"id": issuer_id},
    )
    return True


async def list_active_registry() -> List[Dict[str, Any]]:
    """Every active binding — what the gateway loads into its per-issuer verifier registry."""
    await ensure_agent_identity_issuers_table()
    rows = await database.fetch_all(
        "SELECT * FROM agent_identity_issuers WHERE status = 'active' ORDER BY agent_id, issuer"
    )
    return [_row_to_public(r) for r in rows]

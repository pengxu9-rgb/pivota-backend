"""
MCP OAuth Authorization Server — protocol flow over a pluggable store.

Holds the authorization-code grant + refresh logic so it can be unit-tested end-to-end with an
in-memory store (no DB). The router (routes/mcp_oauth_as.py) provides a Postgres-backed store and
the HTTP/login/consent glue. FAIL CLOSED on every error.

Codes and refresh tokens are stored ONLY as sha256 hashes (a DB leak must not yield usable creds).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from services import mcp_oauth_as as core


class OAuthFlowError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error = error  # OAuth error code, e.g. invalid_grant
        self.description = description
        self.status = status


# --------------------------------------------------------------------------- store

class OAuthStore(Protocol):
    async def save_client(self, record: Dict[str, Any]) -> None: ...
    async def get_client(self, client_id: str) -> Optional[Dict[str, Any]]: ...
    async def save_code(self, code_hash: str, data: Dict[str, Any]) -> None: ...
    async def get_code(self, code_hash: str) -> Optional[Dict[str, Any]]: ...
    async def consume_code(self, code_hash: str) -> bool:
        """Atomically mark a code consumed; return False if already consumed/missing."""
        ...
    async def save_refresh(self, token_hash: str, data: Dict[str, Any]) -> None: ...
    async def get_refresh(self, token_hash: str) -> Optional[Dict[str, Any]]: ...
    async def revoke_refresh(self, token_hash: str) -> None: ...


@dataclass
class InMemoryStore:
    clients: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    codes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    consumed: set = field(default_factory=set)
    refresh: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    async def save_client(self, record):
        self.clients[record["client_id"]] = dict(record)

    async def get_client(self, client_id):
        c = self.clients.get(client_id)
        return dict(c) if c else None

    async def save_code(self, code_hash, data):
        self.codes[code_hash] = dict(data)

    async def get_code(self, code_hash):
        c = self.codes.get(code_hash)
        return dict(c) if c else None

    async def consume_code(self, code_hash):
        if code_hash in self.consumed or code_hash not in self.codes:
            return False
        self.consumed.add(code_hash)
        return True

    async def save_refresh(self, token_hash, data):
        self.refresh[token_hash] = dict(data)

    async def get_refresh(self, token_hash):
        r = self.refresh.get(token_hash)
        return dict(r) if r else None

    async def revoke_refresh(self, token_hash):
        self.refresh.pop(token_hash, None)


def _now() -> int:
    return int(time.time())


AUTH_CODE_TTL_SECONDS = 300
REFRESH_TTL_SECONDS = 60 * 60 * 24 * 30


# --------------------------------------------------------------------------- DCR

async def register_client(store: OAuthStore, body: Dict[str, Any]) -> Dict[str, Any]:
    shaped = core.shape_registration_request(body)
    record = {
        "client_id": shaped.client_id,
        "client_secret_hash": core.hash_secret(shaped.client_secret) if shaped.client_secret else None,
        "redirect_uris": list(shaped.redirect_uris),
        "token_endpoint_auth_method": shaped.token_endpoint_auth_method,
        "client_name": shaped.client_name,
        "created_at": _now(),
    }
    await store.save_client(record)
    out = {
        "client_id": shaped.client_id,
        "redirect_uris": shaped.redirect_uris,
        "token_endpoint_auth_method": shaped.token_endpoint_auth_method,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if shaped.client_name:
        out["client_name"] = shaped.client_name
    if shaped.client_secret:
        out["client_secret"] = shaped.client_secret  # returned ONCE, only the hash is stored
    return out


# --------------------------------------------------------------------------- authorize

async def validate_authorization_request(store: OAuthStore, params: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an /authorize request BEFORE the user logs in / consents. Returns normalized params."""
    if str(params.get("response_type") or "").strip() != "code":
        raise OAuthFlowError("unsupported_response_type", "only response_type=code is supported")
    client_id = str(params.get("client_id") or "").strip()
    client = await store.get_client(client_id)
    if not client:
        raise OAuthFlowError("invalid_client", "unknown client_id", status=401)
    redirect_uri = str(params.get("redirect_uri") or "").strip()
    if redirect_uri not in (client.get("redirect_uris") or []):
        # never redirect to an unregistered URI
        raise OAuthFlowError("invalid_request", "redirect_uri does not match a registered value")
    code_challenge = str(params.get("code_challenge") or "").strip()
    method = str(params.get("code_challenge_method") or "").strip().upper()
    if not code_challenge or method != "S256":
        raise OAuthFlowError("invalid_request", "PKCE code_challenge with S256 is required")
    resource = str(params.get("resource") or "").strip()
    if not resource:
        raise OAuthFlowError("invalid_target", "resource (RFC 8707) is required")
    if resource not in core.allowed_resources():
        # exact-match allowlist; unset MCP_OAUTH_AS_ALLOWED_RESOURCES allows nothing
        raise OAuthFlowError("invalid_target", "resource is not an allowed audience")
    scope = str(params.get("scope") or "").strip() or " ".join(core.DEFAULT_SCOPES)
    unsupported = [s for s in scope.split() if s not in core.SUPPORTED_SCOPES]
    if unsupported:
        # refuse unknown scopes outright rather than silently dropping them, so a client
        # never believes it holds a scope the AS won't grant
        raise OAuthFlowError("invalid_scope", f"unsupported scope(s): {' '.join(unsupported)}")
    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "scope": scope,
        "resource": resource,
        "state": str(params.get("state") or ""),
    }


async def issue_authorization_code(
    store: OAuthStore, *, validated: Dict[str, Any], subject: str, now: Optional[int] = None
) -> str:
    """Called AFTER the buyer has authenticated and consented. Returns the opaque code."""
    if not subject:
        raise OAuthFlowError("access_denied", "no authenticated subject", status=401)
    now = now or _now()
    code = core.new_authorization_code()
    # Clamp to what this subject may hold. A guest subject is stripped of pivota.account here —
    # the stored code (and every token minted from it, incl. refreshes) carries only the
    # clamped scope, so the reduction cannot be undone downstream.
    granted_scope = core.clamp_scope_for_subject(validated["scope"], subject)
    await store.save_code(
        core.hash_secret(code),
        {
            "client_id": validated["client_id"],
            "redirect_uri": validated["redirect_uri"],
            "code_challenge": validated["code_challenge"],
            "scope": granted_scope,
            "resource": validated["resource"],
            "subject": subject,
            "expires_at": now + AUTH_CODE_TTL_SECONDS,
            "created_at": now,
        },
    )
    return code


# --------------------------------------------------------------------------- token

def _authenticate_client(client: Dict[str, Any], provided_secret: Optional[str]) -> None:
    method = client.get("token_endpoint_auth_method") or "none"
    if method == "none":
        return  # public client; PKCE is the proof
    if not provided_secret or not client.get("client_secret_hash"):
        raise OAuthFlowError("invalid_client", "client authentication required", status=401)
    import secrets as _s
    if not _s.compare_digest(core.hash_secret(provided_secret), client["client_secret_hash"]):
        raise OAuthFlowError("invalid_client", "bad client secret", status=401)


async def exchange_authorization_code(
    store: OAuthStore, *, params: Dict[str, Any], client_secret: Optional[str] = None, now: Optional[int] = None
) -> Dict[str, Any]:
    now = now or _now()
    client_id = str(params.get("client_id") or "").strip()
    code = str(params.get("code") or "").strip()
    redirect_uri = str(params.get("redirect_uri") or "").strip()
    code_verifier = str(params.get("code_verifier") or "").strip()

    client = await store.get_client(client_id)
    if not client:
        raise OAuthFlowError("invalid_client", "unknown client_id", status=401)
    _authenticate_client(client, client_secret)

    code_hash = core.hash_secret(code)
    record = await store.get_code(code_hash)
    if not record:
        raise OAuthFlowError("invalid_grant", "unknown or expired code")
    # Bindings: code must match the client + redirect_uri it was issued for.
    if record["client_id"] != client_id:
        raise OAuthFlowError("invalid_grant", "code was issued to a different client")
    if record["redirect_uri"] != redirect_uri:
        raise OAuthFlowError("invalid_grant", "redirect_uri mismatch")
    if int(record["expires_at"]) < now:
        raise OAuthFlowError("invalid_grant", "authorization code expired")
    if not core.verify_pkce(code_verifier=code_verifier, code_challenge=record["code_challenge"]):
        raise OAuthFlowError("invalid_grant", "PKCE verification failed")
    # single-use: atomic consume AFTER all checks
    if not await store.consume_code(code_hash):
        raise OAuthFlowError("invalid_grant", "authorization code already used")

    return await _mint_grant(store, subject=record["subject"], resource=record["resource"],
                             scope=record["scope"], client_id=client_id, now=now)


async def exchange_refresh_token(
    store: OAuthStore, *, params: Dict[str, Any], client_secret: Optional[str] = None, now: Optional[int] = None
) -> Dict[str, Any]:
    now = now or _now()
    client_id = str(params.get("client_id") or "").strip()
    refresh_token = str(params.get("refresh_token") or "").strip()
    client = await store.get_client(client_id)
    if not client:
        raise OAuthFlowError("invalid_client", "unknown client_id", status=401)
    _authenticate_client(client, client_secret)

    rt_hash = core.hash_secret(refresh_token)
    rec = await store.get_refresh(rt_hash)
    if not rec or rec["client_id"] != client_id:
        raise OAuthFlowError("invalid_grant", "unknown refresh token")
    if int(rec["expires_at"]) < now:
        raise OAuthFlowError("invalid_grant", "refresh token expired")
    # rotate: revoke the old, issue a new pair
    await store.revoke_refresh(rt_hash)
    return await _mint_grant(store, subject=rec["subject"], resource=rec["resource"],
                             scope=rec["scope"], client_id=client_id, now=now)


async def _mint_grant(store, *, subject, resource, scope, client_id, now) -> Dict[str, Any]:
    scope_list = scope.split() if isinstance(scope, str) else list(scope or [])
    access_token = core.mint_access_token(subject=subject, audience=resource, scope=scope_list)
    refresh_token = core.new_refresh_token()
    await store.save_refresh(
        core.hash_secret(refresh_token),
        {
            "client_id": client_id,
            "subject": subject,
            "resource": resource,
            "scope": scope,
            "expires_at": now + REFRESH_TTL_SECONDS,
            "created_at": now,
        },
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": core.ACCESS_TOKEN_TTL_SECONDS_DEFAULT,
        "refresh_token": refresh_token,
        "scope": scope,
    }

"""
MCP OAuth Authorization Server — crypto/protocol core (DB-free, unit-testable).

Pivota self-hosts the Authorization Server that lets native frontier MCP clients
(Claude/ChatGPT/Gemini) obtain an access token for the Pivota Agent `/mcp` resource server
*after the buyer logs in (existing accounts login) and consents*. No third party.

This module holds ONLY the security-critical, side-effect-free pieces so they can be tested
exhaustively without a DB or web framework:
  - RSA signing key management + JWKS publication
  - RS256 access-token minting (claims the agent_user_jwt verifier accepts)
  - PKCE (RFC 7636) verification
  - opaque secret/code/client-id generation
  - authorization-server metadata (RFC 8414) + DCR response shaping (RFC 7591)

The router (routes/mcp_oauth_as.py) owns persistence (clients, auth codes), the buyer-login +
consent integration, and HTTP. FAIL CLOSED on every error.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import jwt
from jwt import algorithms
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


ALG = "RS256"
ACCESS_TOKEN_TTL_SECONDS_DEFAULT = 3600
DEFAULT_SCOPES = ["pivota.checkout"]

# Every scope the AS will grant to anyone. A request for anything outside this set is
# refused at /authorize (invalid_scope) rather than silently dropped.
SUPPORTED_SCOPES = ["pivota.checkout", "pivota.account"]
# Scopes a guest (non-account) subject may ever hold. `pivota.account` — vault reads and
# after-sales mutations — is deliberately absent: a consent-minted guest can never obtain it,
# no matter what the client asks for. Enforced by clamp_scope_for_subject at code-issue time.
GUEST_SCOPES = ["pivota.checkout"]

# Namespace for consent-minted pseudonymous subjects (channel 2 native MCP clients). Chosen so
# it can NEVER equal an account subject (`u_<hex>` from db.accounts) or an identity_id or the
# UGC guest form (`guest:<digest>`, colon-delimited) — no shared prefix, no colon.
GUEST_SUBJECT_PREFIX = "mcpguest_"


class McpOAuthAsError(Exception):
    """Configuration or protocol error in the AS core."""


# --------------------------------------------------------------------------- keys

_KEY_CACHE: Dict[str, Any] = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def issuer() -> str:
    iss = (os.getenv("MCP_OAUTH_AS_ISSUER") or "").strip().rstrip("/")
    if not iss:
        raise McpOAuthAsError("MCP_OAUTH_AS_ISSUER is not configured")
    return iss


def key_id() -> str:
    return (os.getenv("MCP_OAUTH_AS_KEY_ID") or "pivota-mcp-as-1").strip()


def allowed_resources() -> List[str]:
    """Exact resource URLs (RFC 8707) this AS may bind tokens to.

    Fail closed: unset/empty means NO resource is acceptable — the AS must never mint
    an audience nobody deliberately allowed. Matching is byte-exact (the RS compares
    `aud` to its resource string byte-exactly, so anything looser here mints tokens
    the RS would accept for a resource we never approved).
    """
    raw = os.getenv("MCP_OAUTH_AS_ALLOWED_RESOURCES") or ""
    return [v.strip() for v in raw.split(",") if v.strip()]


def _load_private_key():
    """Load the RSA private key from env, or generate an ephemeral one (dev/test only).

    Production MUST set MCP_OAUTH_AS_PRIVATE_KEY_PEM so the JWKS is stable across instances and
    restarts; an ephemeral key would invalidate every issued token on redeploy.
    """
    pem = (os.getenv("MCP_OAUTH_AS_PRIVATE_KEY_PEM") or "").strip()
    cache_key = "pem:" + (hashlib.sha256(pem.encode()).hexdigest() if pem else "ephemeral")
    cached = _KEY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if pem:
        # allow literal "\n" escapes from single-line env vars
        normalized = pem.replace("\\n", "\n").encode("utf-8")
        try:
            key = serialization.load_pem_private_key(normalized, password=None)
        except Exception as e:  # noqa: BLE001
            raise McpOAuthAsError("MCP_OAUTH_AS_PRIVATE_KEY_PEM is not a valid PEM private key") from e
        if not isinstance(key, rsa.RSAPrivateKey):
            raise McpOAuthAsError("MCP_OAUTH_AS_PRIVATE_KEY_PEM must be an RSA private key")
    else:
        if (os.getenv("MCP_OAUTH_AS_ALLOW_EPHEMERAL_KEY") or "").strip() != "1":
            raise McpOAuthAsError(
                "No signing key: set MCP_OAUTH_AS_PRIVATE_KEY_PEM (or MCP_OAUTH_AS_ALLOW_EPHEMERAL_KEY=1 for dev/test)"
            )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    _KEY_CACHE[cache_key] = key
    return key


def build_jwks() -> Dict[str, Any]:
    """Public JWKS for the resource server / clients to verify access tokens."""
    private_key = _load_private_key()
    public_jwk = json.loads(algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = key_id()
    public_jwk["use"] = "sig"
    public_jwk["alg"] = ALG
    return {"keys": [public_jwk]}


# --------------------------------------------------------------------------- tokens

def _now() -> int:
    return int(time.time())


def mint_access_token(
    *,
    subject: str,
    audience: str,
    scope: Optional[List[str]] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
    ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS_DEFAULT,
) -> str:
    """Mint an RS256 access token bound to the buyer (subject) and the MCP resource (audience)."""
    if not isinstance(subject, str) or not subject.strip():
        raise McpOAuthAsError("subject is required")
    if not isinstance(audience, str) or not audience.strip():
        raise McpOAuthAsError("audience (resource) is required")
    private_key = _load_private_key()
    iat = _now()
    claims: Dict[str, Any] = {
        "iss": issuer(),
        "sub": subject,
        "aud": audience,
        "iat": iat,
        "nbf": iat,
        "exp": iat + int(ttl_seconds),
        "jti": secrets.token_urlsafe(18),
        "scope": " ".join(scope or DEFAULT_SCOPES),
        "token_type": "access",
    }
    if extra_claims:
        for k, v in extra_claims.items():
            if k not in claims:  # never let extras override registered/security claims
                claims[k] = v
    return jwt.encode(claims, private_key, algorithm=ALG, headers={"kid": key_id()})


# --------------------------------------------------------------------------- PKCE

def verify_pkce(*, code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    """RFC 7636. Only S256 is accepted (plain is refused — downgrade protection)."""
    if not code_verifier or not code_challenge:
        return False
    m = (method or "S256").upper()
    if m != "S256":
        return False
    # RFC 7636 §4.1: verifier is 43..128 chars from the unreserved set
    if not (43 <= len(code_verifier) <= 128):
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = _b64url(digest)
    return secrets.compare_digest(expected, code_challenge)


# --------------------------------------------------------------------------- guest subjects

def guest_subjects_enabled() -> bool:
    """Channel-2 guest minting is OFF unless explicitly enabled; flag-off is byte-identical to
    the login-required behavior that shipped before this feature."""
    return (os.getenv("MCP_OAUTH_AS_GUEST_SUBJECTS") or "").strip() == "1"


def new_guest_subject() -> str:
    return GUEST_SUBJECT_PREFIX + secrets.token_urlsafe(18)


def is_guest_subject(subject: Optional[str]) -> bool:
    return isinstance(subject, str) and subject.startswith(GUEST_SUBJECT_PREFIX)


def clamp_scope_for_subject(scope: str, subject: str) -> str:
    """Reduce a requested scope string to what `subject` is allowed to hold, order-preserving.

    Guest subjects are clamped to GUEST_SCOPES — a guest that asked for `pivota.account` gets
    only `pivota.checkout` back, never the account scope. Account subjects keep any supported
    scope. Callers pass a scope already validated against SUPPORTED_SCOPES.
    """
    requested = [s for s in (scope or "").split() if s]
    allowed = set(GUEST_SCOPES) if is_guest_subject(subject) else set(SUPPORTED_SCOPES)
    kept = [s for s in requested if s in allowed]
    return " ".join(kept) if kept else " ".join(DEFAULT_SCOPES)


# --------------------------------------------------------------------------- DCR / codes

def new_client_id() -> str:
    return "mcpc_" + secrets.token_hex(16)


def new_client_secret() -> str:
    return "mcps_" + secrets.token_urlsafe(40)


def hash_secret(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def new_authorization_code() -> str:
    return secrets.token_urlsafe(40)


def new_refresh_token() -> str:
    return "mcpr_" + secrets.token_urlsafe(40)


@dataclass(frozen=True)
class RegisteredClient:
    client_id: str
    client_secret: Optional[str]  # None for public (PKCE) clients
    redirect_uris: List[str]
    token_endpoint_auth_method: str
    client_name: Optional[str]


def shape_registration_request(body: Dict[str, Any]) -> RegisteredClient:
    """Validate a DCR request (RFC 7591) and produce a client record.

    MCP clients are public + PKCE by default (no secret); a confidential client may be requested
    with token_endpoint_auth_method=client_secret_basic.
    """
    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or not all(
        isinstance(u, str) and u.strip() for u in redirect_uris
    ):
        raise McpOAuthAsError("redirect_uris must be a non-empty array of strings")
    for u in redirect_uris:
        if not _is_allowed_redirect_uri(u):
            raise McpOAuthAsError(f"redirect_uri not allowed: {u}")

    method = str(body.get("token_endpoint_auth_method") or "none").strip().lower()
    if method not in ("none", "client_secret_basic", "client_secret_post"):
        raise McpOAuthAsError("unsupported token_endpoint_auth_method")

    confidential = method != "none"
    return RegisteredClient(
        client_id=new_client_id(),
        client_secret=new_client_secret() if confidential else None,
        redirect_uris=list(redirect_uris),
        token_endpoint_auth_method=method,
        client_name=(str(body["client_name"]).strip() if body.get("client_name") else None),
    )


def _is_allowed_redirect_uri(uri: str) -> bool:
    """https only, except http loopback for local clients (RFC 8252)."""
    from urllib.parse import urlparse

    try:
        p = urlparse(uri)
    except Exception:  # noqa: BLE001
        return False
    if p.scheme == "https":
        return True
    if p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1"):
        return True
    # allow custom-scheme native redirects (e.g. claude://, com.example.app:/) — must be absolute
    if p.scheme and p.scheme not in ("http",) and (p.netloc or p.path):
        return True
    return False


# --------------------------------------------------------------------------- metadata

def authorization_server_metadata() -> Dict[str, Any]:
    """RFC 8414 authorization-server metadata."""
    iss = issuer()
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/oauth/authorize",
        "token_endpoint": f"{iss}/oauth/token",
        "registration_endpoint": f"{iss}/oauth/register",
        "jwks_uri": f"{iss}/.well-known/jwks.json",
        "scopes_supported": list(SUPPORTED_SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
        "resource_indicators_supported": True,
    }

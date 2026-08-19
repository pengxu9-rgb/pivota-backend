"""
Agent-tools user identity JWT verification (agent_user_jwt -> agent_user_ref).

Threat model:
- Requests are authenticated at the *agent* level via Agent API key.
- The *end-user* identity is supplied by the Agent tools surface (frontend/backend) as a JWT.
- Pivota must verify this JWT via JWKS before using it for authorization, attribution, or audit.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

import httpx
import jwt
from jwt import algorithms


@dataclass(frozen=True)
class AgentUserIdentity:
    agent_user_ref: str
    issuer: Optional[str]
    subject: Optional[str]
    claims: Dict[str, Any]


class AgentUserJwtError(Exception):
    pass


_JWKS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _env_list(name: str) -> Optional[list[str]]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def _get_allowed_algs() -> list[str]:
    return _env_list("AGENT_USER_JWT_ALGS") or ["RS256"]


def _get_expected_issuers() -> Optional[list[str]]:
    return _env_list("AGENT_USER_JWT_ISSUERS") or _env_list("AGENT_USER_JWT_ISSUER")


def _get_expected_audience() -> Optional[str]:
    aud = (os.getenv("AGENT_USER_JWT_AUDIENCE") or "").strip()
    return aud or None


def _jwks_sources() -> list[Tuple[str, str]]:
    sources: list[Tuple[str, str]] = []
    jwks_json = (os.getenv("AGENT_USER_JWKS_JSON") or "").strip()
    jwks_file = (os.getenv("AGENT_USER_JWKS_FILE") or "").strip()
    jwks_url = (os.getenv("AGENT_USER_JWKS_URL") or "").strip()
    if jwks_json:
        sources.append(("json", jwks_json))
    if jwks_file:
        sources.append(("file", jwks_file))
    if jwks_url:
        sources.append(("url", jwks_url))
    return sources


def _load_jwks(*, refresh: bool = False, sources: Optional[list[Tuple[str, str]]] = None) -> Dict[str, Any]:
    if sources is None:
        sources = _jwks_sources()
    if not sources:
        raise AgentUserJwtError(
            "Missing JWKS configuration (set AGENT_USER_JWKS_URL or AGENT_USER_JWKS_JSON or AGENT_USER_JWKS_FILE)"
        )

    ttl_s = float(os.getenv("AGENT_USER_JWKS_TTL_SECONDS") or "600")
    now = time.time()

    # Cache by the first configured source to keep behavior deterministic.
    cache_key = f"{sources[0][0]}:{sources[0][1]}"
    if not refresh:
        cached = _JWKS_CACHE.get(cache_key)
        if cached:
            expires_at, jwks = cached
            if expires_at > now:
                return jwks

    kind, value = sources[0]
    if kind == "json":
        jwks = json.loads(value)
    elif kind == "file":
        with open(value, "r", encoding="utf-8") as f:
            jwks = json.load(f)
    elif kind == "url":
        timeout_s = float(os.getenv("AGENT_USER_JWKS_HTTP_TIMEOUT_SECONDS") or "5")
        try:
            resp = httpx.get(value, timeout=timeout_s, headers={"Accept": "application/json"})
        except Exception as e:
            raise AgentUserJwtError(f"Failed to fetch JWKS: {type(e).__name__}") from e
        if resp.status_code < 200 or resp.status_code >= 300:
            raise AgentUserJwtError(f"Failed to fetch JWKS: HTTP {resp.status_code}")
        try:
            jwks = resp.json()
        except Exception as e:
            raise AgentUserJwtError("Invalid JWKS JSON") from e
    else:
        raise AgentUserJwtError("Unsupported JWKS source")

    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise AgentUserJwtError("Invalid JWKS format: expected {keys: []}")

    _JWKS_CACHE[cache_key] = (now + ttl_s, jwks)
    return jwks


def _iter_jwk_candidates(jwks: Dict[str, Any], *, kid: Optional[str]) -> Iterable[Dict[str, Any]]:
    keys = jwks.get("keys") or []
    if not isinstance(keys, list):
        return []
    if kid:
        return [k for k in keys if isinstance(k, dict) and k.get("kid") == kid]
    # If no kid provided, allow only when there's exactly one key to avoid ambiguous verification.
    dict_keys = [k for k in keys if isinstance(k, dict)]
    return dict_keys if len(dict_keys) == 1 else []


def _public_key_from_jwk(jwk_dict: Dict[str, Any]):
    try:
        kty = jwk_dict.get("kty")
        jwk_json = json.dumps(jwk_dict)
        if kty == "RSA":
            return algorithms.RSAAlgorithm.from_jwk(jwk_json)
        if kty == "EC":
            return algorithms.ECAlgorithm.from_jwk(jwk_json)
    except Exception as e:
        raise AgentUserJwtError("Failed to parse JWK") from e
    raise AgentUserJwtError("Unsupported JWK kty")


def _normalize_agent_user_ref(*, issuer: Optional[str], raw_ref: str) -> str:
    ref = (raw_ref or "").strip()
    if not ref:
        raise AgentUserJwtError("Missing agent user reference")
    iss = (issuer or "").strip()
    if not iss:
        return ref
    # Prevent accidental double-prefixing if caller already embeds issuer.
    if ref.startswith(f"{iss}:"):
        return ref
    return f"{iss}:{ref}"


def verify_agent_user_jwt(token: str) -> AgentUserIdentity:
    """
    Verify an agent-user JWT against the GLOBAL (env-configured) issuer and return a stable
    agent_user_ref. For a per-agent (federated) issuer use `verify_agent_user_jwt_for_agent`.

    Required env (one of):
    - AGENT_USER_JWKS_URL
    - AGENT_USER_JWKS_JSON
    - AGENT_USER_JWKS_FILE

    Optional env:
    - AGENT_USER_JWT_ISSUER / AGENT_USER_JWT_ISSUERS (comma-separated)
    - AGENT_USER_JWT_AUDIENCE
    - AGENT_USER_JWT_ALGS (comma-separated, default RS256)
    """
    return _verify_against(
        token,
        jwks_sources=None,
        expected_issuers=_get_expected_issuers(),
        expected_audience=_get_expected_audience(),
        allowed_algs=_get_allowed_algs(),
    )


def _peek_unverified_issuer(token: str) -> Optional[str]:
    """The `iss` claim WITHOUT verification — only ever used to SELECT a registered binding; the
    signature is then checked against that binding's pinned JWKS and iss is re-checked there."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except Exception:
        return None
    iss = claims.get("iss") if isinstance(claims, dict) else None
    return iss if isinstance(iss, str) and iss.strip() else None


async def verify_agent_user_jwt_for_agent(token: str, agent_id: Optional[str]) -> AgentUserIdentity:
    """
    Verify an agent-user JWT for a SPECIFIC calling agent.

    Order: if the token's (unverified) `iss` is an ACTIVE issuer registered to `agent_id`
    (db.agent_identity_issuers), verify against THAT binding — its JWKS, audience and alg
    allowlist. Otherwise fall back to the global env issuer. A token from an issuer registered to
    a DIFFERENT agent never matches here and is not accepted by the fallback either (unless ops
    also configured it globally), which is the binding this exists to enforce.
    """
    token = (token or "").strip()
    if not token:
        raise AgentUserJwtError("Missing token")
    if agent_id:
        iss = _peek_unverified_issuer(token)
        if iss:
            from db.agent_identity_issuers import get_active_issuer  # late import: db layer

            binding = await get_active_issuer(str(agent_id), iss)
            if binding:
                return _verify_against(
                    token,
                    jwks_sources=[("url", binding["jwks_uri"])],
                    expected_issuers=[binding["issuer"]],
                    expected_audience=binding["audience"],
                    allowed_algs=list(binding.get("algs") or []) or ["RS256"],
                    authorized_party=binding.get("authorized_party"),
                    required_scopes=binding.get("required_scopes"),
                )
    return verify_agent_user_jwt(token)


def _verify_against(
    token: str,
    *,
    jwks_sources: Optional[list[Tuple[str, str]]],
    expected_issuers: Optional[list[str]],
    expected_audience: Optional[str],
    allowed_algs: list[str],
    authorized_party: Optional[str] = None,
    required_scopes: Optional[list[str]] = None,
) -> AgentUserIdentity:
    token = (token or "").strip()
    if not token:
        raise AgentUserJwtError("Missing token")

    try:
        unverified_header = jwt.get_unverified_header(token)
    except Exception as e:
        raise AgentUserJwtError("Invalid JWT header") from e

    alg = (unverified_header.get("alg") or "").strip()
    kid = (unverified_header.get("kid") or "").strip() or None
    if not alg or alg not in allowed_algs:
        raise AgentUserJwtError("Unsupported JWT alg")

    jwks = _load_jwks(refresh=False, sources=jwks_sources)
    candidates = list(_iter_jwk_candidates(jwks, kid=kid))
    if not candidates:
        # Retry once with refreshed JWKS to handle rotation.
        jwks = _load_jwks(refresh=True, sources=jwks_sources)
        candidates = list(_iter_jwk_candidates(jwks, kid=kid))
    if not candidates:
        raise AgentUserJwtError("No matching JWKS key")

    single_issuer = expected_issuers[0] if expected_issuers and len(expected_issuers) == 1 else None

    last_err: Optional[Exception] = None
    for jwk_dict in candidates:
        try:
            key = _public_key_from_jwk(jwk_dict)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                options={
                    "require": ["exp"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "verify_aud": bool(expected_audience),
                    "verify_iss": bool(single_issuer),
                },
                audience=expected_audience,
                issuer=single_issuer,
            )
        except Exception as e:
            last_err = e
            continue

        issuer = claims.get("iss")
        if expected_issuers:
            if not issuer or issuer not in expected_issuers:
                last_err = AgentUserJwtError("Invalid issuer")
                continue
        if authorized_party:
            azp = claims.get("azp") if isinstance(claims.get("azp"), str) else claims.get("client_id")
            if azp != authorized_party:
                last_err = AgentUserJwtError("Token authorized party not allowed")
                continue
        if required_scopes:
            raw_scope = claims.get("scope")
            if isinstance(raw_scope, str):
                granted = set(raw_scope.split())
            elif isinstance(claims.get("scp"), list):
                granted = {str(x) for x in claims.get("scp")}
            else:
                granted = set()
            if not all(sc in granted for sc in required_scopes):
                last_err = AgentUserJwtError("Token missing a required scope")
                continue

        subject = claims.get("sub")
        raw_ref = (
            claims.get("agent_user_ref")
            or claims.get("agentUserRef")
            or claims.get("user_ref")
            or claims.get("userRef")
            or subject
            or claims.get("user_id")
            or claims.get("uid")
        )
        if not isinstance(raw_ref, str):
            raise AgentUserJwtError("Missing agent user ref claim")

        agent_user_ref = _normalize_agent_user_ref(issuer=issuer if isinstance(issuer, str) else None, raw_ref=raw_ref)
        return AgentUserIdentity(
            agent_user_ref=agent_user_ref,
            issuer=issuer if isinstance(issuer, str) else None,
            subject=subject if isinstance(subject, str) else None,
            claims={k: v for k, v in (claims or {}).items() if k in {"iss", "sub", "aud", "exp", "iat", "nbf", "scope"}},
        )

    raise AgentUserJwtError("JWT verification failed") from last_err

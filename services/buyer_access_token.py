"""Verify the buyer's own pb-oauth-as access token on a backend route the gateway calls.

WHY. The gateway tells the backend who the buyer is with `X-Buyer-Ref`, a raw
header (PIVOTA-Agent `src/server.js`, the forwarded-headers builder). Nothing
signs it, so any caller that can reach the backend can claim any buyer. That is
tolerable for reads and not for a route that spends through a buyer's linked
Shop account. Those routes instead take the access token that our own
authorization server (`services/mcp_oauth_as.py`) minted for the buyer, which
the gateway already holds after verifying it on `/mcp`. The gateway forwards it
unchanged in `X-Pivota-Buyer-Token`, and this module verifies it again here.

Why not `X-Agent-User-JWT` and `services/agent_user_jwt.py`: that header is
passed through from whatever the caller sent the gateway, and its verifier is
built for partner agents' OWN users. It federates across partner issuers, and
issuer and audience checks are optional by configuration. A partner's user
token passing it says nothing about which Pivota account owns a Shop link.

The backend IS the authorization server, so the key is local: there is no JWKS
fetch, no `jku`, and no key material from the token's header.

ALL OF THESE MUST HOLD (each is its own check, so a test can kill a mutant that
drops one):

1. the token is a non-empty string of at most 8,192 bytes;
2. its header says `alg: RS256` and `kid` equals our key id;
3. the signature verifies with our own RS256 public key (HS256, `none` and any
   other algorithm are refused by the allow-list);
4. `iss` equals our configured issuer exactly;
5. `aud` is one of our configured MCP resources exactly. With none configured
   the verifier is OFF (`not_configured`), never "any audience";
6. `exp`, `iat`, `nbf` are present and valid, with 10 s of leeway, and the
   lifetime is no longer than our access-token TTL plus a minute;
7. `token_type` is `access` (a refresh token or another JWT of ours is refused);
8. `sub` is present and is NOT a guest subject;
9. `scope` contains the required scope (`pivota.account` by default);
10. `client_id` is present, so a caller can bind state to the agent client.

WHAT IS NEVER DONE. The token never appears in an exception message, a log line
or a response. A refusal carries a short code (`expired`, `bad_audience` ...)
for our own logs, and routes answer every refusal with the same 401 body.

KNOWN LIMIT. An access token stays valid until it expires (one hour), even if
the buyer's Pivota grant is revoked earlier: pb-oauth-as has no revocation list
for access tokens. Callers that need fresher state check their own row (for
the Shop link: `status` and `link_generation`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, FrozenSet, Optional

import jwt
from fastapi import HTTPException, Request

from services import mcp_oauth_as as core

logger = logging.getLogger(__name__)

BUYER_TOKEN_HEADER = "x-pivota-buyer-token"
MAX_TOKEN_BYTES = 8192
LEEWAY_SECONDS = 10
MAX_LIFETIME_SECONDS = core.ACCESS_TOKEN_TTL_SECONDS_DEFAULT + 60
ACCOUNT_SCOPE = "pivota.account"


class BuyerTokenError(Exception):
    """A refused token. `code` is safe to log; nothing else about the token is kept."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedBuyer:
    account_id: str
    client_id: str
    scopes: FrozenSet[str]
    jti: Optional[str]
    expires_at: int


def _public_key():
    try:
        return core._load_private_key().public_key()
    except core.McpOAuthAsError:
        raise BuyerTokenError("not_configured") from None


def verify_buyer_access_token(token: object, *, required_scope: str = ACCOUNT_SCOPE) -> VerifiedBuyer:
    if not isinstance(token, str) or not token:
        raise BuyerTokenError("missing")
    if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise BuyerTokenError("too_large")

    try:
        expected_issuer = core.issuer()
    except core.McpOAuthAsError:
        raise BuyerTokenError("not_configured") from None
    audiences = core.allowed_resources()
    if not audiences:
        raise BuyerTokenError("not_configured")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise BuyerTokenError("malformed") from None
    if header.get("alg") != core.ALG:
        raise BuyerTokenError("bad_alg")
    if header.get("kid") != core.key_id():
        raise BuyerTokenError("bad_kid")

    try:
        claims = jwt.decode(
            token,
            _public_key(),
            algorithms=[core.ALG],
            audience=audiences,
            issuer=expected_issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise BuyerTokenError("expired") from None
    except jwt.InvalidAudienceError:
        raise BuyerTokenError("bad_audience") from None
    except jwt.InvalidIssuerError:
        raise BuyerTokenError("bad_issuer") from None
    except jwt.InvalidSignatureError:
        raise BuyerTokenError("bad_signature") from None
    except jwt.MissingRequiredClaimError:
        raise BuyerTokenError("missing_claim") from None
    except jwt.PyJWTError:
        raise BuyerTokenError("invalid") from None

    # PyJWT accepts a list `aud` when ANY member matches. A token minted for
    # several audiences is not one we mint, so require exactly one.
    if not isinstance(claims.get("aud"), str):
        raise BuyerTokenError("bad_audience")
    try:
        lifetime = int(claims["exp"]) - int(claims["iat"])
    except (TypeError, ValueError):
        raise BuyerTokenError("invalid") from None
    if lifetime <= 0 or lifetime > MAX_LIFETIME_SECONDS:
        raise BuyerTokenError("bad_lifetime")
    if claims.get("token_type") != "access":
        raise BuyerTokenError("not_access_token")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise BuyerTokenError("missing_claim")
    if core.is_guest_subject(subject):
        raise BuyerTokenError("guest_subject")

    scope = claims.get("scope")
    scopes = frozenset(scope.split()) if isinstance(scope, str) else frozenset()
    if required_scope not in scopes:
        raise BuyerTokenError("insufficient_scope")

    client_id = claims.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise BuyerTokenError("missing_client")

    jti = claims.get("jti")
    return VerifiedBuyer(
        account_id=subject,
        client_id=client_id,
        scopes=scopes,
        jti=jti if isinstance(jti, str) else None,
        expires_at=int(claims["exp"]),
    )


def _buyer_from_request(request: Request, required_scope: str) -> VerifiedBuyer:
    try:
        return verify_buyer_access_token(request.headers.get(BUYER_TOKEN_HEADER), required_scope=required_scope)
    except BuyerTokenError as refused:
        logger.warning("buyer_token_refused code=%s", refused.code)
        raise HTTPException(status_code=401, detail="buyer authentication required") from None


def buyer_dependency(required_scope: str = ACCOUNT_SCOPE) -> Callable[[Request], VerifiedBuyer]:
    """A FastAPI dependency: `buyer: VerifiedBuyer = Depends(buyer_dependency())`.

    The scope is fixed HERE, when the route is declared, and the returned
    function takes only the `Request`. A dependency with a `required_scope`
    keyword of its own would be exposed by FastAPI as a query parameter, and
    `?required_scope=pivota.checkout` would downgrade the check.

    The buyer comes from `X-Pivota-Buyer-Token` only. `X-Buyer-Ref`,
    `buyer_ref` in a body, and every other identity source are ignored on
    purpose: they are unsigned. Every refusal is the same 401, so a caller
    cannot tell which check failed.
    """
    if not isinstance(required_scope, str) or not required_scope.strip():
        raise ValueError("required_scope must be a non-empty string")

    def require_verified_buyer(request: Request) -> VerifiedBuyer:
        return _buyer_from_request(request, required_scope)

    return require_verified_buyer

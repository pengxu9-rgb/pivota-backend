"""The REQUEST-BOUND issuing-agent assertion (v2): who the gateway verified, for ONE request.

The v1 token (services/issuing_agent_assertion.py) binds only (subject, op, ts). That is enough
for offers.resolve, where a replay can at most misattribute a click. On the MCP money ops (FIX_PLAN
O-04, PARTNER_IDENTITY_FORWARDING_ENABLED) the asserted agent becomes the OWNER of what the route
writes, so a v1 token plus the gateway key would be a bearer capability: "act as this partner for
this op, any body, for ten minutes" (review of O-04, P2-2). v2 binds the token to one request:

    X-Pivota-Issuing-Agent: v2.<b64url(JSON payload)>.<b64url(HMAC-SHA256(secret, "v2." + part1))>

    payload = { v: 2, kind, sub | iss+cid, op, ts, jti, m, p, rid, bh }
      m    the HTTP method, upper case
      p    the request path as sent, no query (raw, still percent-encoded)
      rid  the resource the op acts on: the order id (path or body) or the quote id; null when the
           route has none (a quote preview, a seed-link mint)
      bh   sha256 hex of the EXACT request body bytes (the gateway sends the string it hashed)
      jti  a random single-use nonce (services/assertion_replay_guard.py)
      ts   unix seconds, accepted within MAX_SKEW_SECONDS_V2 (60) either way

Same secret as v1 (ISSUING_AGENT_ASSERTION_SECRET), same subject rules, a different version prefix
inside the MAC input, so a v1 token can never be read as v2 or the reverse. Every check returns a
reason; nothing here raises. The caller decides what a failure costs (routes/agent_forwarded_owner.py
refuses the request).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Collection, Dict, Optional

from services.issuing_agent_assertion import (
    _B64URL_PART,
    _MAX_AGENT_ID,
    _MAX_OAUTH_FIELD,
    _b64url_decode,
    _b64url_encode,
    _text,
    assertion_secret,
)

VERSION_V2 = "v2"
#: Narrower than v1's 300 s: a money op's token is minted for the call it rides on.
MAX_SKEW_SECONDS_V2 = 60
_MAX_PATH = 2048
_MAX_RID = 256
_JTI = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def body_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw or b"").hexdigest()


def is_v2_token(token: Optional[str]) -> bool:
    return isinstance(token, str) and token.strip().startswith(f"{VERSION_V2}.")


@dataclass(frozen=True)
class BoundSubject:
    kind: str  # "agent" | "oauth"
    op: str
    ts: int
    jti: str
    agent_id: Optional[str] = None
    issuer: Optional[str] = None
    client_id: Optional[str] = None


@dataclass(frozen=True)
class BoundVerdict:
    subject: Optional[BoundSubject]
    reason: str  # "ok" or why not


def sign_bound_issuing_agent_assertion(payload: Dict[str, Any], secret: str) -> str:
    """The gateway's v2 signer, mirrored for tests and operator probes (compact JSON, key order as given)."""
    body = _b64url_encode(json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signing_input = f"{VERSION_V2}.{body}"
    mac = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(mac)}"


def verify_bound_issuing_agent_assertion(
    token: Optional[str],
    *,
    ops: Collection[str],
    method: str,
    path: str,
    resource_id: Optional[str],
    body_hash: str,
    secret: Optional[str] = None,
    now: Optional[float] = None,
) -> BoundVerdict:
    """Is `token` a v2 assertion for THIS request? Order: shape, MAC, version, op, freshness, method,
    path, resource, body. The nonce is NOT consumed here (see assertion_replay_guard)."""
    try:
        key = secret if secret is not None else assertion_secret()
        if not key:
            return BoundVerdict(None, "no_secret")
        if not isinstance(token, str):
            return BoundVerdict(None, "malformed")
        parts = token.strip().split(".")
        if len(parts) != 3 or parts[0] != VERSION_V2:
            return BoundVerdict(None, "malformed")
        if not _B64URL_PART.match(parts[1]) or not _B64URL_PART.match(parts[2]):
            return BoundVerdict(None, "malformed")
        signing_input = f"{parts[0]}.{parts[1]}"
        expected = hmac.new(key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(parts[2])):
            return BoundVerdict(None, "bad_mac")
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        if not isinstance(payload, dict) or type(payload.get("v")) is not int or payload.get("v") != 2:
            return BoundVerdict(None, "malformed")
        op = payload.get("op")
        if not isinstance(op, str) or not op or op not in set(ops):
            return BoundVerdict(None, "op")
        ts = payload.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, int):
            return BoundVerdict(None, "malformed")
        clock = time.time() if now is None else now
        if abs(clock - ts) > MAX_SKEW_SECONDS_V2:
            return BoundVerdict(None, "stale")
        jti = payload.get("jti")
        if not isinstance(jti, str) or not _JTI.match(jti):
            return BoundVerdict(None, "malformed")
        if payload.get("m") != str(method or "").upper():
            return BoundVerdict(None, "method")
        sent_path = payload.get("p")
        if not isinstance(sent_path, str) or len(sent_path) > _MAX_PATH or sent_path != path:
            return BoundVerdict(None, "path")
        rid = payload.get("rid")
        want_rid = _text(resource_id, _MAX_RID) if resource_id is not None else None
        if rid is not None and not isinstance(rid, str):
            return BoundVerdict(None, "resource")
        if (rid or None) != want_rid:
            return BoundVerdict(None, "resource")
        bh = payload.get("bh")
        if not isinstance(bh, str) or not _SHA256_HEX.match(bh) or not hmac.compare_digest(bh, body_hash):
            return BoundVerdict(None, "body")
        kind = payload.get("kind")
        if kind == "agent":
            agent_id = _text(payload.get("sub"), _MAX_AGENT_ID)
            if agent_id:
                return BoundVerdict(BoundSubject(kind="agent", op=op, ts=ts, jti=jti, agent_id=agent_id), "ok")
        elif kind == "oauth":
            issuer = _text(payload.get("iss"), _MAX_OAUTH_FIELD)
            client_id = _text(payload.get("cid"), _MAX_OAUTH_FIELD)
            if issuer and client_id:
                return BoundVerdict(
                    BoundSubject(kind="oauth", op=op, ts=ts, jti=jti, issuer=issuer, client_id=client_id), "ok"
                )
        return BoundVerdict(None, "subject")
    except Exception:  # noqa: BLE001 -- a malformed header is a reason, never a crash
        return BoundVerdict(None, "malformed")

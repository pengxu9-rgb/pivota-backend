"""The issuing-agent assertion: which agent the GATEWAY verified, for a link issued over MCP.

ADR-025 D1 records a link's agent from the caller's OWN api key (routes/agent_auth.
resolve_issuing_agent_id). Over MCP that key never reaches this service: the gateway's commerce
kernel calls upstream with its own service key, and an MCP OAuth caller has no api key at all. The
gateway therefore signs the identity it verified into one header (PIVOTA-Agent
src/attribution/issuingAgentAssertion.js):

    X-Pivota-Issuing-Agent: v1.<b64url(JSON payload)>.<b64url(HMAC-SHA256(secret, "v1." + part1))>

    payload = {"v": 1, "kind": "agent", "sub": <agent_id>, "op": ..., "ts": <unix s>}
            | {"v": 1, "kind": "oauth", "iss": <issuer>, "cid": <client_id>, "op": ..., "ts": ...}

The secret (ISSUING_AGENT_ASSERTION_SECRET) is held by the gateway and this service only. It is
deliberately NOT the gateway's api key: that key is an ordinary agent key that also lives on
operator machines, so possessing it proves nothing about which process sent a request.

Trust, in order, and every step fails to None ("not bound to one agent"), never to a guess:
  1. the header verifies: version, MAC, `op` equals the operation being served, `ts` within
     MAX_SKEW_SECONDS of now;
  2. the subject resolves to an ACTIVE agent that is not a service identity: an `agent` subject by
     its agent_id; an `oauth` subject (decision 2026-09-24: a frontier connector is credited to the
     agent its client is registered to) only when it was issued by PIVOTA'S OWN authorization server
     to a CONFIDENTIAL client (one that authenticates with a secret at the token endpoint) that is
     actively registered in agent_oauth_clients. Those are the clients an operator provisioned and
     handed to a partner (scripts/agent_oauth_client.py). A public client from open dynamic
     registration is never credited, whatever its redirect URIs: anyone can register one with a
     partner's callback and complete a grant in their own browser (db/agent_oauth_clients.py).
The caller of this module additionally requires the REQUEST to be authenticated as a service
identity (the gateway) before consulting the header at all; see resolve_issuing_agent_for_request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Any, Collection, Dict, Mapping, Optional
from urllib.parse import urlsplit

from db.database import database

logger = logging.getLogger(__name__)

ISSUING_AGENT_ASSERTION_HEADER = "X-Pivota-Issuing-Agent"
ISSUING_AGENT_ASSERTION_SECRET_ENV = "ISSUING_AGENT_ASSERTION_SECRET"
_VERSION = "v1"

#: How far `ts` may be from this service's clock, either way. Wide enough for clock skew between two
#: Cloud Run services and a slow upstream queue; narrow enough that a captured header is useless soon.
MAX_SKEW_SECONDS = 300

#: surface_click_events.agent_id is VARCHAR(64); a longer id is not an agent we can record.
_MAX_AGENT_ID = 64
#: agent_oauth_clients.issuer / client_id are VARCHAR(512).
_MAX_OAUTH_FIELD = 512

#: One compact-token part: unpadded base64url, nothing else. `urlsafe_b64decode` alone would skip
#: characters outside the alphabet, making the MAC part malleable.
_B64URL_PART = re.compile(r"^[A-Za-z0-9_-]+$")

#: The token-endpoint auth methods that prove the caller holds the client's secret. "none" (a public
#: client, PKCE only) proves nothing about which client it is.
CONFIDENTIAL_AUTH_METHODS = frozenset({"client_secret_basic", "client_secret_post"})

_CLIENT_AUTH_SQL = (
    "SELECT token_endpoint_auth_method, client_secret_hash FROM mcp_oauth_clients WHERE client_id = :client_id"
)

_CLIENT_REDIRECT_URIS_SQL = "SELECT redirect_uris FROM mcp_oauth_clients WHERE client_id = :client_id"

#: Context keys an OAuth caller's click carries: WHICH PLATFORM the connector says it is, from the
#: redirect URIs its OAuth client registered (e.g. "claude.ai", "chatgpt.com", "loopback"). ANALYTICS
#: ONLY, never credit: open dynamic registration lets anyone register any callback URL, so the label
#: is unverified and never touches agent_id. `oauth_platform_verified` is true only when the client is
#: one Pivota PROVISIONED (and so credits an agent); a label on a public client is a claim, not a fact.
OAUTH_PLATFORM_KEY = "oauth_platform"
OAUTH_PLATFORM_VERIFIED_KEY = "oauth_platform_verified"
_MAX_PLATFORM_LABEL = 128
_DNS_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")

_ACTIVE_CLIENT_AGENT_SQL = """
SELECT agent_id FROM agent_oauth_clients
WHERE issuer = :issuer AND client_id = :client_id AND disabled_at IS NULL
LIMIT 1
"""


@dataclass(frozen=True)
class AssertedSubject:
    kind: str  # "agent" | "oauth"
    op: str
    ts: int
    agent_id: Optional[str] = None
    issuer: Optional[str] = None
    client_id: Optional[str] = None


def assertion_secret() -> Optional[str]:
    value = (os.getenv(ISSUING_AGENT_ASSERTION_SECRET_ENV) or "").strip()
    return value or None


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit:
        return None
    return text


def sign_issuing_agent_assertion(payload: Mapping[str, Any], secret: str) -> str:
    """The gateway's signer, mirrored for tests and operator probes. JSON is serialised exactly as the
    gateway's JSON.stringify does for these payloads (compact, key order as given)."""
    body = _b64url_encode(json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signing_input = f"{_VERSION}.{body}"
    mac = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(mac)}"


def verify_issuing_agent_assertion(
    token: Optional[str],
    *,
    op: str,
    secret: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[AssertedSubject]:
    """The subject a well-formed, correctly signed, fresh assertion for `op` names. Never raises."""
    try:
        key = secret if secret is not None else assertion_secret()
        if not key or not isinstance(token, str):
            return None
        parts = token.strip().split(".")
        if len(parts) != 3 or parts[0] != _VERSION:
            return None
        if not _B64URL_PART.match(parts[1]) or not _B64URL_PART.match(parts[2]):
            return None
        signing_input = f"{parts[0]}.{parts[1]}"
        expected = hmac.new(key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(parts[2])):
            return None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        if not isinstance(payload, dict) or type(payload.get("v")) is not int or payload.get("v") != 1:
            return None
        if payload.get("op") != op or not op:
            return None
        ts = payload.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, int):
            return None
        clock = time.time() if now is None else now
        if abs(clock - ts) > MAX_SKEW_SECONDS:
            return None
        kind = payload.get("kind")
        if kind == "agent":
            agent_id = _text(payload.get("sub"), _MAX_AGENT_ID)
            return AssertedSubject(kind="agent", op=op, ts=ts, agent_id=agent_id) if agent_id else None
        if kind == "oauth":
            issuer = _text(payload.get("iss"), _MAX_OAUTH_FIELD)
            client_id = _text(payload.get("cid"), _MAX_OAUTH_FIELD)
            if issuer and client_id:
                return AssertedSubject(kind="oauth", op=op, ts=ts, issuer=issuer, client_id=client_id)
        return None
    except Exception:  # noqa: BLE001 -- a malformed header is "no agent", never a failed request
        return None


def agent_is_active(agent: Optional[Mapping[str, Any]]) -> bool:
    """The same activity rule get_agent_context applies: is_active, else status == 'active'."""
    if not agent:
        return False
    is_active = agent.get("is_active")
    if is_active is None:
        status = agent.get("status")
        return (str(status).strip().lower() == "active") if status else True
    return bool(is_active)


async def _active_agent_id(agent_id: Optional[str], excluded_agent_ids: Collection[str]) -> Optional[str]:
    if not agent_id or agent_id in excluded_agent_ids:
        return None
    from db.agents import get_agent

    agent = await get_agent(agent_id)
    if not agent_is_active(agent):
        return None
    resolved = str((agent or {}).get("agent_id") or "").strip()
    return resolved if resolved == agent_id else None


def _own_issuer() -> Optional[str]:
    try:
        from services.mcp_oauth_as import issuer

        return issuer()
    except Exception:  # noqa: BLE001 -- no authorization server configured: no OAuth credit
        return None


async def is_confidential_client(client_id: str) -> bool:
    """Does our authorization server hold this client as CONFIDENTIAL (secret-authenticated)?"""
    row = await database.fetch_one(_CLIENT_AUTH_SQL, {"client_id": client_id})
    if row is None:
        return False
    record = dict(row)
    return (
        str(record.get("token_endpoint_auth_method") or "").strip().lower() in CONFIDENTIAL_AUTH_METHODS
        and bool(record.get("client_secret_hash"))
    )


async def agent_for_oauth_client(issuer: str, client_id: str) -> Optional[str]:
    """The agent a PROVISIONED confidential client of our own authorization server is actively
    registered to, or None. A foreign issuer's client, a public client, or an unregistered one: None."""
    own = _own_issuer()
    if not own or issuer != own:
        return None
    if not await is_confidential_client(client_id):
        return None
    row = await database.fetch_one(_ACTIVE_CLIENT_AGENT_SQL, {"issuer": issuer, "client_id": client_id})
    if row is None:
        return None
    value = str(dict(row).get("agent_id") or "").strip()
    return value or None


def platform_label(redirect_uris: Any) -> Optional[str]:
    """A short platform label from an OAuth client's registered redirect URIs, or None.

    https host (IDNA-encoded, lowercased, `www.` dropped) for a web connector; "(loopback)" for
    localhost / a loopback IP (desktop and CLI clients: Claude Desktop, Claude Code, Gemini CLI); "(ip)"
    for any other IP literal; "app:<scheme>" for a custom scheme (e.g. "app:cursor"); "(invalid)" for
    a host that is not a plain DNS name. Several distinct labels are joined with "+", sorted, so the
    same client always gets the same label.

    The URIs are registrant-controlled. A host must survive IDNA and match [a-z0-9.-] or it becomes
    "(invalid)", so no control, bidi or escape character reaches a terminal or report, and the
    built-in labels use characters no host can contain, so no registrant can impersonate them.
    """
    if not isinstance(redirect_uris, list):
        return None
    labels = set()
    for uri in redirect_uris:
        if not isinstance(uri, str) or not uri.strip():
            continue
        try:
            parts = urlsplit(uri.strip())
            parts.port  # a malformed port raises here too
        except ValueError:
            labels.add("(invalid)")
            continue
        scheme = (parts.scheme or "").lower()
        if scheme in ("http", "https"):
            host = (parts.hostname or "").strip().lower().rstrip(".")
            if not host:
                continue
            if host == "localhost" or host.endswith(".localhost"):
                labels.add("(loopback)")
                continue
            try:
                labels.add("(loopback)" if ipaddress.ip_address(host).is_loopback else "(ip)")
                continue
            except ValueError:
                pass
            try:
                host = host.encode("idna").decode("ascii").lower()
            except (UnicodeError, ValueError):
                labels.add("(invalid)")
                continue
            if not _DNS_HOST.fullmatch(host):
                labels.add("(invalid)")
                continue
            labels.add(host[4:] if host.startswith("www.") else host)
        elif re.fullmatch(r"[a-z][a-z0-9+.-]{0,30}", scheme):
            labels.add(f"app:{scheme}")
    if not labels:
        return None
    return "+".join(sorted(labels))[:_MAX_PLATFORM_LABEL]


async def oauth_platform_for_client(issuer: str, client_id: str) -> Optional[str]:
    """The platform label of a client of OUR authorization server, or None (foreign issuer, unknown client)."""
    own = _own_issuer()
    if not own or issuer != own:
        return None
    row = await database.fetch_one(_CLIENT_REDIRECT_URIS_SQL, {"client_id": client_id})
    if row is None:
        return None
    raw = dict(row).get("redirect_uris")
    try:
        uris = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return platform_label(uris)


@dataclass(frozen=True)
class IssuingContext:
    """Who a link is issued to (`agent_id`: credit) and what else is known about the caller
    (`context`: analytics only, e.g. the OAuth platform label). Empty when nothing is known."""

    agent_id: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)


async def resolve_asserted_issuing(
    token: Optional[str],
    *,
    op: str,
    excluded_agent_ids: Collection[str] = (),
    now: Optional[float] = None,
) -> IssuingContext:
    """The ACTIVE, non-service agent a verified assertion names, plus, for an OAuth subject, its
    unverified platform label. Never raises."""
    subject = verify_issuing_agent_assertion(token, op=op, now=now)
    if subject is None:
        return IssuingContext()
    agent_id: Optional[str] = None
    context: Dict[str, Any] = {}
    try:
        if subject.kind == "agent":
            agent_id = await _active_agent_id(subject.agent_id, excluded_agent_ids)
        elif subject.kind == "oauth":
            registered = await agent_for_oauth_client(subject.issuer or "", subject.client_id or "")
            agent_id = await _active_agent_id(registered, excluded_agent_ids)
            label = await oauth_platform_for_client(subject.issuer or "", subject.client_id or "")
            if label:
                context = {OAUTH_PLATFORM_KEY: label, OAUTH_PLATFORM_VERIFIED_KEY: agent_id is not None}
    except Exception as exc:  # noqa: BLE001 -- the link is still served; its click is agent-less
        logger.warning("issuing-agent assertion: subject lookup failed: %s", type(exc).__name__)
    return IssuingContext(agent_id=agent_id, context=context)


async def resolve_asserted_agent_id(
    token: Optional[str],
    *,
    op: str,
    excluded_agent_ids: Collection[str] = (),
    now: Optional[float] = None,
) -> Optional[str]:
    """The ACTIVE, non-service agent a verified assertion names, or None. Never raises."""
    resolved = await resolve_asserted_issuing(token, op=op, excluded_agent_ids=excluded_agent_ids, now=now)
    return resolved.agent_id

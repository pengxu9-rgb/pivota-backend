"""A2A rail client: the transport for the A2A token rail (payment orchestration P1).

The rail provider exposes ONE A2A endpoint, `{base}/a2a/agents/base-orchestrator/v1/message:send`
(A2A 0.3.0, JSON-RPC 2.0). Every step is a structured "card": the server answers with cards, the client
replies with `{component, action, payload}`. This module is ONLY the transport: it authenticates,
sends one structured action, and classifies the answer. It decides nothing about a purchase.

THE RULES THIS MODULE ENFORCES, each measured against the provider's sandbox on 2026-10-11:

1. NO CARD DATA LEAVES THIS MODULE. `payment.token-result` returns `networkToken.value` and
   `networkToken.cryptogram` in cleartext, by design on the provider's side. `_strip_credentials`
   removes every credential-shaped field from every card BEFORE a card is returned, so nothing above this
   module can log or store one. Each removal is counted (`credential_fields_stripped`), so an
   operator can see that the provider is still sending them.
2. ONLY ALLOWLISTED ACTIONS ARE SENT. `ALLOWED_ACTIONS` holds the payment-method and token steps
   Pivota uses. The provider's standalone money skills (`payment.pay-*`, `payment.charge-token-*`,
   `payment.token-result#charge`, refunds without an order, the trip cart) charge under the
   DEVELOPER's key, which would make Pivota the payee; they are not on the list, so they raise
   `ActionNotAllowed` before any byte is sent.
3. STRUCTURED ACTIONS ONLY, NEVER FREE TEXT. The provider's intent detection is an LLM. A free-text
   request is not deterministic, so this module has no way to send one. A free-text ANSWER where a
   card was expected (measured: `payment.method-add` without `user_email` answers "What's your
   email address?") is `unexpected_text`, and it fails closed.
4. NEVER RESEND A MAYBE-WRITE. The provider injects its own idempotency key per call
   ("new_per_call"), so a resent write after a timeout is a NEW write. A timeout or transport error on an action
   that may change state is `outcome_unknown`: the caller must read the state, never resend.
5. TRANSPORT HYGIENE, as in services/reap_agentic_client.py: an https allowlist of rail hosts
   checked on every call (the API key goes in a header addressed to that URL); redirects never
   followed; response bodies read with a hard size cap; no body, payload, token, key or member id
   is ever logged.

Dark by default: `from_env()` returns None unless A2A_RAIL_ENABLED is on and both
A2A_RAIL_BASE_URL and A2A_RAIL_API_KEY are set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

A2A_RAIL_ENABLED_ENV = "A2A_RAIL_ENABLED"
A2A_RAIL_BASE_URL_ENV = "A2A_RAIL_BASE_URL"
A2A_RAIL_API_KEY_ENV = "A2A_RAIL_API_KEY"
_TRUTHY = frozenset({"1", "true", "on", "yes"})

#: SHA-256 of the provider's production and test host names (lower case). The API key is only ever
#: sent to a host whose digest is here. Digests, not names: the provider is configured, not named.
ALLOWED_HOST_SHA256: FrozenSet[str] = frozenset({
    "c91cff4b3b7cf1ed9344a5334df60dc9751db61743bd7a41d082c35345097d7b",
    "a88fafbb95893904e7dcc07c3937e4119093120255ea2ca530ac27b11e865b97",
})
AGENT_ID = "base-orchestrator"
MESSAGE_PATH = f"/a2a/agents/{AGENT_ID}/v1/message:send"
TOKEN_PATH = "/orch-public/auth/token"

DEFAULT_TIMEOUT_S = 30.0
MAX_BODY_BYTES = 1024 * 1024
#: Re-mint an access token this long before the provider says it expires (tokens last 3600 s).
TOKEN_REFRESH_MARGIN_S = 120.0

#: (component, action) pairs this client may send. Everything else raises `ActionNotAllowed`.
ALLOWED_ACTIONS: FrozenSet[Tuple[str, str]] = frozenset({
    # list the buyer's cards
    ("payment.method-view", "submit"),
    # bind a card on the provider's hosted page (non-UnionPay and UnionPay)
    ("payment.method-add", "submit"),
    ("payment.method-add-required-card", "bound"),
    ("payment.method-add-await-card", "poll"),
    ("payment.method-add-required", "bound"),
    ("payment.method-add-await", "poll"),
    # choose a card inside a flow (method-add from the list starts binding)
    ("payment.method-list", "select-method"),
    ("payment.method-list", "add-method"),
    # remove a card, behind the provider's own confirm step
    ("payment.method-remove", "submit"),
    ("payment.method-remove-confirm", "confirm"),
    ("payment.method-remove-confirm", "cancel"),
    # mint a token bound to an order (no charge), and wait for the buyer's passkey
    ("payment.token-setup", "submit"),
    ("payment.action-required", "paid"),
    ("payment.await", "poll"),
})

#: The actions that only read. Every other allowed action may change state on the provider's side,
#: so a timeout on it is `outcome_unknown` (see rule 4).
READ_ONLY_ACTIONS: FrozenSet[Tuple[str, str]] = frozenset({
    ("payment.method-view", "submit"),
    ("payment.method-add-await-card", "poll"),
    ("payment.method-add-await", "poll"),
    ("payment.await", "poll"),
})

#: Keys whose whole subtree is a payment credential. Matched case-insensitively with `_` and `-`
#: removed, at any depth.
_CREDENTIAL_SUBTREE_KEYS = frozenset({"networktoken"})
#: Keys whose value is a credential wherever they appear.
_CREDENTIAL_LEAF_KEYS = frozenset({
    "cryptogram", "tokencryptogram", "pan", "cardnumber", "cvv", "cvc", "cvv2", "cvc2", "tavv",
})
_CARD_DIGITS_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
_MEMBER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_ERROR_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}\Z")


class A2ARailConfigError(RuntimeError):
    """Operator error: a wrong host, a missing key. Raised, never degraded into "unavailable"."""


class ActionNotAllowed(ValueError):
    """A (component, action) outside `ALLOWED_ACTIONS`. A programming error; nothing was sent."""


def is_enabled() -> bool:
    return (os.getenv(A2A_RAIL_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def _host_digest(host: str) -> str:
    return hashlib.sha256(host.lower().encode("utf-8")).hexdigest()


def validate_base_url(raw: str) -> str:
    """The base URL, or `A2ARailConfigError`. https, an allowed host, no userinfo, no port, no path."""
    value = str(raw or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise A2ARailConfigError(f"{A2A_RAIL_BASE_URL_ENV} must be https")
    if parsed.username or parsed.password:
        raise A2ARailConfigError(f"{A2A_RAIL_BASE_URL_ENV} must not contain userinfo")
    if parsed.port is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise A2ARailConfigError(f"{A2A_RAIL_BASE_URL_ENV} must be a bare https origin")
    if _host_digest(parsed.hostname or "") not in ALLOWED_HOST_SHA256:
        raise A2ARailConfigError(f"{A2A_RAIL_BASE_URL_ENV} host is not an allowed rail host")
    return f"https://{parsed.hostname.lower()}"


@dataclass(frozen=True)
class CardAction:
    id: str
    dispatch: Optional[str] = None
    role: Optional[str] = None


@dataclass(frozen=True)
class Card:
    """One structured card from the provider, with every credential field already removed."""

    component: str
    kind: Optional[str]
    data: Dict[str, Any]
    actions: Tuple[CardAction, ...]

    def action_ids(self) -> Tuple[str, ...]:
        return tuple(a.id for a in self.actions)


@dataclass
class A2AResult:
    """The outcome of one call. `ok` only for `kind == "cards"`.

    kind:
      cards             one or more structured cards (`cards`)
      error_card        the provider answered with its `error` card (`error_code`, `recoverable`)
      unexpected_text   free text where a card was expected; fail closed
      outcome_unknown   a maybe-write did not get a definite answer; read state, never resend
      timeout           a read-only action timed out (safe to retry)
      transport         a read-only action failed to connect (safe to retry)
      http_error        a non-2xx (redirects included)
      protocol_error    a JSON-RPC `error`, or a body that is not the documented shape
      too_large         the body exceeded `MAX_BODY_BYTES`
      auth_failed       the token exchange failed
    """

    kind: str
    cards: List[Card] = field(default_factory=list)
    texts: List[str] = field(default_factory=list)
    task_state: Optional[str] = None
    error_code: Optional[str] = None
    recoverable: Optional[bool] = None
    http_status: Optional[int] = None
    credential_fields_stripped: int = 0
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.kind == "cards"

    def card(self, component: str) -> Optional[Card]:
        for card in self.cards:
            if card.component == component:
                return card
        return None


# ── credential stripping ─────────────────────────────────────────────────────────────────────


def _canon(key: Any) -> str:
    return str(key).lower().replace("_", "").replace("-", "")


def _strip_credentials(value: Any) -> Tuple[Any, int]:
    """A copy of `value` with every credential field removed, and how many were removed."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        removed = 0
        for key, item in value.items():
            canon = _canon(key)
            if canon in _CREDENTIAL_SUBTREE_KEYS or canon in _CREDENTIAL_LEAF_KEYS:
                removed += 1
                continue
            cleaned, count = _strip_credentials(item)
            out[key] = cleaned
            removed += count
        return out, removed
    if isinstance(value, list):
        items = []
        removed = 0
        for item in value:
            cleaned, count = _strip_credentials(item)
            items.append(cleaned)
            removed += count
        return items, removed
    if isinstance(value, str) and _CARD_DIGITS_RE.search(value):
        return _CARD_DIGITS_RE.sub("[REMOVED]", value), 1
    return value, 0


# ── response parsing ─────────────────────────────────────────────────────────────────────────


def _latest_agent_parts(task: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The parts of the agent's answer to THIS message: agent messages after the last user one."""
    history = task.get("history") if isinstance(task.get("history"), list) else []
    last_user = -1
    for index, message in enumerate(history):
        if isinstance(message, dict) and message.get("role") == "user":
            last_user = index
    parts: List[Mapping[str, Any]] = []
    for message in history[last_user + 1:]:
        if isinstance(message, dict) and message.get("role") == "agent":
            parts.extend(p for p in (message.get("parts") or []) if isinstance(p, dict))
    return parts


def _card_from(data: Mapping[str, Any]) -> Tuple[Card, int]:
    actions = tuple(
        CardAction(id=str(a.get("id")), dispatch=a.get("dispatch"), role=a.get("role"))
        for a in (data.get("actions") or [])
        if isinstance(a, dict) and a.get("id")
    )
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    cleaned, removed = _strip_credentials(inner)
    return Card(
        component=str(data.get("component") or ""),
        kind=data.get("kind") if isinstance(data.get("kind"), str) else None,
        data=cleaned,
        actions=actions,
    ), removed


def parse_response(body: Any) -> A2AResult:
    """Classify one JSON-RPC response body. Pure: no I/O, no logging."""
    if not isinstance(body, dict):
        return A2AResult(kind="protocol_error")
    if "error" in body:
        return A2AResult(kind="protocol_error")
    task = body.get("result")
    if not isinstance(task, dict) or task.get("kind") != "task":
        return A2AResult(kind="protocol_error")
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    result = A2AResult(kind="cards", task_state=status.get("state") if isinstance(status.get("state"), str) else None)
    for part in _latest_agent_parts(task):
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            card, removed = _card_from(part["data"])
            result.credential_fields_stripped += removed
            if card.component == "error":
                code = str(card.data.get("code") or "")
                result.kind = "error_card"
                result.error_code = code if _ERROR_CODE_RE.match(code) else "UNKNOWN"
                result.recoverable = bool(card.data.get("recoverable"))
            result.cards.append(card)
        elif part.get("kind") == "text" and isinstance(part.get("text"), str):
            text, removed = _strip_credentials(part["text"][:2000])
            result.credential_fields_stripped += removed
            result.texts.append(text)
    if result.kind == "cards" and not result.cards:
        result.kind = "unexpected_text" if result.texts else "protocol_error"
    return result


# ── the client ───────────────────────────────────────────────────────────────────────────────


class A2ARailClient:
    """One instance per process. Holds a per-member access-token cache; nothing else is stateful."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = validate_base_url(base_url)
        if not str(api_key or "").strip():
            raise A2ARailConfigError(f"{A2A_RAIL_API_KEY_ENV} is not set")
        self._api_key = str(api_key).strip()
        self._timeout_s = float(timeout_s)
        self._transport = transport
        self._clock = clock
        self._tokens: Dict[str, Tuple[str, float]] = {}
        self._token_lock = asyncio.Lock()

    def _client(self):
        import httpx

        # `follow_redirects=False` explicitly: a 3xx re-sends the bearer token wherever Location
        # points, past the host allowlist. A redirect is a visible http_error instead.
        return httpx.AsyncClient(
            timeout=self._timeout_s, follow_redirects=False, transport=self._transport
        )

    async def _post_json(self, path: str, body: Mapping[str, Any], headers: Mapping[str, str]):
        """(status, parsed body or None, too_large). Raises httpx errors to the caller."""
        async with self._client() as client:
            async with client.stream(
                "POST", f"{self._base}{path}", json=dict(body),
                headers={"Content-Type": "application/json", **headers},
            ) as resp:
                chunks: List[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY_BYTES:
                        return resp.status_code, None, True
                    chunks.append(chunk)
                raw = b"".join(chunks)
        try:
            return resp.status_code, json.loads(raw.decode("utf-8")) if raw else None, False
        except (UnicodeDecodeError, ValueError):
            return resp.status_code, None, False

    async def _access_token(self, member_id: str) -> Optional[str]:
        async with self._token_lock:
            cached = self._tokens.get(member_id)
            if cached and cached[1] - self._clock() > TOKEN_REFRESH_MARGIN_S:
                return cached[0]
            try:
                status, body, too_large = await self._post_json(
                    TOKEN_PATH, {"api_key": self._api_key, "member_id": member_id}, {}
                )
            except Exception as exc:  # noqa: BLE001 - every failure is "no token", logged by type only
                logger.warning("a2a_rail: token exchange failed error_type=%s", type(exc).__name__)
                return None
            token = body.get("access_token") if isinstance(body, dict) else None
            if status != 200 or too_large or not isinstance(token, str) or not token:
                logger.warning("a2a_rail: token exchange refused http_status=%s", status)
                return None
            try:
                expires_in = float(body.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0.0
            self._tokens[member_id] = (token, self._clock() + max(0.0, expires_in))
            return token

    async def send_action(
        self,
        *,
        member_id: str,
        context_id: str,
        component: str,
        action: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> A2AResult:
        """Send ONE structured action in conversation `context_id` on behalf of `member_id`."""
        if (component, action) not in ALLOWED_ACTIONS:
            raise ActionNotAllowed(f"{component}#{action} is not an allowed rail action")
        if not _MEMBER_ID_RE.match(str(member_id or "")):
            raise ValueError("member_id must be an opaque id of 1..128 safe characters")
        if not _CONTEXT_ID_RE.match(str(context_id or "")):
            raise ValueError("context_id must be 1..128 safe characters")
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "user",
                    "contextId": context_id,
                    # A DataPart and nothing else: there is no way to send free text (rule 3).
                    "parts": [{"kind": "data", "data": {
                        "component": component, "action": action, "payload": dict(payload or {}),
                    }}],
                }
            },
        }
        json.dumps(body)  # fail here, not mid-request, on a payload that cannot be serialised
        may_write = (component, action) not in READ_ONLY_ACTIONS
        started = self._clock()

        token = await self._access_token(member_id)
        if token is None:
            return self._done(A2AResult(kind="auth_failed"), component, action, started)
        import httpx

        try:
            status, parsed, too_large = await self._post_json(
                MESSAGE_PATH, body, {"Authorization": f"Bearer {token}"}
            )
        except httpx.TimeoutException:
            kind = "outcome_unknown" if may_write else "timeout"
            return self._done(A2AResult(kind=kind), component, action, started)
        except httpx.HTTPError:
            kind = "outcome_unknown" if may_write else "transport"
            return self._done(A2AResult(kind=kind), component, action, started)
        if too_large:
            return self._done(A2AResult(kind="too_large", http_status=status), component, action, started)
        if status == 401:
            # The cached token was refused: forget it so the next call re-mints.
            self._tokens.pop(member_id, None)
        if status != 200:
            return self._done(A2AResult(kind="http_error", http_status=status), component, action, started)
        result = parse_response(parsed)
        result.http_status = status
        return self._done(result, component, action, started)

    def _done(self, result: A2AResult, component: str, action: str, started: float) -> A2AResult:
        result.elapsed_s = round(self._clock() - started, 3)
        logger.info(
            "a2a_rail: action=%s#%s kind=%s cards=%s http_status=%s stripped=%s elapsed_s=%s",
            component, action, result.kind, ",".join(c.component for c in result.cards),
            result.http_status, result.credential_fields_stripped, result.elapsed_s,
        )
        if result.credential_fields_stripped:
            logger.warning(
                "a2a_rail: removed %s credential field(s) from %s#%s (the provider still sends them)",
                result.credential_fields_stripped, component, action,
            )
        return result


def from_env(**kwargs: Any) -> Optional[A2ARailClient]:
    """The configured client, or None while the dial is off. A set but invalid config raises."""
    if not is_enabled():
        return None
    return A2ARailClient(
        os.getenv(A2A_RAIL_BASE_URL_ENV) or "",
        os.getenv(A2A_RAIL_API_KEY_ENV) or "",
        **kwargs,
    )

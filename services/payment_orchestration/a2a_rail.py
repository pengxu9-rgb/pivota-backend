"""A2A rail client: the transport for the A2A token rail (payment orchestration P1).

The rail provider exposes ONE A2A endpoint, `{base}/a2a/agents/base-orchestrator/v1/message:send`
(A2A 0.3.0, JSON-RPC 2.0). Every step is a structured "card": the server answers with cards, the client
replies with `{component, action, payload}`. This module is ONLY the transport: it authenticates,
sends one structured action, and classifies the answer. It decides nothing about a purchase.

THE RULES THIS MODULE ENFORCES, each measured against the provider's sandbox on 2026-10-11:

1. NO CARD DATA LEAVES THIS MODULE. `payment.token-result` returns `networkToken.value` and
   `networkToken.cryptogram` in cleartext, by design on the provider's side. `_strip_credentials`
   removes every credential-shaped field from every card BEFORE a card is returned, so nothing
   above this module can log or store one. Each removal is counted (`credential_fields_stripped`),
   so an operator can see that the provider is still sending them. The provider's free-text parts
   are never returned at all (only how many there were): prose is not a shape a stripper can trust.
2. ONLY ALLOWLISTED ACTIONS ARE SENT. `ALLOWED_ACTIONS` holds the payment-method and token steps
   Pivota uses. The provider's standalone money skills (`payment.pay-*`, `payment.charge-token-*`,
   `payment.token-result#charge`, refunds without an order) charge under the DEVELOPER's key,
   which would make Pivota the payee; they are not on the list, so they raise `ActionNotAllowed`
   before any byte is sent. Three allowed steps (`action-required#paid`, `visa-action-required#paid`,
   `await#poll`) are shared with a money flow, so (a) a conversation id must be one this client signed (`context_for`),
   and (b) any money-result card in an answer is `money_result`, logged at ERROR, never `ok`.
3. STRUCTURED ACTIONS ONLY, NEVER FREE TEXT. The provider's intent detection is an LLM. A free-text
   request is not deterministic, so this module has no way to send one. A free-text ANSWER where a
   card was expected (measured: `payment.method-add` without `user_email` answers "What's your
   email address?") is `unexpected_text`, and it fails closed.
4. NEVER RESEND A MAYBE-WRITE. The provider injects its own idempotency key per call
   ("new_per_call"), so a resent write after a timeout is a NEW write. A timeout or transport error on an action
   that may change state is `outcome_unknown`: the caller must read the state, never resend. So
   is any answer on such an action that does not prove the write was refused: a 5xx, a 408, an
   oversized or unparseable body. Only a 4xx is a definite "not done".
5. TRANSPORT HYGIENE, as in services/reap_agentic_client.py: the base URL is checked against an
   https host allowlist when the client is built, and every request goes to that fixed origin
   (the API key goes in a header addressed to it); no environment proxy or netrc; redirects never
   followed; response bodies read with a hard size cap; no body, payload, token, key or member id
   is ever logged.

Dark by default: `from_env()` returns None unless A2A_RAIL_ENABLED is on and both
A2A_RAIL_BASE_URL and A2A_RAIL_API_KEY are set.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
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
#: Optional: signs conversation ids. Set it so an API-key rotation does not orphan open flows.
A2A_RAIL_CONTEXT_SECRET_ENV = "A2A_RAIL_CONTEXT_SECRET"
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
#: Members whose access token is cached at once.
_TOKEN_CACHE_MAX = 10_000

#: (component, action) pairs this client may send. Everything else raises `ActionNotAllowed`.
ALLOWED_ACTIONS: FrozenSet[Tuple[str, str]] = frozenset({
    # list the buyer's cards
    ("payment.method-view", "submit"),
    # bind a card on the provider's hosted page (two card-scheme variants)
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
    # mint a token bound to an order (no charge), and wait for the buyer's approval: the scheme's
    # approval page (`visa-action-required`, measured 2026-10-12; `action-required` for the other scheme)
    # answers `paid` once the buyer is done, with the token result or the await card
    ("payment.token-setup", "submit"),
    ("payment.action-required", "paid"),
    ("payment.visa-action-required", "paid"),
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

#: Key prefixes whose whole subtree is a payment credential. Matched case-insensitively with `_`,
#: `-` and `.` removed, at any depth.
_CREDENTIAL_SUBTREE_PREFIXES = ("networktoken",)
#: Keys whose value is a credential wherever they appear (same matching).
_CREDENTIAL_LEAF_KEYS = frozenset({
    "cryptogram", "tokencryptogram", "cryptogramvalue", "pan", "dpan", "fpan", "cardnumber",
    "primaryaccountnumber", "accountnumber", "tokenvalue", "cvv", "cvc", "cvv2", "cvc2", "cvn",
    "securitycode", "tavv", "track1", "track2", "eci", "ucaf", "aav",
})
#: Key fragments that only ever name an authentication credential (`paymentCryptogram`, `cavv`,
#: `authenticationValue`, ...), matched as substrings of the canonical key.
_CREDENTIAL_KEY_PARTS = ("crypt", "cavv", "tavv", "authenticationvalue", "authvalue")
#: 14-19 digits, optionally grouped by single spaces or dashes, that pass the Luhn check: a PAN or
#: DPAN in any spelling. The Luhn gate keeps ids and timestamps that merely contain a long digit
#: run (1 in 56 random 32-hex ids does) from being taken for a card. (13-digit PANs are retired.)
_CARD_DIGITS_RE = re.compile(r"(?<!\d)\d(?:[ -]?\d){13,18}(?!\d)")
#: Padded base64 of 15+ bytes: the shape of a cryptogram/TAVV. Not applied to URL strings.
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{20,}={1,2}")
#: Exact keys (canonical) whose value is an order/charge reference or a time, where a long digit
#: run is expected. Only the digit rule is relaxed there, and never for a key that names a card,
#: token or account (`_CARD_WORDS`).
_REFERENCE_KEYS = frozenset({
    "orderno", "ordernumber", "orderid", "orderref", "orderreference", "merchantorderno",
    "merchantorderid", "merchantorderref", "tradeno", "outtradeno", "chargeno",
})
_TIME_KEY_SUFFIXES = ("at", "time", "timestamp", "ms", "date")
_CARD_WORDS = ("card", "pan", "token", "account", "number", "cvv", "cvc", "crypt", "track", "secret")
_PURE_DIGITS_RE = re.compile(r"^\d{1,19}\Z")
#: Past this depth a subtree is dropped (and counted): nothing legitimate nests this deep.
_MAX_STRIP_DEPTH = 24
_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}\Z")
#: Card components that report money having moved. One of these in an answer means a money flow
#: ran in a conversation this client touched: `money_result`, logged at ERROR.
_MONEY_RESULT_PREFIXES = (
    "payment.pay-", "payment.card-result", "payment.charge", "payment.refund", "payment.capture",
)
#: Hex digits of the HMAC that binds a conversation id to this client's key.
_CONTEXT_SIG_HEX = 16
_MEMBER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_CONTEXT_REF_RE = re.compile(r"^[A-Za-z0-9_]{1,80}\Z")
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


_FAILED_TASK_STATES = frozenset({"failed", "rejected", "canceled"})
#: A2A 0.3 task states. Anything else from the provider is reported as None, not passed upward.
_TASK_STATES = frozenset({
    "submitted", "working", "input-required", "completed", "canceled", "failed", "rejected",
    "auth-required", "unknown",
})


@dataclass
class A2AResult:
    """The outcome of one call. `ok` only for `kind == "cards"`.

    kind:
      cards             one or more structured cards (`cards`); `ok` unless the task failed
      error_card        the provider answered with its `error` card (`error_code`, `recoverable`)
      money_result      an answer carried a money-result card; never `ok`, logged at ERROR
      unexpected_text   free text where a card was expected; fail closed (on a maybe-write it is
                        NOT safe to resend: check `resend_safe`)
      outcome_unknown   a maybe-write did not get a definite answer; read state, never resend
      timeout           a read-only action timed out (safe to retry)
      transport         a read-only action failed to connect (safe to retry)
      http_error        a non-2xx (redirects included); on a maybe-write only a 4xx other than 408
      protocol_error    a JSON-RPC `error`, or a body that is not the documented shape (read-only)
      too_large         the body exceeded `MAX_BODY_BYTES` (read-only)
      auth_failed       the token exchange failed
    """

    kind: str
    cards: List[Card] = field(default_factory=list)
    #: How many free-text parts the answer had. Their content is never returned (rule 1).
    text_parts: int = 0
    task_state: Optional[str] = None
    error_code: Optional[str] = None
    recoverable: Optional[bool] = None
    http_status: Optional[int] = None
    credential_fields_stripped: int = 0
    #: The conversation id the provider answered under (validated), to check it kept ours.
    context_id: Optional[str] = None
    #: Whether the action sent may have changed state on the provider's side.
    may_write: bool = False
    elapsed_s: float = 0.0

    @property
    def resend_safe(self) -> bool:
        """True only when sending the same action again cannot do a write twice: a read, or a
        write the provider definitely refused (a 4xx other than 408, an error card, no token)."""
        return not self.may_write or self.kind in ("http_error", "error_card", "auth_failed")

    @property
    def ok(self) -> bool:
        return self.kind == "cards" and self.task_state not in _FAILED_TASK_STATES

    def card(self, component: str) -> Optional[Card]:
        for card in self.cards:
            if card.component == component:
                return card
        return None


# ── credential stripping ─────────────────────────────────────────────────────────────────────


def _canon(key: Any) -> str:
    return str(key).lower().replace("_", "").replace("-", "").replace(".", "")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        n = int(char)
        if index % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _has_card_number(text: str) -> bool:
    return any(_luhn(re.sub(r"[ -]", "", m.group(0))) for m in _CARD_DIGITS_RE.finditer(text))


def _is_credential_key(key: Any, parent: str = "") -> bool:
    """`parent` is the canonical key of the enclosing object (`token` for `token.number`)."""
    canon = _canon(key)
    text = str(key)
    return (
        canon in _CREDENTIAL_LEAF_KEYS
        or canon.startswith(_CREDENTIAL_SUBTREE_PREFIXES)
        or any(part in canon for part in _CREDENTIAL_KEY_PARTS)
        or ("token" in canon and canon.endswith(("number", "value", "pan")))
        or ("token" in parent and canon in ("number", "value", "pan", "data"))
        or _has_card_number(text)
        or bool(_BASE64_BLOB_RE.search(text))
    )


def _digits_expected(key: Any) -> bool:
    """A key whose value may legitimately be a long number: an order reference or a time."""
    canon = _canon(key)
    if any(word in canon for word in _CARD_WORDS):
        return canon in _REFERENCE_KEYS and "card" not in canon and "token" not in canon
    return canon in _REFERENCE_KEYS or canon.endswith(_TIME_KEY_SUFFIXES)


def _strip_string(value: str, *, digits: bool = True) -> Tuple[Optional[str], int]:
    """The string, or None when it carries a credential. The WHOLE value goes, not a substring:
    a mangled id would otherwise be sent back to the provider instead of failing loudly."""
    if digits and _has_card_number(value):
        return None, 1
    if not value.startswith(("https://", "http://")) and _BASE64_BLOB_RE.search(value):
        return None, 1
    return value, 0


def _strip_credentials(value: Any, depth: int = 0, parent: str = "") -> Tuple[Any, int]:
    """A copy of `value` with every credential field removed, and how many were removed."""
    if depth > _MAX_STRIP_DEPTH:
        return None, 1
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        removed = 0
        for key, item in value.items():
            if _is_credential_key(key, parent):
                removed += 1
                continue
            if _digits_expected(key) and (
                isinstance(item, str) or (isinstance(item, int) and not isinstance(item, bool))
            ):
                if isinstance(item, int):
                    out[key] = item
                    continue
                cleaned, count = _strip_string(item, digits=not _PURE_DIGITS_RE.match(item))
                out[key] = cleaned
                removed += count
                continue
            cleaned, count = _strip_credentials(item, depth + 1, _canon(key))
            out[key] = cleaned
            removed += count
        return out, removed
    if isinstance(value, list):
        items = []
        removed = 0
        for item in value:
            cleaned, count = _strip_credentials(item, depth + 1, parent)
            items.append(cleaned)
            removed += count
        return items, removed
    if isinstance(value, str):
        return _strip_string(value)
    if isinstance(value, int) and not isinstance(value, bool) and _has_card_number(str(abs(value))):
        return None, 1
    if isinstance(value, float) and value.is_integer() and _has_card_number(str(int(abs(value)))):
        return None, 1
    return value, 0


def _name(value: Any) -> Optional[str]:
    """A provider-supplied identifier (component, kind, action id), or None if it is not one."""
    if isinstance(value, str) and _NAME_RE.match(value) and not _has_card_number(value):
        return value
    return None


def _is_money_result(component: str) -> bool:
    return component.startswith(_MONEY_RESULT_PREFIXES)


# ── response parsing ─────────────────────────────────────────────────────────────────────────


def _latest_agent_parts(task: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The parts of the agent's answer to THIS message: agent messages after the last user one,
    or, when the history ends with the user's own message, the task's `status.message`."""
    history = task.get("history") if isinstance(task.get("history"), list) else []
    last_user = -1
    for index, message in enumerate(history):
        if isinstance(message, dict) and message.get("role") == "user":
            last_user = index
    messages = [m for m in history[last_user + 1:] if isinstance(m, dict) and m.get("role") == "agent"]
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    if not messages and isinstance(status.get("message"), dict) and status["message"].get("role") == "agent":
        messages = [status["message"]]
    parts: List[Mapping[str, Any]] = []
    for message in messages:
        if isinstance(message.get("parts"), list):
            parts.extend(p for p in message["parts"] if isinstance(p, dict))
    return parts


def _card_from(data: Mapping[str, Any]) -> Tuple[Card, int]:
    """Identifiers must look like identifiers (a PAN in an action id is dropped, not returned)."""
    raw_actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    actions = tuple(
        CardAction(id=action_id, dispatch=_name(a.get("dispatch")), role=_name(a.get("role")))
        for a in raw_actions
        if isinstance(a, dict) and (action_id := _name(a.get("id")))
    )
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    cleaned, removed = _strip_credentials(inner)
    component = _name(data.get("component")) or ""
    if not component and isinstance(data.get("component"), str) and _has_card_number(data["component"]):
        removed += 1
    kind = _name(data.get("kind"))
    return Card(component=component, kind=kind, data=cleaned, actions=actions), removed


def _artifacts_report_money(task: Mapping[str, Any]) -> bool:
    artifacts = task.get("artifacts") if isinstance(task.get("artifacts"), list) else []
    for artifact in artifacts:
        parts = artifact.get("parts") if isinstance(artifact, dict) else None
        for part in parts if isinstance(parts, list) else []:
            data = part.get("data") if isinstance(part, dict) else None
            component = data.get("component") if isinstance(data, dict) else None
            if isinstance(component, str) and _is_money_result(component):
                return True
    return False


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
    state = status.get("state")
    echoed = task.get("contextId")
    result = A2AResult(
        kind="cards",
        task_state=state if isinstance(state, str) and state in _TASK_STATES else None,
        context_id=(
            echoed if isinstance(echoed, str) and _CONTEXT_ID_RE.match(echoed) and not _has_card_number(echoed)
            else None
        ),
    )
    money = False
    for part in _latest_agent_parts(task):
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            card, removed = _card_from(part["data"])
            result.credential_fields_stripped += removed
            if card.component == "error":
                code = str(card.data.get("code") or "")
                result.kind = "error_card"
                result.error_code = code if _ERROR_CODE_RE.match(code) else "UNKNOWN"
                result.recoverable = bool(card.data.get("recoverable"))
            money = money or _is_money_result(card.component)
            result.cards.append(card)
        elif part.get("kind") == "text":
            result.text_parts += 1
    # Artifacts are never returned, but a money-result card there is still a money flow.
    money = money or _artifacts_report_money(task)
    if money:
        result.kind = "money_result"
    elif result.kind == "cards" and not result.cards:
        result.kind = "unexpected_text" if result.text_parts else "protocol_error"
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
        context_secret: Optional[str] = None,
        total_timeout_s: Optional[float] = None,
    ) -> None:
        self._base = validate_base_url(base_url)
        if not str(api_key or "").strip():
            raise A2ARailConfigError(f"{A2A_RAIL_API_KEY_ENV} is not set")
        self._api_key = str(api_key).strip()
        self._timeout_s = float(timeout_s)
        #: Wall-clock bound on one whole call (token exchange or action), on top of httpx's.
        self._total_timeout_s = float(total_timeout_s if total_timeout_s is not None else 2 * self._timeout_s)
        self._transport = transport
        self._clock = clock
        self._tokens: Dict[str, Tuple[str, float]] = {}
        # One in-flight exchange per member: a slow exchange for one buyer stalls only that buyer,
        # and concurrent calls for the same buyer share it. Entries leave when the exchange ends.
        self._minting: Dict[str, "asyncio.Future[Optional[str]]"] = {}
        # Signs conversation ids. Its own secret when configured, so rotating the API key does not
        # orphan conversations waiting on a buyer; the API key otherwise.
        secret = str(context_secret or "").strip() or self._api_key
        self._context_key = secret.encode("utf-8")

    def context_for(self, member_id: str, ref: str) -> str:
        """The conversation id for one Pivota purchase (`ref`) by one buyer (`member_id`), signed.

        `send_action` accepts only ids made here FOR THAT MEMBER, so a caller can neither continue
        a conversation this client did not start (rule 2: some allowed steps are shared with a
        money flow) nor continue one buyer's conversation under another buyer's token.
        """
        if not _CONTEXT_REF_RE.match(str(ref or "")):
            raise ValueError("context ref must be 1..80 of [A-Za-z0-9_]")
        if not _MEMBER_ID_RE.match(str(member_id or "")):
            raise ValueError("member_id must be an opaque id of 1..128 safe characters")
        return f"pvt-{ref}-{self._context_sig(member_id, ref)}"

    def _context_sig(self, member_id: str, ref: str) -> str:
        message = f"a2a-context:{member_id}:{ref}".encode("utf-8")
        return hmac.new(self._context_key, message, hashlib.sha256).hexdigest()[:_CONTEXT_SIG_HEX]

    def _is_own_context(self, context_id: str, member_id: str) -> bool:
        match = re.fullmatch(r"pvt-([A-Za-z0-9_]{1,80})-([0-9a-f]{%d})" % _CONTEXT_SIG_HEX, context_id)
        return bool(match) and hmac.compare_digest(match.group(2), self._context_sig(member_id, match.group(1)))

    def _client(self):
        import httpx

        # `follow_redirects=False` explicitly: a 3xx re-sends the bearer token wherever Location
        # points, past the host allowlist. A redirect is a visible http_error instead.
        # `trust_env=False`: no environment proxy and no .netrc credentials for this host.
        return httpx.AsyncClient(
            timeout=self._timeout_s, follow_redirects=False, transport=self._transport, trust_env=False
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
        except (UnicodeDecodeError, ValueError, RecursionError):
            return resp.status_code, None, False

    async def _access_token(self, member_id: str) -> Optional[str]:
        cached = self._tokens.get(member_id)
        if cached and cached[1] - self._clock() > TOKEN_REFRESH_MARGIN_S:
            return cached[0]
        pending = self._minting.get(member_id)
        if pending is None:
            pending = asyncio.ensure_future(self._mint(member_id))
            self._minting[member_id] = pending
            pending.add_done_callback(lambda _f, m=member_id: self._minting.pop(m, None))
        # Shielded: a caller that gives up does not cancel the exchange others are waiting on.
        return await asyncio.shield(pending)

    async def _mint(self, member_id: str) -> Optional[str]:
        try:
            status, body, too_large = await asyncio.wait_for(
                self._post_json(TOKEN_PATH, {"api_key": self._api_key, "member_id": member_id}, {}),
                self._total_timeout_s,
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
        now = self._clock()
        if len(self._tokens) >= _TOKEN_CACHE_MAX:
            # Bounded: drop expired entries, then the soonest-expiring, never the one being set.
            for stale in [m for m, (_, exp) in self._tokens.items() if exp <= now]:
                self._tokens.pop(stale, None)
            while len(self._tokens) >= _TOKEN_CACHE_MAX:
                self._tokens.pop(min(self._tokens, key=lambda m: self._tokens[m][1]), None)
        self._tokens[member_id] = (token, now + max(0.0, expires_in))
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
        if not self._is_own_context(context_id, member_id):
            raise ValueError("context_id was not made by this client's context_for() for this member")
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
            # httpx's timeout is per phase (a drip-fed answer never trips it): this bounds the call.
            status, parsed, too_large = await asyncio.wait_for(
                self._post_json(MESSAGE_PATH, body, {"Authorization": f"Bearer {token}"}),
                self._total_timeout_s,
            )
        except (httpx.TimeoutException, asyncio.TimeoutError):
            kind = "outcome_unknown" if may_write else "timeout"
            return self._done(A2AResult(kind=kind), component, action, started)
        except httpx.HTTPError:
            kind = "outcome_unknown" if may_write else "transport"
            return self._done(A2AResult(kind=kind), component, action, started)
        except Exception:  # noqa: BLE001 - anything else mid-request: same rule, classified not raised
            kind = "outcome_unknown" if may_write else "transport"
            return self._done(A2AResult(kind=kind), component, action, started)
        if status == 401:
            # The cached token was refused: forget it so the next call re-mints. Never resent here.
            self._tokens.pop(member_id, None)
        if too_large:
            kind = "outcome_unknown" if may_write else "too_large"
            return self._done(A2AResult(kind=kind, http_status=status), component, action, started)
        if status != 200:
            # Only a 4xx (other than 408) proves a write was refused. A 5xx, a 408, a 202 or a 3xx
            # does not: on a maybe-write those are outcome_unknown.
            refused = 400 <= status < 500 and status != 408
            kind = "outcome_unknown" if may_write and not refused else "http_error"
            return self._done(A2AResult(kind=kind, http_status=status), component, action, started)
        result = parse_response(parsed)
        result.http_status = status
        if may_write and result.kind == "protocol_error":
            result.kind = "outcome_unknown"
        return self._done(result, component, action, started)

    def _done(self, result: A2AResult, component: str, action: str, started: float) -> A2AResult:
        result.may_write = (component, action) not in READ_ONLY_ACTIONS
        result.elapsed_s = round(self._clock() - started, 3)
        logger.info(
            "a2a_rail: action=%s#%s kind=%s cards=%s http_status=%s stripped=%s elapsed_s=%s",
            component, action, result.kind, ",".join(c.component for c in result.cards[:8]),
            result.http_status, result.credential_fields_stripped, result.elapsed_s,
        )
        if result.kind == "money_result":
            logger.error(
                "a2a_rail: a money-result card came back on %s#%s; nothing may act on this answer",
                component, action,
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
    kwargs.setdefault("context_secret", os.getenv(A2A_RAIL_CONTEXT_SECRET_ENV) or None)
    return A2ARailClient(
        os.getenv(A2A_RAIL_BASE_URL_ENV) or "",
        os.getenv(A2A_RAIL_API_KEY_ENV) or "",
        **kwargs,
    )

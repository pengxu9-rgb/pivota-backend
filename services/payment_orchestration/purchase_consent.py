"""The AgenPas consent gate: no rail card / approval page reaches anyone before the buyer accepted
Pivota's terms of service and privacy policy on Pivota's own page (rail-neutral: Reap and the A2A rail).

A payment partner's production rule: the buyer sees and accepts Pivota's terms AND privacy policy before a
card is stored and again before every approval, and we record it. The record is db/agent_purchase_consents
(migration 289), written by POST /agent/internal/commerce/purchases/{id}/consents from the AgenPas page.

THE GATE (`AGENPAS_CONSENT_GATE`, default off, read on every call). It sits in the rail view builders every
surface takes its hosted URL from (routes/agent_commerce_reap._owner_public_body, which the Reap rail's
GET / list / create / resume / recover and the unified door use, and a2a_purchase_view.owner_view), so the
public door, the Reap routes, the purchase.* webhook payloads, the MCP / UCP answers built on them and the
internal AgenPas read are gated by one piece of code. On, for a state whose page needs a consent
(`card_binding` while the card is bound, `approval` while the buyer approves) and while no consent row exists
for (rail, rail purchase, step, approval epoch), the rail detail:
  - drops `hosted_url` (the deadline, `hosted_url_expires_at` / `approval_deadline`, stays),
  - adds `buyer_step = {kind, expires_at, consent_required: true}`,
and purchase_view.unified_body then builds `next_action = {kind, expires_at, consent_required: true}` with no
url. The hosted URL string appears nowhere in such a body.

THE SHIPPING-ADDRESS STEP (A2A only; the owner's decision, 11-20). While an A2A purchase is `opened` and waits
for the buyer's contact (its view carries `shipping_address_required`, the fields missing; only with this gate
on: a2a_shipping_address.capture_enabled requires it), the form is a step like a page: with no consent row for
(a2a, rail purchase, `shipping_address`, epoch 0) the gate WITHHOLDS `shipping_address_required` (no field
list, no form) and sets `buyer_step = {kind: "shipping_address", expires_at, consent_required: true,
available}`, by the same `offer_available` rule; unified_body then builds `next_action = {kind:
"shipping_address", consent_required: true, expires_at}`, never `fields_missing`. The write
(POST .../shipping-address) refuses 409 consent_required without that row. Epoch 0: the address is asked once
per purchase, and a terms / privacy version bump does not re-ask within a step (as for the other steps). On a LIVE read (the door, the Reap routes, the
internal read) `buyer_step.available` says whether the consent can be given now, computed by the serving
process (`offer_available`, the rule the internal read's block uses); purchase.* payloads never carry it. Stored `purchase.*` payloads
are re-gated by whichever process sends or lists them, at that moment (`gate_stored_payload`). `authenticate` (the A2A rail's 3DS page while it
places the order) is NEVER gated. Stored payloads re-gate the shipping form too (its own path: it has no url). A consent read that fails is "no consent": fail closed, never open.

THE OFFER. The internal read, gate on, carries a `consent` block for a state that needs one, with a
stateless offer: `offer.id` = HMAC-SHA256(AGENPAS_CONSENT_OFFER_SECRET, length-prefixed agent id, rail, rail
purchase id, step, approval epoch, terms version, privacy version, terms url, privacy url, issued_at). An offer
that verifies only under AGENPAS_CONSENT_OFFER_SECRET_PREVIOUS (a rotation) is stale (409), not refused. Nothing is
written on a read. The POST echoes {step, offer_id, issued_at, terms_version, privacy_version}; it is
accepted only when the HMAC verifies against the CURRENT configuration (the backend, not the browser, says
which version is current), the offer is at least AGENPAS_CONSENT_MIN_DWELL_SECONDS (3) and at most
MAX_OFFER_AGE_SECONDS (1800) old, and the purchase is still in the step. The dwell and the gateway's
Origin / Sec-Fetch-Site checks stop naive automation only: the agent holds the AgenPas link too, and the
record says exactly that (`evidence = link_holder_on_pivota_page`).

APPROVAL EPOCH. An approval consent binds to the purchase's single approval. Reap: 1 (a re-quote fails the
purchase; there is no edge back to quoting). A2A: 1 as well, because the ledger has NO
`awaiting_authorization -> quoting` edge (db/a2a_rail_ledger.ALLOWED_TRANSITIONS; it was allowed but never
written, and was closed for this). tests/test_agenpas_consent_gate.py fails if the edge is re-added or a raw
SQL writer of `state = 'quoting'` appears; re-adding it needs an approval epoch here, or an approval consent
would carry over to a re-quoted price.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from services.payment_orchestration.rails import unified_state

logger = logging.getLogger(__name__)

GATE_ENV = "AGENPAS_CONSENT_GATE"
WRITE_ENABLED_ENV = "AGENPAS_CONSENT_WRITE_ENABLED"
TERMS_URL_ENV = "AGENPAS_TERMS_URL"
TERMS_VERSION_ENV = "AGENPAS_TERMS_VERSION"
PRIVACY_URL_ENV = "AGENPAS_PRIVACY_URL"
PRIVACY_VERSION_ENV = "AGENPAS_PRIVACY_VERSION"
OFFER_SECRET_ENV = "AGENPAS_CONSENT_OFFER_SECRET"
#: Optional: the secret before the last rotation. Verification only (an offer minted under it is answered
#: 409 consent_stale, so the page re-reads); never used to mint.
OFFER_SECRET_PREVIOUS_ENV = "AGENPAS_CONSENT_OFFER_SECRET_PREVIOUS"
MIN_DWELL_ENV = "AGENPAS_CONSENT_MIN_DWELL_SECONDS"
#: The AgenPas internal read's key, and the consent write's own key (routes/agent_internal_commerce_purchases).
READ_KEY_ENV = "AGENPAS_INTERNAL_READ_KEY"
CONSENT_KEY_ENV = "AGENPAS_INTERNAL_CONSENT_KEY"

#: A configured offer secret shorter than this counts as missing: the gate then offers nothing (fail closed).
OFFER_SECRET_MIN_LENGTH = 32
#: The consent write's key: shorter than this (or equal to the read key) counts as missing.
CONSENT_KEY_MIN_LENGTH = 32
MIN_DWELL_SECONDS_DEFAULT = 3
#: The dwell a configuration may ask for: a whole number of seconds, 0..120 (the gateway's contract).
MIN_DWELL_SECONDS_MAX = 120
#: An offer older than this is stale: the page re-reads and shows a fresh one.
MAX_OFFER_AGE_SECONDS = 1800

_TRUTHY = frozenset({"1", "true", "on", "yes"})
#: A version tag: printable ASCII, no space, at most 32 (the column width).
_VERSION_RE = re.compile(r"^[!-~]{1,32}\Z")
_URL_MAX_CHARS = 512
#: The offer's issued_at, exactly as minted: ISO-8601 UTC to the second, `Z`.
_ISSUED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_ISSUED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_OFFER_TAG = b"agenpas-consent-offer/v1"

#: Unified state -> the step whose consent it asks for. The approval consent may be taken while the rail
#: is still pricing (`locking`, the "confirming your total" screen) or while the buyer approves.
STEP_FOR_STATE: Dict[str, str] = {
    "needs_payment_method": "card_binding",
    "locking": "approval",
    "awaiting_buyer_authorization": "approval",
}
#: Unified state -> the step whose hosted page the gate withholds. Only states that carry a page; never
#: `placing` (the A2A rail's 3DS `authenticate` page).
GATED_PAGE_FOR_STATE: Dict[str, str] = {
    "needs_payment_method": "card_binding",
    "awaiting_buyer_authorization": "approval",
}

#: The A2A rail's shipping-address step (no unified state of its own: `routing` while the rail is `opened`).
SHIPPING_STEP = "shipping_address"

#: A consent snapshot: {(rail, rail_purchase_id, step, approval_epoch)}.
Snapshot = FrozenSet[Tuple[str, str, str, int]]


class _Sentinel:
    def __init__(self, name: str):
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return self._name


#: "Read the consent yourself" (a single read).
LOOKUP = _Sentinel("LOOKUP")
#: "The consent read failed": every gated page is withheld.
FAILED = _Sentinel("FAILED")


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUTHY


def gate_on() -> bool:
    return _flag(GATE_ENV)


def write_enabled() -> bool:
    return _flag(WRITE_ENABLED_ENV)


def _env_key(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def configured_consent_key() -> Optional[str]:
    """The consent write's key, or None when it is missing, shorter than 32 characters, or the read key."""
    key = _env_key(CONSENT_KEY_ENV)
    if len(key) < CONSENT_KEY_MIN_LENGTH or _same(key, _env_key(READ_KEY_ENV)):
        return None
    return key


#: Whether THIS process registered the consent write (set by the route module's `mount`, which runs wherever
#: `main:app` is built: web AND the worker run the same app, each with its own boot-time dial). None: `mount`
#: never ran here (a script, a test), so only the environment can say. Only a LIVE read's `available` is
#: meaningful (the serving process answers for its own routes); purchase.* payloads never carry it.
_WRITE_MOUNTED: Optional[bool] = None


def record_write_mount(mounted: bool) -> None:
    global _WRITE_MOUNTED
    _WRITE_MOUNTED = bool(mounted)


def consent_can_be_given() -> bool:
    """Whether a consent COULD be recorded, by configuration alone: the terms / privacy configuration and offer
    secret set, the write's dial on, its key valid. The same answer in every process (web and the worker share
    the environment), unlike `write_ready`, which also asks whether THIS process mounted the write's router."""
    return config() is not None and write_enabled() and configured_consent_key() is not None


def write_ready() -> bool:
    """Whether a consent POST could succeed now: registered in this process (when `mount` ran here), its dial
    on, its key valid. `available` (in the read's block and a live read's `buyer_step`) is false otherwise."""
    return _WRITE_MOUNTED is not False and write_enabled() and configured_consent_key() is not None


def _secret_ok(secret: str) -> bool:
    """At least 32 characters, and neither the read key nor the consent key (a secret that is also a key is a
    secret held by whoever holds that key)."""
    return (len(secret) >= OFFER_SECRET_MIN_LENGTH and not _same(secret, _env_key(READ_KEY_ENV))
            and not _same(secret, _env_key(CONSENT_KEY_ENV)))


def min_dwell_seconds() -> int:
    raw = (os.getenv(MIN_DWELL_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return MIN_DWELL_SECONDS_DEFAULT
    return value if 0 <= value <= MIN_DWELL_SECONDS_MAX else MIN_DWELL_SECONDS_DEFAULT


def valid_version(value: Any) -> bool:
    return isinstance(value, str) and bool(_VERSION_RE.match(value))


def valid_legal_url(value: Any) -> bool:
    """https, a host, no userinfo, the default port, printable ASCII, at most 512 characters. The gateway
    narrows the host to its own allowlist (AGENPAS_LEGAL_HOSTS) before it renders a link."""
    if not isinstance(value, str) or not value or len(value) > _URL_MAX_CHARS or not re.match(r"^[!-~]+\Z", value):
        return False
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and "@" not in parts.netloc and port in (None, 443)


@dataclass(frozen=True)
class ConsentConfig:
    terms_url: str
    terms_version: str
    privacy_url: str
    privacy_version: str
    secret: bytes

    def __repr__(self) -> str:  # never the secret
        return f"ConsentConfig(terms_version={self.terms_version!r}, privacy_version={self.privacy_version!r})"


def config() -> Optional[ConsentConfig]:
    """The configured terms and privacy policy and the offer secret, or None when any is missing or
    malformed (the gate then offers no consent and withholds every page: fail closed)."""
    terms_url = (os.getenv(TERMS_URL_ENV) or "").strip()
    privacy_url = (os.getenv(PRIVACY_URL_ENV) or "").strip()
    terms_version = (os.getenv(TERMS_VERSION_ENV) or "").strip()
    privacy_version = (os.getenv(PRIVACY_VERSION_ENV) or "").strip()
    secret = _env_key(OFFER_SECRET_ENV)
    if (not valid_legal_url(terms_url) or not valid_legal_url(privacy_url) or not valid_version(terms_version)
            or not valid_version(privacy_version) or not _secret_ok(secret)):
        return None
    return ConsentConfig(terms_url, terms_version, privacy_url, privacy_version, secret.encode("utf-8"))


def previous_config(cfg: ConsentConfig) -> Optional[ConsentConfig]:
    """`cfg` under the previous offer secret, for verification only; None when it is unset, unusable, or the
    current secret."""
    previous = _env_key(OFFER_SECRET_PREVIOUS_ENV)
    if not _secret_ok(previous) or _same(previous, cfg.secret.decode("utf-8")):
        return None
    return dataclasses.replace(cfg, secret=previous.encode("utf-8"))


def log_boot_state(service: str) -> None:
    """Once per boot, at WARNING (prod drops module-logger INFO), on web AND worker: whether this process gates,
    and whether it captures a missing shipping contact (a2a_shipping_address.capture_enabled, with its own dial
    and whether a consent can be given here). The gate is decided by the process that builds or SENDS a body,
    and capture by the door (web) and the worker alike: a worker that answers capture=off while web answers on
    ends every waiting purchase at its next poll, so a mismatch between services shows here.
    Names and yes/no only, never a value."""
    from services.payment_orchestration import a2a_shipping_address

    logger.warning(
        "purchase_consent: service=%s %s=%s %s=%s configured=%s previous_secret=%s %s=%s consent_givable=%s "
        "shipping_capture=%s",
        service or "unknown", GATE_ENV, "on" if gate_on() else "off", WRITE_ENABLED_ENV,
        "on" if write_enabled() else "off", "yes" if config() is not None else "no",
        "yes" if _env_key(OFFER_SECRET_PREVIOUS_ENV) else "no",
        a2a_shipping_address.CAPTURE_ENABLED_ENV,
        "on" if (os.getenv(a2a_shipping_address.CAPTURE_ENABLED_ENV) or "").strip().lower() in _TRUTHY else "off",
        "yes" if consent_can_be_given() else "no", "on" if a2a_shipping_address.capture_enabled() else "off",
    )


def approval_epoch(rail: str, step: str) -> int:
    """0 for `card_binding` and `shipping_address`; the purchase's approval number for `approval` (see the
    module docstring: 1 on both rails until something writes the A2A re-quote edge)."""
    return 1 if step == "approval" else 0


def step_for(rail: str, rail_state: Any, *, awaits_contact: bool = False) -> Optional[str]:
    """The step a purchase in `rail_state` asks a consent for, or None. `shipping_address` only on the A2A rail,
    while it is `opened` and `awaits_contact` (it waits for the buyer's shipping address / email)."""
    if rail == "a2a" and str(rail_state or "") == "opened":
        return SHIPPING_STEP if awaits_contact else None
    return STEP_FOR_STATE.get(unified_state(rail, rail_state) or "")


def view_step(rail: str, unified: str, detail: Any) -> Optional[str]:
    """The step a built view asks a consent for: `shipping_address` for an A2A `routing` view that carries the
    form (`shipping_address_required`) or its withheld marker (`buyer_step.kind`), else by its unified state."""
    if rail == "a2a" and unified == "routing":
        detail = detail if isinstance(detail, Mapping) else {}
        marker = detail.get("buyer_step")
        if isinstance(detail.get("shipping_address_required"), Mapping) or (
                isinstance(marker, Mapping) and marker.get("kind") == SHIPPING_STEP):
            return SHIPPING_STEP
        return None
    return STEP_FOR_STATE.get(unified)


# ── reading the consents ─────────────────────────────────────────────────────────────────────


async def snapshot(agent_id: str, refs: Iterable[Tuple[str, str]]) -> Any:
    """The consents for a page of purchases, in ONE read: a Snapshot, FAILED when the read failed, or
    LOOKUP when the gate is off (nothing will ask). THE one read every reader and writer of a consent decision
    uses (the views' gate, the internal read's block, the shipping-address write), judged by `satisfied`."""
    if not gate_on():
        return LOOKUP
    import db.agent_purchase_consents as consents_db
    from db.database import database

    try:
        # In its own (nested) transaction: inside a caller's transaction that is a SAVEPOINT, so a failed read
        # (the table missing, say) rolls back to it and does not abort the caller's work on Postgres.
        async with database.transaction():
            return await consents_db.consents_for(agent_id, [rail_id for _rail, rail_id in refs])
    except Exception as exc:  # noqa: BLE001 - fail closed: no consent read, no hosted page
        logger.warning("purchase_consent: consent read failed error_type=%s", type(exc).__name__)
        return FAILED


def _satisfied(consents: Any, rail: str, rail_purchase_id: str, step: str) -> bool:
    if consents is FAILED or consents is LOOKUP or consents is None:
        return False
    return (rail, rail_purchase_id, step, approval_epoch(rail, step)) in consents


def satisfied(consents: Any, rail: str, rail_purchase_id: str, step: str) -> bool:
    """Whether `consents` (a `snapshot`) holds this step's consent. FAILED / LOOKUP: no (fail closed)."""
    return _satisfied(consents, rail, rail_purchase_id, step)


def offer_available(cfg: Optional[ConsentConfig], *, blocked: bool, consents: Any) -> bool:
    """THE one rule for "can the buyer give the consent now", for the read's block and every `buyer_step`:
    configuration and secret set, the write ready, the consent read worked, and the step not blocked (Reap's
    contact re-entry)."""
    return cfg is not None and write_ready() and not blocked and consents is not FAILED


def gated_detail(detail: Mapping[str, Any], step: str, *, available: bool) -> Dict[str, Any]:
    """The rail detail with its hosted URL withheld and the step marked. Pure."""
    gated = dict(detail)
    gated.pop("hosted_url", None)
    expires = (gated.get("approval_deadline") if step == "approval" else None) or gated.get("hosted_url_expires_at")
    gated["buyer_step"] = {"kind": step, "expires_at": expires, "consent_required": True, "available": bool(available)}
    return gated


def gated_shipping_detail(detail: Mapping[str, Any], *, available: bool) -> Dict[str, Any]:
    """The A2A detail with its shipping-address form (`shipping_address_required`: the fields missing) withheld
    and the step marked. Pure."""
    gated = dict(detail)
    required = gated.pop("shipping_address_required", None)
    expires = required.get("expires_at") if isinstance(required, Mapping) else None
    gated["buyer_step"] = {"kind": SHIPPING_STEP, "expires_at": expires, "consent_required": True,
                           "available": bool(available)}
    return gated


def _blocked(detail: Mapping[str, Any]) -> bool:
    return detail.get("contact_reentry_required") is True


async def gate(
    detail: Dict[str, Any], *, rail: str, rail_purchase_id: str, rail_state: Any, agent_id: Any,
    consents: Any = LOOKUP,
) -> Dict[str, Any]:
    """`detail` itself (the same object) unless the gate withholds its page; then the gated copy."""
    if not gate_on():
        return detail
    if rail == "a2a" and isinstance(detail.get("shipping_address_required"), Mapping):
        # The A2A shipping-address form: withheld until its consent (module docstring).
        if consents is LOOKUP:
            consents = await snapshot(str(agent_id or ""), [(rail, rail_purchase_id)]) if agent_id else FAILED
        if _satisfied(consents, rail, rail_purchase_id, SHIPPING_STEP):
            return detail
        return gated_shipping_detail(detail, available=offer_available(config(), blocked=False, consents=consents))
    step = GATED_PAGE_FOR_STATE.get(unified_state(rail, rail_state) or "")
    if step is None or not detail.get("hosted_url"):
        return detail
    if consents is LOOKUP:
        consents = await snapshot(str(agent_id or ""), [(rail, rail_purchase_id)]) if agent_id else FAILED
    if _satisfied(consents, rail, rail_purchase_id, step):
        return detail
    return gated_detail(detail, step, available=offer_available(config(), blocked=_blocked(detail), consents=consents))


def without_live_fields(payload: Any) -> Any:
    """A purchase.* payload without `detail.buyer_step.available`: that value is a LIVE read's, true for the
    process that serves it at that moment; frozen into an event (built or sent by whichever process, web or
    worker, with its own dials) it can be wrong. `payload` itself when there is nothing to drop."""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    step = detail.get("buyer_step") if isinstance(detail, dict) else None
    if not isinstance(step, dict) or "available" not in step:
        return payload
    return {**payload, "detail": {**detail, "buyer_step": {k: v for k, v in step.items() if k != "available"}}}


async def gate_stored_payload(payload: Any, *, agent_id: Any) -> Any:
    """A purchase.* payload written BEFORE the gate went on (or before its consent lapsed) carries the hosted
    URL; the hand-over re-applies the gate so it is not sent. `payload` itself (the same object) unless the
    gate withholds its page or the payload carries a live-only field; then a copy: no url, `detail.buyer_step`
    (never with `available`), `next_action.consent_required`. The stored event is not rewritten."""
    return without_live_fields(await _gate_stored_payload(payload, agent_id=agent_id))


def _stored_shipping_form(payload: Mapping[str, Any]) -> bool:
    """An A2A payload that still carries the shipping-address form (built before the gate went on): the field
    list in `detail.shipping_address_required`, or a `next_action` of kind shipping_address with `fields_missing`.
    It has no url, so the url path below never sees it."""
    if payload.get("rail") != "a2a":
        return False
    detail, action = payload.get("detail"), payload.get("next_action")
    return (isinstance(detail, dict) and isinstance(detail.get("shipping_address_required"), dict)) or (
        isinstance(action, dict) and action.get("kind") == SHIPPING_STEP and "fields_missing" in action)


async def _gate_stored_payload(payload: Any, *, agent_id: Any) -> Any:
    if not gate_on() or not isinstance(payload, dict):
        return payload
    action, detail = payload.get("next_action"), payload.get("detail")
    if _stored_shipping_form(payload):
        rail_purchase_id = str(payload.get("rail_purchase_id") or "")
        consents = await snapshot(str(agent_id), [("a2a", rail_purchase_id)]) if agent_id and rail_purchase_id else FAILED
        if _satisfied(consents, "a2a", rail_purchase_id, SHIPPING_STEP):
            return payload
        detail = detail if isinstance(detail, dict) else {}
        required = detail.get("shipping_address_required")
        expires = (required.get("expires_at") if isinstance(required, dict) else None) or (
            action.get("expires_at") if isinstance(action, dict) else None)
        gated = dict(payload)
        gated["detail"] = gated_shipping_detail(detail, available=False)  # `available` dropped on the way out
        gated["detail"]["buyer_step"]["expires_at"] = expires
        gated["next_action"] = {"kind": SHIPPING_STEP, "expires_at": expires, "consent_required": True}
        return gated
    if not isinstance(action, dict) or not action.get("url") or not isinstance(detail, dict):
        return payload
    step = GATED_PAGE_FOR_STATE.get(str(payload.get("state") or ""))
    if step is None or action.get("kind") != step:
        return payload
    rail, rail_purchase_id = str(payload.get("rail") or ""), str(payload.get("rail_purchase_id") or "")
    consents = await snapshot(str(agent_id), [(rail, rail_purchase_id)]) if agent_id and rail_purchase_id else FAILED
    if _satisfied(consents, rail, rail_purchase_id, step):
        return payload
    gated = dict(payload)
    gated["detail"] = gated_detail(detail, step, available=False)  # dropped on the way out: live reads only
    gated["next_action"] = {"kind": step, "expires_at": action.get("expires_at"), "consent_required": True}
    return gated


# ── the offer ────────────────────────────────────────────────────────────────────────────────


def issued_at_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_ISSUED_AT_FORMAT)


def parse_issued_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not _ISSUED_AT_RE.match(value):
        return None
    try:
        return datetime.strptime(value, _ISSUED_AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def offer_id(cfg: ConsentConfig, *, agent_id: str, rail: str, rail_purchase_id: str, step: str, epoch: int,
             issued_at: str) -> str:
    """HMAC over length-prefixed fields, so no two field lists can spell the same message."""
    message = bytearray(_OFFER_TAG)
    for field in (agent_id, rail, rail_purchase_id, step, str(int(epoch)), cfg.terms_version, cfg.privacy_version,
                  cfg.terms_url, cfg.privacy_url, issued_at):
        data = str(field).encode("utf-8")
        message += len(data).to_bytes(4, "big") + data
    return hmac.new(cfg.secret, bytes(message), hashlib.sha256).hexdigest()


def offer_matches(cfg: ConsentConfig, provided: str, **fields: Any) -> bool:
    expected = offer_id(cfg, **fields)
    return hmac.compare_digest(expected.encode("ascii"), str(provided or "").encode("utf-8", "surrogateescape"))


def consent_block(
    *, agent_id: str, rail: str, rail_purchase_id: str, unified: str, consents: Any,
    blocked: bool = False, now: Optional[datetime] = None, detail: Any = None,
) -> Optional[Dict[str, Any]]:
    """The internal read's `consent` block for a state that asks for a consent, else None. Writes nothing.

    `available` is true only when the POST could succeed now: the terms / privacy configuration and the offer
    secret are set, the write is ready (`write_ready`: registered, dial on, key valid), the consent read worked,
    and nothing `blocked` the step (Reap's contact re-entry): `offer_available`, the rule `buyer_step` uses too.
    Otherwise there is no offer: the page says "not available right now", and the hosted page stays withheld."""
    step = view_step(rail, unified, detail)
    if step is None:
        return None
    cfg = config()
    satisfied = _satisfied(consents, rail, rail_purchase_id, step)
    block: Dict[str, Any] = {
        "required": True,
        "step": step,
        "satisfied": satisfied,
        "available": offer_available(cfg, blocked=blocked, consents=consents),
        # How long the page must wait after `offer.issued_at` before the write accepts it (0..120).
        "min_dwell_seconds": min_dwell_seconds(),
    }
    if cfg is not None:
        block.update({"terms_version": cfg.terms_version, "privacy_version": cfg.privacy_version,
                      "terms_url": cfg.terms_url, "privacy_url": cfg.privacy_url})
    if block["available"] and not satisfied:
        issued = issued_at_text(now or datetime.now(timezone.utc))
        block["offer"] = {
            "id": offer_id(cfg, agent_id=agent_id, rail=rail, rail_purchase_id=rail_purchase_id, step=step,
                           epoch=approval_epoch(rail, step), issued_at=issued),
            "issued_at": issued,
        }
    return block


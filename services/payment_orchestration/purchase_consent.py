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
url. The hosted URL string appears nowhere in such a body. `authenticate` (the A2A rail's 3DS page while it
places the order) is NEVER gated. A consent read that fails is "no consent": fail closed, never open.

THE OFFER. The internal read, gate on, carries a `consent` block for a state that needs one, with a
stateless offer: `offer.id` = HMAC-SHA256(AGENPAS_CONSENT_OFFER_SECRET, length-prefixed rail, rail purchase
id, step, approval epoch, terms version, privacy version, terms url, privacy url, issued_at). Nothing is
written on a read. The POST echoes {step, offer_id, issued_at, terms_version, privacy_version}; it is
accepted only when the HMAC verifies against the CURRENT configuration (the backend, not the browser, says
which version is current), the offer is at least AGENPAS_CONSENT_MIN_DWELL_SECONDS (3) and at most
MAX_OFFER_AGE_SECONDS (1800) old, and the purchase is still in the step. The dwell and the gateway's
Origin / Sec-Fetch-Site checks stop naive automation only: the agent holds the AgenPas link too, and the
record says exactly that (`evidence = link_holder_on_pivota_page`).

APPROVAL EPOCH. An approval consent binds to the purchase's single approval. Reap: 1 (a re-quote fails the
purchase; there is no edge back to quoting). A2A: the number of entries into `quoting`, which is 1 for
every purchase today: the ledger allows `awaiting_authorization -> quoting` but NOTHING WRITES IT
(tests/test_agenpas_consent_gate.py fails the build if a writer appears; whoever adds one must add a
quoting-entry counter to a2a_rail_purchases and return it here, or an approval consent would carry over
to a re-quoted price).
"""

from __future__ import annotations

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
MIN_DWELL_ENV = "AGENPAS_CONSENT_MIN_DWELL_SECONDS"

#: A configured offer secret shorter than this counts as missing: the gate then offers nothing (fail closed).
OFFER_SECRET_MIN_LENGTH = 32
MIN_DWELL_SECONDS_DEFAULT = 3
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


def min_dwell_seconds() -> int:
    raw = (os.getenv(MIN_DWELL_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return MIN_DWELL_SECONDS_DEFAULT
    return value if 0 <= value <= MAX_OFFER_AGE_SECONDS else MIN_DWELL_SECONDS_DEFAULT


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
    secret = (os.getenv(OFFER_SECRET_ENV) or "").strip()
    if (not valid_legal_url(terms_url) or not valid_legal_url(privacy_url) or not valid_version(terms_version)
            or not valid_version(privacy_version) or len(secret) < OFFER_SECRET_MIN_LENGTH):
        return None
    return ConsentConfig(terms_url, terms_version, privacy_url, privacy_version, secret.encode("utf-8"))


def approval_epoch(rail: str, step: str) -> int:
    """0 for `card_binding`; the purchase's approval number for `approval` (see the module docstring:
    1 on both rails until something writes the A2A re-quote edge)."""
    return 0 if step == "card_binding" else 1


def step_for(rail: str, rail_state: Any) -> Optional[str]:
    return STEP_FOR_STATE.get(unified_state(rail, rail_state) or "")


# ── reading the consents ─────────────────────────────────────────────────────────────────────


async def snapshot(agent_id: str, refs: Iterable[Tuple[str, str]]) -> Any:
    """The consents for a page of purchases, in ONE read: a Snapshot, FAILED when the read failed, or
    LOOKUP when the gate is off (nothing will ask)."""
    if not gate_on():
        return LOOKUP
    import db.agent_purchase_consents as consents_db

    try:
        return await consents_db.consents_for(agent_id, [rail_id for _rail, rail_id in refs])
    except Exception as exc:  # noqa: BLE001 - fail closed: no consent read, no hosted page
        logger.warning("purchase_consent: consent read failed error_type=%s", type(exc).__name__)
        return FAILED


def _satisfied(consents: Any, rail: str, rail_purchase_id: str, step: str) -> bool:
    if consents is FAILED or consents is LOOKUP or consents is None:
        return False
    return (rail, rail_purchase_id, step, approval_epoch(rail, step)) in consents


def gated_detail(detail: Mapping[str, Any], step: str) -> Dict[str, Any]:
    """The rail detail with its hosted URL withheld and the step marked. Pure."""
    gated = dict(detail)
    gated.pop("hosted_url", None)
    expires = (gated.get("approval_deadline") if step == "approval" else None) or gated.get("hosted_url_expires_at")
    gated["buyer_step"] = {"kind": step, "expires_at": expires, "consent_required": True}
    return gated


async def gate(
    detail: Dict[str, Any], *, rail: str, rail_purchase_id: str, rail_state: Any, agent_id: Any,
    consents: Any = LOOKUP,
) -> Dict[str, Any]:
    """`detail` itself (the same object) unless the gate withholds its page; then the gated copy."""
    if not gate_on():
        return detail
    step = GATED_PAGE_FOR_STATE.get(unified_state(rail, rail_state) or "")
    if step is None or not detail.get("hosted_url"):
        return detail
    if consents is LOOKUP:
        consents = await snapshot(str(agent_id or ""), [(rail, rail_purchase_id)]) if agent_id else FAILED
    if _satisfied(consents, rail, rail_purchase_id, step):
        return detail
    return gated_detail(detail, step)


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


def offer_id(cfg: ConsentConfig, *, rail: str, rail_purchase_id: str, step: str, epoch: int, issued_at: str) -> str:
    """HMAC over length-prefixed fields, so no two field lists can spell the same message."""
    message = bytearray(_OFFER_TAG)
    for field in (rail, rail_purchase_id, step, str(int(epoch)), cfg.terms_version, cfg.privacy_version,
                  cfg.terms_url, cfg.privacy_url, issued_at):
        data = str(field).encode("utf-8")
        message += len(data).to_bytes(4, "big") + data
    return hmac.new(cfg.secret, bytes(message), hashlib.sha256).hexdigest()


def offer_matches(cfg: ConsentConfig, provided: str, **fields: Any) -> bool:
    expected = offer_id(cfg, **fields)
    return hmac.compare_digest(expected.encode("ascii"), str(provided or "").encode("utf-8", "surrogateescape"))


def consent_block(
    *, rail: str, rail_purchase_id: str, unified: str, consents: Any, now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """The internal read's `consent` block for a state that asks for a consent, else None. Writes nothing.

    `available` is false (and no offer is made) when the terms / privacy configuration or the offer secret
    is missing, the write is not switched on, or the consent read failed: the page then says "not available
    right now", and the gate keeps the hosted page withheld."""
    step = STEP_FOR_STATE.get(unified)
    if step is None:
        return None
    cfg = config()
    satisfied = _satisfied(consents, rail, rail_purchase_id, step)
    block: Dict[str, Any] = {
        "required": True,
        "step": step,
        "satisfied": satisfied,
        "available": cfg is not None and write_enabled() and consents is not FAILED,
    }
    if cfg is not None:
        block.update({"terms_version": cfg.terms_version, "privacy_version": cfg.privacy_version,
                      "terms_url": cfg.terms_url, "privacy_url": cfg.privacy_url})
    if block["available"] and not satisfied:
        issued = issued_at_text(now or datetime.now(timezone.utc))
        block["offer"] = {
            "id": offer_id(cfg, rail=rail, rail_purchase_id=rail_purchase_id, step=step,
                           epoch=approval_epoch(rail, step), issued_at=issued),
            "issued_at": issued,
        }
    return block


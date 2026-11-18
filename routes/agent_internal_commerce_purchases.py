"""`GET /agent/internal/commerce/purchases/{purchase_id}`: the server-to-server purchase read (AgenPas),
and `POST /agent/internal/commerce/purchases/{purchase_id}/consents`: the AgenPas consent write.

The buyer opens a signed AgenPas link on the gateway. They hold no agent key and no JWT, and the gateway
never stores a partner's credentials, so the gateway reads the purchase here, server to server.

  AUTH   `X-Internal-Key` must equal AGENPAS_INTERNAL_READ_KEY, a key DEDICATED to this read: a key that
         reads any agent's purchase must not share scope with the introspect key
         (AGENT_AUTH_INTROSPECT_INTERNAL_KEY), which this route never accepts. Compared in constant time,
         as bytes. Missing on the server, or shorter than INTERNAL_READ_KEY_MIN_LENGTH (32) characters:
         503, never open. Missing or wrong in the request: 401.
  SCOPE  `X-Agenpas-Agent-Id`, the agent id the gateway verified on the signed link. The read is scoped
         by (agent id, purchase id) IN SQL (db/agent_purchase_ledger.get_for_agent): another agent's
         purchase and no purchase are the same 404. NOT by the end user: the gateway's `usr_…` ref is not
         the backend's agent-user hash, so there is nothing to scope by. `X-Agenpas-Agent-User-Ref`, if
         sent, is ignored on purpose.
  BODY   the SAME unified body `GET /agent/v2/commerce/purchases/{id}` returns, built by the same
         builders (services/payment_orchestration/purchase_view, through the public route's
         `_unified_for`), from the parent's own owner identity: no PII, no rail ids, only vetted URLs.
  DIAL   AGENPAS_INTERNAL_READ_ENABLED, default off, read WHEN THE APP IS BUILT (`mount`, called by
         main.py). Off: the route is not registered at all, so every method on the path, and the OpenAPI
         schema, answer exactly as for a path that does not exist. TURNING IT ON NEEDS A RESTART (a new
         revision). The dial is also checked on every request: turned off without a restart, a GET answers
         the unknown-route 404 before the key is looked at (other methods then answer 405 until the restart).

The GET writes nothing, heals no parent (a rail purchase whose parent row is still missing is a 404
here until the events job or a public read heals it) and calls no rail. NOT exempt from the rate limiter
(only the introspect key is, middleware/rate_limiter.py): gateway page reads count as anonymous traffic
and can be throttled (429) under a burst. An exemption or a limit of its own is a follow-up.

CONSENT (services/payment_orchestration/purchase_consent.py has the design). With AGENPAS_CONSENT_GATE on
(default off, read per request), the GET body is gated like every other surface (no rail card / approval
URL before a consent) and, for a state that asks for one, carries a `consent` block:
`{required, step, satisfied, available, terms_version, privacy_version, terms_url, privacy_url,
offer: {id, issued_at}}` (the versions and urls only when configured; the offer only while available and
not yet satisfied). Gate off: no block, the body is byte for byte what it was.

THE WRITE is a SECOND router with its OWN key and its OWN dial, so the read key stays read-only:
  AUTH   `X-Internal-Key` must equal AGENPAS_INTERNAL_CONSENT_KEY (>= 32 characters, and not equal to the
         read key: a write key that is also the read key is no separate key). Missing / short / equal to
         the read key on the server: 503. Missing or wrong in the request (the read key included): 401.
  SCOPE  `X-Agenpas-Agent-Id`, as for the read. A `pp_` id is normalised to its rail id; the consent is
         keyed on (rail, rail purchase id, step, approval epoch) and the rail row is read agent-scoped,
         so the write does not need the parent row (it heals it best effort, after every check passed).
  BODY   exactly `{step, offer_id, issued_at, terms_version, privacy_version}` (strings, <= 1 KiB): the offer
         the GET showed. Accepted only when the versions are the configured ones (else 409 consent_stale),
         the purchase is not final and is in that step (else 409 consent_stale), the offer's HMAC verifies
         (else 400 consent_offer_invalid) and it is at least AGENPAS_CONSENT_MIN_DWELL_SECONDS old (else
         409 consent_too_soon) and at most 1800 s (else 409 consent_stale). Configuration or offer secret
         missing: 503 consent_unavailable. Any refusal writes nothing. A repeat is the same one row
         (201 when this call wrote it, 200 when it already existed). Calls no rail.
  DIAL   AGENPAS_CONSENT_WRITE_ENABLED, default off, read at build (`mount`) like the read's dial: off, the
         route is not registered (the OpenAPI schema is unchanged); turned off without a restart, the
         write answers the unknown-route 404 before the key is looked at.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

import db.agent_purchase_consents as consents_db
import db.agent_purchase_ledger as purchases
from routes import agent_commerce_purchases as public
from services.payment_orchestration import purchase_consent
from services.payment_orchestration.rails import RAIL_ID_PREFIX

logger = logging.getLogger(__name__)

AGENPAS_INTERNAL_READ_ENABLED_ENV = "AGENPAS_INTERNAL_READ_ENABLED"
#: This route's own key. Not the introspect key: that one is held for other /agent/internal routers.
INTERNAL_KEY_ENV = "AGENPAS_INTERNAL_READ_KEY"
#: A configured key shorter than this counts as missing (503): a short key is a guessable key.
INTERNAL_READ_KEY_MIN_LENGTH = 32
_TRUTHY = frozenset({"1", "true", "on", "yes"})
#: An agent id as the gateway passes it: an opaque id, checked before any bind (a NUL on Postgres raises).
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")


def is_enabled() -> bool:
    return (os.getenv(AGENPAS_INTERNAL_READ_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def _read_enabled() -> None:
    # Off: exactly what FastAPI answers for a path it does not have.
    if not is_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _require_internal_key(x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key")) -> None:
    expected = str(os.getenv(INTERNAL_KEY_ENV) or "").strip()
    if len(expected) < INTERNAL_READ_KEY_MIN_LENGTH:
        # Never open: a server without the key (or with one too short to count) refuses everyone.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"error": "internal_key_not_configured"})
    provided = str(x_internal_key or "").strip()
    # Compare as BYTES: Starlette decodes headers latin-1, and hmac.compare_digest on str RAISES on
    # non-ASCII (routes/agent_internal_decision_slates.py has the history).
    if not provided or not hmac.compare_digest(provided.encode("utf-8", "surrogateescape"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "unauthorized"})


# The guards are on the ROUTER, not the handler, so a route added here later inherits them
# (tests/test_unauthenticated_admin_ops_routes.py has the history). The dial runs first.
router = APIRouter(
    prefix="/agent/internal/commerce/purchases",
    tags=["agent-internal-commerce-purchases"],
    dependencies=[Depends(_read_enabled), Depends(_require_internal_key)],
)


def mount(app: Any) -> bool:
    """Register the read on `app` only while its dial is on, and the consent write only while ITS dial is
    on (both read now, at build). Returns whether anything was registered. Both dials off: nothing is, so
    the app is exactly what it was without this module."""
    mounted = False
    if is_enabled():
        app.include_router(router)
        mounted = True
    if purchase_consent.write_enabled():
        app.include_router(consent_router)
        mounted = True
    return mounted


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "purchase_not_found"})


def _unavailable() -> JSONResponse:
    """A ledger or view fault: "try again", never a 500 (and never which part failed)."""
    return JSONResponse(status_code=503, content={"error": "temporarily_unavailable"})


async def _parent_for_agent(purchase_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
    if purchase_id.startswith("pp_"):
        return await purchases.get_for_agent(purchase_id, agent_id)
    rail = next((r for prefix, r in RAIL_ID_PREFIX.items() if purchase_id.startswith(prefix)), None)
    if rail is None:
        return None
    return await purchases.get_by_rail_id_for_agent(rail, purchase_id, agent_id)


@router.get("/{purchase_id}")
async def get_purchase_for_gateway(
    purchase_id: str,
    x_agenpas_agent_id: Optional[str] = Header(None, alias="X-Agenpas-Agent-Id"),
    # Read so the contract is visible, and IGNORED for scoping: the gateway's user ref is not the
    # backend's agent-user hash (see the module docstring). Never logged.
    x_agenpas_agent_user_ref: Optional[str] = Header(None, alias="X-Agenpas-Agent-User-Ref"),
):
    del x_agenpas_agent_user_ref
    agent_id = str(x_agenpas_agent_id or "").strip()
    if not _AGENT_ID_RE.match(agent_id):
        return JSONResponse(status_code=400, content={"error": "agent_id_required"})
    purchase_id = str(purchase_id or "").strip()
    if not purchases.is_enabled() or not public._PURCHASE_ID_RE.fullmatch(purchase_id):
        return _not_found()
    try:
        parent = await _parent_for_agent(purchase_id, agent_id)
    except Exception as exc:  # noqa: BLE001 - any ledger fault is "say nothing, retry", never a 500
        logger.warning("agent_internal_purchases: ledger read failed error_type=%s", type(exc).__name__)
        return _unavailable()
    owner = str((parent or {}).get("agent_user_ref_hash") or "")
    if parent is None or not owner:
        return _not_found()
    # The owner hash goes only to the owner-scoped view builders, never into the body.
    parent = {k: v for k, v in parent.items() if k != "agent_user_ref_hash"}
    gated = purchase_consent.gate_on()
    try:
        if not gated:
            body, error = await public._unified_for(parent, agent_id, owner)
        else:
            # ONE consent read, used by the view's gate and by the consent block alike (FAILED on a fault:
            # the page stays withheld and the block says unavailable).
            consents = await purchase_consent.snapshot(agent_id, [(parent["rail"], str(parent["rail_purchase_id"]))])
            body, error = await public._unified_for(parent, agent_id, owner, consents)
            if error is None:
                block = purchase_consent.consent_block(
                    rail=str(parent["rail"]), rail_purchase_id=str(parent["rail_purchase_id"]),
                    unified=str(body.get("state") or ""), consents=consents)
                if block is not None:
                    body["consent"] = block
    except Exception as exc:  # noqa: BLE001 - a rail read or view fault is "try again", never a 500
        logger.warning("agent_internal_purchases: view failed error_type=%s", type(exc).__name__)
        return _unavailable()
    return error if error is not None else body


# ── the consent write ────────────────────────────────────────────────────────────────────────

#: The write's own key. Never the read key, never the introspect key.
CONSENT_KEY_ENV = "AGENPAS_INTERNAL_CONSENT_KEY"
INTERNAL_CONSENT_KEY_MIN_LENGTH = 32
_CONSENT_BODY_KEYS = frozenset({"step", "offer_id", "issued_at", "terms_version", "privacy_version"})
_CONSENT_BODY_MAX_BYTES = 1024
_OFFER_ID_RE = re.compile(r"^[0-9a-f]{64}\Z")


def _write_enabled() -> None:
    # Off: exactly what FastAPI answers for a path it does not have.
    if not purchase_consent.write_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _require_consent_key(x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key")) -> None:
    expected = str(os.getenv(CONSENT_KEY_ENV) or "").strip()
    read_key = str(os.getenv(INTERNAL_KEY_ENV) or "").strip()
    if len(expected) < INTERNAL_CONSENT_KEY_MIN_LENGTH or (read_key and hmac.compare_digest(
            expected.encode("utf-8"), read_key.encode("utf-8"))):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"error": "internal_key_not_configured"})
    provided = str(x_internal_key or "").strip()
    if not provided or not hmac.compare_digest(provided.encode("utf-8", "surrogateescape"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "unauthorized"})


consent_router = APIRouter(
    prefix="/agent/internal/commerce/purchases",
    tags=["agent-internal-commerce-purchases"],
    dependencies=[Depends(_write_enabled), Depends(_require_consent_key)],
)


def _refusal(status_code: int, error: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error})


def _consent_body(raw: bytes) -> Optional[Dict[str, str]]:
    """The exact body shape, or None. Never echoes what was sent."""
    if len(raw) > _CONSENT_BODY_MAX_BYTES:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or set(body) != _CONSENT_BODY_KEYS:
        return None
    if not all(isinstance(v, str) for v in body.values()):
        return None
    if (body["step"] not in consents_db.STEPS or not _OFFER_ID_RE.match(body["offer_id"])
            or purchase_consent.parse_issued_at(body["issued_at"]) is None
            or not purchase_consent.valid_version(body["terms_version"])
            or not purchase_consent.valid_version(body["privacy_version"])):
        return None
    return body


async def _consent_target(purchase_id: str, agent_id: str) -> Optional[Tuple[str, str, Dict[str, Any], Optional[str]]]:
    """(rail, rail purchase id, the agent's rail row, the parent id if known), or None."""
    parent_id: Optional[str] = None
    if purchase_id.startswith("pp_"):
        parent = await purchases.get_for_agent(purchase_id, agent_id)
        if parent is None:
            return None
        rail, rail_id, parent_id = str(parent["rail"]), str(parent["rail_purchase_id"]), str(parent["id"])
    else:
        rail = next((r for prefix, r in RAIL_ID_PREFIX.items() if purchase_id.startswith(prefix)), None)
        if rail is None:
            return None
        rail_id = purchase_id
    row = await consents_db.rail_purchase_for_agent(rail, rail_id, agent_id)
    return (rail, rail_id, row, parent_id) if row is not None else None


async def _healed_parent_id(rail: str, rail_id: str, agent_id: str) -> Optional[str]:
    """The parent's id, writing the parent if it is missing. Best effort: None on any fault."""
    try:
        parent = await purchases.get_by_rail_id_for_agent(rail, rail_id, agent_id)
        if parent is None:
            heal = purchases.ensure_reap_parent if rail == "reap" else purchases.ensure_a2a_parent
            await heal(rail_id, basis="backfill")
            parent = await purchases.get_by_rail_id_for_agent(rail, rail_id, agent_id)
        return str(parent["id"]) if parent else None
    except Exception as exc:  # noqa: BLE001 - the consent does not need its parent
        logger.warning("agent_internal_purchases: consent parent heal failed error_type=%s", type(exc).__name__)
        return None


@consent_router.post("/{purchase_id}/consents")
async def record_purchase_consent(
    purchase_id: str,
    request: Request,
    x_agenpas_agent_id: Optional[str] = Header(None, alias="X-Agenpas-Agent-Id"),
):
    agent_id = str(x_agenpas_agent_id or "").strip()
    if not _AGENT_ID_RE.match(agent_id):
        return JSONResponse(status_code=400, content={"error": "agent_id_required"})
    purchase_id = str(purchase_id or "").strip()
    if not purchases.is_enabled() or not public._PURCHASE_ID_RE.fullmatch(purchase_id):
        return _not_found()
    body = _consent_body(await request.body())
    if body is None:
        return _refusal(400, "invalid_consent")
    cfg = purchase_consent.config()
    if cfg is None:
        return _refusal(503, "consent_unavailable")
    # The backend, not the browser, says which version is current.
    if (body["terms_version"], body["privacy_version"]) != (cfg.terms_version, cfg.privacy_version):
        return _refusal(409, "consent_stale")
    try:
        target = await _consent_target(purchase_id, agent_id)
    except Exception as exc:  # noqa: BLE001 - any ledger fault is "say nothing, retry", never a 500
        logger.warning("agent_internal_purchases: consent target read failed error_type=%s", type(exc).__name__)
        return _unavailable()
    if target is None:
        return _not_found()
    rail, rail_id, row, parent_id = target
    step = body["step"]
    # Not final and in this step, in one check: a final (or unmapped) state asks for no step at all.
    if purchase_consent.step_for(rail, row.get("state")) != step:
        return _refusal(409, "consent_stale")
    epoch = purchase_consent.approval_epoch(rail, step)
    if not purchase_consent.offer_matches(cfg, body["offer_id"], rail=rail, rail_purchase_id=rail_id, step=step,
                                          epoch=epoch, issued_at=body["issued_at"]):
        return _refusal(400, "consent_offer_invalid")
    issued = purchase_consent.parse_issued_at(body["issued_at"])
    age = (datetime.now(timezone.utc) - issued).total_seconds()
    if age < purchase_consent.min_dwell_seconds():
        return _refusal(409, "consent_too_soon")
    if age > purchase_consent.MAX_OFFER_AGE_SECONDS:
        return _refusal(409, "consent_stale")
    if parent_id is None:
        parent_id = await _healed_parent_id(rail, rail_id, agent_id)
    try:
        record, created = await consents_db.record(
            rail=rail, rail_purchase_id=rail_id, agent_id=agent_id, step=step, approval_epoch=epoch,
            terms_version=cfg.terms_version, privacy_version=cfg.privacy_version, terms_url=cfg.terms_url,
            privacy_url=cfg.privacy_url, offer_issued_at=issued, purchase_id=parent_id,
            rail_buyer_ref=(str(row["rail_buyer_ref"]) if step == "card_binding" and row.get("rail_buyer_ref") else None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_internal_purchases: consent write failed error_type=%s", type(exc).__name__)
        return _unavailable()
    # WARNING, not INFO: prod drops module-logger INFO, and this is the audit line.
    logger.warning("agent_internal_purchases: consent recorded rail=%s purchase=%s step=%s epoch=%s created=%s",
                   rail, rail_id, step, epoch, created)
    return JSONResponse(status_code=201 if created else 200, content={
        "created": created,
        "consent": {
            "step": record["step"], "approval_epoch": record["approval_epoch"],
            "terms_version": record["terms_version"], "privacy_version": record["privacy_version"],
            "evidence": record["evidence"], "accepted_at": record["accepted_at"].isoformat(),
        },
    })

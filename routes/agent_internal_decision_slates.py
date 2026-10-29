"""Decision-slate ingest for slates the backend never sees.

The gateway answers about 95% of find_products_multi from its own lanes (2026-10-29: 209 of 219
requests had primary_path_used=local), so the decision ledger only held the few slates that
fell through to /agent/v2/products/search. This route lets the gateway deposit the slates it
served itself, through the same store every backend writer uses (services/agent_decision_event_store).

Server-to-server only: X-Internal-Key = AGENT_AUTH_INTROSPECT_INTERNAL_KEY, the key the gateway
already holds for /agent/internal/auth. Off unless DECISION_SLATES_INGEST_ENABLED is on.

One decision_id is one slate. The decision row is claimed atomically: a replay of the identical
request is a 200 (its rows are rewritten under the same ids, a no-op or a repair), and anything
else under an existing decision_id is a 409. Without that, a reused id produced a hybrid slate
(the first agent, the first slate's cards, plus the second slate's extra positions).

Rows are written before the answer: 202/200 means they are in (or lost to a row-level error,
counted in rows_lost), and a 503 means the database failed and the gateway should resend the same
request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from services.agent_decision_event_store import (
    _ROW_LEVEL_ERRORS,
    _row_offer_id,
    claim_decision_event,
    resolve_card_identities,
    write_slate_rows,
)
from services.catalog_identity import is_content_key
from services.protocols import DEFAULT_PROTOCOL

# Candidate and exposure ids are derived from (decision_id, position), so a replayed slate hits
# ON CONFLICT DO NOTHING instead of writing its rows twice.
_ID_NAMESPACE = uuid.UUID("5b0f4a52-6f0e-4f43-9a51-2d7a3c1e8b90")
MAX_CARDS = 100
# Gateway-minted decision ids carry this prefix ("gw:" + uuid4), so they can never equal an id
# another writer minted. agent_v2 mints bare uuid4s and queues its decision row; without the prefix
# a gateway claim could land first and v2's queued rows would then file under the gateway's claim.
GATEWAY_DECISION_PREFIX = "gw:"
# The body's agent_id is the gateway's word, not an authenticated identity here. Every row says
# so, so that no attribution or payout join ever trusts it as one.
AGENT_ID_SOURCE = "gateway_relay"


def _strip_nul(value: Any) -> Any:
    # Postgres text cannot hold NUL; one in surface/query made the claim INSERT raise, a 500 the
    # gateway would retry forever.
    return value.replace("\x00", "") if isinstance(value, str) else value


class SlateCard(BaseModel):
    # Numeric ids from the gateway are ids, not a reason to refuse the slate.
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    position: Optional[int] = Field(default=None, ge=0, le=10_000)
    product_id: Optional[str] = Field(default=None, max_length=255)
    merchant_id: Optional[str] = Field(default=None, max_length=255)
    product_key: Optional[str] = Field(default=None, max_length=255)
    attached_product_key: Optional[str] = Field(default=None, max_length=255)
    content_key: Optional[str] = Field(default=None, max_length=64)
    # The backend writers read catalog_offer_id or offer_id; accept both spellings here too.
    offer_id: Optional[str] = Field(
        default=None, max_length=512, validation_alias=AliasChoices("offer_id", "catalog_offer_id")
    )
    platform: Optional[str] = Field(default=None, max_length=64)
    source: Optional[str] = Field(default=None, max_length=128)
    in_stock: Optional[bool] = None

    _no_nul = field_validator("*", mode="before")(_strip_nul)


class DecisionSlate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Required: a minted id would make every retry a new decision. "gw:" + up to 61 id characters
    # (agent_decision_events.decision_id is VARCHAR(64), migration 140).
    decision_id: str = Field(pattern=r"^gw:[A-Za-z0-9_.-]{1,61}$")
    surface: str = Field(min_length=1, max_length=128)
    channel: Optional[str] = Field(default=None, max_length=128)
    protocol: Optional[str] = Field(default=None, max_length=64)
    merchant_id: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)
    query: Optional[str] = Field(default=None, max_length=1000)
    market: Optional[str] = Field(default=None, max_length=8)
    request_id: Optional[str] = Field(default=None, max_length=128)
    primary_path: Optional[str] = Field(default=None, max_length=64)
    cards: List[SlateCard] = Field(default_factory=list, max_length=MAX_CARDS)

    _no_nul = field_validator("*", mode="before")(_strip_nul)


def _ingest_enabled() -> None:
    # Off until the gateway writer ships: the introspect key would otherwise also grant writes to
    # the attribution ledger, and its holders are exempt from the rate limiter.
    if str(os.getenv("DECISION_SLATES_INGEST_ENABLED") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "NOT_ENABLED"})


def _expected_internal_key() -> str:
    return str(os.getenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY") or "").strip()


def _require_internal_key(x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key")) -> None:
    expected = _expected_internal_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "CONFIG_MISSING", "message": "AGENT_AUTH_INTROSPECT_INTERNAL_KEY is not configured"},
        )
    provided = str(x_internal_key or "").strip()
    # Compare as BYTES: Starlette decodes headers latin-1, and hmac.compare_digest on str
    # RAISES on non-ASCII (the #1883 class).
    if not provided or not hmac.compare_digest(
        provided.encode("utf-8", "surrogateescape"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Missing or invalid X-Internal-Key"},
        )


# The guards are on the ROUTER, not the handler, so a route added here later inherits them
# (tests/test_unauthenticated_admin_ops_routes.py has the history).
router = APIRouter(
    prefix="/agent/internal",
    tags=["agent-internal-decision-slates"],
    dependencies=[Depends(_ingest_enabled), Depends(_require_internal_key)],
)


def _positions(cards: List[SlateCard]) -> List[int]:
    """Every card's explicit position, or none (then the list index). Two cards on one position
    would share a row id and the second would vanish; a mix would silently re-rank the cards."""
    explicit = [card.position for card in cards]
    if all(p is None for p in explicit):
        return list(range(len(cards)))
    if any(p is None for p in explicit):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "MIXED_POSITIONS", "message": "give every card a position, or none"},
        )
    if len(set(explicit)) != len(explicit):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "DUPLICATE_POSITION", "message": "each card needs a distinct position"},
        )
    return [int(p) for p in explicit]  # type: ignore[arg-type]


def _fingerprint(slate: DecisionSlate, positions: List[int]) -> str:
    """The whole validated request except decision_id: every slate field and every card field, the
    cards in position order with positions applied. An omitted field and an explicit null validate
    to the same model, so they fingerprint alike; any other difference (query, market, a card's
    stock...) is a different request and gets a 409 rather than being dropped as a "replay"."""
    body = slate.model_dump(mode="json", exclude={"decision_id", "cards"})
    cards = []
    for pos, card in sorted(zip(positions, slate.cards), key=lambda pair: pair[0]):
        dumped = card.model_dump(mode="json")
        dumped["position"] = pos
        cards.append(dumped)
    body["cards"] = cards
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _card_rows(decision_id: str, cards: List[SlateCard], positions: List[int]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for position, card in zip(positions, cards):
        rows.append({
            "candidate_id": str(uuid.uuid5(_ID_NAMESPACE, f"{decision_id}:candidate:{position}")),
            "exposure_id": str(uuid.uuid5(_ID_NAMESPACE, f"{decision_id}:exposure:{position}")),
            "content_key": card.content_key,
            "product_key": card.product_key,
            "attached_product_key": card.attached_product_key,
            "offer_id": card.offer_id,
            "position": position,
            "eligibility_flags": {
                "merchant_id": card.merchant_id,
                "product_id": card.product_id,
                "product_key": card.product_key or card.attached_product_key,
                "platform": card.platform,
                "source": card.source,
                "in_stock": card.in_stock,
            },
            "slot": "search_result",
        })
    return rows


@router.post("/decision-slates", status_code=status.HTTP_202_ACCEPTED)
async def ingest_decision_slate(slate: DecisionSlate, response: Response) -> Dict[str, Any]:
    decision_id = slate.decision_id
    positions = _positions(slate.cards)
    fingerprint = _fingerprint(slate, positions)

    try:
        existing = await claim_decision_event(
            decision_id=decision_id,
            merchant_id=slate.merchant_id,
            surface=slate.surface,
            channel=slate.channel,
            protocol=slate.protocol or DEFAULT_PROTOCOL,
            agent_context={
                "agent_id": slate.agent_id,
                "agent_id_source": AGENT_ID_SOURCE,
                "query": slate.query,
                "market": slate.market,
                "request_id": slate.request_id,
                "primary_path": slate.primary_path,
                "result_count": len(slate.cards),
                "slate_fingerprint": fingerprint,
            },
        )
    except _ROW_LEVEL_ERRORS:
        # A value Postgres refuses: resending the same request cannot succeed.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"error": "UNSTORABLE_VALUE"})
    except Exception:  # noqa: BLE001 - the database failed; the same request will succeed later
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"error": "LEDGER_UNAVAILABLE"})
    replayed = existing is not None
    if replayed and (existing or {}).get("agent_context", {}).get("slate_fingerprint") != fingerprint:
        # A different request under this id. (Another writer's id cannot get here: the gw: prefix
        # keeps the id spaces apart, and such a row would carry no fingerprint.)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "DECISION_ID_CONFLICT", "message": "decision_id already holds a different slate"},
        )

    rows = _card_rows(decision_id, slate.cards, positions)
    await resolve_card_identities(rows)
    try:
        # Written now, not queued: the answer tells the gateway whether to resend. On a replay the
        # same row ids make this a no-op where rows landed and a repair where they did not.
        rows_lost = await write_slate_rows(decision_id, rows)
    except Exception:  # noqa: BLE001 - systemic; reported by the store
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"error": "LEDGER_UNAVAILABLE"})

    if replayed:
        response.status_code = status.HTTP_200_OK
    return {
        "decision_id": decision_id,
        "replayed": replayed,
        "cards": len(rows),
        "rows_lost": rows_lost,
        "content_key_resolved": sum(1 for row in rows if is_content_key(row.get("content_key"))),
        "offer_id_kept": sum(1 for row in rows if _row_offer_id(row)),
    }

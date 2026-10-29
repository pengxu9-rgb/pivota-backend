"""Decision-slate ingest for slates the backend never sees.

The gateway answers about 95% of find_products_multi from its own lanes (2026-10-29: 209 of 219
requests had primary_path_used=local), so the decision ledger only held the few slates that
fell through to /agent/v2/products/search. This route lets the gateway deposit the slates it
served itself, through the same store every backend writer uses (services/agent_decision_event_store).

Server-to-server only: X-Internal-Key = AGENT_AUTH_INTROSPECT_INTERNAL_KEY, the key the gateway
already holds for /agent/internal/auth. The gateway authenticated the end agent, so it sends that
agent_id in the body.
"""

from __future__ import annotations

import hmac
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from services.agent_decision_event_store import (
    record_decision_candidates,
    record_decision_event,
    record_exposure_events,
    resolve_card_identities,
)
from services.catalog_identity import is_content_key
from services.protocols import DEFAULT_PROTOCOL

# Candidate and exposure ids are derived from (decision_id, position), so a retried POST of the
# same slate hits ON CONFLICT DO NOTHING instead of writing the slate twice.
_ID_NAMESPACE = uuid.UUID("5b0f4a52-6f0e-4f43-9a51-2d7a3c1e8b90")
MAX_CARDS = 100


class SlateCard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    position: Optional[int] = Field(default=None, ge=0, le=10_000)
    product_id: Optional[str] = Field(default=None, max_length=255)
    merchant_id: Optional[str] = Field(default=None, max_length=255)
    product_key: Optional[str] = Field(default=None, max_length=255)
    content_key: Optional[str] = Field(default=None, max_length=64)
    offer_id: Optional[str] = Field(default=None, max_length=512)
    platform: Optional[str] = Field(default=None, max_length=64)
    source: Optional[str] = Field(default=None, max_length=128)
    in_stock: Optional[bool] = None


class DecisionSlate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # agent_decision_events.decision_id is VARCHAR(64) (migration 140).
    decision_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
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


# The guard is on the ROUTER, not the handler, so a route added here later inherits it
# (tests/test_unauthenticated_admin_ops_routes.py has the history).
router = APIRouter(
    prefix="/agent/internal",
    tags=["agent-internal-decision-slates"],
    dependencies=[Depends(_require_internal_key)],
)


def _card_rows(decision_id: str, cards: List[SlateCard]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for idx, card in enumerate(cards):
        position = card.position if card.position is not None else idx
        rows.append({
            "candidate_id": str(uuid.uuid5(_ID_NAMESPACE, f"{decision_id}:candidate:{position}")),
            "exposure_id": str(uuid.uuid5(_ID_NAMESPACE, f"{decision_id}:exposure:{position}")),
            "content_key": card.content_key,
            "product_key": card.product_key,
            "offer_id": card.offer_id,
            "position": position,
            "eligibility_flags": {
                "merchant_id": card.merchant_id,
                "product_id": card.product_id,
                "product_key": card.product_key,
                "platform": card.platform,
                "source": card.source,
                "in_stock": card.in_stock,
            },
            "slot": "search_result",
        })
    return rows


@router.post("/decision-slates", status_code=status.HTTP_202_ACCEPTED)
async def ingest_decision_slate(slate: DecisionSlate) -> Dict[str, Any]:
    decision_id = slate.decision_id or str(uuid.uuid4())
    rows = _card_rows(decision_id, slate.cards)
    await resolve_card_identities(rows)

    await record_decision_event(
        decision_id=decision_id,
        merchant_id=slate.merchant_id,
        surface=slate.surface,
        channel=slate.channel,
        protocol=slate.protocol or DEFAULT_PROTOCOL,
        agent_context={
            "agent_id": slate.agent_id,
            "query": slate.query,
            "market": slate.market,
            "request_id": slate.request_id,
            "primary_path": slate.primary_path,
            "result_count": len(rows),
        },
    )
    if rows:
        await record_decision_candidates(decision_id, rows)
        await record_exposure_events(decision_id, rows)

    return {
        "decision_id": decision_id,
        "cards": len(rows),
        "content_key_resolved": sum(1 for row in rows if is_content_key(row.get("content_key"))),
        "offer_id_kept": sum(1 for row in rows if row.get("offer_id")),
    }

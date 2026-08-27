from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.commerce_interaction_service import record_commerce_event


STANDARD_COMMERCE_EVENT_TYPES = frozenset(
    {
        "agent.requested",
        "search.performed",
        "product.viewed",
        "cart.created",
        "cart.item_added",
        "cart.item_removed",
        "cart.updated",
        "checkout.started",
        "checkout.submitted",
        "payment.attempted",
        "payment.authorized",
        "payment.declined",
        "payment.succeeded",
        "payment.failed",
        "order.created",
        "order.paid",
        "order.cancelled",
        "refund.created",
        "refund.succeeded",
        "return.created",
        "return.completed",
    }
)


class MerchantCommerceEvent(BaseModel):
    """Platform-neutral event contract accepted from store adapters/collectors."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    platform: str = Field(default="custom", min_length=1, max_length=32)
    source: Optional[str] = Field(default=None, max_length=128)
    store_id: Optional[str] = Field(default=None, max_length=128)
    surface: Optional[str] = Field(default=None, max_length=64)

    interaction_id: Optional[str] = Field(default=None, max_length=64)
    session_id: Optional[str] = Field(default=None, max_length=128)
    visitor_id: Optional[str] = Field(default=None, max_length=128)
    buyer_id: Optional[str] = Field(default=None, max_length=128)
    agent_id: Optional[str] = Field(default=None, max_length=64)
    caller_id: Optional[str] = Field(default=None, max_length=128)

    prompt_id: Optional[str] = Field(default=None, max_length=64)
    result_id: Optional[str] = Field(default=None, max_length=64)
    click_id: Optional[str] = Field(default=None, max_length=64)
    cart_id: Optional[str] = Field(default=None, max_length=128)
    quote_id: Optional[str] = Field(default=None, max_length=64)
    checkout_id: Optional[str] = Field(default=None, max_length=128)
    payment_id: Optional[str] = Field(default=None, max_length=128)
    order_id: Optional[str] = Field(default=None, max_length=128)
    refund_id: Optional[str] = Field(default=None, max_length=128)
    return_id: Optional[str] = Field(default=None, max_length=128)
    canonical_product_id: Optional[str] = Field(default=None, max_length=64)
    canonical_variant_id: Optional[str] = Field(default=None, max_length=64)
    trace_id: Optional[str] = Field(default=None, max_length=128)
    brief_id: Optional[str] = Field(default=None, max_length=128)

    source_channel: Optional[str] = Field(default=None, max_length=128)
    query_source: Optional[str] = Field(default=None, max_length=128)
    protocol_name: Optional[str] = Field(default=None, max_length=64)
    llm_provider: Optional[str] = Field(default=None, max_length=64)
    llm_model: Optional[str] = Field(default=None, max_length=128)

    amount_cents: Optional[int] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in STANDARD_COMMERCE_EVENT_TYPES:
            raise ValueError(f"unsupported event_type: {value}")
        return normalized

    @field_validator("platform")
    @classmethod
    def normalize_platform(cls, value: str) -> str:
        return value.strip().lower().replace(" ", "_")

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: Optional[str]) -> Optional[str]:
        return value.strip().upper() if value else None

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_stitch_key(self) -> "MerchantCommerceEvent":
        stitch_keys = (
            self.interaction_id,
            self.session_id,
            self.click_id,
            self.cart_id,
            self.quote_id,
            self.checkout_id,
            self.payment_id,
            self.order_id,
            self.refund_id,
            self.return_id,
            self.trace_id,
        )
        if not any(stitch_keys):
            raise ValueError("event must include at least one interaction/session/commerce stitch key")
        return self


class MerchantEventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: List[MerchantCommerceEvent] = Field(min_length=1, max_length=100)


async def ingest_merchant_event_batch(
    *,
    merchant_id: str,
    batch: MerchantEventBatch,
) -> Dict[str, Any]:
    """Normalize and append an idempotent batch to the canonical commerce ledger.

    Writes are intentionally individually idempotent rather than wrapped in one long
    transaction. A failed batch can be retried safely using the same event ids while
    already-accepted events are returned as duplicates.
    """
    results: List[Dict[str, Any]] = []
    for event in batch.events:
        metadata = {
            **event.metadata,
            **{
                key: value
                for key, value in {
                    "store_id": event.store_id,
                    "visitor_id": event.visitor_id,
                    "cart_id": event.cart_id,
                    "payment_id": event.payment_id,
                    "amount_cents": event.amount_cents,
                    "currency": event.currency,
                    "source_channel": event.source_channel,
                    "query_source": event.query_source,
                    "protocol_name": event.protocol_name,
                    "llm_provider": event.llm_provider,
                    "llm_model": event.llm_model,
                    "agent_id": event.agent_id,
                    "agent_identity_confidence": "merchant_asserted" if event.agent_id else "unknown",
                }.items()
                if value is not None
            },
        }
        result = await record_commerce_event(
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            source=event.source or f"{event.platform}_adapter",
            upstream_idempotency_key=event.event_id,
            actor_type="external_agent" if event.agent_id else "store_adapter",
            actor_id=event.agent_id or merchant_id,
            metadata=metadata,
            merchant_id=merchant_id,
            platform=event.platform,
            store_id=event.store_id,
            surface=event.surface,
            interaction_id=event.interaction_id,
            session_id=event.session_id,
            visitor_id=event.visitor_id,
            buyer_id=event.buyer_id,
            agent_id=event.agent_id,
            caller_id=event.caller_id,
            prompt_id=event.prompt_id,
            result_id=event.result_id,
            click_id=event.click_id,
            cart_id=event.cart_id,
            quote_id=event.quote_id,
            checkout_id=event.checkout_id,
            payment_id=event.payment_id,
            order_id=event.order_id,
            refund_id=event.refund_id,
            return_id=event.return_id,
            canonical_product_id=event.canonical_product_id,
            canonical_variant_id=event.canonical_variant_id,
            trace_id=event.trace_id,
            brief_id=event.brief_id,
            source_channel=event.source_channel,
            query_source=event.query_source,
            protocol_name=event.protocol_name,
            llm_provider=event.llm_provider,
            llm_model=event.llm_model,
        )
        results.append(
            {
                "event_id": event.event_id,
                "ledger_event_id": result["event_id"],
                "interaction_id": result["interaction_id"],
                "duplicate": bool(result.get("duplicate")),
            }
        )

    duplicate_count = sum(1 for result in results if result["duplicate"])
    return {
        "accepted": len(results) - duplicate_count,
        "duplicates": duplicate_count,
        "events": results,
    }

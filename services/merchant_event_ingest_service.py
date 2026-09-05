from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.commerce_interaction_service import record_commerce_event
from services.commerce_order_ref import ORDER_REF_MAX_LENGTH, is_valid_order_ref


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

AgentIdentityConfidence = Literal[
    "browser_observed",
    "merchant_asserted",
    "platform_asserted",
    "verified",
    "unknown",
]

_AGENT_WRITER_BY_CONFIDENCE = {
    "browser_observed": "browser_collector",
    "merchant_asserted": "merchant_adapter",
    "platform_asserted": "platform_adapter",
    "verified": "external_agent",
    "unknown": "store_adapter",
}

# The write-path / authority / confidence contract lives in
# services.commerce_ledger_provenance so the direct ledger writers can share
# it without importing this module. Re-exported here for the batch callers.
from services.commerce_ledger_provenance import (  # noqa: E402
    LEDGER_AUTHORITY_BY_WRITE_PATH,
    OPS_CANARY_SURFACE,
    LedgerAuthority,
    WritePath,
    _ALLOWED_CONFIDENCE_BY_WRITE_PATH,
    resolve_ledger_authority,
)

# The public collector is an analytics contract, not an arbitrary document
# store. Native adapters reduce their payloads to this vocabulary before model
# validation; custom/headless collectors must do the same.
ALLOWED_MERCHANT_METADATA_KEYS = frozenset(
    {
        "quantity",
        "native_topic",
        "native_status",
        "native_financial_status",
        "native_fulfillment_status",
        "native_payment_method",
        "native_payment_method_title",
        "native_line_items",
        "native_products",
        "native_product_bundle",
        "native_product_no",
        "native_variant_code",
        "native_quantity",
        "native_discount_total",
        "native_shipping_total",
        "native_total_tax",
        "native_cumulative_refund_total",
        "native_amount_semantics",
        "native_transaction_kind",
        "native_transaction_status",
        "native_event_name",
        "native_site_id",
        "native_event_no",
        "native_event_code",
        "native_checkout_id",
        "native_mall_id",
        "native_shop_no",
        "native_order_place_id",
        "native_paid_state",
        "native_payment_gateway",
        "native_shipping_type",
        "webhook_trace_id",
        "webhook_delivery_id",
    }
)
SAFE_NATIVE_LINE_ITEM_KEYS = frozenset(
    {
        "id",
        "product_id",
        "variation_id",
        "variant_id",
        "sku",
        "spu",
        "quantity",
        "price",
        "subtotal",
        "total",
    }
)
SAFE_NATIVE_PRODUCT_KEYS = frozenset(
    {
        "product_no",
        "variant_code",
        "product_code",
        "product_name",
        "cate_no",
        "cate_name",
        "quantity",
        "product_price",
        "option_extra_price",
        "option_value",
    }
)
SENSITIVE_METADATA_KEYS = frozenset(
    {
        "email",
        "phone",
        "address",
        "address1",
        "address2",
        "first_name",
        "last_name",
        "full_name",
        "browser_ip",
        "ip",
        "token",
        "access_token",
        "secret",
        "password",
        "authorization",
        "cookie",
        "card_number",
        "credit_card_number",
        "cpf",
        "tax_id",
        "receipt_json",
    }
)
SENSITIVE_METADATA_KEY_TOKENS = frozenset(
    {
        "email",
        "phone",
        "address",
        "ip",
        "token",
        "secret",
        "password",
        "authorization",
        "cookie",
        "card",
        "cpf",
    }
)
SENSITIVE_PERSON_NAME_KEYS = frozenset(
    {
        "customer_name",
        "buyer_name",
        "recipient_name",
        "billing_name",
        "shipping_name",
    }
)


def _is_sensitive_metadata_key(key: str) -> bool:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip())
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", separated).strip("_").lower()
    if normalized in SENSITIVE_METADATA_KEYS or normalized in SENSITIVE_PERSON_NAME_KEYS:
        return True
    return bool(set(normalized.split("_")) & SENSITIVE_METADATA_KEY_TOKENS)


def _validate_safe_native_items(value: Any, *, field: str, allowed: frozenset[str]) -> None:
    if not isinstance(value, list):
        raise ValueError(f"metadata.{field} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"metadata.{field}[{index}] must be an object")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ValueError(
                f"metadata.{field}[{index}] contains unsupported keys: "
                + ", ".join(str(key) for key in unknown[:10])
            )
        for key, child in item.items():
            if _is_sensitive_metadata_key(str(key)):
                raise ValueError(f"metadata.{field}[{index}] contains forbidden sensitive key: {key}")
            if isinstance(child, (dict, list)):
                raise ValueError(f"metadata.{field}[{index}].{key} must be a scalar")


def _validate_safe_metadata_tree(value: Any, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if _is_sensitive_metadata_key(key):
                raise ValueError(f"{path} contains forbidden sensitive key: {raw_key}")
            _validate_safe_metadata_tree(child, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_safe_metadata_tree(child, f"{path}[{index}]")


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
    # The canonical cross-authority order identity, `<namespace>:<native id>`.
    # Server writers (adapters and bridges) set it; a merchant collector may
    # only claim its OWN platform's namespace, which
    # services/merchant_event_store_binding.py enforces against the store the
    # batch is bound to.
    order_ref: Optional[str] = Field(default=None, max_length=ORDER_REF_MAX_LENGTH)
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

    @field_validator("order_ref")
    @classmethod
    def validate_order_ref(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if not is_valid_order_ref(normalized):
            raise ValueError(
                "order_ref must be '<namespace>:<native order id>' with a "
                "lowercase namespace and no whitespace"
            )
        return normalized

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

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        unknown = sorted(set(value) - ALLOWED_MERCHANT_METADATA_KEYS)
        if unknown:
            raise ValueError(
                "metadata contains unsupported keys: " + ", ".join(unknown[:10])
            )
        _validate_safe_metadata_tree(value)
        if "native_line_items" in value:
            _validate_safe_native_items(
                value["native_line_items"],
                field="native_line_items",
                allowed=SAFE_NATIVE_LINE_ITEM_KEYS,
            )
        if "native_products" in value:
            _validate_safe_native_items(
                value["native_products"],
                field="native_products",
                allowed=SAFE_NATIVE_PRODUCT_KEYS,
            )
        return value

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
            self.order_ref,
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
    # A caller may only lower its own standing: marking a batch synthetic
    # removes it from the caller's default funnel and nothing else.
    synthetic: bool = False


async def ingest_merchant_event_batch(
    *,
    merchant_id: str,
    batch: MerchantEventBatch,
    agent_identity_confidence: AgentIdentityConfidence,
    write_path: WritePath,
    synthetic: bool = False,
) -> Dict[str, Any]:
    """Normalize and append an idempotent batch to the canonical commerce ledger.

    Writes are intentionally individually idempotent rather than wrapped in one long
    transaction. A failed batch can be retried safely using the same event ids while
    already-accepted events are returned as duplicates.

    ``write_path`` names the ingress that authenticated this batch. The ledger
    authority is derived from it here, on the server, so no event body can
    promote itself to a PSP or platform fact.
    """
    actor_type = _AGENT_WRITER_BY_CONFIDENCE[agent_identity_confidence]
    authority = resolve_ledger_authority(write_path, agent_identity_confidence)
    batch_synthetic = bool(synthetic or batch.synthetic)
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
                    "agent_identity_confidence": (
                        agent_identity_confidence if event.agent_id else None
                    ),
                }.items()
                if value is not None
            },
        }
        result = await record_commerce_event(
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            source=event.source or f"{event.platform}_adapter",
            upstream_idempotency_key=event.event_id,
            actor_type=actor_type,
            actor_id=(
                event.agent_id
                if event.agent_id and agent_identity_confidence == "verified"
                else merchant_id
            ),
            metadata=metadata,
            write_path=write_path,
            authority=authority,
            agent_identity_confidence=agent_identity_confidence,
            synthetic=batch_synthetic or event.surface == OPS_CANARY_SURFACE,
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
            order_ref=event.order_ref,
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

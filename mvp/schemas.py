from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from mvp.constants import RiskTier, SchemaVersion, SCHEMA_VERSION


class Money(BaseModel):
    value: float
    currency: str = Field(min_length=3, max_length=8)


class Geo(BaseModel):
    country: str = Field(min_length=2, max_length=2)
    postal_code: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None


class ProductRef(BaseModel):
    platform: str
    platform_product_id: str
    variant_id: str
    sku_id: Optional[str] = None


class OfferPricing(BaseModel):
    currency: str = Field(min_length=3, max_length=8)
    subtotal: float
    discount_total: float = 0.0
    shipping_fee: float = 0.0
    tax: float = 0.0
    total: float


class DeliveryOption(BaseModel):
    handle: Optional[str] = None
    title: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    min_delivery_days: Optional[int] = Field(default=None, ge=0)
    max_delivery_days: Optional[int] = Field(default=None, ge=0)


class InventoryInfo(BaseModel):
    status: Literal["unknown", "in_stock", "out_of_stock"] = "unknown"
    checked_at: Optional[datetime] = None
    available_quantity: Optional[int] = Field(default=None, ge=0)


class Orderability(BaseModel):
    status: Literal["orderable", "unknown", "blocked"] = "unknown"
    reasons: List[str] = Field(default_factory=list)
    inventory: InventoryInfo = Field(default_factory=InventoryInfo)


class QuoteRef(BaseModel):
    quote_id: str
    expires_at: datetime
    engine: str
    engine_ref: str


class OfferConstraints(BaseModel):
    requires_hil: bool = False
    max_quantity: Optional[int] = Field(default=None, ge=1)
    restricted_categories: List[str] = Field(default_factory=list)
    notes: Optional[str] = None


class OfferObject(BaseModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    offer_id: str
    merchant_id: str
    product_ref: ProductRef
    geo: Geo
    pricing: OfferPricing
    delivery_options: List[DeliveryOption] = Field(default_factory=list)
    orderability: Orderability = Field(default_factory=Orderability)
    quote_ref: QuoteRef
    constraints: OfferConstraints = Field(default_factory=OfferConstraints)


class PreFlightCheck(BaseModel):
    status: Literal["pass", "warn", "fail"] = "pass"
    reason_codes: List[str] = Field(default_factory=list)
    debug: Dict[str, Any] = Field(default_factory=dict)


class PreFlightChecks(BaseModel):
    pricing: PreFlightCheck = Field(default_factory=PreFlightCheck)
    inventory: PreFlightCheck = Field(default_factory=PreFlightCheck)
    delivery: PreFlightCheck = Field(default_factory=PreFlightCheck)
    policy_disclosure: PreFlightCheck = Field(default_factory=PreFlightCheck)


class PreFlightResult(BaseModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    merchant_id: str
    product_ref: ProductRef
    geo: Geo
    status: Literal["pass", "warn", "fail"]
    reasons: List[str] = Field(default_factory=list)
    checks: PreFlightChecks = Field(default_factory=PreFlightChecks)
    required_actions: List[Dict[str, Any]] = Field(default_factory=list)
    fallback_options: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class EvidencePointer(BaseModel):
    type: str
    ref: Dict[str, Any] = Field(default_factory=dict)


class RecommendationClaim(BaseModel):
    claim: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[EvidencePointer] = Field(default_factory=list)


class AuthorizedSignal(BaseModel):
    signal_type: str
    actor: Dict[str, Any]
    proof: Dict[str, Any]


class RecommendationDisclosures(BaseModel):
    sponsored: bool = False
    affiliate: bool = False
    notes: Optional[str] = None


class RecommendationItem(BaseModel):
    offer_id: str
    title: str
    narrative: str
    claims: List[RecommendationClaim] = Field(default_factory=list)
    authorized_signals: List[AuthorizedSignal] = Field(default_factory=list)
    disclosures: RecommendationDisclosures = Field(default_factory=RecommendationDisclosures)


class RecommendationContext(BaseModel):
    session_id: str
    creator_id: Optional[str] = None
    language: Literal["en", "zh"] = "en"
    geo: Geo
    surface: str = "unknown"


class RecommendationPack(BaseModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    pack_id: str
    created_at: datetime
    context: RecommendationContext
    recommendations: List[RecommendationItem] = Field(default_factory=list)


class AuditActor(BaseModel):
    type: Literal["system", "staff", "app", "merchant", "customer", "agent"]
    ref: Optional[str] = None


class AuditRequestContext(BaseModel):
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    surface: Optional[str] = None
    ip_hash: Optional[str] = None
    user_agent_hash: Optional[str] = None


class AuditConsent(BaseModel):
    consent_token_ref: Optional[str] = None
    nonce: Optional[str] = None
    scope: List[str] = Field(default_factory=list)


class AuditSubject(BaseModel):
    order_id: Optional[str] = None
    quote_id: Optional[str] = None
    dispute_ref: Optional[str] = None


class PayloadDigests(BaseModel):
    request_sha256: Optional[str] = None
    response_sha256: Optional[str] = None


class HashChain(BaseModel):
    prev_chain_hash: Optional[str] = None
    chain_hash: str


class AuditEvent(BaseModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    audit_event_id: str
    merchant_id: str
    actor: AuditActor
    action: str
    occurred_at: datetime
    request_context: AuditRequestContext = Field(default_factory=AuditRequestContext)
    consent: AuditConsent = Field(default_factory=AuditConsent)
    subject: AuditSubject = Field(default_factory=AuditSubject)
    payload_digests: PayloadDigests = Field(default_factory=PayloadDigests)
    chain: HashChain


class LedgerSource(BaseModel):
    type: str
    psp: Optional[str] = None
    external_event_id: Optional[str] = None


class LedgerIngest(BaseModel):
    received_at: datetime
    signature_verified: bool = False
    idempotency_key: Optional[str] = None


class LedgerRefs(BaseModel):
    payment_intent_id: Optional[str] = None
    psp_transaction_id: Optional[str] = None
    shopify_order_id: Optional[str] = None


class LedgerEvent(BaseModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    event_id: str
    merchant_id: str
    order_id: Optional[str] = None
    event_type: str
    source: LedgerSource
    amount: Optional[Money] = None
    occurred_at: datetime
    ingest: LedgerIngest
    payload_sha256: str
    chain: HashChain
    refs: LedgerRefs = Field(default_factory=LedgerRefs)


class MeasurementTags(BaseModel):
    merchant_id: Optional[str] = None
    geo: Optional[Geo] = None
    surface: str = "unknown"
    adapter: Optional[str] = None
    risk_tier: RiskTier = "unknown"

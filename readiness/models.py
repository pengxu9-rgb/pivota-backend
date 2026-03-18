from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from models.standard_product import StandardProduct


class FieldFreshness(BaseModel):
    field: str
    observed_at: Optional[str] = None
    max_age_hours: float
    stale: bool
    source: str


class FieldProvenance(BaseModel):
    field: str
    source: str
    observed_at: Optional[str] = None
    source_of_truth: bool = True
    fallback_source: Optional[str] = None
    notes: Optional[str] = None


class FieldFamilyStatus(BaseModel):
    family: str
    status: str
    canonical_source: str
    source: str
    fallback_source: Optional[str] = None
    observed_at: Optional[str] = None
    max_age_hours: Optional[float] = None
    stale: bool = False
    blockers: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class CapabilityStatus(BaseModel):
    capability: str
    status: str
    score: int
    blockers: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class ReviewSummary(BaseModel):
    scope: str = "product"
    source: str
    default_view: str = "none"
    has_group: bool = False
    has_reviews: bool = False
    group_id: Optional[int] = None
    group_key: Optional[str] = None
    group_confidence: Optional[float] = None
    membership_confidence: Optional[float] = None
    review_count: int = 0
    rating_count: int = 0
    average_rating: Optional[float] = None
    verified_review_count: int = 0
    featured_review_count: int = 0
    latest_review_at: Optional[str] = None


class ChannelCoverageStatus(BaseModel):
    channel: str
    status: str
    ready_variant_count: int
    blocked_variant_count: int


class ReadyVariant(BaseModel):
    variant_id: str
    sku: Optional[str] = None
    title: str
    attributes: Dict[str, str] = Field(default_factory=dict)
    price: Dict[str, Any] = Field(default_factory=dict)
    inventory: Dict[str, Any] = Field(default_factory=dict)
    freshness: Dict[str, FieldFreshness] = Field(default_factory=dict)
    provenance: List[FieldProvenance] = Field(default_factory=list)
    source_of_truth: Dict[str, FieldFamilyStatus] = Field(default_factory=dict)
    reviews: Optional[ReviewSummary] = None
    blockers: Dict[str, List[str]] = Field(default_factory=dict)
    warnings: Dict[str, List[str]] = Field(default_factory=dict)
    discovery: CapabilityStatus
    checkout: CapabilityStatus
    channel_coverage: Dict[str, str] = Field(default_factory=dict)


class ReadyProduct(BaseModel):
    product_id: str
    title: str
    description: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    default_image_url: Optional[str] = None
    reviews: Optional[ReviewSummary] = None
    variants: List[ReadyVariant] = Field(default_factory=list)


class MerchantReadinessSnapshot(BaseModel):
    report_version: str = "readiness.v1"
    merchant_id: str
    merchant_name: str
    channel: str
    generated_at: str
    merchant_alpha_mode: str = "synthetic_fixture"
    readiness_score: int
    domain_scores: Dict[str, int] = Field(default_factory=dict)
    capability_status: Dict[str, str] = Field(default_factory=dict)
    blockers: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    merchant_capabilities: List[CapabilityStatus] = Field(default_factory=list)
    channel_coverage: List[ChannelCoverageStatus] = Field(default_factory=list)
    source_of_truth: Dict[str, str] = Field(default_factory=dict)
    stubbed_capabilities: List[str] = Field(default_factory=list)
    audit_notes: List[str] = Field(default_factory=list)
    products: List[ReadyProduct] = Field(default_factory=list)


class ReadinessSummary(BaseModel):
    tier: str
    label: str
    assessment_state: str
    assessment_scope: str = "one_merchant_alpha"
    channel: str = "ucp"
    score: Optional[int] = None
    merchant_alpha_mode: Optional[str] = None
    ready_variant_count: int = 0
    blocked_variant_count: int = 0
    top_blockers: List[str] = Field(default_factory=list)
    top_warnings: List[str] = Field(default_factory=list)
    summary_text: Optional[str] = None
    action_text: Optional[str] = None
    recommended_actions: List[str] = Field(default_factory=list)
    blocker_breakdown: List[Dict[str, Any]] = Field(default_factory=list)
    capability_status: Dict[str, str] = Field(default_factory=dict)
    generated_at: Optional[str] = None
    next_action: Optional[str] = None


class ChannelReadinessReport(BaseModel):
    export_version: str = "readiness_ucp_export.v1"
    merchant_id: str
    channel: str
    generated_at: str
    merchant_alpha_mode: str = "synthetic_fixture"
    readiness_score: int
    capability_status: Dict[str, str] = Field(default_factory=dict)
    blockers: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    source_of_truth: Dict[str, str] = Field(default_factory=dict)
    validation_warnings: List[str] = Field(default_factory=list)
    stubbed_capabilities: List[str] = Field(default_factory=list)
    offers: List[Dict[str, Any]] = Field(default_factory=list)


class MerchantSourceDataset(BaseModel):
    merchant_id: str
    merchant_name: str
    evaluation_reference_time: str
    merchant_alpha_mode: str = "synthetic_fixture"
    source_of_truth: Dict[str, str] = Field(default_factory=dict)
    capability_status: Dict[str, str] = Field(default_factory=dict)
    merchant_blockers: List[str] = Field(default_factory=list)
    merchant_warnings: List[str] = Field(default_factory=list)
    stubbed_capabilities: List[str] = Field(default_factory=list)
    merchant_policy: Dict[str, Any] = Field(default_factory=dict)
    payment_capabilities: Dict[str, Any] = Field(default_factory=dict)
    merchant_connection: Dict[str, Any] = Field(default_factory=dict)
    product_review_summaries: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    variant_review_summaries: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    review_diagnostics: Dict[str, Any] = Field(default_factory=dict)
    products: List[StandardProduct] = Field(default_factory=list)
    product_diagnostics: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    variant_diagnostics: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    audit_notes: List[str] = Field(default_factory=list)


class CheckoutSessionRecord(BaseModel):
    checkout_id: str
    merchant_id: str
    channel: str
    variant_id: str
    quantity: int
    payment_mode: str
    status: str
    continue_url: Optional[str] = None
    idempotency_key: Optional[str] = None
    order_id: Optional[str] = None
    session_payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OrderSyncEventRecord(BaseModel):
    checkout_id: str
    event_type: str
    event_payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None

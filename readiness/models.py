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
    platform: Optional[str] = None
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


class ScoreBundle(BaseModel):
    readiness_score: Optional[int] = None
    exposure_score: Optional[int] = None
    conversion_score: Optional[int] = None


class OptimizationPlan(BaseModel):
    plan_id: str
    snapshot_id: str
    workspace_version: str = "agent_commerce_optimization.v1"
    priority_policy_version: str = "merchant_readiness_priority.v1"
    refresh_state: str = "fresh"
    plan_status: str = "active"
    generated_at: Optional[str] = None
    expires_at: Optional[str] = None
    can_apply_actions: bool = True
    last_successful_rescore_at: Optional[str] = None


class ReadinessIssueBucket(BaseModel):
    code: str
    label: str
    severity: str
    scope: str
    affected_count: int = 0
    fix_surface: str
    fixability: str = "merchant_fixable"
    impact: str
    direct_target: str
    priority_score: float = 0.0
    priority_reason: Optional[str] = None
    reason_codes: List[str] = Field(default_factory=list)


class MerchantReadinessAction(BaseModel):
    action_id: Optional[str] = None
    action_type: str = "review"
    label: str
    description: str
    target_url: str
    fix_surface: str
    fixability: str = "merchant_fixable"
    scope: str
    impact: str
    affected_count: int = 0
    priority_score: float = 0.0
    priority_reason: Optional[str] = None
    related_bucket_codes: List[str] = Field(default_factory=list)


class ProductQueueIssue(BaseModel):
    code: str
    label: str
    impact: str
    affected_variant_count: int = 0


class ProductReadinessQueueItem(BaseModel):
    queue_item_scope: str = "product"
    queue_item_id: str
    product_id: str
    platform: str
    platform_product_id: Optional[str] = None
    title: str
    image_url: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    price_value: Optional[float] = None
    price_currency: Optional[str] = None
    content_quality_score: Optional[float] = None
    model_readiness_score: Optional[float] = None
    conversion_potential_score: Optional[float] = None
    quality_last_evaluated_at: Optional[str] = None
    blocked_variant_count: int = 0
    ready_variant_count: int = 0
    top_issues: List[ProductQueueIssue] = Field(default_factory=list)
    primary_action: Optional[str] = None
    fix_surface: str = "product_content"
    fixability: str = "merchant_fixable"
    impact: str = "discovery_only"
    priority_score: float = 0.0
    priority_reason: Optional[str] = None
    recommended_action_id: Optional[str] = None
    recommended_action_type: Optional[str] = None


class MerchantReadinessOptimizationPayload(BaseModel):
    plan: OptimizationPlan
    score_bundle: ScoreBundle = Field(default_factory=ScoreBundle)
    readiness_summary: ReadinessSummary
    issue_buckets: List[ReadinessIssueBucket] = Field(default_factory=list)
    merchant_actions: List[MerchantReadinessAction] = Field(default_factory=list)
    product_queue: List[ProductReadinessQueueItem] = Field(default_factory=list)
    last_generated_at: Optional[str] = None


class RemediationAction(BaseModel):
    action_id: str
    plan_id: str
    action_type: str
    surface: str
    scope: str
    targets: List[Dict[str, Any]] = Field(default_factory=list)
    fixability: str = "merchant_fixable"
    priority_score: float = 0.0
    priority_reason: Optional[str] = None
    reason: Optional[str] = None
    preconditions: List[str] = Field(default_factory=list)
    idempotency_key: Optional[str] = None
    status: str = "suggested"


class ExecutionJob(BaseModel):
    job_id: str
    action_id: str
    executor_type: str
    status: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Dict[str, Any] = Field(default_factory=dict)
    error_code: Optional[str] = None
    retry_count: int = 0


class PatchCandidate(BaseModel):
    candidate_id: str
    action_id: str
    target_field: str
    before: Any = None
    after: Any = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    evidence_used: List[Dict[str, Any]] = Field(default_factory=list)
    risk_flags: List[str] = Field(default_factory=list)
    requires_approval: bool = True


class VerificationResult(BaseModel):
    verification_id: str
    action_id: str
    before_snapshot_id: str
    after_snapshot_id: str
    delta_scores: Dict[str, Any] = Field(default_factory=dict)
    resolved_issues: List[str] = Field(default_factory=list)
    remaining_issues: List[str] = Field(default_factory=list)
    expected_impact: Dict[str, Any] = Field(default_factory=dict)
    observed_impact: Dict[str, Any] = Field(default_factory=dict)
    merchant_visible_impact: Optional[str] = None


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

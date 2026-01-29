from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


BriefSchemaVersion = Literal["0.1.0"]
BriefVertical = Literal["beauty"]

BeautySkinType = Literal["oily", "dry", "combo", "sensitive", "normal", "unknown"]
BeautyBarrierStatus = Literal["strong", "impaired", "unknown"]
BeautyConcern = Literal[
    "acne",
    "closed_comedones",
    "aging",
    "dark_spots",
    "redness",
    "rough_texture",
    "dryness",
    "oil_control",
    "unknown",
]


class BriefAgentRef(BaseModel):
    agent_id: str
    partner_app_id: Optional[str] = None
    integration_id: Optional[str] = None


class BriefMarket(BaseModel):
    market: Optional[str] = None  # e.g. "US", "CN"
    locale: Optional[str] = None  # e.g. "en-US", "zh-CN"
    currency: Optional[str] = None  # e.g. "USD", "CNY"


class BriefRawIntent(BaseModel):
    text: str
    lang: Optional[str] = None
    captured_at: datetime


class BriefBudgetConstraint(BaseModel):
    currency: Optional[str] = None
    max: Optional[float] = None


class BriefAvailabilityConstraint(BaseModel):
    in_stock_only: Optional[bool] = True


class BriefMerchantsConstraint(BaseModel):
    allowed_merchant_ids: Optional[List[str]] = None


class BriefConstraints(BaseModel):
    budget: BriefBudgetConstraint = Field(default_factory=BriefBudgetConstraint)
    availability: BriefAvailabilityConstraint = Field(default_factory=BriefAvailabilityConstraint)
    merchants: BriefMerchantsConstraint = Field(default_factory=BriefMerchantsConstraint)


class StandardProductRef(BaseModel):
    merchant_id: str
    platform: Optional[str] = None  # shopify/wix/...
    product_id: str
    variant_id: Optional[str] = None


class BriefProposedItem(BaseModel):
    role: Optional[str] = None  # anchor / preferred / excluded
    quantity: int = Field(default=1, ge=1)
    standard_product_ref: StandardProductRef


class BriefAssumption(BaseModel):
    id: str
    text: str
    confidence: float = Field(ge=0.0, le=1.0)


class BriefEvidenceSource(BaseModel):
    type: str  # user_input | retrieval | rulepack | api
    source: str
    captured_at: datetime
    details: Optional[Dict[str, Any]] = None


class BriefAppliedRule(BaseModel):
    rule_id: str
    reason: str


class BriefEvidence(BaseModel):
    retrieved_at: datetime
    retrieval_sources: List[BriefEvidenceSource] = Field(default_factory=list)
    applied_rules: List[BriefAppliedRule] = Field(default_factory=list)


class BriefTelemetry(BaseModel):
    trace_id: str
    session_id: Optional[str] = None
    request_id: Optional[str] = None


class BeautyRoutinePreferences(BaseModel):
    split_am_pm: Optional[bool] = True


class BeautyExtension(BaseModel):
    skin_type: BeautySkinType = "unknown"
    barrier_status: BeautyBarrierStatus = "unknown"
    concerns: List[BeautyConcern] = Field(default_factory=list)
    sensitive_skin: Optional[bool] = None
    routine_preferences: BeautyRoutinePreferences = Field(default_factory=BeautyRoutinePreferences)


class BriefExtensions(BaseModel):
    beauty: Optional[BeautyExtension] = None


class ShoppingBriefV0(BaseModel):
    schema_version: BriefSchemaVersion = "0.1.0"
    brief_id: str
    created_at: datetime

    agent: BriefAgentRef
    market: BriefMarket
    raw_intent: BriefRawIntent
    vertical: BriefVertical = "beauty"

    constraints: BriefConstraints = Field(default_factory=BriefConstraints)
    proposed_items: List[BriefProposedItem] = Field(default_factory=list)
    assumptions: List[BriefAssumption] = Field(default_factory=list)
    evidence: BriefEvidence
    risk_tags: List[str] = Field(default_factory=list)
    telemetry: BriefTelemetry
    extensions: BriefExtensions = Field(default_factory=BriefExtensions)


class BriefQuestionChoice(BaseModel):
    value: str
    label: str


class BriefQuestion(BaseModel):
    id: str
    text: str
    type: Literal["number", "single_choice", "multi_choice", "text"]
    unit: Optional[str] = None
    choices: Optional[List[BriefQuestionChoice]] = None


class BriefClarifyRequest(BaseModel):
    raw_query: str
    partial_brief: Optional[Dict[str, Any]] = None
    market: Optional[str] = None
    locale: Optional[str] = None
    currency: Optional[str] = None


class BriefClarifyResponse(BaseModel):
    status: Literal["success"] = "success"
    suggested_vertical: BriefVertical = "beauty"
    missing_fields: List[str] = Field(default_factory=list)
    questions: List[BriefQuestion] = Field(default_factory=list)


class BriefBuildRequest(BaseModel):
    raw_query: str
    answers: Optional[Dict[str, Any]] = None
    market: Optional[str] = None
    locale: Optional[str] = None
    currency: Optional[str] = None
    telemetry: Optional[Dict[str, Any]] = None


class BriefBuildResponse(BaseModel):
    status: Literal["success"] = "success"
    confidence: float = Field(ge=0.0, le=1.0)
    brief: ShoppingBriefV0


class CompatibilityCandidateItem(BaseModel):
    merchant_id: str
    platform: Optional[str] = None
    product_id: str
    variant_id: Optional[str] = None
    title: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    in_stock: Optional[bool] = None
    orderable: Optional[bool] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


class CompatibilityResult(BaseModel):
    candidate: StandardProductRef
    fit_score: float = Field(ge=0.0, le=1.0)
    reasons: List[str] = Field(default_factory=list)
    required_changes: List[str] = Field(default_factory=list)
    risk_tags: List[str] = Field(default_factory=list)
    evidence: Optional[Dict[str, Any]] = None


class BriefCompatibilityRequest(BaseModel):
    brief: ShoppingBriefV0
    candidate_items: List[CompatibilityCandidateItem]


class BriefCompatibilityResponse(BaseModel):
    status: Literal["success"] = "success"
    results: List[CompatibilityResult]

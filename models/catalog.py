from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CatalogSyncJobCreateRequest(BaseModel):
    merchant_id: str
    connector: str = "shopify"
    mode: str = "reconcile"
    platform: Optional[str] = "shopify"
    force_refresh: bool = False
    limit: int = Field(default=500, ge=1, le=5000)
    sync_from_cache: bool = True
    scope: Optional[Dict[str, Any]] = None
    requested_by: Optional[str] = None


class CatalogSyncJobResponse(BaseModel):
    job_id: str
    merchant_id: str
    connector: str
    mode: str
    status: str
    scope: Dict[str, Any] = Field(default_factory=dict)
    stats: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CatalogWebhookIngestResponse(BaseModel):
    event_id: str
    merchant_id: str
    connector: str
    event_type: str
    topic: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None


class PaymentIncentiveInput(BaseModel):
    incentive_id: Optional[str] = None
    incentive_type: str
    funding_source: Optional[str] = None
    payment_method_type: Optional[str] = None
    card_network: Optional[str] = None
    issuer_name: Optional[str] = None
    wallet_type: Optional[str] = None
    installment_provider: Optional[str] = None
    label: str
    benefit_kind: str
    benefit_value: Optional[Decimal] = None
    benefit_currency: Optional[str] = None
    market: Optional[str] = None
    eligibility_confidence: Optional[Decimal] = None
    source_system: str = "merchant_config"
    source_ref: Optional[str] = None
    status: str = "active"
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    rule_scope: Dict[str, Any] = Field(default_factory=dict)
    rule_conditions: Dict[str, Any] = Field(default_factory=dict)
    schedule: Dict[str, Any] = Field(default_factory=dict)
    human_rule: Optional[str] = None


class IncentivesReconcileRequest(BaseModel):
    source_system: str = "merchant_config"
    payment_incentives: List[PaymentIncentiveInput] = Field(default_factory=list)


class IncentivesReconcileResponse(BaseModel):
    merchant_id: str
    source_system: str
    promotions_synced: int
    payment_incentives_synced: int
    offer_links_synced: int
    reconciled_at: datetime


class PivotPaymentContext(BaseModel):
    psp: Optional[str] = None
    payment_method_type: Optional[str] = None
    card_network: Optional[str] = None
    issuer_name: Optional[str] = None
    wallet_type: Optional[str] = None
    installment_provider: Optional[str] = None


class PivotQueryRequest(BaseModel):
    query: str
    merchant_id: Optional[str] = None
    market: str = "US"
    limit: int = Field(default=20, ge=1, le=100)
    include_external: bool = True
    include_incentives: bool = True
    payment_context: Optional[PivotPaymentContext] = None


class PivotPricing(BaseModel):
    currency: Optional[str] = None
    list_price: Optional[Decimal] = None
    merchant_effective_price: Optional[Decimal] = None
    estimated_best_price: Optional[Decimal] = None
    exact_quote_price: Optional[Decimal] = None
    price_confidence: Optional[Decimal] = None


class IncentiveNode(BaseModel):
    incentive_id: str
    label: str
    incentive_type: str
    benefit_kind: str
    benefit_value: Optional[Decimal] = None
    benefit_currency: Optional[str] = None
    funding_source: Optional[str] = None
    payment_method_type: Optional[str] = None
    card_network: Optional[str] = None
    issuer_name: Optional[str] = None
    wallet_type: Optional[str] = None
    installment_provider: Optional[str] = None
    market: Optional[str] = None
    eligibility_confidence: Optional[Decimal] = None
    source_system: Optional[str] = None


class ProductClaim(BaseModel):
    """A single benefit claim with its provenance. claim_text is what's asserted;
    the rest trace it to a source so an agent can cite it claim-safely. See
    services.claim_safety for the substantiation/source vocabularies."""

    claim_text: str
    source_ref: Optional[str] = None
    source_type: Optional[str] = None
    evidence_grade: Optional[str] = None
    substantiation_status: str = "unverified"


class RequiredDisclaimer(BaseModel):
    """A disclaimer that must accompany a product (e.g. the FDA/DSHEA supplement
    disclaimer). code is a stable key; applies_to is the category_kind."""

    code: str
    text: str
    applies_to: Optional[str] = None


class EvidenceProfile(BaseModel):
    """Structured evidence for a product: its provenance-backed claims plus the
    review state of that evidence as a whole."""

    claims: List[ProductClaim] = Field(default_factory=list)
    review_state: str = "observed"


class BeautyVerticalPayload(BaseModel):
    # Durable category_kind (skincare/haircare/supplement) the record resolves
    # to; drives claim-safety + which disclaimers are required. See mig 151.
    category_kind: Optional[str] = None
    # Structured skincare attributes (mig-free; profile_payload + text-derived).
    # skincare_format e.g. serum/cream/sheet mask/sunscreen; spf_value for
    # sunscreens; fragrance_free/sensitive_safe for sensitive-skin fit.
    skincare_format: Optional[str] = None
    texture: Optional[str] = None
    spf_value: Optional[int] = None
    fragrance_free: bool = False
    sensitive_safe: bool = False
    # Structured haircare attributes (mig-free; profile_payload + text-derived).
    # haircare_format e.g. shampoo/conditioner/treatment/hair serum; sulfate_free
    # / silicone_free formulation flags; vegan_status / cruelty_free_status are
    # "verified" (recognized certifying authority) or "claimed" (bare marketing
    # tag) or None -- the niche-new cert-trust signal for Anuko's positioning.
    haircare_format: Optional[str] = None
    sulfate_free: bool = False
    silicone_free: bool = False
    vegan_status: Optional[str] = None
    cruelty_free_status: Optional[str] = None
    taxonomy: Dict[str, Any] = Field(default_factory=dict)
    concerns: List[str] = Field(default_factory=list)
    claims: List[str] = Field(default_factory=list)
    routine_phase: Optional[str] = None
    benefits: List[str] = Field(default_factory=list)
    ingredients: List[str] = Field(default_factory=list)
    active_ingredients: List[Dict[str, Any]] = Field(default_factory=list)
    how_to_use: Optional[str] = None
    usage_steps: List[str] = Field(default_factory=list)
    shades: List[Dict[str, Any]] = Field(default_factory=list)
    tutorials: List[Dict[str, Any]] = Field(default_factory=list)
    compatibility_rules: List[Dict[str, Any]] = Field(default_factory=list)
    # Provenance-backed evidence + any required disclaimers (data contract,
    # Evidence/provenance layer; mig 150). None/empty until authored.
    evidence_profile: Optional[EvidenceProfile] = None
    required_disclaimers: List[RequiredDisclaimer] = Field(default_factory=list)


class MerchantNode(BaseModel):
    merchant_id: Optional[str] = None
    merchant_name: Optional[str] = None
    primary_platform: Optional[str] = None


class ProductNode(BaseModel):
    product_key: Optional[str] = None
    source_product_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    brand: Optional[str] = None
    product_type: Optional[str] = None
    category: Optional[str] = None
    canonical_url: Optional[str] = None
    image_url: Optional[str] = None


class SkuNode(BaseModel):
    sku_key: Optional[str] = None
    source_variant_id: Optional[str] = None
    sku: Optional[str] = None
    barcode: Optional[str] = None
    title: Optional[str] = None
    visible_attributes: Dict[str, List[str]] = Field(default_factory=dict)
    visible_option_labels: List[str] = Field(default_factory=list)
    ingredient_ids: List[str] = Field(default_factory=list)


class OfferNode(BaseModel):
    offer_id: str
    catalog_track: str
    truth_tier: str
    readiness_tier: str
    offer_mode: str
    source_system: Optional[str] = None
    availability: Optional[str] = None
    inventory_quantity: Optional[int] = None
    # Agent-decision-grade offer fields (data contract, Offers/buy-box layer):
    # offer_type is brand_direct | retailer | None ("unknown"), market scopes the
    # offer, is_first_party flags the merchant's own storefront, and
    # why_buy_direct carries an honest rationale (authored later, NULL until then).
    offer_type: Optional[str] = None
    market: str = "US"
    is_first_party: bool = False
    why_buy_direct: Optional[str] = None
    pricing: PivotPricing = Field(default_factory=PivotPricing)
    incentives: List[IncentiveNode] = Field(default_factory=list)
    payment_offer_evidence: Dict[str, Any] = Field(default_factory=dict)
    savings_presentation: Dict[str, Any] = Field(default_factory=dict)


class PivotResultItem(BaseModel):
    merchant: MerchantNode = Field(default_factory=MerchantNode)
    product: ProductNode = Field(default_factory=ProductNode)
    sku: SkuNode = Field(default_factory=SkuNode)
    offers: List[OfferNode] = Field(default_factory=list)
    catalog_track: str
    truth_tier: str
    readiness_tier: str
    freshness: Dict[str, Any] = Field(default_factory=dict)
    source_system: Optional[str] = None
    match_explanation: Dict[str, Any] = Field(default_factory=dict)
    verticals: Dict[str, Any] = Field(default_factory=dict)


class PivotQueryResponse(BaseModel):
    query: str
    total: int
    items: List[PivotResultItem] = Field(default_factory=list)


class PivotQuoteItem(BaseModel):
    offer_id: Optional[str] = None
    product_key: Optional[str] = None
    sku_key: Optional[str] = None
    product_id: Optional[str] = None
    variant_id: Optional[str] = None
    quantity: int = Field(default=1, ge=1)


class PivotQuoteRequest(BaseModel):
    merchant_id: str
    items: List[PivotQuoteItem]
    payment_context: Optional[PivotPaymentContext] = None
    discount_codes: Optional[List[str]] = None
    customer_email: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None
    selected_delivery_option: Optional[Dict[str, Any]] = None


class PivotQuoteResponse(BaseModel):
    quote_id: Optional[str] = None
    merchant_id: str
    pricing: PivotPricing
    incentives: List[IncentiveNode] = Field(default_factory=list)
    store_discount_evidence: Dict[str, Any] = Field(default_factory=dict)
    payment_offer_evidence: Dict[str, Any] = Field(default_factory=dict)
    savings_presentation: Dict[str, Any] = Field(default_factory=dict)
    quote_payload: Dict[str, Any] = Field(default_factory=dict)


class PivotOffersResolveRequest(BaseModel):
    merchant_id: Optional[str] = None
    product_key: Optional[str] = None
    sku_key: Optional[str] = None
    query: Optional[str] = None
    market: str = "US"
    include_external: bool = True
    payment_context: Optional[PivotPaymentContext] = None


class PivotOffersResolveResponse(BaseModel):
    merchant_id: Optional[str] = None
    product_key: Optional[str] = None
    sku_key: Optional[str] = None
    offers: List[OfferNode] = Field(default_factory=list)
    offers_count: int = 0
    # The single resolved best US-buyable offer (buy-box), or None when no
    # offer qualifies. See services.offer_classification.select_best_us_offer.
    best_us_offer: Optional[OfferNode] = None

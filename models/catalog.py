from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


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
    # Canonical catalog mode: product identity must be a public sig_*. Supply
    # provenance remains offer-scoped, so this disables the legacy direct-seed
    # result lane without excluding external referral offers in catalog_offers.
    canonical_entities_only: bool = False
    include_incentives: bool = True
    payment_context: Optional[PivotPaymentContext] = None
    # ADR-007 SLICE 3: the intent signal threaded down from the gateway. When the
    # caller surface is strict/commerce-explicit (shopping intent), the OFFER-FREE
    # citable lane is SUPPRESSED — citation-only rows must never appear for a
    # buy-now request. Default False (non-strict / inform-recommend) so the citable
    # lane MAY contribute when INDEX_ELIGIBLE_RECALL is on. The flag still gates
    # whether the lane runs at all; this only narrows WHEN it may contribute.
    strict_serving_mode: bool = False


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
    pivota_signature_id: Optional[str] = None
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

    @field_validator("visible_attributes", mode="before")
    @classmethod
    def _coerce_visible_attributes(cls, value: Any) -> Dict[str, List[str]]:
        """Coerce attribute values to lists.

        Upstream producers are inconsistent: external_seed records and the
        variant promoter (services/catalog_variant_promoter.py keys this as
        Dict[str, str]) emit a scalar label (e.g. {"Format": "Garden Gift Set"}),
        while the contract is Dict[str, List[str]]. Strict validation rejected
        the scalar shape and dropped the whole SkuNode — silently erroring SKUs
        out of the pivot/eval assembly. Normalize scalars to single-element
        lists rather than failing."""
        if not isinstance(value, dict):
            return {}
        out: Dict[str, List[str]] = {}
        for key, val in value.items():
            label = str(key)
            if val is None:
                out[label] = []
            elif isinstance(val, str):
                out[label] = [val] if val.strip() else []
            elif isinstance(val, (list, tuple, set)):
                out[label] = [str(x) for x in val if x is not None and str(x).strip()]
            else:
                out[label] = [str(val)]
        return out


class OfferNode(BaseModel):
    offer_id: str
    merchant_id: Optional[str] = None
    merchant_name: Optional[str] = None
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
    # official_source — the authenticity/trust signal. ⚠️ ITS CONTRACT IS CHANGING;
    # see docs/adr/ADR-019-official-source-seller-derived.md.
    #
    # AS ORIGINALLY DEFINED (still the behaviour while OFFICIAL_SOURCE_SELLER_DERIVED
    # is OFF — which is the default, so prod today): distinct from is_first_party (a
    # Pivota merchant's OWN storefront) — True when the offer is served from the
    # BRAND'S OWN official domain, detected by matching offer source_domain against
    # the product's canonical PDP host. A retailer or marketplace mirror is NOT
    # official_source. Its stated purpose was to let the decision-grade `trust`
    # dimension pass on official-brand seeds correctly not marked is_first_party.
    #
    # WHY THAT IS WRONG: on the external-seed mirror lane both sides of that
    # comparison derive from the same seed record, so it is a tautology. Measured on
    # prod 2026-07-27 it was True for 2,646 of 2,646 candidate rows — including 480
    # typed `retailer`. And the cohort it exists to serve (official-brand,
    # correctly-not-first-party) is EMPTY: no writer produces brand_direct WITHOUT
    # is_first_party. (Some do the reverse — catalog_sync_service.py:1383-1397 sets
    # is_first_party=True and deliberately leaves offer_type NULL — but that is the
    # harmless direction and does not create the cohort.)
    #
    # WITH THE FLAG ON: official_source == is_first_party, exactly. Be honest about
    # what that means — the field becomes an ALIAS, not a repaired signal, and so it
    # inherits is_first_party's own limitations. Two, and the second is closer to the
    # defect being fixed than the first:
    #   * a Pivota-onboarded RESELLER merchant still reports official_source=True;
    #   * scripts/onboard_external_brand_from_crawl.py:390 coerces the seller
    #     derivation's honest "unknown" (None) to brand_direct via `or "brand_direct"`,
    #     so a crawl seed with NO positive brand evidence also reports True —
    #     manufacturing a positive claim from nothing, which is precisely what this
    #     change removes on the other lane.
    # OPEN DECISION for the flip (ADR-019 rollout
    # step 5): DEPRECATE this field rather than redefine it. It is a public contract
    # field, so that should be chosen deliberately, not by letting an alias persist.
    official_source: bool = False
    why_buy_direct: Optional[str] = None
    # Market-aware buyability (set at serve against the request market).
    # market_availability is "domestic" when this offer serves the buyer's market
    # or "cross_border" when it's a different market (possibly shippable, with
    # caveats — we don't collapse that into "unavailable" without ships_to data).
    # is_buy_pick flags the single offer to present as the buy: cheapest in-stock
    # domestic, falling back to a flagged cross_border offer.
    market_availability: str = "domestic"
    is_buy_pick: bool = False
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
    # ADR-007 SLICE 3: buyability is now EXPLICIT on the result item. Offer-backed
    # recall rows are buyable (default True ⇒ pre-ADR-007 behavior is unchanged).
    # The OFFER-FREE "citable" lane (index_eligible, no catalog_offers join) emits
    # items with buyable=False and offers=[] — citation-only, NEVER transactable.
    # The quote/order path fails closed for these (no offer_id/sku_key to resolve).
    buyable: bool = True
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

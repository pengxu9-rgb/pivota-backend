"""Commerce Index v2 field-authority and delta-publication contract.

The canonical catalog tables remain the serving projection.  This module decides
whether a newly observed field may advance that projection and which downstream
projections need work.  It is intentionally deterministic: crawlers, feeds, and
merchant APIs cannot each invent their own freshness or graph-rebuild rules.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Tuple


SOURCE_AUTHORITY = {
    "merchant_api": 100,
    "pim_erp_pos": 100,
    "contracted_feed": 95,
    "review_provider": 90,
    "official_pdp": 75,
    "retailer_listing": 60,
    "public_crawl": 45,
}


def commerce_index_v2_enabled() -> bool:
    """Feature gate for the migration-safe rollout of the delta publication lane."""
    return str(os.getenv("COMMERCE_INDEX_V2_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def commerce_index_v2_enabled_for_merchant(merchant_id: Optional[str]) -> bool:
    """Return true only for an explicitly allowlisted merchant.

    A global feature flag is not a canary.  Requiring the second, scoped gate
    prevents a staging or production rollout for one merchant from enqueueing
    work for every existing catalog connector.
    """
    if not commerce_index_v2_enabled():
        return False
    normalized_merchant_id = str(merchant_id or "").strip()
    allowed = {
        item.strip()
        for item in str(os.getenv("COMMERCE_INDEX_V2_MERCHANT_ALLOWLIST") or "").split(",")
        if item.strip()
    }
    return bool(normalized_merchant_id and allowed and normalized_merchant_id in allowed)


def source_kind_for_system(source_system: str) -> str:
    """Conservative source-class mapping for the existing catalog adapters."""
    source = str(source_system or "").strip().lower()
    if any(token in source for token in ("shopify", "wix", "woocommerce", "bigcommerce", "square", "merchant", "products_cache")):
        return "merchant_api"
    if "review" in source:
        return "review_provider"
    if any(token in source for token in ("feed", "erp", "pim", "pos")):
        return "contracted_feed"
    if any(token in source for token in ("crawl", "seed", "pdp")):
        return "public_crawl"
    # Unknown integrations must earn authority explicitly rather than silently
    # acquiring price/inventory publishing rights.
    return "public_crawl"


@dataclass(frozen=True)
class FieldObservation:
    entity_type: str
    entity_id: str
    field_family: str
    field_key: str
    value: Any
    source_system: str
    source_kind: str
    observed_at: datetime
    confidence: float
    source_ref: Optional[str] = None
    fresh_until: Optional[datetime] = None

    @property
    def field_path(self) -> str:
        return f"{self.field_family}.{self.field_key}"


@dataclass(frozen=True)
class FieldChangePlan:
    changed: bool
    review_required: bool
    value_fingerprint: str
    publication_targets: Tuple[str, ...]
    reason: str


def canonical_json(value: Any) -> str:
    """Stable representation for content change detection, not display."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def value_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _publication_targets(field_family: str) -> Tuple[str, ...]:
    family = str(field_family or "").strip().lower()
    if family in {"identity", "taxonomy", "attributes", "content", "media"}:
        return ("search_index", "relation_graph", "product_insights")
    if family in {"price", "pricing", "inventory", "availability"}:
        return ("search_index", "checkout_validation", "product_insights")
    if family in {"review", "reviews"}:
        return ("search_index", "product_insights")
    return ("search_index", "product_insights")


def plan_field_change(
    observation: FieldObservation,
    *,
    previous_value: Any = None,
    previous_fingerprint: Optional[str] = None,
) -> FieldChangePlan:
    """Classify one observed fact before it is published downstream.

    Public crawl observations are allowed as evidence, but cannot automatically
    advance price/availability/inventory because those are checkout-sensitive.
    """
    fingerprint = value_fingerprint(observation.value)
    prior = previous_fingerprint or (value_fingerprint(previous_value) if previous_value is not None else None)
    if prior == fingerprint:
        return FieldChangePlan(False, False, fingerprint, (), "value_unchanged")

    authority = SOURCE_AUTHORITY.get(str(observation.source_kind or "").strip().lower(), 0)
    checkout_sensitive = str(observation.field_family or "").strip().lower() in {
        "price", "pricing", "inventory", "availability"
    }
    review_required = checkout_sensitive and authority < SOURCE_AUTHORITY["official_pdp"]
    targets = () if review_required else _publication_targets(observation.field_family)
    return FieldChangePlan(
        changed=True,
        review_required=review_required,
        value_fingerprint=fingerprint,
        publication_targets=targets,
        reason="checkout_sensitive_source_below_authority_threshold" if review_required else "value_changed",
    )

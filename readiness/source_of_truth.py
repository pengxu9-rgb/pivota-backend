from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from readiness.models import FieldFamilyStatus, FieldFreshness, FieldProvenance


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class FieldFamilyRule:
    family: str
    canonical_source: str
    fallback_source: Optional[str]
    max_age_hours: float


FIELD_FAMILY_RULES: Dict[str, FieldFamilyRule] = {
    "catalog": FieldFamilyRule(
        family="catalog",
        canonical_source="shopify_cache.standard_product.v1",
        fallback_source="shopify_admin.products.v2025-10",
        max_age_hours=24,
    ),
    "price": FieldFamilyRule(
        family="price",
        canonical_source="shopify_cache.variant_offer.v1",
        fallback_source="shopify_admin.products.v2025-10",
        max_age_hours=1,
    ),
    "inventory": FieldFamilyRule(
        family="inventory",
        canonical_source="shopify_admin.inventory.v2025-10",
        fallback_source="shopify_cache.inventory.v1",
        max_age_hours=0.25,
    ),
    "fulfillment_policy": FieldFamilyRule(
        family="fulfillment_policy",
        canonical_source="readiness.alpha_policy_config.v1",
        fallback_source=None,
        max_age_hours=24 * 30,
    ),
    "checkout_capability": FieldFamilyRule(
        family="checkout_capability",
        canonical_source="readiness.checkout_capability.v1",
        fallback_source=None,
        max_age_hours=1,
    ),
    "order_status": FieldFamilyRule(
        family="order_status",
        canonical_source="readiness.order_sync.v2",
        fallback_source="orders.local.v1",
        max_age_hours=1,
    ),
    "reviews_confidence": FieldFamilyRule(
        family="reviews_confidence",
        canonical_source="reviews_center.review_group.v1",
        fallback_source="reviews_center.product_reviews.v1",
        max_age_hours=24 * 30,
    ),
}


def build_field_family_status(
    *,
    family: str,
    source: Optional[str],
    observed_at: Optional[str],
    reference_time: datetime,
    max_age_hours_override: Optional[float] = None,
    blockers: Optional[Iterable[str]] = None,
    warnings: Optional[Iterable[str]] = None,
    notes: Optional[Iterable[str]] = None,
    fallback_source: Optional[str] = None,
) -> Tuple[FieldFamilyStatus, FieldFreshness, FieldProvenance]:
    rule = FIELD_FAMILY_RULES[family]
    source_value = source or rule.canonical_source
    max_age_hours = rule.max_age_hours if max_age_hours_override is None else max_age_hours_override
    observed = _parse_datetime(observed_at)
    stale = True
    if observed is not None:
        age_hours = (reference_time - observed.astimezone(timezone.utc)).total_seconds() / 3600
        stale = age_hours > max_age_hours

    blockers_list = [str(item) for item in (blockers or []) if str(item).strip()]
    warnings_list = [str(item) for item in (warnings or []) if str(item).strip()]
    notes_list = [str(item) for item in (notes or []) if str(item).strip()]

    if stale and "stale" not in {warning.split("_")[-1] for warning in warnings_list}:
        # Keep stale classification explicit without forcing all callers to add it by hand.
        warnings_list = list(warnings_list)
        warnings_list.append(f"{family}_snapshot_stale")

    status = "blocked" if blockers_list else "warning" if warnings_list else "ready"
    freshness = FieldFreshness(
        field=family,
        observed_at=_iso(observed),
        max_age_hours=max_age_hours,
        stale=stale,
        source=source_value,
    )
    provenance = FieldProvenance(
        field=family,
        source=source_value,
        observed_at=freshness.observed_at,
        source_of_truth=(source_value == rule.canonical_source),
        fallback_source=fallback_source or rule.fallback_source,
        notes="; ".join(notes_list) if notes_list else None,
    )
    return (
        FieldFamilyStatus(
            family=family,
            status=status,
            canonical_source=rule.canonical_source,
            source=source_value,
            fallback_source=fallback_source or rule.fallback_source,
            observed_at=freshness.observed_at,
            max_age_hours=max_age_hours,
            stale=stale,
            blockers=blockers_list,
            warnings=warnings_list,
            notes=notes_list,
        ),
        freshness,
        provenance,
    )

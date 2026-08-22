from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from db.database import database
from db.merchant_onboarding import get_all_merchant_onboardings
from services.external_seed_audit import (
    audit_external_seed_row,
    get_last_extracted_at,
    get_snapshot,
)
from services.outbound_links_service import (
    DEFAULT_UTM_TEMPLATE,
    apply_utm,
    get_allowed_domains_for_market,
    is_destination_domain_allowed,
)


logger = logging.getLogger(__name__)

EXTERNAL_REFERRAL_GATING_POLICY_VERSION = "external_referral_v1"
EXTERNAL_REFERRAL_STALE_DAYS = 7
EXTERNAL_REFERRAL_BLOCKER_ANOMALIES = {
    "locale_market_mismatch",
    "non_product_fallback_page",
    "price_currency_mismatch",
    "zero_variants",
    "stale_snapshot",
    "redirect_unavailable",
    "destination_domain_not_allowed",
}
EXTERNAL_REFERRAL_REVIEW_ANOMALIES = {
    "zero_images",
    "generic_template_description",
    "manual_image_override_present",
    "gift_card_duplicate_sku",
}

_REFERRAL_METRICS_LOCK = Lock()
_REFERRAL_METRICS: Dict[str, Any] = {
    "referral_seeds_evaluated": 0,
    "referral_seeds_blocked_total": 0,
    "referral_seeds_blocked_by_reason": {},
    "referral_runtime_filtered_total": 0,
    "referral_redirect_generation_failures": 0,
    "referral_domain_allowlist_failures": 0,
    "referral_stale_snapshot_total": 0,
}


@dataclass(frozen=True)
class ExternalReferralIssueBucket:
    issue_type: str
    severity: str
    count: int
    operator_action: str
    sample_seed_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExternalReferralStatus:
    seed_id: str
    status: str
    gating_policy_version: str
    matched_via: str
    blocker_anomaly_types: List[str] = field(default_factory=list)
    review_anomaly_types: List[str] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    last_extracted_at: Optional[str] = None
    tracked_destination_url: Optional[str] = None


@dataclass(frozen=True)
class AttachedFallbackSeedSummary:
    merchant_id: str
    status: str
    gating_policy_version: str
    matched_domains: List[str]
    total_active_seeds: int
    attached_seed_count: int
    domain_unattached_seed_count: int
    healthy_seed_count: int
    blocked_seed_count: int
    review_seed_count: int
    issue_buckets: List[Dict[str, Any]]
    sample_blocked_seeds: List[Dict[str, Any]]
    last_extracted_at_oldest: Optional[str]
    last_extracted_at_newest: Optional[str]


@dataclass(frozen=True)
class PlatformFallbackIssueBucket:
    issue_type: str
    severity: str
    count: int
    operator_action: str
    sample_seed_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlatformFallbackDomainSummary:
    domain: str
    count: int


@dataclass(frozen=True)
class PlatformFallbackRuntimeSurfaceCoverageSummary:
    total_surface_eligible_seeds: int
    total_surface_blocked_seeds: int
    surface_eligible_rate_pct: float
    attached_surface_eligible_seed_count: int
    attached_surface_blocked_seed_count: int


@dataclass(frozen=True)
class PlatformFallbackProgramSummary:
    status: str
    generated_at: str
    gating_policy_version: str
    total_active_seeds: int
    healthy_seed_count: int
    blocked_seed_count: int
    review_seed_count: int
    attached_seed_count: int
    unattached_seed_count: int
    issue_buckets: List[Dict[str, Any]]
    top_domains: List[Dict[str, Any]]
    top_blocked_domains: List[Dict[str, Any]]
    runtime_surface_coverage_summary: Dict[str, Any]


@dataclass(frozen=True)
class MerchantCommerceCohortMerchant:
    merchant_id: str
    business_name: str
    store_count: int
    catalog_product_count: int
    psp_connected: bool
    psp_providers: List[str]
    invalid_reasons: List[str]
    operator_action: str


@dataclass(frozen=True)
class MerchantCommerceCohortSummary:
    generated_at: str
    total_registered_merchants: int
    store_connected_merchants: int
    store_connected_with_psp_merchants: int
    merchant_valid_count: int
    merchant_invalid_count: int
    top_invalid_merchants: List[Dict[str, Any]]


@dataclass(frozen=True)
class MerchantCommerceReadinessListItem:
    merchant_id: str
    business_name: str
    status: str
    merchant_valid: bool
    rollout_ready: bool
    store_count: int
    connected_store_domain_count: int
    catalog_product_count: int
    psp_connected: bool
    psp_provider_count: int
    psp_providers: List[str]
    paid_orders_last_30_days: int
    all_time_paid_orders: int
    invalid_reasons: List[str]
    operator_action: str


@dataclass(frozen=True)
class MerchantCommerceReadinessListSummary:
    generated_at: str
    total_registered_merchants: int
    merchant_valid_count: int
    rollout_ready_count: int
    attention_count: int
    merchants: List[Dict[str, Any]]


def _record_metric(name: str, delta: int = 1) -> None:
    with _REFERRAL_METRICS_LOCK:
        _REFERRAL_METRICS[name] = int(_REFERRAL_METRICS.get(name) or 0) + int(delta)


def _record_blocked_reasons(reasons: Iterable[str]) -> None:
    normalized = [str(reason or "").strip() for reason in reasons if str(reason or "").strip()]
    if not normalized:
        return
    with _REFERRAL_METRICS_LOCK:
        _REFERRAL_METRICS["referral_seeds_blocked_total"] = int(
            _REFERRAL_METRICS.get("referral_seeds_blocked_total") or 0
        ) + 1
        bucket = dict(_REFERRAL_METRICS.get("referral_seeds_blocked_by_reason") or {})
        for reason in normalized:
            bucket[reason] = int(bucket.get(reason) or 0) + 1
            if reason == "destination_domain_not_allowed":
                _REFERRAL_METRICS["referral_domain_allowlist_failures"] = int(
                    _REFERRAL_METRICS.get("referral_domain_allowlist_failures") or 0
                ) + 1
            elif reason == "redirect_unavailable":
                _REFERRAL_METRICS["referral_redirect_generation_failures"] = int(
                    _REFERRAL_METRICS.get("referral_redirect_generation_failures") or 0
                ) + 1
            elif reason == "stale_snapshot":
                _REFERRAL_METRICS["referral_stale_snapshot_total"] = int(
                    _REFERRAL_METRICS.get("referral_stale_snapshot_total") or 0
                ) + 1
        _REFERRAL_METRICS["referral_seeds_blocked_by_reason"] = bucket


def get_external_referral_metrics() -> Dict[str, Any]:
    with _REFERRAL_METRICS_LOCK:
        return {
            "referral_seeds_evaluated": int(_REFERRAL_METRICS.get("referral_seeds_evaluated") or 0),
            "referral_seeds_blocked_total": int(_REFERRAL_METRICS.get("referral_seeds_blocked_total") or 0),
            "referral_seeds_blocked_by_reason": dict(_REFERRAL_METRICS.get("referral_seeds_blocked_by_reason") or {}),
            "referral_runtime_filtered_total": int(_REFERRAL_METRICS.get("referral_runtime_filtered_total") or 0),
            "referral_redirect_generation_failures": int(
                _REFERRAL_METRICS.get("referral_redirect_generation_failures") or 0
            ),
            "referral_domain_allowlist_failures": int(
                _REFERRAL_METRICS.get("referral_domain_allowlist_failures") or 0
            ),
            "referral_stale_snapshot_total": int(_REFERRAL_METRICS.get("referral_stale_snapshot_total") or 0),
        }


def normalize_referral_domain(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    if not candidate:
        return ""
    try:
        if "://" in candidate:
            candidate = (urlparse(candidate).hostname or "").strip().lower()
        else:
            candidate = candidate.split("/", 1)[0].strip().lower()
    except Exception:
        return ""
    candidate = candidate.strip(".")
    if candidate.startswith("www."):
        candidate = candidate[4:]
    return candidate


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return dict(row or {})


def _match_seed_domain_to_merchant_domains(seed_domain: Optional[str], merchant_domains: Sequence[str]) -> bool:
    normalized_seed_domain = normalize_referral_domain(seed_domain)
    if not normalized_seed_domain:
        return False
    for merchant_domain in merchant_domains:
        if normalized_seed_domain == merchant_domain or normalized_seed_domain.endswith(f".{merchant_domain}"):
            return True
    return False


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_iso(value: Any) -> Optional[str]:
    parsed = _parse_timestamp(value)
    return parsed.isoformat() if parsed else None


def _product_data_to_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_storefront_handle_from_product_data(product_data: Dict[str, Any]) -> Optional[str]:
    raw = _product_data_to_dict(product_data.get("raw"))
    platform_metadata = _product_data_to_dict(product_data.get("platform_metadata"))
    for candidate in (
        product_data.get("handle"),
        raw.get("handle"),
        platform_metadata.get("handle"),
    ):
        value = str(candidate or "").strip().strip("/")
        if value:
            return value
    return None


def _product_has_storefront_backfill_candidate(product_data: Dict[str, Any]) -> bool:
    handle = _extract_storefront_handle_from_product_data(product_data)
    if not handle:
        return False
    if product_data.get("price") is not None:
        return True
    variants = product_data.get("variants")
    if isinstance(variants, list) and variants:
        return True
    raw = _product_data_to_dict(product_data.get("raw"))
    raw_variants = raw.get("variants")
    if isinstance(raw_variants, list) and raw_variants:
        return True
    return False


def _status_from_counts(*, total_active_seeds: int, blocked_seed_count: int, review_seed_count: int) -> str:
    if total_active_seeds <= 0:
        return "red"
    if blocked_seed_count > 0:
        return "red"
    if review_seed_count > 0:
        return "yellow"
    return "green"


def _fleet_status_from_counts(
    *,
    total_merchants: int,
    merchants_with_attached_referral_seeds: int,
    merchants_with_blocked_referrals: int,
    merchants_with_review_referrals: int,
) -> str:
    if total_merchants <= 0:
        return "red"
    coverage_rate = merchants_with_attached_referral_seeds / float(total_merchants)
    if merchants_with_attached_referral_seeds <= 0 or coverage_rate < 0.5:
        return "red"
    if (
        merchants_with_attached_referral_seeds < total_merchants
        or merchants_with_blocked_referrals > 0
        or merchants_with_review_referrals > 0
    ):
        return "yellow"
    return "green"


async def get_merchant_referral_domains(merchant_id: str) -> List[str]:
    rows = await database.fetch_all(
        """
        SELECT domain
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND domain IS NOT NULL
          AND TRIM(domain) != ''
        ORDER BY connected_at DESC NULLS LAST, store_id ASC
        """,
        {"merchant_id": merchant_id},
    )
    seen: set[str] = set()
    domains: List[str] = []
    for row in rows or []:
        normalized = normalize_referral_domain(_row_to_dict(row).get("domain"))
        if normalized and normalized not in seen:
            seen.add(normalized)
            domains.append(normalized)
    return domains


async def _fetch_catalog_product_counts_by_merchant() -> Dict[str, int]:
    rows = await database.fetch_all(
        """
        SELECT merchant_id, COUNT(DISTINCT (platform || '|' || platform_product_id)) AS product_count
        FROM products_cache
        WHERE platform_product_id IS NOT NULL
          AND (expires_at IS NULL OR expires_at > NOW())
        GROUP BY merchant_id
        """
    )
    counts: Dict[str, int] = {}
    for row in rows or []:
        row_dict = _row_to_dict(row)
        merchant_id = str(row_dict.get("merchant_id") or "").strip()
        if not merchant_id:
            continue
        counts[merchant_id] = int(row_dict.get("product_count") or 0)
    return counts


async def _fetch_store_domains_by_merchant() -> Dict[str, List[str]]:
    rows = await database.fetch_all(
        """
        SELECT merchant_id, domain
        FROM merchant_stores
        WHERE domain IS NOT NULL
          AND TRIM(domain) != ''
        ORDER BY merchant_id ASC, connected_at DESC NULLS LAST, store_id ASC
        """
    )
    domains_by_merchant: Dict[str, List[str]] = {}
    seen_pairs: set[Tuple[str, str]] = set()
    for row in rows or []:
        row_dict = _row_to_dict(row)
        merchant_id = str(row_dict.get("merchant_id") or "").strip()
        domain = normalize_referral_domain(row_dict.get("domain"))
        if not merchant_id or not domain:
            continue
        pair = (merchant_id, domain)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        domains_by_merchant.setdefault(merchant_id, []).append(domain)
    return domains_by_merchant


async def _fetch_active_psp_providers_by_merchant() -> Dict[str, List[str]]:
    rows = await database.fetch_all(
        """
        SELECT merchant_id, provider
        FROM merchant_psps
        WHERE status = 'active'
          AND merchant_id IS NOT NULL
          AND TRIM(merchant_id) != ''
        ORDER BY merchant_id ASC, provider ASC
        """
    )
    providers_by_merchant: Dict[str, List[str]] = {}
    seen_pairs: set[Tuple[str, str]] = set()
    for row in rows or []:
        row_dict = _row_to_dict(row)
        merchant_id = str(row_dict.get("merchant_id") or "").strip()
        provider = str(row_dict.get("provider") or "").strip().lower()
        if not merchant_id or not provider:
            continue
        pair = (merchant_id, provider)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        providers_by_merchant.setdefault(merchant_id, []).append(provider)
    return providers_by_merchant


async def _fetch_paid_order_evidence_by_merchant() -> Dict[str, Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT
            merchant_id,
            SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days'
                     AND payment_status IN ('paid','completed','succeeded','success','settled','partially_refunded')
                     THEN 1 ELSE 0 END) AS paid_orders_last_30_days,
            SUM(CASE WHEN payment_status IN ('paid','completed','succeeded','success','settled','partially_refunded')
                     THEN 1 ELSE 0 END) AS paid_orders_all_time
        FROM orders
        WHERE merchant_id IS NOT NULL
          AND TRIM(merchant_id) != ''
          AND (is_deleted IS NULL OR is_deleted = FALSE)
        GROUP BY merchant_id
        """
    )
    evidence_by_merchant: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        row_dict = _row_to_dict(row)
        merchant_id = str(row_dict.get("merchant_id") or "").strip()
        if not merchant_id:
            continue
        evidence_by_merchant[merchant_id] = {
            "paid_orders_last_30_days": int(row_dict.get("paid_orders_last_30_days") or 0),
            "paid_orders_all_time": int(row_dict.get("paid_orders_all_time") or 0),
        }
    return evidence_by_merchant


async def _fetch_all_active_referral_seed_rows() -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT *
        FROM external_product_seeds
        WHERE status = 'active'
        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        """
    )
    return [_row_to_dict(row) for row in rows or []]


async def estimate_storefront_backfill_candidate_count(
    merchant_id: str,
    *,
    limit: int = 50,
) -> int:
    row_limit = max(int(limit or 50) * 8, 200)
    rows = await database.fetch_all(
        """
        SELECT product_data
        FROM (
          SELECT DISTINCT ON (platform, platform_product_id)
            platform,
            platform_product_id,
            product_data,
            cached_at,
            id
          FROM products_cache
          WHERE merchant_id = :merchant_id
            AND platform = 'shopify'
          ORDER BY platform, platform_product_id, cached_at DESC NULLS LAST, id DESC NULLS LAST
        ) latest
        LIMIT :limit
        """,
        {"merchant_id": merchant_id, "limit": row_limit},
    )
    candidate_count = 0
    for row in rows or []:
        product_data = _product_data_to_dict(_row_to_dict(row).get("product_data"))
        if not _product_has_storefront_backfill_candidate(product_data):
            continue
        candidate_count += 1
        if candidate_count >= int(limit or 50):
            break
    return candidate_count


def _build_domain_filter_sql(domains: Sequence[str], *, param_prefix: str) -> Tuple[str, Dict[str, Any]]:
    clauses: List[str] = []
    values: Dict[str, Any] = {}
    for idx, domain in enumerate(domains):
        exact_key = f"{param_prefix}_exact_{idx}"
        sub_key = f"{param_prefix}_sub_{idx}"
        values[exact_key] = domain
        values[sub_key] = f"%.{domain}"
        clauses.append(
            "("
            f"LOWER(COALESCE(domain, '')) = :{exact_key}"
            f" OR LOWER(COALESCE(domain, '')) LIKE :{sub_key}"
            ")"
        )
    if not clauses:
        return "FALSE", values
    return "(" + " OR ".join(clauses) + ")", values


async def fetch_merchant_referral_inventory(
    *,
    merchant_id: str,
    status: str = "active",
) -> Dict[str, Any]:
    matched_domains = await get_merchant_referral_domains(merchant_id)
    attached_rows = await database.fetch_all(
        """
        SELECT *
        FROM external_product_seeds
        WHERE status = :status
          AND attached_product_key LIKE :attached_prefix
        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        """,
        {
            "status": status,
            "attached_prefix": f"{merchant_id}|%",
        },
    )

    unattached_rows: List[Dict[str, Any]] = []
    if matched_domains:
        domain_clause, domain_values = _build_domain_filter_sql(matched_domains, param_prefix="merchant_domain")
        rows = await database.fetch_all(
            f"""
            SELECT *
            FROM external_product_seeds
            WHERE status = :status
              AND attached_product_key IS NULL
              AND {domain_clause}
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            """,
            {
                "status": status,
                **domain_values,
            },
        )
        unattached_rows = [_row_to_dict(row) for row in rows or []]

    attached = [_row_to_dict(row) for row in attached_rows or []]
    seen_seed_ids: set[str] = set()
    deduped_rows: List[Dict[str, Any]] = []
    matched_via_by_seed: Dict[str, str] = {}
    for row in attached:
        seed_id = str(row.get("id") or "").strip()
        if not seed_id or seed_id in seen_seed_ids:
            continue
        seen_seed_ids.add(seed_id)
        matched_via_by_seed[seed_id] = "attached_product_key"
        deduped_rows.append(row)
    for row in unattached_rows:
        seed_id = str(row.get("id") or "").strip()
        if not seed_id or seed_id in seen_seed_ids:
            continue
        if not _match_seed_domain_to_merchant_domains(row.get("domain"), matched_domains):
            continue
        seen_seed_ids.add(seed_id)
        matched_via_by_seed[seed_id] = "merchant_domain"
        deduped_rows.append(row)

    return {
        "merchant_id": merchant_id,
        "matched_domains": matched_domains,
        "attached_rows": attached,
        "domain_unattached_rows": [
            row for row in deduped_rows if matched_via_by_seed.get(str(row.get("id") or "").strip()) == "merchant_domain"
        ],
        "rows": deduped_rows,
        "matched_via_by_seed": matched_via_by_seed,
    }


def _seed_matches_q(row: Dict[str, Any], q: Optional[str]) -> bool:
    normalized_q = str(q or "").strip().lower()
    if not normalized_q:
        return True
    haystacks = [
        str(row.get("id") or ""),
        str(row.get("external_product_id") or ""),
        str(row.get("domain") or ""),
        str(row.get("title") or ""),
        str(row.get("canonical_url") or ""),
        str(row.get("destination_url") or ""),
        str((row.get("seed_data") or {}).get("external_product_id") or ""),
        str((row.get("seed_data") or {}).get("product_id") or ""),
    ]
    return any(normalized_q in value.lower() for value in haystacks if value)


def filter_referral_inventory_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    q: Optional[str] = None,
    attached: Optional[bool] = None,
    domain: Optional[str] = None,
    market: Optional[str] = None,
    seed_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    normalized_domain = normalize_referral_domain(domain)
    normalized_market = str(market or "").strip().upper()
    normalized_seed_id = str(seed_id or "").strip()
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        if attached is True and not str(row.get("attached_product_key") or "").strip():
            continue
        if attached is False and str(row.get("attached_product_key") or "").strip():
            continue
        if normalized_domain and not _match_seed_domain_to_merchant_domains(row.get("domain"), [normalized_domain]):
            continue
        if normalized_market and str(row.get("market") or "").strip().upper() != normalized_market:
            continue
        if normalized_seed_id and str(row.get("id") or "").strip() != normalized_seed_id:
            continue
        if not _seed_matches_q(row, q):
            continue
        filtered.append(row)
        if limit is not None and len(filtered) >= int(limit):
            break
    return filtered


async def evaluate_external_referral_seed(
    row: Dict[str, Any],
    *,
    market: Optional[str] = None,
    tool: Optional[str] = None,
    matched_via: str = "unknown",
    allowed_domains: Optional[List[str]] = None,
) -> ExternalReferralStatus:
    normalized_row = _row_to_dict(row)
    _record_metric("referral_seeds_evaluated")
    audit_result = audit_external_seed_row(normalized_row)
    findings = [dict(finding) for finding in (audit_result.get("findings") or []) if isinstance(finding, dict)]
    payload = get_snapshot(normalized_row)
    seed_data = payload["seed_data"]
    snapshot = payload["snapshot"]
    seed_market = str(market or normalized_row.get("market") or "US").strip().upper() or "US"
    seed_tool = str(tool or normalized_row.get("tool") or "*").strip() or "*"
    utm_template = (
        normalized_row.get("utm_template")
        or seed_data.get("utm_template")
        or DEFAULT_UTM_TEMPLATE
    )
    destination_url = (
        str(normalized_row.get("canonical_url") or "").strip()
        or str(normalized_row.get("destination_url") or "").strip()
        or str(snapshot.get("canonical_url") or "").strip()
        or str(snapshot.get("destination_url") or "").strip()
        or str(seed_data.get("canonical_url") or "").strip()
        or str(seed_data.get("destination_url") or "").strip()
    )
    tracked_destination_url: Optional[str] = None

    if not destination_url.startswith(("http://", "https://")):
        findings.append(
            {
                "anomaly_type": "redirect_unavailable",
                "severity": "blocker",
                "recommended_action": "Patch destination_url or canonical_url before allowing referral runtime.",
                "auto_fixable": False,
                "evidence": {"destination_url": destination_url or None},
            }
        )
    else:
        try:
            tracked_destination_url = apply_utm(
                destination_url,
                utm_template,
                {"market": seed_market, "tool": seed_tool},
            )
        except Exception:
            tracked_destination_url = None
            findings.append(
                {
                    "anomaly_type": "redirect_unavailable",
                    "severity": "blocker",
                    "recommended_action": "Patch destination_url or utm_template before allowing referral runtime.",
                    "auto_fixable": False,
                    "evidence": {"destination_url": destination_url},
                }
            )

    if tracked_destination_url:
        runtime_allowed_domains = allowed_domains if allowed_domains is not None else await get_allowed_domains_for_market(market=seed_market)
        if not is_destination_domain_allowed(
            destination_url=tracked_destination_url,
            allowed_domains=runtime_allowed_domains,
        ):
            findings.append(
                {
                    "anomaly_type": "destination_domain_not_allowed",
                    "severity": "blocker",
                    "recommended_action": "Add the destination domain to the outbound allowlist or patch the referral destination.",
                    "auto_fixable": False,
                    "evidence": {
                        "destination_url": tracked_destination_url,
                        "allowed_domains": list(runtime_allowed_domains[:20]),
                    },
                }
            )

    last_extracted_at = get_last_extracted_at(normalized_row, snapshot)
    extracted_dt = _parse_timestamp(last_extracted_at)
    if extracted_dt is not None and extracted_dt < (datetime.now(timezone.utc) - timedelta(days=EXTERNAL_REFERRAL_STALE_DAYS)):
        findings.append(
            {
                "anomaly_type": "stale_snapshot",
                "severity": "blocker",
                "recommended_action": f"Refresh the seed snapshot to keep referral data fresher than {EXTERNAL_REFERRAL_STALE_DAYS} days.",
                "auto_fixable": True,
                "evidence": {
                    "last_extracted_at": _to_iso(extracted_dt or last_extracted_at),
                    "threshold_days": EXTERNAL_REFERRAL_STALE_DAYS,
                },
            }
        )

    blocker_anomaly_types = sorted(
        {
            str(finding.get("anomaly_type") or "").strip()
            for finding in findings
            if str(finding.get("severity") or "").strip().lower() == "blocker"
            or str(finding.get("anomaly_type") or "").strip() in EXTERNAL_REFERRAL_BLOCKER_ANOMALIES
        }
        & EXTERNAL_REFERRAL_BLOCKER_ANOMALIES
    )
    review_anomaly_types = sorted(
        {
            str(finding.get("anomaly_type") or "").strip()
            for finding in findings
            if (
                str(finding.get("severity") or "").strip().lower() in {"review", "info"}
                or str(finding.get("anomaly_type") or "").strip() in EXTERNAL_REFERRAL_REVIEW_ANOMALIES
            )
        }
        | ({str(finding.get("anomaly_type") or "").strip() for finding in findings} & EXTERNAL_REFERRAL_REVIEW_ANOMALIES)
    )
    status = "blocked" if blocker_anomaly_types else "review" if review_anomaly_types else "healthy"
    if blocker_anomaly_types:
        _record_blocked_reasons(blocker_anomaly_types)
    return ExternalReferralStatus(
        seed_id=str(normalized_row.get("id") or "").strip(),
        status=status,
        gating_policy_version=EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
        matched_via=matched_via,
        blocker_anomaly_types=blocker_anomaly_types,
        review_anomaly_types=review_anomaly_types,
        findings=findings,
        last_extracted_at=_to_iso(extracted_dt or last_extracted_at),
        tracked_destination_url=tracked_destination_url,
    )


async def build_external_referral_summary(merchant_id: str) -> Dict[str, Any]:
    inventory = await fetch_merchant_referral_inventory(merchant_id=merchant_id, status="active")
    rows = inventory["rows"]
    matched_via_by_seed = inventory["matched_via_by_seed"]
    statuses = await asyncio.gather(
        *[
            evaluate_external_referral_seed(
                row,
                matched_via=matched_via_by_seed.get(str(row.get("id") or "").strip(), "unknown"),
            )
            for row in rows
        ]
    )

    issue_counter: Counter[Tuple[str, str]] = Counter()
    issue_seed_samples: Dict[Tuple[str, str], List[str]] = {}
    blocked_samples: List[Dict[str, Any]] = []
    healthy_seed_count = 0
    blocked_seed_count = 0
    review_seed_count = 0
    extracted_timestamps: List[datetime] = []

    for row, status in zip(rows, statuses):
        seed_id = str(row.get("id") or "").strip()
        if status.status == "healthy":
            healthy_seed_count += 1
        elif status.status == "blocked":
            blocked_seed_count += 1
            blocked_samples.append(
                {
                    "seed_id": seed_id,
                    "title": row.get("title") or (row.get("seed_data") or {}).get("title"),
                    "domain": row.get("domain"),
                    "attached_product_key": row.get("attached_product_key"),
                    "matched_via": status.matched_via,
                    "blocker_anomaly_types": list(status.blocker_anomaly_types),
                    "last_extracted_at": status.last_extracted_at,
                }
            )
        else:
            review_seed_count += 1

        parsed_dt = _parse_timestamp(status.last_extracted_at)
        if parsed_dt:
            extracted_timestamps.append(parsed_dt)

        for issue_type in status.blocker_anomaly_types:
            key = ("blocker", issue_type)
            issue_counter[key] += 1
            issue_seed_samples.setdefault(key, [])
            if seed_id and seed_id not in issue_seed_samples[key] and len(issue_seed_samples[key]) < 5:
                issue_seed_samples[key].append(seed_id)
        for issue_type in status.review_anomaly_types:
            key = ("review", issue_type)
            issue_counter[key] += 1
            issue_seed_samples.setdefault(key, [])
            if seed_id and seed_id not in issue_seed_samples[key] and len(issue_seed_samples[key]) < 5:
                issue_seed_samples[key].append(seed_id)

    issue_buckets: List[Dict[str, Any]] = []
    for (severity, issue_type), count in sorted(
        issue_counter.items(),
        key=lambda item: (
            0 if item[0][0] == "blocker" else 1,
            -item[1],
            item[0][1],
        ),
    ):
        issue_buckets.append(
            asdict(
                ExternalReferralIssueBucket(
                    issue_type=issue_type,
                    severity=severity,
                    count=count,
                    operator_action=(
                        "Open the audit queue and remediate blocker seeds before referral runtime."
                        if severity == "blocker"
                        else "Open the audit queue and review/refine affected seed content."
                    ),
                    sample_seed_ids=list(issue_seed_samples.get((severity, issue_type), [])),
                )
            )
        )

    extracted_timestamps.sort()
    summary = AttachedFallbackSeedSummary(
        merchant_id=merchant_id,
        status=_status_from_counts(
            total_active_seeds=len(rows),
            blocked_seed_count=blocked_seed_count,
            review_seed_count=review_seed_count,
        ),
        gating_policy_version=EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
        matched_domains=list(inventory["matched_domains"]),
        total_active_seeds=len(rows),
        attached_seed_count=len(inventory["attached_rows"]),
        domain_unattached_seed_count=len(inventory["domain_unattached_rows"]),
        healthy_seed_count=healthy_seed_count,
        blocked_seed_count=blocked_seed_count,
        review_seed_count=review_seed_count,
        issue_buckets=issue_buckets,
        sample_blocked_seeds=blocked_samples[:5],
        last_extracted_at_oldest=extracted_timestamps[0].isoformat() if extracted_timestamps else None,
        last_extracted_at_newest=extracted_timestamps[-1].isoformat() if extracted_timestamps else None,
    )
    return asdict(summary)


def _merchant_invalid_action_and_reasons(
    *,
    has_store_domain: bool,
    has_catalog_products: bool,
    has_psp: bool,
) -> Tuple[List[str], str]:
    reasons: List[str] = []
    if not has_store_domain:
        reasons.append("missing_store_domain")
    if not has_catalog_products:
        reasons.append("missing_catalog_sync")
    if not has_psp:
        reasons.append("missing_psp_or_checkout")

    if "missing_store_domain" in reasons:
        action = "Connect or repair merchant store metadata before treating this merchant as commerce-valid."
    elif "missing_catalog_sync" in reasons:
        action = "Sync active merchant catalog into products_cache before rollout."
    elif "missing_psp_or_checkout" in reasons:
        action = "Connect a live PSP or internal checkout path before rollout."
    else:
        action = "Merchant already satisfies the commerce-valid baseline."
    return reasons, action


def _merchant_invalid_sort_key(row: Dict[str, Any]) -> Tuple[int, int, str]:
    reasons = list(row.get("invalid_reasons") or [])
    if reasons == ["missing_psp_or_checkout"]:
        priority = 0
    elif "missing_catalog_sync" in reasons and "missing_store_domain" not in reasons:
        priority = 1
    elif "missing_store_domain" in reasons:
        priority = 2
    else:
        priority = 3
    return (
        priority,
        -int(row.get("catalog_product_count") or 0),
        str(row.get("business_name") or row.get("merchant_id") or ""),
    )


async def build_platform_fallback_program_summary() -> Dict[str, Any]:
    rows = await _fetch_all_active_referral_seed_rows()
    if not rows:
        return asdict(
            PlatformFallbackProgramSummary(
                status="red",
                generated_at=datetime.now(timezone.utc).isoformat(),
                gating_policy_version=EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
                total_active_seeds=0,
                healthy_seed_count=0,
                blocked_seed_count=0,
                review_seed_count=0,
                attached_seed_count=0,
                unattached_seed_count=0,
                issue_buckets=[],
                top_domains=[],
                top_blocked_domains=[],
                runtime_surface_coverage_summary=asdict(
                    PlatformFallbackRuntimeSurfaceCoverageSummary(
                        total_surface_eligible_seeds=0,
                        total_surface_blocked_seeds=0,
                        surface_eligible_rate_pct=0.0,
                        attached_surface_eligible_seed_count=0,
                        attached_surface_blocked_seed_count=0,
                    )
                ),
            )
        )

    statuses = await asyncio.gather(
        *[
            evaluate_external_referral_seed(
                row,
                matched_via="program_summary",
            )
            for row in rows
        ]
    )

    issue_counter: Counter[Tuple[str, str]] = Counter()
    issue_seed_samples: Dict[Tuple[str, str], List[str]] = {}
    domain_counter: Counter[str] = Counter()
    blocked_domain_counter: Counter[str] = Counter()
    healthy_seed_count = 0
    blocked_seed_count = 0
    review_seed_count = 0
    attached_seed_count = 0
    unattached_seed_count = 0
    attached_surface_eligible_seed_count = 0
    attached_surface_blocked_seed_count = 0

    for row, status in zip(rows, statuses):
        seed_id = str(row.get("id") or "").strip()
        domain = normalize_referral_domain(row.get("domain"))
        if domain:
            domain_counter[domain] += 1
        is_attached = bool(str(row.get("attached_product_key") or "").strip())
        if is_attached:
            attached_seed_count += 1
        else:
            unattached_seed_count += 1

        if status.status == "healthy":
            healthy_seed_count += 1
            if is_attached:
                attached_surface_eligible_seed_count += 1
        elif status.status == "blocked":
            blocked_seed_count += 1
            if domain:
                blocked_domain_counter[domain] += 1
            if is_attached:
                attached_surface_blocked_seed_count += 1
        else:
            review_seed_count += 1
            if is_attached:
                attached_surface_eligible_seed_count += 1

        for issue_type in status.blocker_anomaly_types:
            key = ("blocker", issue_type)
            issue_counter[key] += 1
            issue_seed_samples.setdefault(key, [])
            if seed_id and seed_id not in issue_seed_samples[key] and len(issue_seed_samples[key]) < 5:
                issue_seed_samples[key].append(seed_id)
        for issue_type in status.review_anomaly_types:
            key = ("review", issue_type)
            issue_counter[key] += 1
            issue_seed_samples.setdefault(key, [])
            if seed_id and seed_id not in issue_seed_samples[key] and len(issue_seed_samples[key]) < 5:
                issue_seed_samples[key].append(seed_id)

    issue_buckets: List[Dict[str, Any]] = []
    for (severity, issue_type), count in sorted(
        issue_counter.items(),
        key=lambda item: (
            0 if item[0][0] == "blocker" else 1,
            -item[1],
            item[0][1],
        ),
    ):
        issue_buckets.append(
            asdict(
                PlatformFallbackIssueBucket(
                    issue_type=issue_type,
                    severity=severity,
                    count=count,
                    operator_action=(
                        "Open the external-seed audit queue and remediate blocker seeds before runtime surfacing."
                        if severity == "blocker"
                        else "Review or refine fallback seed content quality without treating it as a merchant-readiness failure."
                    ),
                    sample_seed_ids=list(issue_seed_samples.get((severity, issue_type), [])),
                )
            )
        )

    top_domains = [
        asdict(PlatformFallbackDomainSummary(domain=domain, count=count))
        for domain, count in sorted(domain_counter.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]
    top_blocked_domains = [
        asdict(PlatformFallbackDomainSummary(domain=domain, count=count))
        for domain, count in sorted(blocked_domain_counter.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]
    total_active_seeds = len(rows)
    total_surface_eligible_seeds = healthy_seed_count + review_seed_count

    return asdict(
        PlatformFallbackProgramSummary(
            status=_status_from_counts(
                total_active_seeds=total_active_seeds,
                blocked_seed_count=blocked_seed_count,
                review_seed_count=review_seed_count,
            ),
            generated_at=datetime.now(timezone.utc).isoformat(),
            gating_policy_version=EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
            total_active_seeds=total_active_seeds,
            healthy_seed_count=healthy_seed_count,
            blocked_seed_count=blocked_seed_count,
            review_seed_count=review_seed_count,
            attached_seed_count=attached_seed_count,
            unattached_seed_count=unattached_seed_count,
            issue_buckets=issue_buckets,
            top_domains=top_domains,
            top_blocked_domains=top_blocked_domains,
            runtime_surface_coverage_summary=asdict(
                PlatformFallbackRuntimeSurfaceCoverageSummary(
                    total_surface_eligible_seeds=total_surface_eligible_seeds,
                    total_surface_blocked_seeds=blocked_seed_count,
                    surface_eligible_rate_pct=(
                        round((total_surface_eligible_seeds / total_active_seeds) * 100, 1)
                        if total_active_seeds > 0
                        else 0.0
                    ),
                    attached_surface_eligible_seed_count=attached_surface_eligible_seed_count,
                    attached_surface_blocked_seed_count=attached_surface_blocked_seed_count,
                )
            ),
        )
    )


async def build_merchant_commerce_cohort_summary() -> Dict[str, Any]:
    merchants = await get_all_merchant_onboardings(include_deleted=False)
    if not merchants:
        return asdict(
            MerchantCommerceCohortSummary(
                generated_at=datetime.now(timezone.utc).isoformat(),
                total_registered_merchants=0,
                store_connected_merchants=0,
                store_connected_with_psp_merchants=0,
                merchant_valid_count=0,
                merchant_invalid_count=0,
                top_invalid_merchants=[],
            )
        )

    product_counts_by_merchant, domains_by_merchant, psp_providers_by_merchant = await asyncio.gather(
        _fetch_catalog_product_counts_by_merchant(),
        _fetch_store_domains_by_merchant(),
        _fetch_active_psp_providers_by_merchant(),
    )

    total_registered_merchants = 0
    store_connected_merchants = 0
    store_connected_with_psp_merchants = 0
    merchant_valid_count = 0
    invalid_rows: List[Dict[str, Any]] = []

    for merchant in merchants:
        merchant_id = str(merchant.get("merchant_id") or "").strip()
        if not merchant_id:
            continue
        total_registered_merchants += 1
        store_domains = list(domains_by_merchant.get(merchant_id) or [])
        store_count = len(store_domains)
        catalog_product_count = int(product_counts_by_merchant.get(merchant_id) or 0)
        psp_providers = list(psp_providers_by_merchant.get(merchant_id) or [])
        has_store_domain = store_count > 0
        has_catalog_products = catalog_product_count > 0
        has_psp = bool(psp_providers)

        if has_store_domain:
            store_connected_merchants += 1
        if has_store_domain and has_psp:
            store_connected_with_psp_merchants += 1

        invalid_reasons, operator_action = _merchant_invalid_action_and_reasons(
            has_store_domain=has_store_domain,
            has_catalog_products=has_catalog_products,
            has_psp=has_psp,
        )
        if not invalid_reasons:
            merchant_valid_count += 1
            continue

        invalid_rows.append(
            asdict(
                MerchantCommerceCohortMerchant(
                    merchant_id=merchant_id,
                    business_name=str(merchant.get("business_name") or merchant_id),
                    store_count=store_count,
                    catalog_product_count=catalog_product_count,
                    psp_connected=has_psp,
                    psp_providers=sorted(psp_providers),
                    invalid_reasons=invalid_reasons,
                    operator_action=operator_action,
                )
            )
        )

    return asdict(
        MerchantCommerceCohortSummary(
            generated_at=datetime.now(timezone.utc).isoformat(),
            total_registered_merchants=total_registered_merchants,
            store_connected_merchants=store_connected_merchants,
            store_connected_with_psp_merchants=store_connected_with_psp_merchants,
            merchant_valid_count=merchant_valid_count,
            merchant_invalid_count=max(0, total_registered_merchants - merchant_valid_count),
            top_invalid_merchants=sorted(invalid_rows, key=_merchant_invalid_sort_key)[:8],
        )
    )


def _merchant_commerce_status(*, merchant_valid: bool, order_payment_loop_observed: bool) -> str:
    if merchant_valid and order_payment_loop_observed:
        return "green"
    if merchant_valid:
        return "yellow"
    return "red"


def _merchant_commerce_list_sort_key(row: Dict[str, Any]) -> Tuple[int, str]:
    status = str(row.get("status") or "").lower()
    if status == "red":
        priority = 0
    elif status == "yellow":
        priority = 1
    else:
        priority = 2
    return (
        priority,
        str(row.get("business_name") or row.get("merchant_id") or "").lower(),
    )


async def build_merchant_commerce_readiness_list() -> Dict[str, Any]:
    merchants = await get_all_merchant_onboardings(include_deleted=False)
    if not merchants:
        return asdict(
            MerchantCommerceReadinessListSummary(
                generated_at=datetime.now(timezone.utc).isoformat(),
                total_registered_merchants=0,
                merchant_valid_count=0,
                rollout_ready_count=0,
                attention_count=0,
                merchants=[],
            )
        )

    product_counts_by_merchant, domains_by_merchant, psp_providers_by_merchant, paid_order_evidence_by_merchant = await asyncio.gather(
        _fetch_catalog_product_counts_by_merchant(),
        _fetch_store_domains_by_merchant(),
        _fetch_active_psp_providers_by_merchant(),
        _fetch_paid_order_evidence_by_merchant(),
    )

    rows: List[Dict[str, Any]] = []
    merchant_valid_count = 0
    rollout_ready_count = 0

    for merchant in merchants:
        merchant_id = str(merchant.get("merchant_id") or "").strip()
        if not merchant_id:
            continue

        store_domains = list(domains_by_merchant.get(merchant_id) or [])
        store_count = len(store_domains)
        catalog_product_count = int(product_counts_by_merchant.get(merchant_id) or 0)
        psp_providers = list(psp_providers_by_merchant.get(merchant_id) or [])
        evidence = dict(paid_order_evidence_by_merchant.get(merchant_id) or {})
        paid_orders_last_30_days = int(evidence.get("paid_orders_last_30_days") or 0)
        all_time_paid_orders = int(evidence.get("paid_orders_all_time") or 0)

        has_store_domain = store_count > 0
        has_catalog_products = catalog_product_count > 0
        has_psp = bool(psp_providers)
        order_payment_loop_observed = all_time_paid_orders > 0
        merchant_valid = has_store_domain and has_catalog_products and has_psp
        status = _merchant_commerce_status(
            merchant_valid=merchant_valid,
            order_payment_loop_observed=order_payment_loop_observed,
        )

        invalid_reasons, operator_action = _merchant_invalid_action_and_reasons(
            has_store_domain=has_store_domain,
            has_catalog_products=has_catalog_products,
            has_psp=has_psp,
        )
        if merchant_valid and not order_payment_loop_observed:
            operator_action = "Run a real order and verify paid order evidence before rollout."

        if merchant_valid:
            merchant_valid_count += 1
        if merchant_valid and order_payment_loop_observed:
            rollout_ready_count += 1

        rows.append(
            asdict(
                MerchantCommerceReadinessListItem(
                    merchant_id=merchant_id,
                    business_name=str(merchant.get("business_name") or merchant_id),
                    status=status,
                    merchant_valid=merchant_valid,
                    rollout_ready=merchant_valid and order_payment_loop_observed,
                    store_count=store_count,
                    connected_store_domain_count=store_count,
                    catalog_product_count=catalog_product_count,
                    psp_connected=has_psp,
                    psp_provider_count=len(psp_providers),
                    psp_providers=sorted(psp_providers),
                    paid_orders_last_30_days=paid_orders_last_30_days,
                    all_time_paid_orders=all_time_paid_orders,
                    invalid_reasons=invalid_reasons,
                    operator_action=operator_action,
                )
            )
        )

    sorted_rows = sorted(rows, key=_merchant_commerce_list_sort_key)
    total_registered_merchants = len(sorted_rows)
    attention_count = max(0, total_registered_merchants - rollout_ready_count)

    return asdict(
        MerchantCommerceReadinessListSummary(
            generated_at=datetime.now(timezone.utc).isoformat(),
            total_registered_merchants=total_registered_merchants,
            merchant_valid_count=merchant_valid_count,
            rollout_ready_count=rollout_ready_count,
            attention_count=attention_count,
            merchants=sorted_rows,
        )
    )


async def build_external_referral_fleet_summary() -> Dict[str, Any]:
    """Deprecated compatibility alias for the old merchant-centric fleet summary."""
    return await build_merchant_commerce_cohort_summary()


async def should_block_external_referral_runtime(
    row: Dict[str, Any],
    *,
    matched_via: str = "runtime",
    allowed_domains: Optional[List[str]] = None,
) -> Tuple[bool, ExternalReferralStatus]:
    status = await evaluate_external_referral_seed(
        row,
        matched_via=matched_via,
        allowed_domains=allowed_domains,
    )
    blocked = status.status == "blocked"
    if blocked:
        _record_metric("referral_runtime_filtered_total")
        logger.info(
            "external referral runtime filtered seed",
            extra={
                "seed_id": status.seed_id,
                "matched_via": matched_via,
                "blockers": list(status.blocker_anomaly_types),
            },
        )
    return blocked, status


async def get_external_referral_refresh_candidate_seed_ids(limit: int = 500) -> List[str]:
    normalized_limit = max(1, min(int(limit or 500), 5000))
    attached_rows = await database.fetch_all(
        """
        SELECT id
        FROM external_product_seeds
        WHERE status = 'active'
          AND attached_product_key IS NOT NULL
        ORDER BY updated_at ASC NULLS FIRST, created_at ASC NULLS FIRST
        LIMIT :limit
        """,
        {"limit": normalized_limit},
    )
    attached_ids = [str(_row_to_dict(row).get("id") or "").strip() for row in attached_rows or []]
    attached_ids = [seed_id for seed_id in attached_ids if seed_id]
    remaining = max(0, normalized_limit - len(attached_ids))
    if remaining <= 0:
        return attached_ids[:normalized_limit]

    merchant_domains_rows = await database.fetch_all(
        """
        SELECT domain
        FROM merchant_stores
        WHERE domain IS NOT NULL
          AND TRIM(domain) != ''
        """,
        {},
    )
    merchant_domains = {
        normalize_referral_domain(_row_to_dict(row).get("domain"))
        for row in merchant_domains_rows or []
    }
    merchant_domains = {domain for domain in merchant_domains if domain}
    if not merchant_domains:
        return attached_ids[:normalized_limit]

    unattached_rows = await database.fetch_all(
        """
        SELECT id, domain
        FROM external_product_seeds
        WHERE status = 'active'
          AND attached_product_key IS NULL
          AND domain IS NOT NULL
          AND TRIM(domain) != ''
        ORDER BY updated_at ASC NULLS FIRST, created_at ASC NULLS FIRST
        LIMIT :limit
        """,
        {"limit": normalized_limit * 4},
    )
    domain_unattached_ids: List[str] = []
    for row in unattached_rows or []:
        row_dict = _row_to_dict(row)
        seed_id = str(row_dict.get("id") or "").strip()
        if not seed_id:
            continue
        if _match_seed_domain_to_merchant_domains(row_dict.get("domain"), list(merchant_domains)):
            domain_unattached_ids.append(seed_id)
        if len(domain_unattached_ids) >= remaining:
            break
    return (attached_ids + domain_unattached_ids)[:normalized_limit]


async def run_external_referral_refresh_batch(
    *,
    refresh_seed_by_id: Callable[[str], Awaitable[Dict[str, Any]]],
    limit: int = 500,
) -> Dict[str, Any]:
    candidate_seed_ids = await get_external_referral_refresh_candidate_seed_ids(limit=limit)
    refreshed = 0
    degraded = 0
    failed = 0
    errors: List[Dict[str, Any]] = []
    # Drift accounting. "refreshed" only says we re-fetched; it says nothing about
    # whether anything was WRONG, which is the number that justifies running this
    # on a schedule at all. `price_changed` is the measured staleness rate of the
    # index, per run. `price_skipped_incomplete_pair` is the honest residue: a
    # fetch that produced an amount without a currency, deliberately not applied
    # (see the pairing note in routes/employee_products._refresh_external_seed_by_id)
    # — if it grows, the extractor needs work, not the writer.
    price_changed = 0
    price_filled = 0
    price_unchanged = 0
    price_unavailable = 0
    price_skipped_incomplete_pair = 0
    # A scraped currency that disagrees with the stored one is refused rather than
    # applied (see routes/employee_products._refresh_external_seed_by_id). This counter
    # is the visible cost of that refusal: if it is non-trivial, the currency EXTRACTOR
    # needs work — `_detect_currency_from_text` has no KRW case and the caller defaults
    # by market — not the writer.
    price_skipped_currency_mismatch = 0
    availability_changed = 0
    for seed_id in candidate_seed_ids:
        try:
            result = await refresh_seed_by_id(seed_id)
            status = str(result.get("status") or "success")
            if status == "success":
                refreshed += 1
                # isinstance, not truthiness: a truthy non-dict here used to raise
                # INSIDE the try and land the same seed in BOTH `refreshed` and
                # `failed`, which is a worse outcome than an uncounted drift report.
                price = result.get("price_refresh")
                price_status = str(price.get("status") or "") if isinstance(price, dict) else ""
                if price_status == "applied":
                    price_changed += 1
                elif price_status == "filled":
                    price_filled += 1
                elif price_status == "unchanged":
                    price_unchanged += 1
                elif price_status == "skipped_incomplete_pair":
                    price_skipped_incomplete_pair += 1
                elif price_status == "skipped_currency_mismatch":
                    price_skipped_currency_mismatch += 1
                elif price_status == "unavailable":
                    price_unavailable += 1
                availability = result.get("availability_refresh")
                if isinstance(availability, dict) and availability.get("status") == "applied":
                    availability_changed += 1
            elif status == "degraded":
                degraded += 1
            else:
                failed += 1
                errors.append({"seed_id": seed_id, "status": status, "error": result.get("error")})
        except Exception as exc:
            failed += 1
            errors.append({"seed_id": seed_id, "status": "failed", "error": str(exc)[:300]})
    return {
        "status": "success" if failed == 0 else "degraded",
        "gating_policy_version": EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
        "candidate_count": len(candidate_seed_ids),
        "refreshed": refreshed,
        "degraded": degraded,
        "failed": failed,
        "price_changed": price_changed,
        "price_unchanged": price_unchanged,
        "price_unavailable": price_unavailable,
        "price_filled": price_filled,
        "price_skipped_incomplete_pair": price_skipped_incomplete_pair,
        "price_skipped_currency_mismatch": price_skipped_currency_mismatch,
        "availability_changed": availability_changed,
        "errors": errors[:20],
    }

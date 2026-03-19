from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from db.database import database
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
class ExternalReferralSummary:
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


def _status_from_counts(*, total_active_seeds: int, blocked_seed_count: int, review_seed_count: int) -> str:
    if total_active_seeds <= 0:
        return "red"
    if blocked_seed_count > 0:
        return "red"
    if review_seed_count > 0:
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
    summary = ExternalReferralSummary(
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
    for seed_id in candidate_seed_ids:
        try:
            result = await refresh_seed_by_id(seed_id)
            status = str(result.get("status") or "success")
            if status == "success":
                refreshed += 1
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
        "errors": errors[:20],
    }

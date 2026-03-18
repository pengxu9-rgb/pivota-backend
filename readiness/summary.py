from __future__ import annotations

from typing import Iterable, Optional

from readiness.flags import (
    readiness_alpha_merchant_id,
    readiness_real_merchant_alpha_enabled,
    readiness_router_enabled,
)
from readiness.models import MerchantReadinessSnapshot, ReadinessSummary
from readiness.service import UnsupportedMerchantError, build_readiness_snapshot


def _dedupe(values: Iterable[str], *, limit: int = 3) -> list[str]:
    seen: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.append(normalized)
        if len(seen) >= limit:
            break
    return seen


def _label_for_tier(tier: str) -> str:
    return {
        "green": "Ready",
        "yellow": "Needs Attention",
        "red": "Blocked",
    }.get(tier, "Blocked")


def _next_action(
    *,
    assessment_state: str,
    blockers: list[str],
    warnings: list[str],
    ready_variant_count: int,
    blocked_variant_count: int,
    capability_status: dict[str, str],
) -> str:
    if assessment_state == "disabled":
        return "Enable readiness assessment before using LLM commerce for this merchant."
    if assessment_state == "not_assessed":
        return "Run readiness assessment and merchant remediation before enabling LLM commerce."
    if "merchant_checkout_capability_missing" in blockers or capability_status.get("checkout_execution") == "blocked":
        return "Connect and verify checkout and payment execution before enabling checkout flows."
    if "merchant_writeback_unavailable" in blockers or capability_status.get("order_writeback_state_sync") == "blocked":
        return "Fix merchant write-back and order sync before allowing autonomous order actions."
    if "merchant_shipping_policy_missing" in blockers or "merchant_return_policy_missing" in blockers:
        return "Publish shipping and returns policy configuration for this merchant."
    if blocked_variant_count > 0:
        return "Resolve blocked variants before enabling checkout broadly."
    if ready_variant_count == 0:
        return "Make at least one variant discovery-ready before enabling LLM commerce."
    if warnings:
        return "Clear the remaining warnings and rerun readiness before broader rollout."
    return "Merchant can be enabled for supervised LLM commerce."


def summarize_readiness_snapshot(
    snapshot: MerchantReadinessSnapshot,
    *,
    channel: str = "ucp",
) -> ReadinessSummary:
    channel_coverage = next(
        (coverage for coverage in snapshot.channel_coverage if coverage.channel == channel),
        None,
    )
    ready_variant_count = int(channel_coverage.ready_variant_count if channel_coverage else 0)
    blocked_variant_count = int(channel_coverage.blocked_variant_count if channel_coverage else 0)
    blockers = _dedupe(snapshot.blockers)
    warnings = _dedupe(snapshot.warnings)

    capability_status = dict(snapshot.capability_status or {})
    checkout_blocked = capability_status.get("checkout_execution") == "blocked"
    order_sync_blocked = capability_status.get("order_writeback_state_sync") == "blocked"
    critical_blockers = {
        "merchant_checkout_capability_missing",
        "merchant_writeback_unavailable",
        "merchant_shipping_policy_missing",
        "merchant_return_policy_missing",
    }

    if (
        snapshot.readiness_score >= 80
        and not checkout_blocked
        and not order_sync_blocked
        and blocked_variant_count == 0
        and not any(blocker in critical_blockers for blocker in snapshot.blockers)
    ):
        tier = "green"
    elif (
        snapshot.readiness_score < 50
        or ready_variant_count == 0
        or checkout_blocked
        or order_sync_blocked
        or any(blocker in critical_blockers for blocker in snapshot.blockers)
    ):
        tier = "red"
    else:
        tier = "yellow"

    return ReadinessSummary(
        tier=tier,
        label=_label_for_tier(tier),
        assessment_state="assessed",
        assessment_scope="one_merchant_alpha",
        channel=channel,
        score=snapshot.readiness_score,
        merchant_alpha_mode=snapshot.merchant_alpha_mode,
        ready_variant_count=ready_variant_count,
        blocked_variant_count=blocked_variant_count,
        top_blockers=blockers,
        top_warnings=warnings,
        capability_status=capability_status,
        generated_at=snapshot.generated_at,
        next_action=_next_action(
            assessment_state="assessed",
            blockers=blockers,
            warnings=warnings,
            ready_variant_count=ready_variant_count,
            blocked_variant_count=blocked_variant_count,
            capability_status=capability_status,
        ),
    )


def _fallback_summary(
    *,
    assessment_state: str,
    blocker: str,
    channel: str,
    merchant_alpha_mode: Optional[str] = None,
) -> ReadinessSummary:
    blockers = [blocker]
    return ReadinessSummary(
        tier="red",
        label=_label_for_tier("red"),
        assessment_state=assessment_state,
        assessment_scope="one_merchant_alpha",
        channel=channel,
        score=None,
        merchant_alpha_mode=merchant_alpha_mode,
        ready_variant_count=0,
        blocked_variant_count=0,
        top_blockers=blockers,
        top_warnings=[],
        capability_status={},
        generated_at=None,
        next_action=_next_action(
            assessment_state=assessment_state,
            blockers=blockers,
            warnings=[],
            ready_variant_count=0,
            blocked_variant_count=0,
            capability_status={},
        ),
    )


async def build_readiness_summary(
    merchant_id: str,
    *,
    channel: str = "ucp",
) -> ReadinessSummary:
    if not readiness_router_enabled():
        return _fallback_summary(
            assessment_state="disabled",
            blocker="readiness_assessment_disabled",
            channel=channel,
        )

    if merchant_id != "synthetic-demo-merchant":
        alpha_merchant_id = readiness_alpha_merchant_id()
        if not readiness_real_merchant_alpha_enabled() or merchant_id != alpha_merchant_id:
            return _fallback_summary(
                assessment_state="not_assessed",
                blocker="merchant_not_assessed_for_readiness_alpha",
                channel=channel,
            )

    try:
        snapshot = await build_readiness_snapshot(merchant_id, channel=channel)
    except UnsupportedMerchantError:
        return _fallback_summary(
            assessment_state="not_assessed",
            blocker="merchant_not_assessed_for_readiness_alpha",
            channel=channel,
        )
    except Exception:
        return _fallback_summary(
            assessment_state="assessed",
            blocker="readiness_summary_unavailable",
            channel=channel,
            merchant_alpha_mode="assessment_error",
        )
    return summarize_readiness_snapshot(snapshot, channel=channel)

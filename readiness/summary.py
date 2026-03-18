from __future__ import annotations

from collections import Counter
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


def _humanize_code(code: str) -> str:
    labels = {
        "merchant_not_assessed_for_readiness_alpha": "Merchant not yet assessed",
        "readiness_assessment_disabled": "Readiness assessment disabled",
        "readiness_summary_unavailable": "Readiness summary unavailable",
        "out_of_stock": "Out of stock",
        "missing_price": "Missing price",
        "missing_currency": "Missing currency",
        "inventory_stale": "Inventory data stale",
        "missing_primary_image": "Missing primary image",
        "missing_title": "Missing product title",
        "missing_description": "Missing description",
        "missing_shipping_profile": "Missing shipping setup",
        "merchant_shipping_policy_missing": "Shipping policy missing",
        "merchant_return_policy_missing": "Returns policy missing",
        "merchant_checkout_capability_missing": "Checkout not connected",
        "merchant_writeback_unavailable": "Order sync unavailable",
        "reviews_summary_unavailable": "Reviews summary unavailable",
        "cross_merchant_review_group_unresolved": "Cross-merchant review grouping incomplete",
        "review_coverage_partial": "Review coverage partial",
    }
    if code in labels:
        return labels[code]
    return str(code or "").replace("_", " ").strip().capitalize()


def _variant_blocker_counts(snapshot: MerchantReadinessSnapshot) -> Counter[str]:
    counts: Counter[str] = Counter()
    for product in snapshot.products:
        for variant in product.variants:
            if variant.channel_coverage.get(snapshot.channel) == "ready":
                continue
            codes = set(variant.blockers.get("discovery", []) + variant.blockers.get("checkout", []))
            for code in codes:
                counts[code] += 1
    return counts


def _recommended_actions(
    *,
    assessment_state: str,
    blocker_counts: Counter[str],
    blockers: list[str],
    warnings: list[str],
    blocked_variant_count: int,
    capability_status: dict[str, str],
) -> list[str]:
    if assessment_state == "disabled":
        return ["Turn on readiness assessment before enabling LLM commerce for this merchant."]
    if assessment_state == "not_assessed":
        return ["Run the merchant readiness assessment and review the remediation checklist."]

    actions: list[str] = []
    if blocker_counts.get("missing_price") or blocker_counts.get("missing_currency"):
        actions.append("Fix pricing sync for variants with missing price or currency.")
    if blocker_counts.get("out_of_stock"):
        actions.append("Restock blocked variants or keep them excluded from AI checkout.")
    if blocker_counts.get("inventory_stale"):
        actions.append("Refresh inventory so checkout decisions use current stock.")
    if (
        "merchant_checkout_capability_missing" in blockers
        or capability_status.get("checkout_execution") == "blocked"
    ):
        actions.append("Connect checkout and payment processing before enabling AI checkout.")
    if (
        "merchant_shipping_policy_missing" in blockers
        or "merchant_return_policy_missing" in blockers
        or blocker_counts.get("missing_shipping_profile")
    ):
        actions.append("Complete shipping and returns setup for blocked products.")
    if blocker_counts.get("missing_primary_image") or blocker_counts.get("missing_title"):
        actions.append("Fill missing catalog fields such as product titles and primary images.")
    if (
        "merchant_writeback_unavailable" in blockers
        or capability_status.get("order_writeback_state_sync") == "blocked"
    ):
        actions.append("Repair order write-back and sync before enabling autonomous order actions.")
    if warnings and not actions:
        actions.append("Clear the remaining warnings and rerun readiness.")
    if blocked_variant_count > 0 and not actions:
        actions.append("Resolve the blocked variants before enabling checkout broadly.")
    if not actions:
        actions.append("This merchant is ready for supervised LLM commerce.")
    return actions[:3]


def _summary_text(
    *,
    assessment_state: str,
    tier: str,
    ready_variant_count: int,
    blocked_variant_count: int,
) -> str:
    if assessment_state == "disabled":
        return "Readiness assessment is currently turned off, so LLM commerce should stay disabled."
    if assessment_state == "not_assessed":
        return "This merchant has not been assessed yet, so LLM commerce should stay off until the first review is complete."
    if tier == "green":
        return f"This merchant is ready for supervised LLM commerce. {ready_variant_count} variants are ready and no variants are currently blocked."
    if tier == "yellow":
        return f"Most of the catalog is usable, but not all of it is safe to expose yet. {ready_variant_count} variants are ready and {blocked_variant_count} variants are still blocked."
    return f"This merchant is currently blocked for LLM commerce. {blocked_variant_count} variants are blocked and the critical setup issues need to be fixed first."


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
    blocker_counts = _variant_blocker_counts(snapshot)

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

    recommended_actions = _recommended_actions(
        assessment_state="assessed",
        blocker_counts=blocker_counts,
        blockers=blockers,
        warnings=warnings,
        blocked_variant_count=blocked_variant_count,
        capability_status=capability_status,
    )

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
        summary_text=_summary_text(
            assessment_state="assessed",
            tier=tier,
            ready_variant_count=ready_variant_count,
            blocked_variant_count=blocked_variant_count,
        ),
        action_text=recommended_actions[0] if recommended_actions else None,
        recommended_actions=recommended_actions,
        blocker_breakdown=[
            {
                "code": code,
                "label": _humanize_code(code),
                "count": count,
            }
            for code, count in blocker_counts.most_common(3)
        ],
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
    recommended_actions = _recommended_actions(
        assessment_state=assessment_state,
        blocker_counts=Counter(),
        blockers=blockers,
        warnings=[],
        blocked_variant_count=0,
        capability_status={},
    )
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
        summary_text=_summary_text(
            assessment_state=assessment_state,
            tier="red",
            ready_variant_count=0,
            blocked_variant_count=0,
        ),
        action_text=recommended_actions[0] if recommended_actions else None,
        recommended_actions=recommended_actions,
        blocker_breakdown=[
            {
                "code": blocker,
                "label": _humanize_code(blocker),
                "count": 0,
            }
        ],
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

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Optional

from db.product_enrichment import get_enrichment
from db.products import get_product_cache_row
from db.readiness_source_data_decisions import (
    delete_source_data_decision,
    list_source_data_decisions,
    upsert_source_data_decision,
)
from models.standard_product import StandardProduct
from readiness.models import (
    ExecutionJob,
    MerchantReadinessOptimizationPayload,
    PatchCandidate,
    ProductBlockerCounts,
    ProductBlockerDetail,
    ProductBlockerSubject,
    ProductBlockerVariant,
    RemediationAction,
    SourceDataTriagePayload,
    SourceDataTriageRow,
    SourceDataTriageSummaryBucket,
    VerificationResult,
)
from readiness.summary import (
    _default_out_of_stock_decision_state,
    get_readiness_optimization_context,
)
from services.product_enrichment_ai import (
    generate_enrichment_draft,
)
from services.product_enrichment_pipeline import run_enrichment_for_product
from services.product_quality_service import build_quality_payload, preview_quality
from utils.availability_vocabulary import is_out_of_stock


class PlanSupersededError(Exception):
    def __init__(self, *, current_plan_id: str, current_snapshot_id: str) -> None:
        self.current_plan_id = current_plan_id
        self.current_snapshot_id = current_snapshot_id
        super().__init__("Optimization plan has been superseded")


class ActionNotFoundError(Exception):
    pass


class ActionNotExecutableError(Exception):
    pass


class JobNotFoundError(Exception):
    pass


_JOB_STORE: dict[str, ExecutionJob] = {}
_SOURCE_DATA_TRIAGE_REASON_DEFS: dict[str, dict[str, str]] = {
    "missing_price": {
        "label": "Missing price or currency",
        "scope": "variant",
    },
    "out_of_stock": {
        "label": "Out of stock",
        "scope": "variant",
    },
    "missing_primary_image": {
        "label": "Missing primary image",
        "scope": "product",
    },
    "shipping_delivery_completeness": {
        "label": "Shipping / delivery completeness",
        "scope": "product",
    },
    "trust_support_policy_completeness": {
        "label": "Trust / support policy completeness",
        "scope": "product",
    },
    "product_fit_composition_completeness": {
        "label": "Product fit / composition completeness",
        "scope": "product",
    },
}
_SOURCE_DATA_DECISION_STATES: dict[str, set[str]] = {
    "out_of_stock": {
        "restock_planned",
        "archive_planned",
        "manual_review",
    },
    "missing_price": {
        "pricing_fix_saved",
    },
    "missing_primary_image": {
        "image_fix_saved",
    },
    "shipping_delivery_completeness": {
        "pending_review",
        "merchant_fix_in_progress",
        "waiting_on_platform_or_policy",
        "not_applicable",
    },
    "trust_support_policy_completeness": {
        "pending_review",
        "merchant_fix_in_progress",
        "waiting_on_platform_or_policy",
        "not_applicable",
    },
    "product_fit_composition_completeness": {
        "pending_review",
        "merchant_fix_in_progress",
        "waiting_on_platform_or_policy",
        "not_applicable",
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _job_id(action_id: str) -> str:
    raw = f"{action_id}|{_now_iso()}"
    return f"rdjob_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def _candidate_id(action_id: str, field_name: str) -> str:
    raw = f"{action_id}|{field_name}"
    return f"rdpatch_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def _action_id_for_target(action_type: str, target: dict[str, Any]) -> str:
    key = "|".join(
        [
            action_type,
            str(target.get("scope") or "product"),
            str(target.get("platform") or ""),
            str(target.get("platform_product_id") or target.get("product_id") or target.get("queue_item_id") or ""),
        ]
    )
    return f"act_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _default_disclaimer(context_text: str) -> str:
    if "鞋" in context_text or "shoe" in context_text.lower():
        return "本产品为运动鞋，不具备医疗功效，具体体验因人而异。"
    return "本产品为日常消费品，不提供任何医疗或金融收益承诺。"


def _generated_enrichment(product: StandardProduct) -> tuple[dict[str, Any], dict[str, Any]]:
    draft, title_suggestion = generate_enrichment_draft(
        product,
        preferred_language="en",
    )
    generated = {
        "title_override": draft.get("title_override"),
        "summary_short": draft.get("summary_short"),
        "bullet_points": draft.get("bullet_points"),
        "usage_scenarios": draft.get("usage_scenarios"),
        "audience_tags": draft.get("audience_tags"),
        "topic_tags": draft.get("topic_tags"),
        "regulatory_disclaimer_local": _default_disclaimer(product.title),
        "extra_images": None,
        "llm_readability_score": draft.get("llm_readability_score"),
        "llm_safety_flags": None,
    }
    return generated, {
        "title_health": title_suggestion.title_health,
        "suggested_title_preview": title_suggestion.suggested_title,
        "suggestion_language": title_suggestion.suggestion_language,
        "suggestion_confidence": title_suggestion.suggestion_confidence,
        "suggestion_rationale": title_suggestion.suggestion_rationale,
        "facts_used": title_suggestion.facts_used,
        "missing_attribute_labels": title_suggestion.missing_attribute_labels,
        "content_gap_codes": title_suggestion.content_gap_codes,
        "skipped_reason": title_suggestion.skipped_reason,
    }


async def _latest_plan_or_raise(
    merchant_id: str,
    *,
    channel: str,
    plan_id: str,
) -> MerchantReadinessOptimizationPayload:
    context = await get_readiness_optimization_context(
        merchant_id,
        channel=channel,
    )
    payload = context[0]
    if payload.plan.plan_id != plan_id:
        raise PlanSupersededError(
            current_plan_id=payload.plan.plan_id,
            current_snapshot_id=payload.plan.snapshot_id,
        )
    return payload


async def _latest_context_or_raise(
    merchant_id: str,
    *,
    channel: str,
    plan_id: str,
) -> tuple[MerchantReadinessOptimizationPayload, Any]:
    context = await get_readiness_optimization_context(
        merchant_id,
        channel=channel,
    )
    payload = context[0]
    snapshot = context[1] if len(context) > 1 else None
    if payload.plan.plan_id != plan_id:
        raise PlanSupersededError(
            current_plan_id=payload.plan.plan_id,
            current_snapshot_id=payload.plan.snapshot_id,
        )
    if snapshot is None:
        raise ActionNotFoundError("Readiness snapshot unavailable for this plan")
    return payload, snapshot


def _build_action_catalog(payload: MerchantReadinessOptimizationPayload) -> dict[str, RemediationAction]:
    actions: dict[str, RemediationAction] = {}

    for action in payload.merchant_actions:
        if not action.action_id:
            continue
        actions[action.action_id] = RemediationAction(
            action_id=action.action_id,
            plan_id=payload.plan.plan_id,
            action_type=action.action_type,
            surface=action.fix_surface,
            scope=action.scope,
            targets=[],
            fixability=action.fixability,
            priority_score=action.priority_score,
            priority_reason=action.priority_reason,
            reason=action.description,
            status="suggested",
        )

    for item in payload.product_queue:
        action_id = item.recommended_action_id or _action_id_for_target(
            item.recommended_action_type or "review_product",
            {
                "scope": item.queue_item_scope,
                "platform": item.platform,
                "platform_product_id": item.platform_product_id or item.product_id,
                "product_id": item.product_id,
                "queue_item_id": item.queue_item_id,
            },
        )
        actions[action_id] = RemediationAction(
            action_id=action_id,
            plan_id=payload.plan.plan_id,
            action_type=item.recommended_action_type or "review_product",
            surface=item.fix_surface,
            scope=item.queue_item_scope,
            targets=[
                {
                    "scope": item.queue_item_scope,
                    "queue_item_id": item.queue_item_id,
                    "product_id": item.product_id,
                    "platform": item.platform,
                    "platform_product_id": item.platform_product_id or item.product_id,
                }
            ],
            fixability=item.fixability,
            priority_score=item.priority_score,
            priority_reason=item.priority_reason,
            reason=item.primary_action,
            status="suggested",
        )
    return actions


def _canonical_target_scope(target: dict[str, Any]) -> str:
    return str(target.get("scope") or "product").strip() or "product"


def _canonical_target_platform(target: dict[str, Any]) -> str:
    return str(target.get("platform") or "").strip().lower()


def _canonical_target_identifiers(target: dict[str, Any]) -> set[str]:
    identifiers = {
        str(target.get("platform_product_id") or "").strip(),
        str(target.get("product_id") or "").strip(),
        str(target.get("queue_item_id") or "").strip(),
    }
    return {identifier for identifier in identifiers if identifier}


def _targets_match(catalog_target: dict[str, Any], request_target: dict[str, Any]) -> bool:
    if _canonical_target_scope(catalog_target) != _canonical_target_scope(request_target):
        return False

    catalog_platform = _canonical_target_platform(catalog_target)
    request_platform = _canonical_target_platform(request_target)
    if catalog_platform and request_platform and catalog_platform != request_platform:
        return False

    catalog_identifiers = _canonical_target_identifiers(catalog_target)
    request_identifiers = _canonical_target_identifiers(request_target)
    if not catalog_identifiers or not request_identifiers:
        return False
    return not catalog_identifiers.isdisjoint(request_identifiers)


def _find_catalog_target_action(
    catalog: dict[str, RemediationAction],
    target: dict[str, Any],
) -> tuple[RemediationAction, dict[str, Any]] | None:
    for action in catalog.values():
        for catalog_target in action.targets:
            if _targets_match(catalog_target, target):
                return action, catalog_target
    return None


def _action_from_request(
    *,
    plan_id: str,
    action_type: str,
    targets: list[dict[str, Any]],
    catalog: dict[str, RemediationAction],
) -> RemediationAction:
    if not targets:
        raise ActionNotFoundError("No action targets provided")

    matched_targets: list[dict[str, Any]] = []
    matched_actions: list[RemediationAction] = []

    for target in targets:
        match = _find_catalog_target_action(catalog, target)
        if match is None:
            raise ActionNotFoundError(
                f"No suggested remediation action found for target: {target!r}"
            )

        matched_action, matched_target = match
        if matched_action.action_type != action_type:
            target_label = (
                str(matched_target.get("platform_product_id") or "")
                or str(matched_target.get("product_id") or "")
                or str(matched_target.get("queue_item_id") or "target")
            )
            raise ActionNotExecutableError(
                f"Requested action '{action_type}' is not executable for {target_label}. "
                f"Use '{matched_action.action_type}' instead."
            )

        matched_actions.append(matched_action)
        matched_targets.append(dict(matched_target))

    if len(matched_actions) == 1:
        return matched_actions[0]

    first_action = matched_actions[0]
    action_id = _action_id_for_target(action_type, matched_targets[0])
    return RemediationAction(
        action_id=action_id,
        plan_id=plan_id,
        action_type=first_action.action_type,
        surface=first_action.surface,
        scope=first_action.scope,
        targets=matched_targets,
        fixability=first_action.fixability,
        priority_score=first_action.priority_score,
        priority_reason=first_action.priority_reason,
        reason=first_action.reason,
        status="suggested",
    )


async def _resolve_action(
    payload: MerchantReadinessOptimizationPayload,
    *,
    action_id: Optional[str],
    action_type: Optional[str],
    targets: Optional[list[dict[str, Any]]],
) -> RemediationAction:
    catalog = _build_action_catalog(payload)
    if action_id:
        action = catalog.get(action_id)
        if action is None:
            raise ActionNotFoundError(f"Unknown action_id: {action_id}")
        return action
    if action_type and targets:
        return _action_from_request(
            plan_id=payload.plan.plan_id,
            action_type=action_type,
            targets=targets,
            catalog=catalog,
        )
    raise ActionNotFoundError("No action_id or action_type/targets provided")


async def _load_product_target(
    merchant_id: str,
    target: dict[str, Any],
) -> tuple[str, str, StandardProduct, dict[str, Any]]:
    platform = str(target.get("platform") or "").strip()
    platform_product_id = str(target.get("platform_product_id") or target.get("product_id") or "").strip()
    if not platform or not platform_product_id:
        raise ActionNotFoundError("Missing platform or platform_product_id for product action")

    cache_row = await get_product_cache_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        include_expired=False,
    )
    if not cache_row:
        raise ActionNotFoundError(f"Product not found in cache: {platform}/{platform_product_id}")

    product_data = cache_row.get("product_data") or {}
    product = StandardProduct(**product_data)
    enrichment = await get_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
    ) or {}
    return platform, platform_product_id, product, enrichment


async def preview_remediation_action(
    merchant_id: str,
    *,
    plan_id: str,
    action_id: Optional[str] = None,
    action_type: Optional[str] = None,
    targets: Optional[list[dict[str, Any]]] = None,
    channel: str = "ucp",
) -> dict[str, Any]:
    payload = await _latest_plan_or_raise(merchant_id, channel=channel, plan_id=plan_id)
    action = await _resolve_action(payload, action_id=action_id, action_type=action_type, targets=targets)

    if action.action_type != "run_product_enrichment":
        return {
            "action": action.model_dump(),
            "candidate_patches": [],
            "expected_impact": {
                "impact": action.priority_reason,
                "priority_score": action.priority_score,
            },
            "requires_approval": False,
            "warnings": ["This action requires manual review in the target surface and is not directly executable from the workspace yet."],
        }

    candidate_patches: list[dict[str, Any]] = []
    warnings: list[str] = []
    expected_impact: dict[str, Any] = {"targets": []}
    generated_content_context: list[dict[str, Any]] = []

    for target in action.targets:
        platform, platform_product_id, product, enrichment = await _load_product_target(merchant_id, target)
        generated, suggestion_context = _generated_enrichment(product)
        current_preview = preview_quality(build_quality_payload(product, enrichment))
        expected_preview = preview_quality(build_quality_payload(product, generated))
        product_candidate_count = 0

        for field_name, after_value in generated.items():
            before_value = enrichment.get(field_name)
            if before_value == after_value:
                continue
            candidate = PatchCandidate(
                candidate_id=_candidate_id(action.action_id, f"{platform_product_id}:{field_name}"),
                action_id=action.action_id,
                target_field=field_name,
                before=before_value,
                after=after_value,
                confidence=float(generated.get("llm_readability_score") or 0.0) if field_name != "llm_readability_score" else None,
                rationale=f"Generated from current product facts for {product.title}.",
                evidence_used=[
                    {
                        "source": "products_cache.standard_product",
                        "platform": platform,
                        "platform_product_id": platform_product_id,
                    }
                ],
                risk_flags=[],
                requires_approval=True,
            )
            candidate_patches.append(candidate.model_dump())
            product_candidate_count += 1

        generated_content_context.append(
            {
                "platform": platform,
                "platform_product_id": platform_product_id,
                "product_title": product.title,
                "title_health": suggestion_context.get("title_health"),
                "suggested_title_preview": suggestion_context.get("suggested_title_preview"),
                "suggestion_language": suggestion_context.get("suggestion_language"),
                "suggestion_confidence": suggestion_context.get("suggestion_confidence"),
                "suggestion_rationale": suggestion_context.get("suggestion_rationale"),
                "facts_used": suggestion_context.get("facts_used") or {},
                "missing_attribute_labels": suggestion_context.get("missing_attribute_labels") or [],
                "content_gap_codes": suggestion_context.get("content_gap_codes") or [],
                "skipped_reason": suggestion_context.get("skipped_reason"),
            }
        )

        expected_impact["targets"].append(
            {
                "platform": platform,
                "platform_product_id": platform_product_id,
                "before_scores": {
                    "content_quality_score": current_preview.get("content_quality_score"),
                    "model_readiness_score": current_preview.get("model_readiness_score"),
                },
                "after_scores": {
                    "content_quality_score": expected_preview.get("content_quality_score"),
                    "model_readiness_score": expected_preview.get("model_readiness_score"),
                },
                "delta": {
                    "content_quality_score": (expected_preview.get("content_quality_score") or 0) - (current_preview.get("content_quality_score") or 0),
                    "model_readiness_score": (expected_preview.get("model_readiness_score") or 0) - (current_preview.get("model_readiness_score") or 0),
                },
            }
        )
        if product_candidate_count <= 0:
            warnings.append("No field-level changes were generated for this product.")

    return {
        "action": action.model_dump(),
        "candidate_patches": candidate_patches,
        "expected_impact": expected_impact,
        "generated_content_context": generated_content_context,
        "requires_approval": True,
        "warnings": warnings,
    }


def _dedupe_codes(*groups: list[str]) -> list[str]:
    codes: list[str] = []
    for group in groups:
        for code in group:
            normalized = str(code or "").strip()
            if not normalized or normalized in codes:
                continue
            codes.append(normalized)
    return codes


def _coerce_price_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _coerce_inventory_quantity(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _variant_agent_push_projection(variant: Any) -> dict[str, Any]:
    blocker_codes = set(
        _dedupe_codes(
            variant.blockers.get("discovery", []),
            variant.blockers.get("checkout", []),
        )
    )
    price_data = variant.price or {}
    inventory_data = variant.inventory or {}
    price_amount = _coerce_price_value(price_data.get("amount"))
    currency = str(price_data.get("currency") or "").strip().upper() or None
    availability = str(inventory_data.get("availability") or "").strip().lower()
    inventory_quantity = _coerce_inventory_quantity(inventory_data.get("quantity"))

    # One shared vocabulary (utils.availability_vocabulary) instead of a local denylist. The
    # denylist omitted the SPACE-separated forms, so a lowercased "out of stock" read as IN
    # STOCK. That is unreachable today — readiness/scoring.py is the only producer of this
    # field and it writes canonical tokens only — so this is behaviour-preserving for every
    # value that actually arrives, and correct if a future producer ever passes raw text
    # through. Unknown stays IN STOCK here, exactly as the denylist treated it.
    in_stock = not is_out_of_stock(availability)
    if inventory_quantity is not None:
        in_stock = in_stock and inventory_quantity > 0

    reason_codes: list[str] = []
    if "out_of_stock" in blocker_codes or not in_stock:
        reason_codes.append("out_of_stock")
    if "missing_price" in blocker_codes or price_amount is None:
        reason_codes.append("missing_price")
    if "missing_currency" in blocker_codes or currency is None:
        reason_codes.append("missing_currency")

    if reason_codes:
        return {
            "agent_push_status": "excluded_from_agent_push",
            "agent_push_reason_codes": _dedupe_codes(reason_codes),
        }
    return {
        "agent_push_status": "eligible_for_agent_push",
        "agent_push_reason_codes": [],
    }


def _normalize_source_data_reason_code(reason_code: Optional[str]) -> Optional[str]:
    normalized = str(reason_code or "").strip().lower()
    if not normalized:
        return None
    if normalized not in _SOURCE_DATA_TRIAGE_REASON_DEFS:
        supported = ", ".join(sorted(_SOURCE_DATA_TRIAGE_REASON_DEFS))
        raise ActionNotFoundError(
            f"Unsupported source-data reason code: {normalized}. Expected one of: {supported}"
        )
    return normalized


def _queue_product_key(platform: Optional[str], platform_product_id: Optional[str], product_id: Optional[str]) -> str:
    return f"{str(platform or '').strip().lower()}|{str(platform_product_id or product_id or '').strip()}"


def _variant_matches_source_data_reason(
    reason_code: str,
    *,
    readiness_blocker_codes: list[str],
    agent_push_reason_codes: list[str],
) -> bool:
    blocker_codes = set(readiness_blocker_codes)
    push_codes = set(agent_push_reason_codes)

    if reason_code == "missing_price":
        return bool(
            blocker_codes.intersection({"missing_price", "missing_currency"})
            or push_codes.intersection({"missing_price", "missing_currency"})
        )
    if reason_code == "out_of_stock":
        return "out_of_stock" in blocker_codes or "out_of_stock" in push_codes
    return False


def _product_matches_source_data_reason(reason_code: str, *, queue_item: Any) -> tuple[bool, int]:
    if reason_code != "missing_primary_image":
        return False, 0

    def _issue_value(issue: Any, field: str) -> Any:
        if isinstance(issue, dict):
            return issue.get(field)
        return getattr(issue, field, None)

    affected_variants = sum(
        int(_issue_value(issue, "affected_variant_count") or 0)
        for issue in (queue_item.top_issues or [])
        if _issue_value(issue, "code") == "missing_primary_image"
    )
    if affected_variants <= 0:
        affected_variants = (
            int(queue_item.blocked_variant_count or 0)
            or int(queue_item.excluded_variant_count or 0)
        )

    has_issue = any(
        _issue_value(issue, "code") == "missing_primary_image"
        for issue in (queue_item.top_issues or [])
    )
    return has_issue, affected_variants


def _current_product_data_from_cache_row(cache_row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not cache_row:
        return {}
    payload = cache_row.get("product_data") or {}
    return payload if isinstance(payload, dict) else {}


def _current_variant_lookup(current_product: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for variant in current_product.get("variants") or []:
        variant_id = str(variant.get("variant_id") or variant.get("id") or "").strip()
        if not variant_id or variant_id in lookup:
            continue
        lookup[variant_id] = variant
    return lookup


def _current_variant_inventory(variant: Optional[dict[str, Any]]) -> Optional[int]:
    if not variant:
        return None
    return _coerce_inventory_quantity(
        variant.get("inventory_quantity", variant.get("stock", variant.get("inventory")))
    )


def _current_out_of_stock_batch_state(
    rows: list[SourceDataTriageRow],
    current_product: dict[str, Any],
) -> str:
    if not rows:
        return "unknown"

    variant_lookup = _current_variant_lookup(current_product)
    pending = 0
    resolved = 0
    for row in rows:
        current_variant = variant_lookup.get(str(row.variant_id or "").strip())
        inventory_quantity = _current_variant_inventory(current_variant)
        if inventory_quantity is not None and inventory_quantity > 0:
            resolved += 1
        else:
            pending += 1

    if pending <= 0:
        return "restocked_waiting_refresh"
    if resolved > 0:
        return "partially_recovered"
    return "whole_product_unavailable"


def _current_missing_price_batch_state(
    rows: list[SourceDataTriageRow],
    current_product: dict[str, Any],
) -> str:
    if not rows:
        return "whole_product_missing_price"

    variant_lookup = _current_variant_lookup(current_product)
    pending = 0
    resolved = 0
    for row in rows:
        current_variant = variant_lookup.get(str(row.variant_id or "").strip())
        price_value = _coerce_price_value(
            current_variant.get("price", current_variant.get("price_value")) if current_variant else None
        )
        if isinstance(current_variant, dict) and isinstance(current_variant.get("price"), dict):
            price_value = _coerce_price_value(
                current_variant.get("price", {}).get("amount", current_variant.get("price", {}).get("value"))
            )
        price_currency = (
            str(
                (current_variant or {}).get(
                    "currency",
                    (current_variant or {}).get("price_currency"),
                )
                or (
                    ((current_variant or {}).get("price") or {}).get("currency")
                    if isinstance((current_variant or {}).get("price"), dict)
                    else None
                )
                or current_product.get("currency")
                or ""
            )
            .strip()
            .upper()
            or None
        )
        if (price_value or 0) > 0 and price_currency:
            resolved += 1
        else:
            pending += 1

    if pending <= 0:
        return "priced_waiting_refresh"
    if resolved > 0:
        return "partially_priced"
    return "whole_product_missing_price"


def _current_missing_image_batch_state(current_product: dict[str, Any]) -> str:
    return "image_visible_now" if _current_product_data_has_visible_image(current_product) else "hero_image_missing"


def _current_product_data_has_visible_image(current_product: dict[str, Any]) -> bool:
    image_url = str(current_product.get("image_url") or "").strip()
    if image_url:
        return True
    images = current_product.get("images") or []
    return any(str(image or "").strip() for image in images)


async def upsert_source_data_decision_state(
    merchant_id: str,
    *,
    reason_code: str,
    platform: str,
    platform_product_id: str,
    decision_state: str,
) -> dict[str, Any]:
    normalized_reason_code = _normalize_source_data_reason_code(reason_code)
    supported_states = _SOURCE_DATA_DECISION_STATES.get(normalized_reason_code or "")
    if not supported_states:
        raise ActionNotExecutableError(
            "Persistent merchant decision tracking is not supported for this source-data lane."
        )

    normalized_state = str(decision_state or "").strip().lower()
    if normalized_state not in supported_states:
        supported_states_message = ", ".join(sorted(supported_states))
        raise ActionNotFoundError(
            f"Unsupported source-data decision state: {decision_state}. "
            f"Expected one of: {supported_states_message}"
        )

    row = await upsert_source_data_decision(
        merchant_id,
        reason_code=normalized_reason_code,
        platform=platform,
        platform_product_id=platform_product_id,
        decision_state=normalized_state,
    )
    return {
        "merchant_id": merchant_id,
        "reason_code": normalized_reason_code,
        "platform": str(row.get("platform") or platform).strip().lower(),
        "platform_product_id": str(
            row.get("platform_product_id") or platform_product_id
        ).strip(),
        "decision_state": str(row.get("decision_state") or normalized_state).strip(),
        "updated_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
    }


async def delete_source_data_decision_state(
    merchant_id: str,
    *,
    reason_code: str,
    platform: str,
    platform_product_id: str,
) -> dict[str, Any]:
    normalized_reason_code = _normalize_source_data_reason_code(reason_code)
    if normalized_reason_code not in _SOURCE_DATA_DECISION_STATES:
        raise ActionNotExecutableError(
            "Persistent merchant decision tracking is not supported for this source-data lane."
        )

    deleted = await delete_source_data_decision(
        merchant_id,
        reason_code=normalized_reason_code,
        platform=platform,
        platform_product_id=platform_product_id,
    )
    return {
        "merchant_id": merchant_id,
        "reason_code": normalized_reason_code,
        "platform": str(platform or "").strip().lower(),
        "platform_product_id": str(platform_product_id or "").strip(),
        "deleted": bool(deleted),
    }


async def clear_resolved_source_data_decisions(
    merchant_id: str,
    *,
    plan_id: str,
    reason_code: Optional[str] = None,
    channel: str = "ucp",
) -> list[dict[str, Any]]:
    cleared: list[dict[str, Any]] = []

    candidate_reason_codes = (
        [_normalize_source_data_reason_code(reason_code)]
        if _normalize_source_data_reason_code(reason_code)
        else list(_SOURCE_DATA_DECISION_STATES.keys())
    )

    for candidate_reason_code in candidate_reason_codes:
        if not candidate_reason_code:
            continue

        decision_rows_by_key = await list_source_data_decisions(
            merchant_id,
            reason_code=candidate_reason_code,
        )
        if not decision_rows_by_key:
            continue

        triage_payload = SourceDataTriagePayload.model_validate(
            await get_source_data_triage(
                merchant_id,
                plan_id=plan_id,
                reason_code=candidate_reason_code,
                limit=5000,
                channel=channel,
            )
        )
        triage_rows_by_key: dict[str, list[SourceDataTriageRow]] = {}
        for row in triage_payload.rows:
            row_key = _queue_product_key(
                row.platform,
                row.platform_product_id or row.product_id,
                row.product_id,
            )
            triage_rows_by_key.setdefault(row_key, []).append(row)

        for decision_key, row in decision_rows_by_key.items():
            platform = str(row.get("platform") or "").strip().lower()
            platform_product_id = str(row.get("platform_product_id") or "").strip()
            matching_rows = triage_rows_by_key.get(decision_key) or []

            if not matching_rows:
                await delete_source_data_decision(
                    merchant_id,
                    reason_code=candidate_reason_code,
                    platform=platform,
                    platform_product_id=platform_product_id,
                )
                cleared.append(
                    {
                        "reason_code": candidate_reason_code,
                        "platform": platform,
                        "platform_product_id": platform_product_id,
                        "decision_state": str(row.get("decision_state") or "").strip() or None,
                        "resolution": "resolved_removed_from_queue",
                    }
                )
                continue

            cache_row = await get_product_cache_row(
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                include_expired=False,
            )
            current_product = _current_product_data_from_cache_row(cache_row)

            if candidate_reason_code == "out_of_stock":
                batch_state = _current_out_of_stock_batch_state(matching_rows, current_product)
                should_clear = batch_state == "restocked_waiting_refresh"
            elif candidate_reason_code == "missing_price":
                batch_state = _current_missing_price_batch_state(matching_rows, current_product)
                should_clear = batch_state == "priced_waiting_refresh"
            else:
                batch_state = _current_missing_image_batch_state(current_product)
                should_clear = batch_state == "image_visible_now"

            if not should_clear:
                continue

            await delete_source_data_decision(
                merchant_id,
                reason_code=candidate_reason_code,
                platform=platform,
                platform_product_id=platform_product_id,
            )
            cleared.append(
                {
                    "reason_code": candidate_reason_code,
                    "platform": platform,
                    "platform_product_id": platform_product_id,
                    "decision_state": str(row.get("decision_state") or "").strip() or None,
                    "resolution": "resolved_now",
                }
            )

    return cleared


async def upsert_out_of_stock_source_data_decision(
    merchant_id: str,
    *,
    reason_code: str,
    platform: str,
    platform_product_id: str,
    decision_state: str,
) -> dict[str, Any]:
    return await upsert_source_data_decision_state(
        merchant_id,
        reason_code=reason_code,
        platform=platform,
        platform_product_id=platform_product_id,
        decision_state=decision_state,
    )


async def delete_out_of_stock_source_data_decision(
    merchant_id: str,
    *,
    reason_code: str,
    platform: str,
    platform_product_id: str,
) -> dict[str, Any]:
    return await delete_source_data_decision_state(
        merchant_id,
        reason_code=reason_code,
        platform=platform,
        platform_product_id=platform_product_id,
    )


async def clear_resolved_out_of_stock_source_data_decisions(
    merchant_id: str,
    *,
    plan_id: str,
    channel: str = "ucp",
) -> list[dict[str, Any]]:
    return await clear_resolved_source_data_decisions(
        merchant_id,
        plan_id=plan_id,
        reason_code="out_of_stock",
        channel=channel,
    )


async def get_source_data_triage(
    merchant_id: str,
    *,
    plan_id: str,
    reason_code: Optional[str] = None,
    limit: int = 500,
    channel: str = "ucp",
) -> dict[str, Any]:
    normalized_reason_code = _normalize_source_data_reason_code(reason_code)
    payload, snapshot = await _latest_context_or_raise(
        merchant_id,
        channel=channel,
        plan_id=plan_id,
    )

    queue_by_key = {
        _queue_product_key(item.platform, item.platform_product_id, item.product_id): item
        for item in payload.product_queue
    }
    queue_index_by_key = {
        _queue_product_key(item.platform, item.platform_product_id, item.product_id): index
        for index, item in enumerate(payload.product_queue)
    }

    summary_products: dict[str, set[str]] = {
        code: set() for code in _SOURCE_DATA_TRIAGE_REASON_DEFS
    }
    summary_variant_keys: dict[str, set[str]] = {
        code: set() for code in _SOURCE_DATA_TRIAGE_REASON_DEFS
    }
    summary_variant_counts: dict[str, int] = {
        code: 0 for code in _SOURCE_DATA_TRIAGE_REASON_DEFS
    }
    decision_rows_by_reason: dict[str, dict[str, dict[str, Any]]] = {}
    decision_reason_codes = (
        [normalized_reason_code]
        if normalized_reason_code
        else list(_SOURCE_DATA_DECISION_STATES.keys())
    )
    for candidate_reason_code in decision_reason_codes:
        if not candidate_reason_code:
            continue
        decision_rows_by_reason[candidate_reason_code] = await list_source_data_decisions(
            merchant_id,
            reason_code=candidate_reason_code,
        )

    row_records: list[tuple[tuple[Any, ...], SourceDataTriageRow]] = []

    for snapshot_product in snapshot.products:
        product_key = _queue_product_key(
            snapshot_product.platform,
            snapshot_product.product_id,
            snapshot_product.product_id,
        )
        queue_item = queue_by_key.get(product_key)
        if queue_item is None:
            continue

        platform = queue_item.platform
        platform_product_id = queue_item.platform_product_id or queue_item.product_id
        product_id = queue_item.product_id
        product_title = queue_item.title or snapshot_product.title
        sort_index = queue_index_by_key.get(product_key, 10**9)
        decision_key = _queue_product_key(platform, platform_product_id, product_id)
        current_product_for_decisions: Optional[dict[str, Any]] = None

        async def _load_current_product_for_decisions() -> dict[str, Any]:
            nonlocal current_product_for_decisions
            if current_product_for_decisions is not None:
                return current_product_for_decisions

            cache_row = await get_product_cache_row(
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                include_expired=False,
            )
            current_product_for_decisions = _current_product_data_from_cache_row(cache_row)
            return current_product_for_decisions

        image_match, image_affected_variants = _product_matches_source_data_reason(
            "missing_primary_image",
            queue_item=queue_item,
        )
        if image_match:
            summary_products["missing_primary_image"].add(product_key)
            summary_variant_counts["missing_primary_image"] += max(
                image_affected_variants,
                0,
            )
            if normalized_reason_code in (None, "missing_primary_image"):
                row_records.append(
                    (
                        (sort_index, 0, product_title.lower(), product_id),
                        SourceDataTriageRow(
                            scope="product",
                            reason_code="missing_primary_image",
                            reason_label=_SOURCE_DATA_TRIAGE_REASON_DEFS["missing_primary_image"]["label"],
                            platform=platform,
                            platform_product_id=platform_product_id,
                            platform_admin_url=queue_item.platform_admin_url,
                            product_id=product_id,
                            product_title=product_title,
                            blocked_variant_count=int(queue_item.blocked_variant_count or 0),
                            excluded_variant_count=int(queue_item.excluded_variant_count or 0),
                            readiness_blocker_codes=["missing_primary_image"],
                            readiness_warning_codes=[],
                            agent_push_status=str(
                                queue_item.agent_push_status or "eligible_for_agent_push"
                            ),
                            agent_push_reason_codes=list(queue_item.agent_push_reason_codes or []),
                            recommended_action_type=queue_item.recommended_action_type,
                            fix_surface=queue_item.fix_surface,
                            decision_state=str(
                                (
                                    decision_rows_by_reason
                                    .get("missing_primary_image", {})
                                    .get(decision_key, {})
                                    .get("decision_state")
                                )
                                or ""
                            ).strip()
                            or None,
                        ),
                    )
                )

        for variant in snapshot_product.variants:
            readiness_blocker_codes = _dedupe_codes(
                variant.blockers.get("discovery", []),
                variant.blockers.get("checkout", []),
            )
            readiness_warning_codes = _dedupe_codes(
                variant.warnings.get("discovery", []),
                variant.warnings.get("checkout", []),
            )
            push_projection = _variant_agent_push_projection(variant)
            agent_push_reason_codes = list(
                push_projection.get("agent_push_reason_codes") or []
            )
            agent_push_status = str(
                push_projection.get("agent_push_status") or "eligible_for_agent_push"
            )

            variant_id = str(variant.variant_id or "").strip()
            variant_title = str(variant.title or variant_id or "Variant")

            for candidate_reason_code in ("missing_price", "out_of_stock"):
                if not _variant_matches_source_data_reason(
                    candidate_reason_code,
                    readiness_blocker_codes=readiness_blocker_codes,
                    agent_push_reason_codes=agent_push_reason_codes,
                ):
                    continue

                variant_key = f"{product_key}|{variant_id}|{candidate_reason_code}"
                summary_products[candidate_reason_code].add(product_key)
                summary_variant_keys[candidate_reason_code].add(variant_key)

                if normalized_reason_code not in (None, candidate_reason_code):
                    continue

                persisted_decision_state = str(
                    (
                        decision_rows_by_reason
                        .get(candidate_reason_code, {})
                        .get(decision_key, {})
                        .get("decision_state")
                    )
                    or ""
                ).strip() or None
                effective_decision_state = persisted_decision_state
                if candidate_reason_code == "out_of_stock" and not effective_decision_state:
                    current_product = await _load_current_product_for_decisions()
                    effective_decision_state = _default_out_of_stock_decision_state(
                        current_product
                    )

                row_records.append(
                    (
                        (sort_index, 1, product_title.lower(), variant_title.lower(), variant_id),
                        SourceDataTriageRow(
                            scope="variant",
                            reason_code=candidate_reason_code,
                            reason_label=_SOURCE_DATA_TRIAGE_REASON_DEFS[candidate_reason_code]["label"],
                            platform=platform,
                            platform_product_id=platform_product_id,
                            platform_admin_url=queue_item.platform_admin_url,
                            product_id=product_id,
                            product_title=product_title,
                            variant_id=variant_id,
                            variant_title=variant_title,
                            sku=variant.sku,
                            price_value=_coerce_price_value((variant.price or {}).get("amount")),
                            price_currency=str((variant.price or {}).get("currency") or "").strip() or None,
                            inventory_quantity=_coerce_inventory_quantity(
                                (variant.inventory or {}).get("quantity")
                            ),
                            blocked_variant_count=int(queue_item.blocked_variant_count or 0),
                            excluded_variant_count=int(queue_item.excluded_variant_count or 0),
                            readiness_blocker_codes=readiness_blocker_codes,
                            readiness_warning_codes=readiness_warning_codes,
                            agent_push_status=agent_push_status,
                            agent_push_reason_codes=agent_push_reason_codes,
                            recommended_action_type=queue_item.recommended_action_type,
                            fix_surface=queue_item.fix_surface,
                            decision_state=effective_decision_state,
                        ),
                    )
                )

    for code, variant_keys in summary_variant_keys.items():
        if code == "missing_primary_image":
            continue
        summary_variant_counts[code] = len(variant_keys)

    sorted_rows = [row for _, row in sorted(row_records, key=lambda item: item[0])]
    limited_rows = sorted_rows[: max(1, int(limit or 500))]

    summary = [
        SourceDataTriageSummaryBucket(
            code=code,
            label=definition["label"],
            scope=definition["scope"],
            affected_products=len(summary_products[code]),
            affected_variants=summary_variant_counts[code],
        ).model_dump()
        for code, definition in _SOURCE_DATA_TRIAGE_REASON_DEFS.items()
    ]

    payload_model = SourceDataTriagePayload(
        plan_id=payload.plan.plan_id,
        snapshot_id=payload.plan.snapshot_id,
        reason_code=normalized_reason_code,
        summary=[SourceDataTriageSummaryBucket.model_validate(item) for item in summary],
        rows=limited_rows,
        total_rows=len(sorted_rows),
    )
    return payload_model.model_dump()


async def get_product_blocker_detail(
    merchant_id: str,
    *,
    plan_id: str,
    platform: str,
    platform_product_id: str,
    channel: str = "ucp",
) -> dict[str, Any]:
    payload, snapshot = await _latest_context_or_raise(
        merchant_id,
        channel=channel,
        plan_id=plan_id,
    )

    snapshot_product = next(
        (
            product
            for product in snapshot.products
            if (product.platform or "unknown") == platform
            and product.product_id == platform_product_id
        ),
        None,
    )
    if snapshot_product is None:
        raise ActionNotFoundError(
            f"Product not found in readiness snapshot: {platform}/{platform_product_id}"
        )

    queue_item = next(
        (
            item
            for item in payload.product_queue
            if item.platform == platform
            and (item.platform_product_id or item.product_id) == platform_product_id
        ),
        None,
    )
    if queue_item is None:
        raise ActionNotFoundError(
            f"Product not found in optimization queue: {platform}/{platform_product_id}"
        )

    _, _, standard_product, _enrichment = await _load_product_target(
        merchant_id,
        {
            "platform": platform,
            "platform_product_id": platform_product_id,
        },
    )

    standard_variants_by_id: dict[str, Any] = {}
    for variant in standard_product.variants or []:
        variant_id = str(
            getattr(variant, "variant_id", None) or getattr(variant, "id", "")
        ).strip()
        if variant_id:
            standard_variants_by_id[variant_id] = variant

    variants: list[ProductBlockerVariant] = []
    for variant in snapshot_product.variants:
        readiness_status = (
            "ready" if variant.channel_coverage.get(channel) == "ready" else "blocked"
        )
        standard_variant = standard_variants_by_id.get(variant.variant_id)
        push_projection = _variant_agent_push_projection(variant)

        price_value = None
        price_currency = None
        inventory_quantity = None

        if standard_variant is not None:
            price_value = _coerce_price_value(getattr(standard_variant, "price", None))
            price_currency = str(getattr(standard_product, "currency", None) or "").strip() or None
            inventory_quantity = _coerce_inventory_quantity(
                getattr(standard_variant, "inventory_quantity", None)
            )

        if price_value is None:
            price_value = _coerce_price_value((variant.price or {}).get("amount"))
        if not price_currency:
            price_currency = str((variant.price or {}).get("currency") or "").strip() or None
        if inventory_quantity is None:
            inventory_quantity = _coerce_inventory_quantity(
                (variant.inventory or {}).get("quantity")
            )

        variants.append(
            ProductBlockerVariant(
                variant_id=variant.variant_id,
                title=str(
                    getattr(standard_variant, "title", None)
                    or variant.title
                    or variant.variant_id
                ),
                sku=getattr(standard_variant, "sku", None) or variant.sku,
                price_value=price_value,
                price_currency=price_currency,
                inventory_quantity=inventory_quantity,
                readiness_status=readiness_status,
                readiness_blocker_codes=_dedupe_codes(
                    variant.blockers.get("discovery", []),
                    variant.blockers.get("checkout", []),
                ),
                readiness_warning_codes=_dedupe_codes(
                    variant.warnings.get("discovery", []),
                    variant.warnings.get("checkout", []),
                ),
                agent_push_status=str(
                    push_projection.get("agent_push_status")
                    or "eligible_for_agent_push"
                ),
                agent_push_reason_codes=list(
                    push_projection.get("agent_push_reason_codes") or []
                ),
            )
        )

    variants.sort(
        key=lambda item: (
            0 if item.readiness_status == "blocked" else 1,
            0 if item.agent_push_status == "excluded_from_agent_push" else 1,
            item.title.lower(),
            item.variant_id,
        )
    )

    detail = ProductBlockerDetail(
        plan_id=payload.plan.plan_id,
        snapshot_id=payload.plan.snapshot_id,
        product=ProductBlockerSubject(
            platform=platform,
            platform_product_id=platform_product_id,
            product_id=snapshot_product.product_id,
            title=snapshot_product.title,
        ),
        summary=ProductBlockerCounts(
            ready_variant_count=queue_item.ready_variant_count,
            blocked_variant_count=queue_item.blocked_variant_count,
            eligible_variant_count=queue_item.eligible_variant_count
            or queue_item.ready_variant_count,
            excluded_variant_count=queue_item.excluded_variant_count or 0,
        ),
        variants=variants,
    )
    return detail.model_dump()


async def run_remediation_action(
    merchant_id: str,
    *,
    plan_id: str,
    action_id: Optional[str] = None,
    action_type: Optional[str] = None,
    targets: Optional[list[dict[str, Any]]] = None,
    idempotency_key: Optional[str] = None,
    channel: str = "ucp",
) -> dict[str, Any]:
    before_payload = await _latest_plan_or_raise(merchant_id, channel=channel, plan_id=plan_id)
    action = await _resolve_action(before_payload, action_id=action_id, action_type=action_type, targets=targets)

    if action.action_type != "run_product_enrichment":
        raise ActionNotExecutableError(f"Action type is not executable: {action.action_type}")

    job = ExecutionJob(
        job_id=_job_id(action.action_id),
        action_id=action.action_id,
        executor_type="deterministic",
        status="running",
        started_at=_now_iso(),
        retry_count=0,
    )
    _JOB_STORE[job.job_id] = job

    processed_targets: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for target in action.targets:
        try:
            platform = str(target.get("platform") or "").strip()
            platform_product_id = str(target.get("platform_product_id") or target.get("product_id") or "").strip()
            await run_enrichment_for_product(
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                geo_code="default",
            )
            processed_targets.append(
                {
                    "platform": platform,
                    "platform_product_id": platform_product_id,
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "target": target,
                    "error": str(exc),
                }
            )

    after_context = await get_readiness_optimization_context(
        merchant_id,
        channel=channel,
        force_refresh=True,
    )
    after_payload = after_context[0]
    verification = VerificationResult(
        verification_id=f"rdverify_{job.job_id[-12:]}",
        action_id=action.action_id,
        before_snapshot_id=before_payload.plan.snapshot_id,
        after_snapshot_id=after_payload.plan.snapshot_id,
        delta_scores={
            "readiness_score": (after_payload.score_bundle.readiness_score or 0) - (before_payload.score_bundle.readiness_score or 0),
            "blocked_variant_count": after_payload.readiness_summary.blocked_variant_count - before_payload.readiness_summary.blocked_variant_count,
        },
        resolved_issues=[],
        remaining_issues=after_payload.readiness_summary.top_blockers,
        expected_impact={},
        observed_impact={
            "before_tier": before_payload.readiness_summary.tier,
            "after_tier": after_payload.readiness_summary.tier,
            "before_blocked_variant_count": before_payload.readiness_summary.blocked_variant_count,
            "after_blocked_variant_count": after_payload.readiness_summary.blocked_variant_count,
        },
        merchant_visible_impact=after_payload.readiness_summary.summary_text,
    )

    job.status = "completed" if not errors else "completed_with_errors"
    job.completed_at = _now_iso()
    job.result = {
        "processed_targets": processed_targets,
        "errors": errors,
        "verification": verification.model_dump(),
        "after_plan_id": after_payload.plan.plan_id,
        "after_snapshot_id": after_payload.plan.snapshot_id,
        "idempotency_key": idempotency_key,
    }
    _JOB_STORE[job.job_id] = job

    return {
        "job": job.model_dump(),
        "action": action.model_dump(),
        "verification": verification.model_dump(),
        "after_plan": after_payload.plan.model_dump(),
    }


def get_execution_job(job_id: str) -> ExecutionJob:
    job = _JOB_STORE.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    return job

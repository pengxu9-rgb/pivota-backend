from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Optional

from db.product_enrichment import get_enrichment
from db.products import get_product_cache_row
from models.standard_product import StandardProduct
from readiness.models import (
    ExecutionJob,
    MerchantReadinessOptimizationPayload,
    PatchCandidate,
    RemediationAction,
    VerificationResult,
)
from readiness.summary import build_readiness_optimization
from services.product_enrichment_ai import (
    build_context_from_standard_product,
    classify_audience_tags,
    classify_topic_tags,
    classify_usage_scenarios,
    compute_auto_confidence,
    generate_bullets,
    generate_summary,
)
from services.product_enrichment_pipeline import run_enrichment_for_product
from services.product_quality_service import preview_quality


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


def _build_quality_payload(product: StandardProduct, enrichment: dict[str, Any]) -> dict[str, Any]:
    return {
        "title_local": enrichment.get("title_override") or product.title,
        "description_local": product.description or "",
        "price_local_value": product.price,
        "main_image_url": product.image_url or (product.images[0] if product.images else None),
        "summary_short": enrichment.get("summary_short") or "",
        "bullet_points": enrichment.get("bullet_points") or [],
        "usage_scenarios": enrichment.get("usage_scenarios") or [],
        "audience_tags": enrichment.get("audience_tags") or [],
        "topic_tags": enrichment.get("topic_tags") or [],
        "brand": product.vendor or None,
        "global_category_id": product.product_type or None,
    }


def _generated_enrichment(product: StandardProduct) -> dict[str, Any]:
    context = build_context_from_standard_product(product)
    summary = generate_summary(context)
    bullets = generate_bullets(context)
    usage_scenarios = classify_usage_scenarios(context)
    audience_tags = classify_audience_tags(context)
    topic_tags = classify_topic_tags(context)
    auto_confidence = compute_auto_confidence(summary, bullets, context)

    return {
        "title_override": context.title,
        "summary_short": summary,
        "bullet_points": bullets,
        "usage_scenarios": usage_scenarios,
        "audience_tags": audience_tags,
        "topic_tags": topic_tags,
        "regulatory_disclaimer_local": _default_disclaimer(context.title),
        "extra_images": None,
        "llm_readability_score": auto_confidence,
        "llm_safety_flags": None,
    }


async def _latest_plan_or_raise(
    merchant_id: str,
    *,
    channel: str,
    plan_id: str,
) -> MerchantReadinessOptimizationPayload:
    payload = await build_readiness_optimization(merchant_id, channel=channel)
    if payload.plan.plan_id != plan_id:
        raise PlanSupersededError(
            current_plan_id=payload.plan.plan_id,
            current_snapshot_id=payload.plan.snapshot_id,
        )
    return payload


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


def _action_from_request(
    *,
    plan_id: str,
    action_type: str,
    targets: list[dict[str, Any]],
) -> RemediationAction:
    if not targets:
        raise ActionNotFoundError("No action targets provided")
    action_id = _action_id_for_target(action_type, targets[0])
    return RemediationAction(
        action_id=action_id,
        plan_id=plan_id,
        action_type=action_type,
        surface=str(targets[0].get("surface") or "product_content"),
        scope=str(targets[0].get("scope") or "product"),
        targets=targets,
        reason=str(targets[0].get("reason") or ""),
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
        return _action_from_request(plan_id=payload.plan.plan_id, action_type=action_type, targets=targets)
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

    for target in action.targets:
        platform, platform_product_id, product, enrichment = await _load_product_target(merchant_id, target)
        generated = _generated_enrichment(product)
        current_preview = preview_quality(_build_quality_payload(product, enrichment))
        expected_preview = preview_quality(_build_quality_payload(product, generated))

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
        if not candidate_patches:
            warnings.append("No field-level changes were generated for this product.")

    return {
        "action": action.model_dump(),
        "candidate_patches": candidate_patches,
        "expected_impact": expected_impact,
        "requires_approval": True,
        "warnings": warnings,
    }


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

    after_payload = await build_readiness_optimization(merchant_id, channel=channel)
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

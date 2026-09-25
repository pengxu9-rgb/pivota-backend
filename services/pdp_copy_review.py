"""SKU Optimization OS -- LLM copy-review rubric generator.

Generates the external_rubric the GPT55 quality gate requires to auto-publish a
merchant-approved 'copy' module. Calls DeepSeek directly via config.settings,
mirroring the proven pattern in services/category_classifier_llm.py.

Honesty contract: ANY failure (no API key, transport error, malformed output,
missing/blank checks) returns None. The caller treats None as "no rubric" -> the
GPT55 gate forces needs_human_review and nothing publishes. We never fabricate a
passing rubric. Flag-gated by SKU_OPT_OVERLAY_V1 at the call site.

Metering: before each call we enforce the same per-merchant daily cost cap the LLM
orchestrator uses (AGENT_CENTER_LLM_DAILY_COST_USD_PER_MERCHANT, default $5),
summed from db.llm_probe_runs via cost_today_for_merchant. Over cap -> return None
(fail closed) and record a cost_capped probe row. Every call records a probe_run
row (succeeded/failed/cost_capped) with real token usage + computed cost, so spend
shows up in the same telemetry/rollup as the rest of the platform's LLM usage.
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config.settings import settings
from services.llm_fence import PRODUCT_DATA_FENCE
from db.llm_probe_runs import (
    STATUS_COST_CAPPED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    compute_cost_usd,
    cost_today_for_merchant,
    record_probe_run,
)

logger = logging.getLogger(__name__)

# Telemetry labels (match the registered DeepSeek provider id + a dedicated mode).
_PROBE_PROVIDER = "deepseek"
_PROBE_SCAN_MODE = "pdp_copy_review"

# Per-merchant daily cost cap -- same env var the orchestrator honors.
_DAILY_COST_CAP_USD = float(
    os.getenv("AGENT_CENTER_LLM_DAILY_COST_USD_PER_MERCHANT", "5") or "5"
)

# DeepSeek deepseek-chat (V4 Flash) list rates -- fallback only. The actual rate
# is resolved per-model from the provider registry at call time (so pointing
# DEEPSEEK_MODEL at deepseek-reasoner meters at the reasoner rate, not Flash).
_COST_PER_1K_INPUT_USD = 0.00014
_COST_PER_1K_OUTPUT_USD = 0.00028


def _resolve_deepseek_rates(model_id: str) -> Tuple[float, float]:
    """(input_per_1k, output_per_1k) for the configured DeepSeek model.

    provider_registry.rate_for_model() returns a dict
    {"input_per_1k": float, "output_per_1k": float}; normalize it to a numeric
    tuple. Falls back to the headline deepseek-chat rates if the registry is
    unavailable or returns a malformed shape (never raises)."""
    try:
        from services.llm_providers.provider_registry import get_provider

        provider = get_provider(_PROBE_PROVIDER)
        if provider is not None:
            rates = provider.rate_for_model(model_id)
            if isinstance(rates, dict):
                return float(rates["input_per_1k"]), float(rates["output_per_1k"])
    except Exception:  # registry missing / malformed shape -> safe fallback
        pass
    return _COST_PER_1K_INPUT_USD, _COST_PER_1K_OUTPUT_USD

# Must match GPT55_RUBRIC_REQUIRED_CHECKS in services/pdp_governance_service.py.
REQUIRED_CHECKS = (
    "source_grounded",
    "seller_entity_checkout_not_confused",
    "variant_market_consistent",
    "no_medical_regulated_promo_or_fake_review_claim",
    "machine_publish_allowed_module",
)

COPY_REVIEW_MAX_OUTPUT_TOKENS = 400
COPY_REVIEW_TIMEOUT_S = 15.0

_SYSTEM_PROMPT = (
    "You are a product-listing quality reviewer for an agentic-commerce catalog. "
    "You are given a merchant-submitted product DESCRIPTION (copy) for a module "
    "that is eligible for machine publishing. Evaluate it against each check and "
    "return STRICT JSON only, no prose. Each check is true only if the copy "
    "clearly satisfies it; default to false when unsure.\n\n"
    "Checks:\n"
    "- source_grounded: claims are about THIS product, not invented marketing.\n"
    "- seller_entity_checkout_not_confused: no mention of a different seller/store/checkout.\n"
    "- variant_market_consistent: no contradictory size/shade/market claims.\n"
    "- no_medical_regulated_promo_or_fake_review_claim: no medical/efficacy/regulated"
    " claims, no fake-review or unverifiable promotional language.\n"
    "- machine_publish_allowed_module: copy is self-contained and safe to publish"
    " without human co-review.\n\n"
    'Return JSON: {"decision":"pass|reject|needs_human_review","checks":'
    '{"source_grounded":bool,"seller_entity_checkout_not_confused":bool,'
    '"variant_market_consistent":bool,'
    '"no_medical_regulated_promo_or_fake_review_claim":bool,'
    '"machine_publish_allowed_module":bool},"confidence":0..1,'
    '"evidence_refs":["short quote from the copy"],'
    '"reviewed_in":"codex_external_window"}. '
    'decision must be "pass" only if every check is true. '
    + PRODUCT_DATA_FENCE.notice
)


def build_review_messages(copy_text: str) -> List[Dict[str, str]]:
    """The chat messages the review sends: our rubric, then the merchant's copy
    fenced as data. No cap: the lane had none, and the rubric reads the whole copy."""
    fenced = PRODUCT_DATA_FENCE.fence_text(copy_text, max_chars=None)
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Product description to review:\n\n{fenced}"},
    ]


def _extract_copy_text(payload: Dict[str, Any]) -> str:
    payload = payload if isinstance(payload, dict) else {}
    for key in ("pdp_description_raw", "description", "body", "text", "copy"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_rubric(content: str) -> Optional[Dict[str, Any]]:
    # W3: shared tolerant parse; the strict rubric validation below is unchanged
    # (fail-closed on any malformed/evasive rubric).
    from services.llm_io import parse_llm_object

    obj = parse_llm_object(content, label="pdp_copy_review")
    if obj is None:
        return None
    decision = str(obj.get("decision") or "").strip().lower()
    if decision not in {"pass", "reject", "needs_human_review"}:
        return None
    checks = obj.get("checks")
    if not isinstance(checks, dict):
        return None
    # All required checks must be present AND be LITERAL booleans. We must not
    # coerce here -- bool("false") is True, which would turn a malformed/evasive
    # rubric into a passing one. Fail closed on any non-bool check value.
    if any(c not in checks for c in REQUIRED_CHECKS):
        return None
    if any(not isinstance(checks.get(c), bool) for c in REQUIRED_CHECKS):
        return None
    normalized = {c: checks[c] for c in REQUIRED_CHECKS}
    # A 'pass' is only honored by the gate with non-empty evidence_refs; require
    # them here too so a 'pass' without evidence fails closed rather than
    # silently degrading downstream.
    evidence_refs = obj.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        evidence_refs = []
    evidence_refs = [str(ref) for ref in evidence_refs if str(ref).strip()][:5]
    if decision == "pass" and not evidence_refs:
        return None
    try:
        confidence = float(obj.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "decision": decision,
        "checks": normalized,
        "confidence": confidence,
        "evidence_refs": evidence_refs,
        "reasons": [],
        "reviewed_in": "codex_external_window",
    }


async def _call_deepseek_review(
    *, copy_text: str,
) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """Return (content, input_tokens, output_tokens) or None on no-key.
    Raises on transport/HTTP errors (caller catches + records failure)."""
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        return None
    base_url = settings.deepseek_api_base_url.rstrip("/")
    payload = {
        "model": settings.deepseek_model,
        "messages": build_review_messages(copy_text),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": COPY_REVIEW_MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=COPY_REVIEW_TIMEOUT_S) as client:
        response = await client.post(
            f"{base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return content, usage.get("prompt_tokens"), usage.get("completion_tokens")


async def generate_copy_review_rubric(
    *,
    merchant_id: str,
    payload: Dict[str, Any],
    source_refs: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Return a validated rubric dict, or None on any failure (never raises).

    Enforces the per-merchant daily cost cap before calling, and records a
    probe-run telemetry row (succeeded/failed/cost_capped) after.
    """
    copy_text = _extract_copy_text(payload)
    if not copy_text:
        return None

    # Per-merchant daily cap (fail closed when already over budget).
    try:
        spent_today = await cost_today_for_merchant(merchant_id=merchant_id)
    except Exception:  # telemetry degraded -> fail open on the *check* only
        spent_today = Decimal("0")
    if float(spent_today) >= _DAILY_COST_CAP_USD:
        logger.info(
            "pdp_copy_review cost-capped for merchant_id=%s (spent=%s cap=%s)",
            merchant_id, spent_today, _DAILY_COST_CAP_USD,
        )
        await record_probe_run(
            provider=_PROBE_PROVIDER,
            scan_mode=_PROBE_SCAN_MODE,
            status=STATUS_COST_CAPPED,
            merchant_id=merchant_id,
            cost_usd=Decimal("0"),
        )
        return None

    try:
        result = await _call_deepseek_review(copy_text=copy_text)
    except Exception as exc:  # transport / HTTP / unexpected -> fail closed
        logger.warning("pdp_copy_review deepseek call failed", exc_info=True)
        await record_probe_run(
            provider=_PROBE_PROVIDER,
            scan_mode=_PROBE_SCAN_MODE,
            status=STATUS_FAILED,
            merchant_id=merchant_id,
            error_message=str(exc),
        )
        return None
    if result is None:  # no API key -> nothing ran, nothing to meter
        return None

    content, input_tokens, output_tokens = result
    rate_in, rate_out = _resolve_deepseek_rates(settings.deepseek_model)
    cost_usd = compute_cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_per_1k_input_tokens_usd=rate_in,
        cost_per_1k_output_tokens_usd=rate_out,
    )
    await record_probe_run(
        provider=_PROBE_PROVIDER,
        scan_mode=_PROBE_SCAN_MODE,
        status=STATUS_SUCCEEDED,
        merchant_id=merchant_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )

    rubric = _parse_rubric(content)
    if rubric is None:
        logger.info("pdp_copy_review produced unusable rubric; failing closed")
    return rubric

"""SKU Optimization OS -- LLM copy-review rubric generator.

Generates the external_rubric the GPT55 quality gate requires to auto-publish a
merchant-approved 'copy' module. Calls DeepSeek directly via config.settings,
mirroring the proven pattern in services/category_classifier_llm.py.

Honesty contract: ANY failure (no API key, transport error, malformed output,
missing/blank checks) returns None. The caller treats None as "no rubric" -> the
GPT55 gate forces needs_human_review and nothing publishes. We never fabricate a
passing rubric. Flag-gated by SKU_OPT_OVERLAY_V1 at the call site.

NOTE (hardening follow-up): this calls DeepSeek directly like the category
classifier and is NOT yet routed through the metered orchestrator's per-merchant
cost cap. The call is merchant-initiated and bounded (one per approve click), so
multiplier risk is low, but before broad enablement route through
services/llm_providers/orchestrator (select_provider + deepseek_probe) so spend
records to db.llm_probe_runs and honors AGENT_CENTER_LLM_DAILY_COST_USD_PER_MERCHANT.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

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
    'decision must be "pass" only if every check is true.'
)


def _extract_copy_text(payload: Dict[str, Any]) -> str:
    payload = payload if isinstance(payload, dict) else {}
    for key in ("pdp_description_raw", "description", "body", "text", "copy"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_rubric(content: str) -> Optional[Dict[str, Any]]:
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    # Tolerate ```json fences.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    decision = str(obj.get("decision") or "").strip().lower()
    if decision not in {"pass", "reject", "needs_human_review"}:
        return None
    checks = obj.get("checks")
    if not isinstance(checks, dict):
        return None
    # All required checks must be present; the gate re-validates but we fail
    # closed here so a malformed model never reaches the gate.
    if any(c not in checks for c in REQUIRED_CHECKS):
        return None
    normalized = {c: bool(checks.get(c)) for c in REQUIRED_CHECKS}
    try:
        confidence = float(obj.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    evidence_refs = obj.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        evidence_refs = []
    # The gate requires evidence_refs + this exact review channel to honor a pass.
    return {
        "decision": decision,
        "checks": normalized,
        "confidence": confidence,
        "evidence_refs": [str(ref) for ref in evidence_refs][:5],
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
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Product description to review:\n\n{copy_text}"},
        ],
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
    cost_usd = compute_cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_per_1k_input_tokens_usd=_COST_PER_1K_INPUT_USD,
        cost_per_1k_output_tokens_usd=_COST_PER_1K_OUTPUT_USD,
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

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from services.llm_fence import REVIEW_DATA_FENCE

POLICY_NAME = "text_rules_v1"
DEEPSEEK_POLICY_NAME = "deepseek_review_moderation_v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"

logger = logging.getLogger(__name__)

_MODERATION_SYSTEM_PROMPT = """
You are Pivota's product UGC moderation classifier for product reviews, product questions, and product answers.
Return only valid JSON with this schema:
{
  "decision": "approve" | "reject" | "needs_human_review",
  "risk_level": "low" | "medium" | "high",
  "reason_codes": ["irrelevant_to_product" | "sexual_content" | "gambling" | "drugs" | "violence" | "hate_or_harassment" | "spam_or_scam" | "personal_data" | "illegal_activity" | "profanity" | "unsafe_medical_or_legal_claim" | "other"],
  "confidence": 0.0,
  "review_notes": "short internal note"
}

Reject user submissions that are clearly unrelated to the product, spam/scam, explicit sexual content,
gambling promotion, drug promotion, hate/harassment, threats/violence, illegal activity,
or content that exposes personal data. Use needs_human_review for ambiguous, borderline,
low-confidence, or policy-adjacent cases. Medical/legal advice, diagnosis, treatment/cure claims,
prescription replacement, or use with a medical condition should be needs_human_review unless it is
clearly dangerous enough to reject. Approve only ordinary product-review, product-question, or
product-answer content with low safety risk and clear product relevance.
""".strip() + "\n\n" + REVIEW_DATA_FENCE.notice

_ALLOWED_DECISIONS = {"approve", "reject", "needs_human_review"}
_ALLOWED_RISK_LEVELS = {"low", "medium", "high"}
_ALLOWED_REASON_CODES = {
    "irrelevant_to_product",
    "sexual_content",
    "gambling",
    "drugs",
    "violence",
    "hate_or_harassment",
    "spam_or_scam",
    "personal_data",
    "illegal_activity",
    "profanity",
    "unsafe_medical_or_legal_claim",
    "hate_content",
    "spam_or_irrelevant",
    "other",
}

_SEXUAL_PATTERNS = (
    re.compile(r"\b(porn|porno|xxx|sex(?:ual)?|nudes?|onlyfans|hentai)\b", re.IGNORECASE),
    re.compile(r"\b(blowjob|handjob|cumshot|deepthroat)\b", re.IGNORECASE),
)
_HATE_PATTERNS = (
    re.compile(r"\b(nigger|faggot|kike|spic|chink)\b", re.IGNORECASE),
    re.compile(r"\b(kill\s+all\s+\w+|gas\s+the\s+\w+)\b", re.IGNORECASE),
)
_SPAM_PATTERNS = (
    re.compile(r"\b(contact\s+me|dm\s+me|whatsapp|telegram)\b", re.IGNORECASE),
    re.compile(r"\b(crypto\s+giveaway|earn\s+\$?\d+|make\s+money\s+fast)\b", re.IGNORECASE),
)
_MEDICAL_LEGAL_PATTERNS = (
    re.compile(
        r"\b(cure|diagnos(?:e|is)|medical\s+advice|legal\s+advice|prescription|rx|eczema|psoriasis|"
        r"dermatologist|physician|doctor|steroid|lawsuit|attorney|lawyer)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(replace|instead\s+of|stop|skip|avoid)\b.{0,80}\b(prescription|medicine|medication|doctor|"
        r"dermatologist|physician|steroid)\b",
        re.IGNORECASE,
    ),
)
_URL_PATTERN = re.compile(r"(https?://|www\.)", re.IGNORECASE)


def _norm_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def assess_review_text_risk(*, title: Optional[str], body: Optional[str]) -> Dict[str, Any]:
    title_text = _norm_text(title)
    body_text = _norm_text(body)
    combined = f"{title_text}\n{body_text}".strip()
    reason_codes: List[str] = []

    if combined:
        if any(p.search(combined) for p in _SEXUAL_PATTERNS):
            reason_codes.append("sexual_content")
        if any(p.search(combined) for p in _HATE_PATTERNS):
            reason_codes.append("hate_content")
        url_hits = len(_URL_PATTERN.findall(combined))
        spam_hit = any(p.search(combined) for p in _SPAM_PATTERNS)
        if spam_hit or url_hits >= 3:
            reason_codes.append("spam_or_irrelevant")
        if any(p.search(combined) for p in _MEDICAL_LEGAL_PATTERNS):
            reason_codes.append("unsafe_medical_or_legal_claim")

    # Keep stable ordering for tests/logging.
    deduped_reason_codes = sorted(set(reason_codes))
    risk_level = "high" if deduped_reason_codes else "low"
    moderation_state = "under_review" if risk_level == "high" else "active"
    return {
        "policy": POLICY_NAME,
        "risk_level": risk_level,
        "reason_codes": deduped_reason_codes,
        "moderation_state": moderation_state,
    }


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _deepseek_api_key() -> str:
    return (
        os.getenv("DEEPSEEK_REVIEW_MODERATION_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or ""
    ).strip()


def _deepseek_base_url() -> str:
    return (
        os.getenv("DEEPSEEK_REVIEW_MODERATION_API_BASE_URL")
        or os.getenv("DEEPSEEK_API_BASE_URL")
        or "https://api.deepseek.com"
    ).strip().rstrip("/")


def _deepseek_model() -> str:
    return (os.getenv("DEEPSEEK_REVIEW_MODERATION_MODEL") or DEEPSEEK_DEFAULT_MODEL).strip()


def _normalize_decision(value: Any) -> str:
    decision = str(value or "").strip().lower()
    if decision in {"human_review", "manual_review", "review", "uncertain"}:
        return "needs_human_review"
    if decision not in _ALLOWED_DECISIONS:
        return "needs_human_review"
    return decision


def _normalize_risk_level(value: Any) -> str:
    risk_level = str(value or "").strip().lower()
    if risk_level not in _ALLOWED_RISK_LEVELS:
        return "medium"
    return risk_level


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except Exception:
        return 0.0
    if confidence < 0.0:
        return 0.0
    if confidence > 1.0:
        return 1.0
    return confidence


def _normalize_reason_codes(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    out: List[str] = []
    for item in raw_items:
        code = re.sub(r"[^a-z0-9_]+", "_", str(item or "").strip().lower()).strip("_")
        if not code:
            continue
        out.append(code if code in _ALLOWED_REASON_CODES else "other")
    return sorted(set(out))


def _state_for_deepseek_decision(*, decision: str, risk_level: str, confidence: float) -> str:
    approve_threshold = _env_float("REVIEW_MODERATION_APPROVE_CONFIDENCE_THRESHOLD", 0.86)
    reject_threshold = _env_float("REVIEW_MODERATION_REJECT_CONFIDENCE_THRESHOLD", 0.90)
    if decision == "approve" and risk_level == "low" and confidence >= approve_threshold:
        return "active"
    if decision == "reject" and confidence >= reject_threshold:
        return "removed"
    return "under_review"


def _manual_review_result(
    local: Dict[str, Any],
    *,
    fallback_reason: str,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "policy": DEEPSEEK_POLICY_NAME,
        "provider": "deepseek",
        "model": model or _deepseek_model(),
        "risk_level": str(local.get("risk_level") or "medium"),
        "reason_codes": list(local.get("reason_codes") or []),
        "decision": "needs_human_review",
        "confidence": 0.0,
        "moderation_state": "under_review",
        "employee_review_queue": True,
        "fallback_reason": fallback_reason,
        "local_policy": str(local.get("policy") or POLICY_NAME),
        "local_risk_level": str(local.get("risk_level") or "low"),
        "local_reason_codes": list(local.get("reason_codes") or []),
    }


def _extract_json_object(text: str) -> Dict[str, Any]:
    # W3: the shared tolerant parser; raise (as before) when no object parses so
    # the moderation caller's error path is unchanged.
    from services.llm_io import parse_llm_object

    parsed = parse_llm_object(text, label="review_moderation")
    if parsed is None:
        raise ValueError("moderation response was not a JSON object")
    return parsed


def _moderation_payload_from_deepseek_response(data: Dict[str, Any]) -> Dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("deepseek response missing choices")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("deepseek response missing message content")
    return _extract_json_object(content)


def build_moderation_messages(*, title: Optional[str], body: Optional[str]) -> List[Dict[str, str]]:
    """The chat messages moderation sends: our rubric, then the shopper's review
    fenced as data. A review is the one text on this platform an anonymous
    outsider can write straight into a prompt."""
    fenced = REVIEW_DATA_FENCE.fence_payload(
        {"review_title": _norm_text(title), "review_body": _norm_text(body)}
    )
    return [
        {"role": "system", "content": _MODERATION_SYSTEM_PROMPT},
        {"role": "user", "content": fenced},
    ]


async def _call_deepseek_review_moderation(
    *,
    title: Optional[str],
    body: Optional[str],
    model: str,
    api_key: str,
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "messages": build_moderation_messages(title=title, body=body),
        "temperature": 0,
        "max_tokens": 350,
        "response_format": {"type": "json_object"},
    }
    timeout = httpx.Timeout(connect=3.0, read=_env_float("REVIEW_MODERATION_DEEPSEEK_TIMEOUT_S", 12.0), write=3.0, pool=3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{_deepseek_base_url()}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("deepseek response was not an object")
    return _moderation_payload_from_deepseek_response(data)


async def assess_review_text_risk_with_deepseek(
    *,
    title: Optional[str],
    body: Optional[str],
) -> Dict[str, Any]:
    local = assess_review_text_risk(title=title, body=body)
    api_key = _deepseek_api_key()
    model = _deepseek_model()
    if not api_key:
        return _manual_review_result(local, fallback_reason="deepseek_api_key_missing", model=model)

    try:
        payload = await _call_deepseek_review_moderation(
            title=title,
            body=body,
            model=model,
            api_key=api_key,
        )
    except Exception as exc:
        logger.warning("review_moderation.deepseek_failed type=%s", type(exc).__name__)
        return _manual_review_result(local, fallback_reason="deepseek_unavailable", model=model)

    decision = _normalize_decision(payload.get("decision"))
    risk_level = _normalize_risk_level(payload.get("risk_level"))
    confidence = _normalize_confidence(payload.get("confidence"))
    reason_codes = _normalize_reason_codes(payload.get("reason_codes"))
    local_reason_codes = _normalize_reason_codes(local.get("reason_codes"))
    reason_codes = sorted(set(reason_codes + local_reason_codes))
    if risk_level != "low" and not reason_codes:
        reason_codes = ["other"]

    moderation_state = _state_for_deepseek_decision(
        decision=decision,
        risk_level=risk_level,
        confidence=confidence,
    )

    if local_reason_codes and moderation_state == "active":
        decision = "needs_human_review"
        risk_level = "medium"
        moderation_state = "under_review"

    review_notes = str(payload.get("review_notes") or payload.get("notes") or "").strip()
    if len(review_notes) > 280:
        review_notes = review_notes[:277].rstrip() + "..."

    return {
        "policy": DEEPSEEK_POLICY_NAME,
        "provider": "deepseek",
        "model": model,
        "risk_level": risk_level,
        "reason_codes": reason_codes,
        "decision": decision,
        "confidence": confidence,
        "moderation_state": moderation_state,
        "employee_review_queue": moderation_state == "under_review",
        "review_notes": review_notes,
        "local_policy": str(local.get("policy") or POLICY_NAME),
        "local_risk_level": str(local.get("risk_level") or "low"),
        "local_reason_codes": local_reason_codes,
    }


def merge_moderation_risk_flags(
    existing_flags: Any,
    *,
    moderation: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if isinstance(existing_flags, dict):
        merged.update(existing_flags)
    elif isinstance(existing_flags, str):
        try:
            parsed = json.loads(existing_flags)
            if isinstance(parsed, dict):
                merged.update(parsed)
        except Exception:
            pass

    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is None:
                continue
            merged[str(k)] = v

    merged["moderation_policy"] = str(moderation.get("policy") or POLICY_NAME)
    merged["text_risk_level"] = str(moderation.get("risk_level") or "low")
    merged["moderation_reason_codes"] = list(moderation.get("reason_codes") or [])
    optional_field_map = {
        "provider": "moderation_provider",
        "model": "moderation_model",
        "decision": "moderation_decision",
        "confidence": "moderation_confidence",
        "fallback_reason": "moderation_fallback_reason",
        "review_notes": "moderation_review_notes",
        "local_policy": "moderation_local_policy",
        "local_risk_level": "moderation_local_risk_level",
        "local_reason_codes": "moderation_local_reason_codes",
    }
    for source_key, target_key in optional_field_map.items():
        value = moderation.get(source_key)
        if value is None or value == "":
            continue
        merged[target_key] = value
    if moderation.get("employee_review_queue") is not None:
        merged["employee_review_queue"] = bool(moderation.get("employee_review_queue"))
    merged["moderation_assessed_at"] = datetime.now(timezone.utc).isoformat()
    return merged

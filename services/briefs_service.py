from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from models.brief import (
    BeautyBarrierStatus,
    BeautyConcern,
    BeautySkinType,
    BriefAppliedRule,
    BriefAssumption,
    BriefEvidence,
    BriefEvidenceSource,
    BriefExtensions,
    BriefMarket,
    BriefRawIntent,
    BriefTelemetry,
    ShoppingBriefV0,
)


_ZH_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _detect_lang(text: str) -> str:
    return "zh" if _ZH_CHAR_RE.search(text or "") else "en"


def _normalize_ws(s: str) -> str:
    return " ".join(str(s or "").strip().split())


def _extract_budget_max(text: str) -> Optional[float]:
    """
    Best-effort budget parsing.
    Supports patterns like: 500, 500元, ¥500, $50, 预算500, budget 50.
    """
    t = str(text or "")
    # Prefer explicit "预算" patterns.
    m = re.search(r"(预算|budget)\s*[:：]?\s*([¥￥$]?\s*\d{2,6})", t, flags=re.IGNORECASE)
    if m:
        raw = re.sub(r"[^\d.]", "", m.group(2) or "")
        try:
            return float(raw)
        except Exception:
            pass
    # Generic currency-number patterns
    m2 = re.search(r"([¥￥$]\s*\d{2,6})", t)
    if m2:
        raw = re.sub(r"[^\d.]", "", m2.group(1) or "")
        try:
            return float(raw)
        except Exception:
            pass
    # Plain number fallback (avoid matching ages like 25; require >= 100)
    m3 = re.search(r"\b(\d{3,6})\b", t)
    if m3:
        try:
            return float(m3.group(1))
        except Exception:
            return None
    return None


def _extract_skin_type(text: str) -> BeautySkinType:
    t = str(text or "").lower()
    # Chinese
    if any(k in t for k in ["油痘", "油皮", "出油"]):
        return "oily"
    if any(k in t for k in ["干皮", "干燥", "起皮"]):
        return "dry"
    if any(k in t for k in ["混合", "t区", "两颊"]):
        return "combo"
    if any(k in t for k in ["敏感", "敏皮", "易敏"]):
        return "sensitive"
    # English
    if "oily" in t:
        return "oily"
    if "dry" in t:
        return "dry"
    if "combination" in t or "combo" in t:
        return "combo"
    if "sensitive" in t:
        return "sensitive"
    if "normal" in t:
        return "normal"
    return "unknown"


def _extract_barrier_status(text: str) -> BeautyBarrierStatus:
    t = str(text or "").lower()
    if any(k in t for k in ["刺痛", "泛红", "红", "灼热", "烂脸", "屏障受损", "不耐受", "stinging", "burning"]):
        return "impaired"
    if any(k in t for k in ["稳定", "耐受", "没问题", "strong barrier"]):
        return "strong"
    return "unknown"


def _extract_concerns(text: str) -> List[BeautyConcern]:
    t = str(text or "").lower()
    concerns: List[BeautyConcern] = []

    def add(c: BeautyConcern) -> None:
        if c not in concerns:
            concerns.append(c)

    if any(k in t for k in ["闭口", "粉刺", "closed comedone", "comedone", "rough texture", "粗糙"]):
        add("closed_comedones")
        if "rough" in t or "粗糙" in t:
            add("rough_texture")
    if any(k in t for k in ["痘", "痤疮", "acne", "breakout"]):
        add("acne")
    if any(k in t for k in ["抗老", "细纹", "皱纹", "aging", "anti-aging", "wrinkle"]):
        add("aging")
    if any(k in t for k in ["色斑", "暗沉", "提亮", "dark spot", "hyperpigmentation", "brighten"]):
        add("dark_spots")
    if any(k in t for k in ["泛红", "红", "redness", "rosacea"]):
        add("redness")
    if any(k in t for k in ["干燥", "紧绷", "dryness"]):
        add("dryness")
    if any(k in t for k in ["控油", "出油", "oil control"]):
        add("oil_control")
    return concerns


def _detect_sensitive_skin(text: str, *, skin_type: BeautySkinType) -> Optional[bool]:
    # Explicit mentions
    t = str(text or "").lower()
    if any(k in t for k in ["敏感", "敏皮", "sensitive"]):
        return True
    if any(k in t for k in ["不敏感", "not sensitive"]):
        return False
    # Skin type implies sensitive
    if skin_type == "sensitive":
        return True
    return None


def generate_brief_id() -> str:
    return f"brf_{uuid.uuid4().hex}"


def build_brief_v0(
    *,
    agent_id: str,
    raw_query: str,
    market: Optional[str],
    locale: Optional[str],
    currency: Optional[str],
    telemetry: Optional[Dict[str, Any]],
) -> Tuple[ShoppingBriefV0, float]:
    now = _utc_now()
    text = _normalize_ws(raw_query)
    lang = _detect_lang(text)

    skin_type = _extract_skin_type(text)
    barrier_status = _extract_barrier_status(text)
    concerns = _extract_concerns(text)
    sensitive_skin = _detect_sensitive_skin(text, skin_type=skin_type)
    budget_max = _extract_budget_max(text)

    applied_rules: List[BriefAppliedRule] = []
    risk_tags: List[str] = []

    if "closed_comedones" in concerns:
        applied_rules.append(BriefAppliedRule(rule_id="beauty.concern.closed_comedones", reason="query mentions 闭口/粗糙/comedones"))
    if barrier_status == "impaired":
        applied_rules.append(BriefAppliedRule(rule_id="beauty.barrier.impaired", reason="query mentions 泛红/刺痛/屏障受损"))
        risk_tags.append("barrier_impaired")
    if isinstance(budget_max, (int, float)):
        applied_rules.append(BriefAppliedRule(rule_id="beauty.budget.max", reason=f"parsed budget max={budget_max}"))
        if budget_max <= 500:
            risk_tags.append("budget_tight")

    assumptions: List[BriefAssumption] = []
    if skin_type == "unknown":
        assumptions.append(BriefAssumption(id="a_skin_type_unknown", text="Skin type not specified; treating as unknown.", confidence=0.4))
    if barrier_status == "unknown":
        assumptions.append(
            BriefAssumption(id="a_barrier_unknown", text="Barrier status not specified; treating as unknown.", confidence=0.5)
        )
    if budget_max is None:
        assumptions.append(BriefAssumption(id="a_budget_unknown", text="Budget max not specified; treating as unknown.", confidence=0.5))

    evidence = BriefEvidence(
        retrieved_at=now,
        retrieval_sources=[
            BriefEvidenceSource(type="user_input", source="raw_intent.text", captured_at=now),
            BriefEvidenceSource(
                type="rulepack",
                source="beauty_pack",
                captured_at=now,
                details={"version": "0.1.0"},
            ),
        ],
        applied_rules=applied_rules,
    )

    # Confidence is purely mechanical (do not overstate).
    confidence = 0.5
    if skin_type != "unknown":
        confidence += 0.15
    if barrier_status != "unknown":
        confidence += 0.1
    if concerns:
        confidence += 0.15
    if budget_max is not None:
        confidence += 0.1
    confidence = min(0.95, max(0.0, confidence))

    trace_id = None
    session_id = None
    request_id = None
    if isinstance(telemetry, dict):
        trace_id = str(telemetry.get("trace_id") or telemetry.get("traceId") or "").strip() or None
        session_id = str(telemetry.get("session_id") or telemetry.get("sessionId") or "").strip() or None
        request_id = str(telemetry.get("request_id") or telemetry.get("requestId") or "").strip() or None

    brief = ShoppingBriefV0(
        brief_id=generate_brief_id(),
        created_at=now,
        agent={"agent_id": agent_id},
        market=BriefMarket(market=market, locale=locale, currency=currency),
        raw_intent=BriefRawIntent(text=text, lang=lang, captured_at=now),
        constraints={
            "budget": {"currency": currency, "max": budget_max},
            "availability": {"in_stock_only": True},
            "merchants": {"allowed_merchant_ids": None},
        },
        proposed_items=[],
        assumptions=assumptions,
        evidence=evidence,
        risk_tags=risk_tags,
        telemetry=BriefTelemetry(
            trace_id=trace_id or request_id or str(uuid.uuid4()),
            session_id=session_id,
            request_id=request_id,
        ),
        extensions=BriefExtensions(
            beauty={
                "skin_type": skin_type,
                "barrier_status": barrier_status,
                "concerns": concerns,
                "sensitive_skin": sensitive_skin,
                "routine_preferences": {"split_am_pm": True},
            }
        ),
    )
    return brief, float(confidence)


def build_clarify(
    *,
    raw_query: str,
    currency: Optional[str],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Returns (missing_fields, questions) with max 2 questions.
    """
    text = _normalize_ws(raw_query)
    skin_type = _extract_skin_type(text)
    barrier_status = _extract_barrier_status(text)
    concerns = _extract_concerns(text)
    budget_max = _extract_budget_max(text)

    missing_fields: List[str] = []
    questions: List[Dict[str, Any]] = []

    def ask_budget() -> None:
        questions.append(
            {
                "id": "budget_max",
                "text": "你的预算上限大概是多少？",
                "type": "number",
                "unit": (currency or "USD"),
            }
        )

    def ask_skin_type() -> None:
        questions.append(
            {
                "id": "skin_type",
                "text": "你的肤质更接近哪种？",
                "type": "single_choice",
                "choices": [
                    {"value": "oily", "label": "油性"},
                    {"value": "dry", "label": "干性"},
                    {"value": "combo", "label": "混合"},
                    {"value": "sensitive", "label": "敏感"},
                    {"value": "unknown", "label": "不确定"},
                ],
            }
        )

    def ask_barrier_status() -> None:
        questions.append(
            {
                "id": "barrier_status",
                "text": "你现在皮肤屏障状态更接近哪种？",
                "type": "single_choice",
                "choices": [
                    {"value": "strong", "label": "稳定（不怎么刺痛/泛红）"},
                    {"value": "impaired", "label": "泛红/刺痛/不耐受"},
                    {"value": "unknown", "label": "不确定"},
                ],
            }
        )

    def ask_concerns() -> None:
        questions.append(
            {
                "id": "concerns",
                "text": "你最在意的 1-2 个问题是什么？",
                "type": "multi_choice",
                "choices": [
                    {"value": "acne", "label": "痘痘"},
                    {"value": "closed_comedones", "label": "闭口/粗糙"},
                    {"value": "redness", "label": "泛红"},
                    {"value": "dark_spots", "label": "暗沉/色斑"},
                    {"value": "aging", "label": "抗老"},
                ],
            }
        )

    if budget_max is None:
        missing_fields.append("constraints.budget.max")
    if skin_type == "unknown":
        missing_fields.append("extensions.beauty.skin_type")
    if barrier_status == "unknown":
        missing_fields.append("extensions.beauty.barrier_status")
    if not concerns:
        missing_fields.append("extensions.beauty.concerns")

    # Priority: budget -> barrier -> skin_type -> concerns
    if budget_max is None and len(questions) < 2:
        ask_budget()
    if barrier_status == "unknown" and len(questions) < 2:
        ask_barrier_status()
    if skin_type == "unknown" and len(questions) < 2:
        ask_skin_type()
    if not concerns and len(questions) < 2:
        ask_concerns()

    return missing_fields, questions[:2]


def _candidate_has_irritant_keywords(title: str, tags: Optional[List[str]]) -> bool:
    t = str(title or "").lower()
    joined_tags = " ".join([str(x or "").lower() for x in (tags or [])])
    blob = f"{t} {joined_tags}"
    irritants = [
        "retinol",
        "tretinoin",
        "adapalene",
        "glycolic",
        "salicylic",
        "bha",
        "aha",
        "benzoyl",
        "alcohol",
        "peel",
        "exfoliant",
        "acid",
        "强酸",
        "a醇",
        "视黄醇",
        "水杨酸",
        "果酸",
        "酒精",
    ]
    return any(k in blob for k in irritants)


def compatibility_check(
    *,
    brief: ShoppingBriefV0,
    candidate: Dict[str, Any],
) -> Tuple[float, List[str], List[str], List[str], List[BriefAppliedRule]]:
    reasons: List[str] = []
    required_changes: List[str] = []
    risk_tags: List[str] = []
    applied_rules: List[BriefAppliedRule] = []

    # Base fit is optimistic but bounded; rules only reduce.
    fit = 0.8

    b = (brief.extensions.beauty if brief.extensions else None) if hasattr(brief, "extensions") else None
    skin_type = getattr(b, "skin_type", "unknown") if b else "unknown"
    barrier_status = getattr(b, "barrier_status", "unknown") if b else "unknown"
    sensitive_skin = getattr(b, "sensitive_skin", None) if b else None

    # Rule 1: merchant allowlist
    allow = None
    try:
        allow = (brief.constraints.merchants.allowed_merchant_ids or None) if brief.constraints else None
    except Exception:
        allow = None
    if isinstance(allow, list) and allow:
        if str(candidate.get("merchant_id") or "") not in set(map(str, allow)):
            applied_rules.append(BriefAppliedRule(rule_id="generic.merchant_allowlist", reason="candidate.merchant_id not in allowed list"))
            return 0.0, ["Candidate merchant is not allowed by constraints."], ["Select a product from an allowed merchant."], ["merchant_not_allowed"], applied_rules

    # Rule 2: availability/orderable
    in_stock_only = True
    try:
        in_stock_only = bool(brief.constraints.availability.in_stock_only)
    except Exception:
        in_stock_only = True
    if in_stock_only:
        if candidate.get("orderable") is False or candidate.get("in_stock") is False:
            applied_rules.append(BriefAppliedRule(rule_id="generic.availability.in_stock_only", reason="candidate not in_stock/orderable"))
            return 0.0, ["Candidate is not orderable / out of stock."], ["Pick an in-stock, orderable item."], ["not_orderable"], applied_rules

    # Rule 3: budget max
    max_budget = None
    try:
        max_budget = brief.constraints.budget.max
    except Exception:
        max_budget = None
    price = candidate.get("price")
    if isinstance(max_budget, (int, float)) and isinstance(price, (int, float)):
        if float(price) > float(max_budget):
            fit *= 0.2
            reasons.append(f"Price {price} exceeds budget max {max_budget}.")
            required_changes.append("Lower the price or increase budget.")
            risk_tags.append("budget_over_max")
            applied_rules.append(BriefAppliedRule(rule_id="generic.budget.max", reason="candidate.price > brief.constraints.budget.max"))

    # Rule 4: missing profile needs clarification
    if skin_type == "unknown":
        fit *= 0.7
        reasons.append("Skin type is unknown; fit is uncertain.")
        required_changes.append("Provide skin type to improve suitability.")
        risk_tags.append("needs_clarification_skin_type")
        applied_rules.append(BriefAppliedRule(rule_id="beauty.missing.skin_type", reason="extensions.beauty.skin_type=unknown"))
    if not getattr(b, "concerns", None):
        fit *= 0.8
        reasons.append("Concerns are not specified; fit is uncertain.")
        required_changes.append("Provide top concerns (e.g., acne/closed comedones/redness).")
        risk_tags.append("needs_clarification_concerns")
        applied_rules.append(BriefAppliedRule(rule_id="beauty.missing.concerns", reason="extensions.beauty.concerns empty"))

    # Rule 5: barrier/sensitive veto for strong actives
    if barrier_status == "impaired" or sensitive_skin is True:
        if _candidate_has_irritant_keywords(candidate.get("title") or "", candidate.get("tags")):
            applied_rules.append(BriefAppliedRule(rule_id="beauty.veto.barrier_impaired_irritant", reason="barrier impaired/sensitive + irritant keyword/tag"))
            return 0.0, ["Barrier is impaired/sensitive and candidate appears irritating (retinoid/acid/alcohol)."], [
                "Prioritize soothing/repair until barrier recovers.",
                "If treating comedones, prefer azelaic/mandelic and patch-test.",
            ], ["veto_barrier_impaired"], applied_rules

    # Rule 6 (soft): currency mismatch
    brief_ccy = (brief.market.currency or None) if getattr(brief, "market", None) else None
    cand_ccy = candidate.get("currency") or None
    if brief_ccy and cand_ccy and str(brief_ccy).upper() != str(cand_ccy).upper():
        fit *= 0.9
        reasons.append(f"Currency mismatch: brief={brief_ccy} candidate={cand_ccy}.")
        risk_tags.append("currency_mismatch")
        applied_rules.append(BriefAppliedRule(rule_id="generic.currency.mismatch", reason="candidate.currency != brief.market.currency"))

    return max(0.0, min(1.0, float(fit))), reasons, required_changes, risk_tags, applied_rules

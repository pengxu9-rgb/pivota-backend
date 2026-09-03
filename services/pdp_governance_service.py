from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config.settings import resolve_public_api_base_url
from db.database import IS_POSTGRES, database

logger = logging.getLogger(__name__)
from db.pdp_governance import (
    merchant_pdp_contributions,
    pdp_gallery_assets,
    pdp_audit_log,
    pdp_module_versions,
    pdp_review_tasks,
    pdp_subject_index,
)
from db.merchant_product_overlay import merchant_product_overlay
from db.products import products_cache
from services.merchant_write_guardrails import (
    ACTOR_HUMAN,
    ACTOR_MODEL,
    ACTOR_SYSTEM,
    KIND_PDP_MODULE_CONTENT,
    GuardrailViolation,
    check_guardrails,
    check_host_approval,
    current_config,
    items_from_payload,
)


DEFAULT_MARKET = "US"
GPT55_REVIEW_MODEL = "gpt-5.5"

REVIEW_ACTOR_HUMAN = "human_employee"
REVIEW_ACTOR_GPT55 = "gpt55_quality_gate"
REVIEW_ACTOR_SYSTEM = "system_policy"


def _guardrail_actor_kind(actor_type: Optional[str]) -> str:
    """Map this service's review actor onto the guardrail module's actor kinds.

    REVIEW_ACTOR_GPT55 is a MODEL, whatever `actor_id` says. The merchant self-approve
    route (routes/merchant_pdp.py) passes actor_id="merchant:<id>" with actor_type
    REVIEW_ACTOR_GPT55 because the LLM gate — not the merchant — is the publish
    authority there; reading the id instead of the type would let a model-decided
    publish present itself as a human one.
    """
    if actor_type == REVIEW_ACTOR_HUMAN:
        return ACTOR_HUMAN
    if actor_type == REVIEW_ACTOR_SYSTEM:
        return ACTOR_SYSTEM
    return ACTOR_MODEL


def _module_write_items(pdp_id: str, module_key: str, payload: Dict[str, Any], before: Optional[Dict[str, Any]] = None):
    """The change lines an operator would approve: one per top-level payload key."""
    return items_from_payload(f"{pdp_id}:{module_key}", payload, before=before)


def _enforce_module_write_guardrails(
    *,
    pdp_id: str,
    module_key: str,
    payload: Dict[str, Any],
    before: Optional[Dict[str, Any]] = None,
    actor_type: Optional[str] = None,
    at_apply: bool = False,
) -> None:
    """Refuse a module write that breaks the guardrails.

    Called twice per lane, exactly as the blueprint requires: once when the payload is
    STAGED (create_module_draft) and again at APPLY (publish_module_version) against
    `current_config()` — the config in force at apply time, not the one that was in
    force when the draft was written. At apply the host-approval switch is checked too;
    at staging it is not, because staging approves nothing.
    """
    config = current_config()
    violations = check_guardrails(
        KIND_PDP_MODULE_CONTENT,
        _module_write_items(pdp_id, module_key, payload, before=before),
        config,
    )
    if at_apply:
        violations = violations + check_host_approval(
            KIND_PDP_MODULE_CONTENT,
            actor_kind=_guardrail_actor_kind(actor_type),
            config=config,
        )
    if violations:
        raise GuardrailViolation(violations)

PDP_MODULE_KEYS: Tuple[str, ...] = (
    "identity",
    "copy",
    "gallery",
    "variants",
    "offers",
    "reviews",
    "pivota_insights",
    "external_sources",
    "quality",
)

MACHINE_PUBLISH_MODULES = {
    "identity",
    "copy",
    "variants",
    "offers",
    "pivota_insights",
    "external_sources",
    "quality",
}

GPT55_RUBRIC_REQUIRED_CHECKS = {
    "source_grounded",
    "seller_entity_checkout_not_confused",
    "variant_market_consistent",
    "no_medical_regulated_promo_or_fake_review_claim",
    "machine_publish_allowed_module",
}

HUMAN_CO_REVIEW_MODULES = {
    "gallery",
    "reviews",
}

SENIOR_REVIEW_ROLES = {"senior_employee", "admin", "super_admin", "superadmin"}
EMPLOYEE_REVIEW_ROLES = {"employee", *SENIOR_REVIEW_ROLES}
OUTSOURCED_REVIEW_ROLES = {"outsourced"}
LOW_RISK_OUTSOURCED_MODULES = {"copy", "pivota_insights", "offers", "external_sources", "quality", "variants"}
HIGH_RISK_REVIEW_MODULES = {
    "gallery",
    "reviews",
    "external_proof",
    "highlight_badges",
    "regulated_claims",
    "safety_claims",
    "merchant_disputes",
    "rollback_recovery",
}
PDP_REVIEW_QUEUE_TABS = {
    "needs_review",
    "my_queue",
    "publish_ready",
    "escalated",
    "senior_review",
    "qa_sample",
    "published_monitor",
    "identity_audit",
}

HIGH_RISK_PAYLOAD_KEYS = {
    "third_party_rights",
    "rights_status",
    "external_proof_badges",
    "proof_badges",
    "regulated_claims",
    "merchant_dispute",
    "review_import",
    "featured_reviews",
}

GENERIC_PRODUCT_MATCH_TOKENS = {
    "balm",
    "blush",
    "cleanser",
    "conditioner",
    "cream",
    "essence",
    "foundation",
    "gel",
    "gloss",
    "highlighter",
    "lotion",
    "mask",
    "mirror",
    "mist",
    "moisturizer",
    "oil",
    "palette",
    "serum",
    "shampoo",
    "soap",
    "spray",
    "stick",
    "sunscreen",
    "toner",
    "treatment",
    "wash",
}

GALLERY_IMAGE_ROLES = {"primary", "gallery", "variant", "detail", "packaging", "swatch"}
GALLERY_RIGHTS_STATUSES = {
    "owned_or_licensed",
    "merchant_provided",
    "third_party_permission_verified",
    "permission_pending",
    "evidence_only",
    "unknown",
}

UNSUPPORTED_CLAIM_PATTERNS = [
    (r"\b(cure|treat|heal|prevent|diagnose|clinically proven)\b", "medical_or_regulated_claim"),
    (r"\b(guaranteed|100%\s*guarantee|risk[- ]?free)\b", "guarantee_claim"),
    (r"\b(best[- ]?selling|#\s*1|number\s*one|top\s*rated|viral)\b", "unverified_popularity_claim"),
    (r"\b(limited\s*time|today\s*only|flash\s*sale|discount|free\s*shipping)\b", "promotion_or_time_sensitive_claim"),
    (r"\b(everyone\s+loves|customers\s+love|buyers\s+say)\b", "unsupported_review_expression"),
    (r"\b(sold\s+by\s+pivota|pivota\s+checkout|owned\s+by\s+pivota)\b", "seller_or_checkout_ownership_confusion"),
]

_TABLES_READY = False


def parse_product_key(product_key: str) -> Tuple[str, str, str]:
    parts = [part.strip() for part in (product_key or "").split("|")]
    if len(parts) != 3 or not all(parts):
        raise ValueError("INVALID_PRODUCT_KEY")
    return parts[0], parts[1], parts[2]


def is_external_seed_product_key(product_key: str) -> bool:
    try:
        merchant_id, platform, _ = parse_product_key(product_key)
    except ValueError:
        return False
    return merchant_id == "external_seed" and platform == "external"


def make_pdp_id(subject_type: str, subject_ref: str, market: str = DEFAULT_MARKET) -> str:
    raw = f"{market.strip().upper()}|{subject_type.strip().lower()}|{subject_ref.strip()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"pdp_{digest}"


def module_requires_human_review(module_key: str, payload: Optional[Dict[str, Any]] = None) -> bool:
    if module_key in HUMAN_CO_REVIEW_MODULES:
        return True
    payload = payload if isinstance(payload, dict) else {}
    if module_key == "identity" and isinstance(payload.get("identity_review"), dict):
        return True
    if module_key == "gallery":
        return True
    if module_key == "pivota_insights":
        external_badges = payload.get("external_proof_badges") or payload.get("proof_badges")
        if external_badges and not payload.get("seller_grounded_only"):
            return True
    return any(key in payload and payload.get(key) not in (None, "", [], {}) for key in HIGH_RISK_PAYLOAD_KEYS)


def module_risk_level(module_key: str, payload: Optional[Dict[str, Any]] = None) -> str:
    if module_requires_human_review(module_key, payload):
        return "high"
    if module_key in {"pivota_insights", "quality"}:
        return "medium"
    return "low"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            return str(value)
    return str(value)


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except Exception:
            return value
    return value


def _json_dict(value: Any) -> Dict[str, Any]:
    parsed = _json(value)
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> List[Any]:
    parsed = _json(value)
    return parsed if isinstance(parsed, list) else []


def _row_dict(row: Any) -> Dict[str, Any]:
    return dict(row) if row else {}


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        val = (os.getenv(name) or "").strip()
        if val:
            return val
    return (default or "").strip()


def merge_source_refs(*groups: Any) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    seen = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for ref in group:
            normalized = ref if isinstance(ref, dict) else {"type": "source_ref", "id": str(ref)}
            key = json.dumps(normalized, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            refs.append(normalized)
    return refs


def _text_blob(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def _extract_product_summary(product_data: Dict[str, Any]) -> Dict[str, Any]:
    title = (
        product_data.get("title")
        or product_data.get("name")
        or product_data.get("product_title")
        or product_data.get("display_name")
    )
    description = (
        product_data.get("description_text")
        or product_data.get("description")
        or product_data.get("body_html")
        or product_data.get("summary")
    )
    images = product_data.get("images")
    if isinstance(images, list):
        image_url = product_data.get("image_url") or product_data.get("main_image_url") or (images[0] if images else None)
    else:
        image_url = product_data.get("image_url") or product_data.get("main_image_url")
    variants = product_data.get("variants") if isinstance(product_data.get("variants"), list) else []
    return {
        "title": title,
        "description": description,
        "image_url": image_url,
        "variants": variants,
        "currency": product_data.get("currency") or product_data.get("price_currency"),
        "price": product_data.get("price") or product_data.get("price_amount"),
        "brand": product_data.get("brand") or product_data.get("vendor") or product_data.get("manufacturer"),
        "availability": product_data.get("availability") or product_data.get("available"),
    }


def _product_key(merchant_id: Any, platform: Any, platform_product_id: Any) -> str:
    return f"{merchant_id}|{platform}|{platform_product_id}"


def _tokenize_match_text(value: Any) -> List[str]:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    stop = {"the", "and", "with", "for", "from", "size", "product", "new", "set", "kit", "a", "an", "of", "to"}
    return [token for token in text.split() if len(token) >= 3 and token not in stop]


def _distinctive_match_tokens(tokens: Iterable[str]) -> List[str]:
    return [token for token in tokens if token not in GENERIC_PRODUCT_MATCH_TOKENS]


def _product_form_tokens(tokens: Iterable[str]) -> List[str]:
    return sorted({token for token in tokens if token in GENERIC_PRODUCT_MATCH_TOKENS})


def _match_score(target: Any, candidate: Any) -> Tuple[float, List[str]]:
    target_token_list = _tokenize_match_text(target)
    candidate_token_list = _tokenize_match_text(candidate)
    target_tokens = set(target_token_list)
    candidate_tokens = set(candidate_token_list)
    if not target_tokens or not candidate_tokens:
        return 0.0, []

    overlap = sorted(target_tokens & candidate_tokens)
    distinctive_overlap = _distinctive_match_tokens(overlap)
    normalized_target = " ".join(target_token_list)
    normalized_candidate = " ".join(candidate_token_list)

    if normalized_target and normalized_target == normalized_candidate:
        reasons = ["exact_title_match"]
        reasons.extend(f"title_distinctive_overlap:{token}" for token in distinctive_overlap[:5])
        return 0.98, reasons

    title_similarity = (
        SequenceMatcher(None, normalized_target, normalized_candidate).ratio()
        if normalized_target and normalized_candidate
        else 0.0
    )

    target_forms = _product_form_tokens(target_tokens)
    candidate_forms = _product_form_tokens(candidate_tokens)
    if target_forms and candidate_forms and not (set(target_forms) & set(candidate_forms)) and title_similarity < 0.9:
        return 0.0, [f"product_form_mismatch:{','.join(target_forms)}!={','.join(candidate_forms)}"]

    if not distinctive_overlap and title_similarity < 0.82:
        return 0.0, [f"generic_only_overlap:{token}" for token in overlap[:5]]

    union = target_tokens | candidate_tokens
    token_jaccard = len(overlap) / max(1, len(union))
    target_distinctive = set(_distinctive_match_tokens(target_tokens))
    distinctive_coverage = len(set(distinctive_overlap)) / max(1, len(target_distinctive))
    score = max(
        token_jaccard,
        distinctive_coverage * 0.72,
        title_similarity if title_similarity >= 0.82 else 0.0,
    )

    reasons = [f"title_distinctive_overlap:{token}" for token in distinctive_overlap[:5]]
    generic_overlap = [token for token in overlap if token in GENERIC_PRODUCT_MATCH_TOKENS]
    if generic_overlap and distinctive_overlap:
        reasons.extend(f"title_generic_overlap:{token}" for token in generic_overlap[:3])
    if title_similarity >= 0.82:
        reasons.append(f"title_similarity:{title_similarity:.2f}")
    return min(1.0, score), reasons


def _score_candidate(target_title: Any, candidate_title: Any, target_brand: Any = None, candidate_brand: Any = None) -> Tuple[float, List[str]]:
    score, reasons = _match_score(target_title, candidate_title)
    if score > 0 and target_brand and candidate_brand and str(target_brand).strip().lower() == str(candidate_brand).strip().lower():
        score = min(1.0, score + 0.12)
        reasons.append("brand_match")
    return score, reasons


def _identity_evidence(kind: str, label: str, value: Any = None, confidence: Optional[float] = None) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {"type": kind, "label": label}
    if value not in (None, ""):
        evidence["value"] = value
    if confidence is not None:
        evidence["confidence"] = round(float(confidence), 3)
    return evidence


def _append_unique(values: List[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _offer_identity_signals(
    subject: Dict[str, Any],
    offer: Dict[str, Any],
    *,
    confirmed_product_keys: Optional[set] = None,
    confirmed_merchant_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """Attach explainability to offers/candidates without changing review decisions."""
    confirmed_product_keys = confirmed_product_keys or set()
    confirmed_merchant_ids = confirmed_merchant_ids or set()
    match_status = str(offer.get("match_status") or "").strip().lower()
    source = str(offer.get("source") or "").strip()
    product_key = str(offer.get("product_key") or "").strip()
    merchant_id = str(offer.get("merchant_id") or "").strip()
    subject_title = subject.get("title") or subject.get("subject_ref")
    offer_title = offer.get("title") or offer.get("external_product_id") or offer.get("id")
    title_score, title_reasons = _match_score(subject_title, offer_title)
    risk_flags: List[str] = []
    evidence: List[Dict[str, Any]] = []
    score = float(offer.get("confidence") or 0.0)

    if match_status == "confirmed":
        score = 0.74 if source == "external_seed" else 0.78
        if product_key and product_key in confirmed_product_keys:
            evidence.append(_identity_evidence("product_group_member", "Product key is attached to this PDP product group.", product_key, 0.9))
            score += 0.08
        if product_key and product_key == str(subject.get("representative_product_key") or ""):
            evidence.append(_identity_evidence("representative_product_key", "Product key is the current representative PDP product.", product_key, 0.88))
            score += 0.04
        if source == "external_seed":
            external_product_id = str(offer.get("external_product_id") or "").strip()
            if external_product_id and external_product_id == str(subject.get("external_product_id") or "").strip():
                evidence.append(_identity_evidence("external_product_id", "External product id matches this external-only PDP.", external_product_id, 0.9))
                score += 0.1
            attached_product_key = str(offer.get("attached_product_key") or "").strip()
            if attached_product_key and attached_product_key in confirmed_product_keys:
                evidence.append(_identity_evidence("attached_product_key", "External seed is attached to a confirmed merchant product.", attached_product_key, 0.86))
                score += 0.08
            if not offer.get("disclosure_text"):
                _append_unique(risk_flags, "external_offer_missing_disclosure")
        if title_score >= 0.82:
            evidence.append(_identity_evidence("title_alignment", "Offer title aligns with PDP title.", f"{title_score:.2f}", title_score))
            score += 0.03
        elif title_score and title_score < 0.45:
            _append_unique(risk_flags, "confirmed_title_weak_match")
    else:
        if not score:
            score = title_score
        evidence.append(_identity_evidence("candidate_state", "Candidate is evidence-only until employee review confirms it.", match_status or "candidate"))
        if title_score:
            evidence.append(_identity_evidence("title_similarity", "Candidate title similarity to PDP title.", f"{title_score:.2f}", title_score))
        for reason in (offer.get("match_reasons") or title_reasons or [])[:5]:
            evidence.append(_identity_evidence("match_reason", "Candidate retrieval signal.", reason))
            if str(reason).startswith("product_form_mismatch"):
                _append_unique(risk_flags, "product_form_mismatch")
            if str(reason).startswith("generic_only_overlap"):
                _append_unique(risk_flags, "generic_only_title_overlap")
        if score < 0.65:
            _append_unique(risk_flags, "low_identity_confidence")
        if merchant_id and merchant_id in confirmed_merchant_ids:
            _append_unique(risk_flags, "same_merchant_distinct_product_candidate")
        strong_candidate_evidence = any(item["type"] in {"external_product_id", "attached_product_key", "product_group_member"} for item in evidence)
        if not strong_candidate_evidence:
            _append_unique(risk_flags, "title_based_candidate_only")

    price = offer.get("price") if isinstance(offer.get("price"), dict) else {}
    if not offer.get("image_url"):
        _append_unique(risk_flags, "missing_image")
    if source == "merchant_product" and price.get("amount") is None:
        _append_unique(risk_flags, "missing_price")
    if source == "merchant_product" and int(offer.get("variants_count") or 0) == 0:
        _append_unique(risk_flags, "missing_variants")

    if match_status == "confirmed":
        verification_status = "confirmed_with_risk_flags" if risk_flags else "confirmed"
    elif score >= 0.82:
        verification_status = "suggested_match"
    elif score >= 0.65:
        verification_status = "possible_match"
    else:
        verification_status = "evidence_only"

    enriched = dict(offer)
    enriched.update(
        {
            "identity_confidence": round(min(0.98, max(0.0, score)), 3),
            "identity_evidence": evidence,
            "risk_flags": risk_flags,
            "verification_status": verification_status,
        }
    )
    return enriched


def _named_in(prefix: str, values: List[str]) -> Tuple[str, Dict[str, str]]:
    params = {f"{prefix}_{idx}": value for idx, value in enumerate(values)}
    return ", ".join(f":{name}" for name in params), params


async def ensure_pdp_governance_tables() -> None:
    global _TABLES_READY
    if _TABLES_READY:
        return

    json_type = "JSONB" if IS_POSTGRES else "JSON"
    binary_type = "BYTEA" if IS_POSTGRES else "BLOB"
    now_expr = "NOW()" if IS_POSTGRES else "CURRENT_TIMESTAMP"
    timestamp_type = "TIMESTAMPTZ" if IS_POSTGRES else "DATETIME"

    await database.execute(
        f"""
        CREATE TABLE IF NOT EXISTS pdp_subject_index (
          pdp_id TEXT PRIMARY KEY,
          subject_type TEXT NOT NULL,
          subject_ref TEXT NOT NULL,
          market TEXT NOT NULL DEFAULT 'US',
          product_group_id TEXT NULL,
          external_product_id TEXT NULL,
          representative_product_key TEXT NULL,
          title TEXT NULL,
          image_url TEXT NULL,
          seller_count INTEGER NOT NULL DEFAULT 0,
          external_only BOOLEAN NOT NULL DEFAULT FALSE,
          status TEXT NOT NULL DEFAULT 'active',
          created_at {timestamp_type} NOT NULL DEFAULT {now_expr},
          updated_at {timestamp_type} NOT NULL DEFAULT {now_expr}
        );
        """
    )
    await database.execute(
        f"""
        CREATE TABLE IF NOT EXISTS pdp_module_versions (
          id TEXT PRIMARY KEY,
          pdp_id TEXT NOT NULL,
          module_key TEXT NOT NULL,
          stage TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'draft',
          payload {json_type} NOT NULL,
          source_refs {json_type} NULL,
          review_actor_type TEXT NULL,
          review_actor_id TEXT NULL,
          review_model TEXT NULL,
          review_decision TEXT NULL,
          review_confidence DOUBLE PRECISION NULL,
          review_rubric {json_type} NULL,
          risk_level TEXT NOT NULL DEFAULT 'low',
          requires_human BOOLEAN NOT NULL DEFAULT FALSE,
          generated_by TEXT NULL,
          generation_ref TEXT NULL,
          created_by_employee_id TEXT NULL,
          created_at {timestamp_type} NOT NULL DEFAULT {now_expr},
          published_at {timestamp_type} NULL,
          superseded_at {timestamp_type} NULL
        );
        """
    )
    await database.execute(
        f"""
        CREATE TABLE IF NOT EXISTS pdp_audit_log (
          id TEXT PRIMARY KEY,
          pdp_id TEXT NOT NULL,
          module_key TEXT NULL,
          action TEXT NOT NULL,
          actor_type TEXT NOT NULL,
          actor_id TEXT NULL,
          details {json_type} NULL,
          created_at {timestamp_type} NOT NULL DEFAULT {now_expr}
        );
        """
    )
    await database.execute(
        f"""
        CREATE TABLE IF NOT EXISTS merchant_pdp_contributions (
          id TEXT PRIMARY KEY,
          pdp_id TEXT NOT NULL,
          product_key TEXT NOT NULL,
          merchant_id TEXT NOT NULL,
          module_key TEXT NOT NULL,
          payload {json_type} NOT NULL,
          notes TEXT NULL,
          status TEXT NOT NULL DEFAULT 'submitted',
          reviewed_by_actor_type TEXT NULL,
          reviewed_by_actor_id TEXT NULL,
          review_decision TEXT NULL,
          review_notes TEXT NULL,
          created_at {timestamp_type} NOT NULL DEFAULT {now_expr},
          updated_at {timestamp_type} NOT NULL DEFAULT {now_expr}
        );
        """
    )
    await database.execute(
        f"""
        CREATE TABLE IF NOT EXISTS pdp_gallery_assets (
          id TEXT PRIMARY KEY,
          pdp_id TEXT NOT NULL,
          filename TEXT NULL,
          content_type TEXT NOT NULL,
          byte_size INTEGER NOT NULL,
          data {binary_type} NOT NULL,
          created_by_actor_type TEXT NULL,
          created_by_actor_id TEXT NULL,
          created_at {timestamp_type} NOT NULL DEFAULT {now_expr}
        );
        """
    )
    await database.execute(
        f"""
        CREATE TABLE IF NOT EXISTS pdp_review_tasks (
          id TEXT PRIMARY KEY,
          pdp_id TEXT NOT NULL,
          module_key TEXT NOT NULL,
          version_id TEXT NULL,
          status TEXT NOT NULL DEFAULT 'needs_review',
          assignee_actor_id TEXT NULL,
          assignee_role TEXT NULL,
          priority TEXT NOT NULL DEFAULT 'normal',
          qa_sample BOOLEAN NOT NULL DEFAULT FALSE,
          checklist {json_type} NULL,
          policy_labels {json_type} NULL,
          decision_tree_path {json_type} NULL,
          escalation_reason TEXT NULL,
          override_reason TEXT NULL,
          review_duration_ms INTEGER NULL,
          created_at {timestamp_type} NOT NULL DEFAULT {now_expr},
          updated_at {timestamp_type} NOT NULL DEFAULT {now_expr},
          resolved_at {timestamp_type} NULL
        );
        """
    )

    index_statements = [
        "CREATE INDEX IF NOT EXISTS idx_pdp_subject_index_subject ON pdp_subject_index(subject_type, subject_ref);",
        "CREATE INDEX IF NOT EXISTS idx_pdp_subject_index_updated ON pdp_subject_index(updated_at);",
        "CREATE INDEX IF NOT EXISTS idx_pdp_subject_index_market ON pdp_subject_index(market);",
        "CREATE INDEX IF NOT EXISTS idx_pdp_module_versions_lookup ON pdp_module_versions(pdp_id, module_key, stage);",
        "CREATE INDEX IF NOT EXISTS idx_pdp_module_versions_created ON pdp_module_versions(created_at);",
        "CREATE INDEX IF NOT EXISTS idx_pdp_audit_log_created ON pdp_audit_log(created_at);",
        "CREATE INDEX IF NOT EXISTS idx_merchant_pdp_contributions_status ON merchant_pdp_contributions(status, created_at);",
        "CREATE INDEX IF NOT EXISTS idx_pdp_gallery_assets_pdp_created ON pdp_gallery_assets(pdp_id, created_at);",
        "CREATE INDEX IF NOT EXISTS idx_pdp_review_tasks_lookup ON pdp_review_tasks(pdp_id, module_key, version_id);",
        "CREATE INDEX IF NOT EXISTS idx_pdp_review_tasks_status_updated ON pdp_review_tasks(status, updated_at);",
        "CREATE INDEX IF NOT EXISTS idx_pdp_review_tasks_assignee ON pdp_review_tasks(assignee_actor_id, status);",
    ]
    for statement in index_statements:
        await database.execute(statement)

    _TABLES_READY = True


async def _audit(
    *,
    pdp_id: str,
    module_key: Optional[str],
    action: str,
    actor_type: str,
    actor_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    await database.execute(
        pdp_audit_log.insert().values(
            id=f"audit_{uuid.uuid4().hex}",
            pdp_id=pdp_id,
            module_key=module_key,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            details=details or {},
            created_at=_now(),
        )
    )


async def _fetch_latest_cache_row(merchant_id: str, platform: str, platform_product_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = await database.fetch_one(
            products_cache.select()
            .where(
                (products_cache.c.merchant_id == merchant_id)
                & (products_cache.c.platform == platform)
                & (products_cache.c.platform_product_id == platform_product_id)
            )
            .order_by(products_cache.c.cached_at.desc(), products_cache.c.id.desc())
            .limit(1)
        )
    except Exception:
        # DB error here used to silently surface as "no PDP found", which is
        # what made PDP Production Smoke fail with red-herring messages.
        # Preserve the None return for callers but log so the real cause is visible.
        logger.warning(
            "_fetch_latest_cache_row failed for merchant=%s platform=%s product=%s",
            merchant_id, platform, platform_product_id,
            exc_info=True,
        )
        return None
    return _row_dict(row) if row else None


async def _resolve_product_group(merchant_id: str, platform: str, platform_product_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = await database.fetch_one(
            """
            SELECT product_group_id
            FROM product_group_members
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND platform_product_id = :platform_product_id
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
            },
        )
    except Exception:
        logger.warning(
            "_resolve_product_group group lookup failed for merchant=%s platform=%s product=%s",
            merchant_id, platform, platform_product_id,
            exc_info=True,
        )
        return None
    if not row or not row["product_group_id"]:
        return None

    product_group_id = str(row["product_group_id"])
    try:
        count_row = await database.fetch_one(
            """
            SELECT COUNT(DISTINCT merchant_id) AS seller_count
            FROM product_group_members
            WHERE product_group_id = :product_group_id
            """,
            {"product_group_id": product_group_id},
        )
        count_data = _row_dict(count_row)
        seller_count = int(count_data.get("seller_count") or 0)
    except Exception:
        logger.warning(
            "_resolve_product_group seller_count query failed for product_group_id=%s",
            product_group_id,
            exc_info=True,
        )
        seller_count = 0

    try:
        primary = await database.fetch_one(
            """
            SELECT merchant_id, platform, platform_product_id
            FROM product_group_members
            WHERE product_group_id = :product_group_id
            ORDER BY is_primary DESC, updated_at DESC
            LIMIT 1
            """,
            {"product_group_id": product_group_id},
        )
    except Exception:
        logger.warning(
            "_resolve_product_group primary-member query failed for product_group_id=%s",
            product_group_id,
            exc_info=True,
        )
        primary = None

    return {
        "product_group_id": product_group_id,
        "seller_count": max(1, seller_count),
        "representative_product_key": (
            f"{primary['merchant_id']}|{primary['platform']}|{primary['platform_product_id']}"
            if primary
            else f"{merchant_id}|{platform}|{platform_product_id}"
        ),
    }


async def _fetch_external_seed_by_product_id(external_product_id: str, market: str) -> Optional[Dict[str, Any]]:
    try:
        row = await database.fetch_one(
            """
            SELECT *
            FROM external_product_seeds
            WHERE external_product_id = :external_product_id
              AND market = :market
              AND status = 'active'
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            {"external_product_id": external_product_id, "market": market},
        )
    except Exception:
        logger.warning(
            "_fetch_external_seed_by_product_id failed for external_product_id=%s market=%s",
            external_product_id, market,
            exc_info=True,
        )
        return None
    return _row_dict(row) if row else None


async def _fetch_external_seed_by_id(seed_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = await database.fetch_one(
            "SELECT * FROM external_product_seeds WHERE id = :id LIMIT 1",
            {"id": seed_id},
        )
    except Exception:
        logger.warning(
            "_fetch_external_seed_by_id failed for seed_id=%s",
            seed_id,
            exc_info=True,
        )
        return None
    return _row_dict(row) if row else None


async def _upsert_subject(subject: Dict[str, Any]) -> Dict[str, Any]:
    existing = await database.fetch_one(
        "SELECT * FROM pdp_subject_index WHERE pdp_id = :pdp_id",
        {"pdp_id": subject["pdp_id"]},
    )
    row_values = {
        "pdp_id": subject["pdp_id"],
        "subject_type": subject["subject_type"],
        "subject_ref": subject["subject_ref"],
        "market": subject.get("market") or DEFAULT_MARKET,
        "product_group_id": subject.get("product_group_id"),
        "external_product_id": subject.get("external_product_id"),
        "representative_product_key": subject.get("representative_product_key"),
        "title": subject.get("title"),
        "image_url": subject.get("image_url"),
        "seller_count": int(subject.get("seller_count") or 0),
        "external_only": bool(subject.get("external_only")),
        "status": subject.get("status") or "active",
    }
    now = _now()
    if existing:
        await database.execute(
            pdp_subject_index.update()
            .where(pdp_subject_index.c.pdp_id == subject["pdp_id"])
            .values(**row_values, updated_at=now)
        )
    else:
        await database.execute(
            pdp_subject_index.insert().values(**row_values, created_at=now, updated_at=now)
        )
        await _audit(
            pdp_id=subject["pdp_id"],
            module_key=None,
            action="pdp_subject_created",
            actor_type=REVIEW_ACTOR_SYSTEM,
            details={"subject_type": subject["subject_type"], "subject_ref": subject["subject_ref"]},
        )
    latest = await database.fetch_one("SELECT * FROM pdp_subject_index WHERE pdp_id = :pdp_id", {"pdp_id": subject["pdp_id"]})
    return _serialize_subject(_row_dict(latest))


async def resolve_pdp_subject(
    *,
    pdp_id: Optional[str] = None,
    product_key: Optional[str] = None,
    external_seed_id: Optional[str] = None,
    market: str = DEFAULT_MARKET,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    market = (market or DEFAULT_MARKET).strip().upper()

    if pdp_id:
        row = await database.fetch_one("SELECT * FROM pdp_subject_index WHERE pdp_id = :pdp_id", {"pdp_id": pdp_id})
        if not row:
            raise LookupError("PDP_NOT_FOUND")
        return _serialize_subject(_row_dict(row))

    if external_seed_id:
        seed = await _fetch_external_seed_by_id(external_seed_id)
        if not seed:
            raise LookupError("EXTERNAL_SEED_NOT_FOUND")
        external_product_id = str(seed.get("external_product_id") or seed.get("id"))
        subject = _subject_from_external_seed(seed, external_product_id=external_product_id, market=market)
        return await _upsert_subject(subject)

    if not product_key:
        raise ValueError("PDP_RESOLUTION_REQUIRES_PRODUCT_KEY_OR_SEED")

    merchant_id, platform, platform_product_id = parse_product_key(product_key)
    if merchant_id == "external_seed" and platform == "external":
        seed = await _fetch_external_seed_by_product_id(platform_product_id, market)
        if seed:
            subject = _subject_from_external_seed(seed, external_product_id=platform_product_id, market=market)
        else:
            subject = {
                "pdp_id": make_pdp_id("external_product", platform_product_id, market),
                "subject_type": "external_product",
                "subject_ref": platform_product_id,
                "market": market,
                "external_product_id": platform_product_id,
                "representative_product_key": product_key,
                "title": platform_product_id,
                "image_url": None,
                "seller_count": 0,
                "external_only": True,
                "status": "active",
            }
        return await _upsert_subject(subject)

    product_group = await _resolve_product_group(merchant_id, platform, platform_product_id)
    cache_row = await _fetch_latest_cache_row(merchant_id, platform, platform_product_id)
    product_data = _json_dict(cache_row.get("product_data")) if cache_row else {}
    summary = _extract_product_summary(product_data)

    if product_group:
        subject_type = "product_group"
        subject_ref = product_group["product_group_id"]
        pdp_ref_id = product_group["product_group_id"]
        representative_product_key = product_group.get("representative_product_key") or product_key
        seller_count = int(product_group.get("seller_count") or 1)
        product_group_id = product_group["product_group_id"]
    else:
        subject_type = "merchant_product"
        subject_ref = product_key
        pdp_ref_id = product_key
        representative_product_key = product_key
        seller_count = 1
        product_group_id = None

    subject = {
        "pdp_id": make_pdp_id(subject_type, pdp_ref_id, market),
        "subject_type": subject_type,
        "subject_ref": subject_ref,
        "market": market,
        "product_group_id": product_group_id,
        "external_product_id": None,
        "representative_product_key": representative_product_key,
        "title": summary.get("title") or platform_product_id,
        "image_url": summary.get("image_url"),
        "seller_count": seller_count,
        "external_only": False,
        "status": "active",
    }
    return await _upsert_subject(subject)


def _subject_from_external_seed(seed: Dict[str, Any], *, external_product_id: str, market: str) -> Dict[str, Any]:
    return {
        "pdp_id": make_pdp_id("external_product", external_product_id, market),
        "subject_type": "external_product",
        "subject_ref": external_product_id,
        "market": market,
        "product_group_id": None,
        "external_product_id": external_product_id,
        "representative_product_key": f"external_seed|external|{external_product_id}",
        "title": seed.get("title") or external_product_id,
        "image_url": seed.get("image_url"),
        "seller_count": 0,
        "external_only": True,
        "status": "active",
    }


def _serialize_subject(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pdp_id": row.get("pdp_id"),
        "subject_type": row.get("subject_type"),
        "subject_ref": row.get("subject_ref"),
        "market": row.get("market") or DEFAULT_MARKET,
        "product_group_id": row.get("product_group_id"),
        "external_product_id": row.get("external_product_id"),
        "representative_product_key": row.get("representative_product_key"),
        "title": row.get("title"),
        "image_url": row.get("image_url"),
        "seller_count": int(row.get("seller_count") or 0),
        "external_only": bool(row.get("external_only")),
        "status": row.get("status") or "active",
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


async def _representative_product_data(subject: Dict[str, Any]) -> Dict[str, Any]:
    product_key = subject.get("representative_product_key")
    if not product_key or is_external_seed_product_key(product_key):
        return {}
    try:
        merchant_id, platform, platform_product_id = parse_product_key(product_key)
    except ValueError:
        return {}
    row = await _fetch_latest_cache_row(merchant_id, platform, platform_product_id)
    return _json_dict(row.get("product_data")) if row else {}


async def _external_seed_payload(subject: Dict[str, Any]) -> Dict[str, Any]:
    external_product_id = subject.get("external_product_id")
    if not external_product_id:
        return {}
    seed = await _fetch_external_seed_by_product_id(str(external_product_id), subject.get("market") or DEFAULT_MARKET)
    if not seed:
        return {}
    return {
        "seed_id": seed.get("id"),
        "external_product_id": external_product_id,
        "destination_url": seed.get("destination_url"),
        "canonical_url": seed.get("canonical_url"),
        "domain": seed.get("domain"),
        "title": seed.get("title"),
        "image_url": seed.get("image_url"),
        "availability": seed.get("availability"),
        "disclosure_text": seed.get("disclosure_text"),
    }


async def _baseline_payloads(subject: Dict[str, Any]) -> Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    product_data = await _representative_product_data(subject)
    product_summary = _extract_product_summary(product_data)
    external_seed = await _external_seed_payload(subject)

    title = subject.get("title") or product_summary.get("title") or external_seed.get("title")
    image_url = subject.get("image_url") or product_summary.get("image_url") or external_seed.get("image_url")
    source_refs = [
        {
            "type": "pdp_subject",
            "id": subject.get("pdp_id"),
            "subject_type": subject.get("subject_type"),
            "subject_ref": subject.get("subject_ref"),
        }
    ]
    if subject.get("representative_product_key"):
        source_refs.append({"type": "product_key", "id": subject.get("representative_product_key")})
    if external_seed:
        source_refs.append({"type": "external_seed", "id": external_seed.get("seed_id"), "url": external_seed.get("canonical_url") or external_seed.get("destination_url")})

    return {
        "identity": (
            {
                "pdp_id": subject.get("pdp_id"),
                "title": title,
                "image_url": image_url,
                "market": subject.get("market") or DEFAULT_MARKET,
                "subject_type": subject.get("subject_type"),
                "product_group_id": subject.get("product_group_id"),
                "external_product_id": subject.get("external_product_id"),
                "seller_count": subject.get("seller_count") or 0,
                "external_only": bool(subject.get("external_only")),
            },
            source_refs,
        ),
        "copy": (
            {
                "title": title,
                "description": product_summary.get("description") or external_seed.get("title") or "",
                "summary": product_summary.get("description") or "",
                "generated_source": "baseline_projection",
            },
            source_refs,
        ),
        "variants": (
            {
                "variants": product_summary.get("variants") or [],
                "variant_source": "representative_product" if product_data else "external_seed",
            },
            source_refs,
        ),
        "offers": (
            {
                "offer_policy": "mixed_internal_external" if not subject.get("external_only") else "external_redirect_only",
                "checkout_integrated_allowed": not bool(subject.get("external_only")),
                "external_redirect_allowed": True,
                "external_disclosure_required": True,
                "representative_product_key": subject.get("representative_product_key"),
                "external_product_id": subject.get("external_product_id"),
            },
            source_refs,
        ),
        "external_sources": (
            {
                "sources": [external_seed] if external_seed else [],
                "external_only": bool(subject.get("external_only")),
            },
            source_refs,
        ),
        "quality": (
            {
                "status": "baseline_ready",
                "checks": [
                    "projection_unified",
                    "published_payload_audited",
                    "llm_candidate_requires_review_gate",
                ],
            },
            source_refs,
        ),
    }


async def ensure_baseline_modules(subject: Dict[str, Any]) -> None:
    payloads = await _baseline_payloads(subject)
    for module_key, (payload, source_refs) in payloads.items():
        current = await _current_published_version(subject["pdp_id"], module_key)
        if current:
            continue
        version_id = f"pdpmod_{uuid.uuid4().hex}"
        await database.execute(
            pdp_module_versions.insert().values(
                id=version_id,
                pdp_id=subject["pdp_id"],
                module_key=module_key,
                stage="published",
                version=1,
                status="published",
                payload=payload,
                source_refs=source_refs,
                review_actor_type=REVIEW_ACTOR_SYSTEM,
                review_actor_id="baseline_projection",
                review_model=None,
                review_decision="pass",
                review_confidence=1.0,
                review_rubric={"policy": "system_baseline", "reason": "Low-risk deterministic baseline projection."},
                risk_level=module_risk_level(module_key, payload),
                requires_human=False,
                generated_by="system_baseline",
                generation_ref=None,
                created_by_employee_id=None,
                created_at=_now(),
                published_at=_now(),
                superseded_at=None,
            )
        )
        await _audit(
            pdp_id=subject["pdp_id"],
            module_key=module_key,
            action="module_baseline_published",
            actor_type=REVIEW_ACTOR_SYSTEM,
            actor_id="baseline_projection",
            details={"version_id": version_id},
        )


async def _current_published_version(pdp_id: str, module_key: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT *
        FROM pdp_module_versions
        WHERE pdp_id = :pdp_id
          AND module_key = :module_key
          AND stage = 'published'
          AND status = 'published'
          AND superseded_at IS NULL
        ORDER BY version DESC, created_at DESC
        LIMIT 1
        """,
        {"pdp_id": pdp_id, "module_key": module_key},
    )
    return _serialize_module(row) if row else None


async def _latest_staged_version(pdp_id: str, module_key: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT *
        FROM pdp_module_versions
        WHERE pdp_id = :pdp_id
          AND module_key = :module_key
          AND stage = 'staged'
          AND superseded_at IS NULL
        ORDER BY version DESC, created_at DESC
        LIMIT 1
        """,
        {"pdp_id": pdp_id, "module_key": module_key},
    )
    return _serialize_module(row) if row else None


async def _module_history(pdp_id: str, module_key: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT *
        FROM pdp_module_versions
        WHERE pdp_id = :pdp_id
          AND module_key = :module_key
        ORDER BY version DESC, created_at DESC
        LIMIT 20
        """,
        {"pdp_id": pdp_id, "module_key": module_key},
    )
    return [_serialize_module(row) for row in rows]


def _serialize_module(row: Any) -> Dict[str, Any]:
    data = _row_dict(row)
    if not data:
        return {}
    return {
        "id": data.get("id"),
        "pdp_id": data.get("pdp_id"),
        "module_key": data.get("module_key"),
        "stage": data.get("stage"),
        "version": int(data.get("version") or 0),
        "status": data.get("status"),
        "payload": _json(data.get("payload")) or {},
        "published_payload": (_json(data.get("payload")) or {}) if data.get("stage") == "published" and data.get("status") == "published" else None,
        "source_refs": _json(data.get("source_refs")) or [],
        "review_actor_type": data.get("review_actor_type"),
        "review_actor_id": data.get("review_actor_id"),
        "review_model": data.get("review_model"),
        "review_decision": data.get("review_decision"),
        "review_confidence": data.get("review_confidence"),
        "review_rubric": _json(data.get("review_rubric")) or {},
        "risk_level": data.get("risk_level") or "low",
        "requires_human": bool(data.get("requires_human")),
        "generated_by": data.get("generated_by"),
        "generation_ref": data.get("generation_ref"),
        "created_by_employee_id": data.get("created_by_employee_id"),
        "created_at": _iso(data.get("created_at")),
        "published_at": _iso(data.get("published_at")),
        "superseded_at": _iso(data.get("superseded_at")),
        "last_reviewer": {
            "actor_type": data.get("review_actor_type"),
            "actor_id": data.get("review_actor_id"),
            "model": data.get("review_model"),
        }
        if data.get("review_actor_type")
        else None,
    }


def _empty_module_summary(module_key: str) -> Dict[str, Any]:
    requires_human = module_key in HUMAN_CO_REVIEW_MODULES
    return {
        "module_key": module_key,
        "version_id": None,
        "status": "needs_human_review" if requires_human else "not_started",
        "risk_level": "high" if requires_human else "low",
        "requires_human": requires_human,
        "current": None,
        "staged": None,
        "published_payload": None,
        "source_refs": [],
        "last_reviewer": None,
        "review_actor_type": None,
        "review_decision": None,
        "created_at": None,
        "source_count": 0,
    }


def normalize_employee_role(role: Optional[str]) -> str:
    normalized = str(role or "employee").strip().lower()
    if normalized == "superadmin":
        return "super_admin"
    return normalized or "employee"


def is_senior_employee_role(role: Optional[str]) -> bool:
    return normalize_employee_role(role) in SENIOR_REVIEW_ROLES


def is_employee_review_role(role: Optional[str]) -> bool:
    return normalize_employee_role(role) in EMPLOYEE_REVIEW_ROLES


def is_outsourced_review_role(role: Optional[str]) -> bool:
    return normalize_employee_role(role) in OUTSOURCED_REVIEW_ROLES


def checklist_passed(checklist: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(checklist, dict) or not checklist:
        return False
    return all(bool(value) for value in checklist.values())


def allowed_pdp_review_actions(
    *,
    actor_role: Optional[str],
    module_key: str,
    risk_level: str,
    requires_human: bool,
    module_status: Optional[str] = None,
) -> List[str]:
    role = normalize_employee_role(actor_role)
    risk = str(risk_level or "low").lower()
    status = str(module_status or "")
    actions = {"view", "assign", "skip", "escalate"}

    senior = is_senior_employee_role(role)
    employee = is_employee_review_role(role)
    outsourced = is_outsourced_review_role(role)
    low_risk_outsourced = (
        outsourced
        and risk == "low"
        and not requires_human
        and module_key in LOW_RISK_OUTSOURCED_MODULES
    )

    if senior:
        actions.update(
            {
                "edit_draft",
                "publish",
                "reject",
                "needs_human_review",
                "rollback",
                "override",
                "qa_sample",
                "reassign",
                "version_restore",
            }
        )
    elif employee:
        actions.update({"edit_draft", "reject", "needs_human_review", "reassign", "qa_sample"})
        if risk != "high":
            actions.add("publish")
    elif low_risk_outsourced:
        actions.update({"edit_draft", "publish", "reject", "needs_human_review"})

    if status == "published":
        actions.discard("publish")
        if senior:
            actions.add("rollback")
    return sorted(actions)


def _source_summary(source_refs: Any) -> Dict[str, Any]:
    refs = source_refs if isinstance(source_refs, list) else []
    by_type: Dict[str, int] = {}
    for ref in refs:
        ref_type = str((ref or {}).get("type") if isinstance(ref, dict) else "source_ref")
        by_type[ref_type] = by_type.get(ref_type, 0) + 1
    return {"count": len(refs), "by_type": by_type}


def _flatten_payload(value: Any, prefix: str = "$") -> Dict[str, Any]:
    if value is None or not isinstance(value, (dict, list)):
        return {prefix: value}
    if isinstance(value, list):
        if not value:
            return {prefix: []}
        merged: Dict[str, Any] = {}
        for index, item in enumerate(value):
            merged.update(_flatten_payload(item, f"{prefix}[{index}]"))
        return merged
    if not value:
        return {prefix: {}}
    merged: Dict[str, Any] = {}
    for key, item in value.items():
        merged.update(_flatten_payload(item, str(key) if prefix == "$" else f"{prefix}.{key}"))
    return merged


def _diff_summary(current_payload: Any, staged_payload: Any) -> Dict[str, Any]:
    if staged_payload is None:
        return {"changed_paths": 0, "added": 0, "removed": 0, "changed": 0}
    current_flat = _flatten_payload(current_payload or {})
    staged_flat = _flatten_payload(staged_payload or {})
    paths = sorted(set(current_flat.keys()) | set(staged_flat.keys()))
    added = removed = changed = 0
    for path in paths:
        if path not in current_flat:
            added += 1
        elif path not in staged_flat:
            removed += 1
        elif json.dumps(current_flat[path], sort_keys=True, default=str) != json.dumps(staged_flat[path], sort_keys=True, default=str):
            changed += 1
    return {"changed_paths": added + removed + changed, "added": added, "removed": removed, "changed": changed}


def _hours_since(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (_now() - dt).total_seconds() / 3600)
    except Exception:
        return None


def _serialize_review_task(row: Any) -> Dict[str, Any]:
    data = _row_dict(row)
    if not data:
        return {}
    return {
        "task_id": data.get("id"),
        "pdp_id": data.get("pdp_id"),
        "module_key": data.get("module_key"),
        "version_id": data.get("version_id"),
        "status": data.get("status") or "needs_review",
        "assignee": data.get("assignee_actor_id"),
        "assignee_role": data.get("assignee_role"),
        "priority": data.get("priority") or "normal",
        "qa_sample": bool(data.get("qa_sample")),
        "checklist": _json(data.get("checklist")) or {},
        "policy_labels": _json(data.get("policy_labels")) or [],
        "decision_tree_path": _json(data.get("decision_tree_path")) or [],
        "escalation_reason": data.get("escalation_reason"),
        "override_reason": data.get("override_reason"),
        "review_duration_ms": data.get("review_duration_ms"),
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
        "resolved_at": _iso(data.get("resolved_at")),
    }


def _synthetic_published_monitor_task(subject: Dict[str, Any], module: Dict[str, Any], risk_level: str) -> Dict[str, Any]:
    timestamp = module.get("published_at") or module.get("created_at")
    return {
        "task_id": f"published:{subject['pdp_id']}:{module['module_key']}:{module.get('version_id')}",
        "pdp_id": subject["pdp_id"],
        "module_key": module["module_key"],
        "version_id": module.get("version_id"),
        "status": "published_monitor",
        "assignee": None,
        "assignee_role": None,
        "priority": "high" if risk_level == "high" else "normal",
        "qa_sample": False,
        "checklist": {},
        "policy_labels": [],
        "decision_tree_path": [],
        "escalation_reason": None,
        "override_reason": None,
        "review_duration_ms": None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "resolved_at": timestamp,
    }


async def get_pdp_projection(
    *,
    pdp_id: Optional[str] = None,
    product_key: Optional[str] = None,
    external_seed_id: Optional[str] = None,
    market: str = DEFAULT_MARKET,
    actor_role: Optional[str] = None,
) -> Dict[str, Any]:
    subject = await resolve_pdp_subject(
        pdp_id=pdp_id,
        product_key=product_key,
        external_seed_id=external_seed_id,
        market=market,
    )
    subject_internal_offers = await _confirmed_internal_seller_offers(subject)
    subject = _subject_with_effective_internal_offers(subject, subject_internal_offers)
    await ensure_baseline_modules(subject)
    baseline_refs = (await _baseline_payloads(subject)).get("identity", ({}, []))[1]

    modules: List[Dict[str, Any]] = []
    for module_key in PDP_MODULE_KEYS:
        current = await _current_published_version(subject["pdp_id"], module_key)
        staged = await _latest_staged_version(subject["pdp_id"], module_key)
        history = await _module_history(subject["pdp_id"], module_key)
        if not current and not staged:
            empty = _empty_module_summary(module_key)
            empty["history"] = history
            empty["source_refs"] = baseline_refs
            empty["allowed_actions"] = allowed_pdp_review_actions(
                actor_role=actor_role,
                module_key=module_key,
                risk_level=empty["risk_level"],
                requires_human=bool(empty["requires_human"]),
                module_status=empty["status"],
            )
            modules.append(empty)
            continue
        active = staged or current
        modules.append(
            {
                "module_key": module_key,
                "status": (staged or current or {}).get("status") or "not_started",
                "risk_level": (active or {}).get("risk_level") or module_risk_level(module_key),
                "requires_human": bool((active or {}).get("requires_human") or module_key in HUMAN_CO_REVIEW_MODULES),
                "current": current,
                "staged": staged,
                "published_payload": current.get("published_payload") if current else None,
                "source_refs": (active or {}).get("source_refs") or [],
                "last_reviewer": (active or {}).get("last_reviewer"),
                "review_actor_type": (active or {}).get("review_actor_type"),
                "review_decision": (active or {}).get("review_decision"),
                "allowed_actions": allowed_pdp_review_actions(
                    actor_role=actor_role,
                    module_key=module_key,
                    risk_level=(active or {}).get("risk_level") or module_risk_level(module_key),
                    requires_human=bool((active or {}).get("requires_human") or module_key in HUMAN_CO_REVIEW_MODULES),
                    module_status=(staged or current or {}).get("status") or "not_started",
                ),
                "history": history,
            }
        )

    activity_rows = await database.fetch_all(
        """
        SELECT *
        FROM pdp_audit_log
        WHERE pdp_id = :pdp_id
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"pdp_id": subject["pdp_id"]},
    )
    activity = [
        {
            "id": row["id"],
            "module_key": row["module_key"],
            "action": row["action"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "details": _json(row["details"]) or {},
            "created_at": _iso(row["created_at"]),
        }
        for row in activity_rows
    ]

    return {
        "status": "success",
        "pdp": subject,
        "modules": modules,
        "published_payload": {
            module["module_key"]: module["published_payload"]
            for module in modules
            if module.get("published_payload") is not None
        },
        "activity": activity,
    }


def _offer_price(amount: Any, currency: Any) -> Dict[str, Any]:
    return {
        "amount": amount if amount not in ("", None) else None,
        "currency": str(currency or "USD").strip().upper() or "USD",
    }


def _seed_data(row: Dict[str, Any]) -> Dict[str, Any]:
    return _json_dict(row.get("seed_data"))


def _seed_title(row: Dict[str, Any]) -> Optional[str]:
    seed_data = _seed_data(row)
    return seed_data.get("title") or row.get("title") or row.get("canonical_url") or row.get("destination_url")


def _seed_brand(row: Dict[str, Any]) -> Optional[str]:
    seed_data = _seed_data(row)
    return seed_data.get("brand") or seed_data.get("vendor") or seed_data.get("merchant_display_name") or row.get("domain")


def _seed_offer_row(row: Dict[str, Any], *, match_status: str = "confirmed") -> Dict[str, Any]:
    seed_data = _seed_data(row)
    variants = seed_data.get("variants") if isinstance(seed_data.get("variants"), list) else []
    return {
        "id": row.get("id"),
        "source": "external_seed",
        "match_status": match_status,
        "external_product_id": row.get("external_product_id") or seed_data.get("external_product_id"),
        "attached_product_key": row.get("attached_product_key"),
        "attached_variant_id": row.get("attached_variant_id") or "∅",
        "market": row.get("market") or DEFAULT_MARKET,
        "tool": row.get("tool"),
        "domain": row.get("domain"),
        "title": _seed_title(row),
        "brand": _seed_brand(row),
        "image_url": seed_data.get("image_url") or row.get("image_url"),
        "price": _offer_price(row.get("price_amount") or seed_data.get("price_amount") or seed_data.get("price"), row.get("price_currency") or seed_data.get("price_currency")),
        "availability": seed_data.get("availability") or row.get("availability"),
        "variants_count": len(variants),
        "canonical_url": row.get("canonical_url"),
        "destination_url": row.get("destination_url"),
        "disclosure_text": row.get("disclosure_text") or seed_data.get("disclosure_text"),
        "updated_at": _iso(row.get("updated_at")),
        "created_at": _iso(row.get("created_at")),
    }


def _merchant_offer_row(product_key: str, merchant_id: str, platform: str, platform_product_id: str, product_data: Dict[str, Any], *, match_status: str = "confirmed") -> Dict[str, Any]:
    summary = _extract_product_summary(product_data)
    variants = summary.get("variants") if isinstance(summary.get("variants"), list) else []
    return {
        "id": product_key,
        "source": "merchant_product",
        "match_status": match_status,
        "product_key": product_key,
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "title": summary.get("title") or platform_product_id,
        "brand": summary.get("brand"),
        "image_url": summary.get("image_url"),
        "price": _offer_price(summary.get("price"), summary.get("currency")),
        "availability": summary.get("availability"),
        "variants_count": len(variants),
    }


def _merchant_offer_quality_score(offer: Dict[str, Any]) -> int:
    title = str(offer.get("title") or "").strip()
    platform_product_id = str(offer.get("platform_product_id") or "").strip()
    score = 0
    if title and title != platform_product_id:
        score += 20
    if offer.get("image_url"):
        score += 12
    price = offer.get("price") if isinstance(offer.get("price"), dict) else {}
    if price.get("amount") is not None:
        score += 4
    if int(offer.get("variants_count") or 0) > 0:
        score += 4
    if offer.get("availability") is not None:
        score += 2
    return score


def _best_merchant_offer(offers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not offers:
        return None
    return sorted(offers, key=_merchant_offer_quality_score, reverse=True)[0]


def _subject_with_effective_internal_offers(subject: Dict[str, Any], offers: List[Dict[str, Any]]) -> Dict[str, Any]:
    if subject.get("external_only") or not offers:
        return subject
    best_offer = _best_merchant_offer(offers)
    seller_count = len({offer.get("merchant_id") for offer in offers if offer.get("merchant_id")})
    updated = dict(subject)
    updated["seller_count"] = seller_count
    if best_offer:
        current_title = str(updated.get("title") or "").strip()
        if not current_title or current_title.startswith("pg:auto:title:") or current_title == str(updated.get("subject_ref") or ""):
            updated["title"] = best_offer.get("title") or current_title
        if not updated.get("image_url") and best_offer.get("image_url"):
            updated["image_url"] = best_offer.get("image_url")
        if best_offer.get("product_key"):
            updated["representative_product_key"] = best_offer.get("product_key")
    return updated


async def _subject_internal_product_keys(subject: Dict[str, Any]) -> List[str]:
    product_keys: List[str] = []
    product_group_id = subject.get("product_group_id")
    if product_group_id:
        try:
            rows = await database.fetch_all(
                """
                SELECT merchant_id, platform, platform_product_id
                FROM product_group_members
                WHERE product_group_id = :product_group_id
                ORDER BY is_primary DESC, updated_at DESC
                """,
                {"product_group_id": product_group_id},
            )
            for row in rows or []:
                data = _row_dict(row)
                product_keys.append(_product_key(data.get("merchant_id"), data.get("platform"), data.get("platform_product_id")))
        except Exception:
            logger.warning(
                "_subject_internal_product_keys query failed; treating as empty",
                exc_info=True,
            )
            product_keys = []

    representative = str(subject.get("representative_product_key") or "").strip()
    if representative and not is_external_seed_product_key(representative):
        product_keys.append(representative)

    seen = set()
    return [key for key in product_keys if key and not (key in seen or seen.add(key))]


async def _confirmed_internal_seller_offers(subject: Dict[str, Any]) -> List[Dict[str, Any]]:
    offers: List[Dict[str, Any]] = []
    product_keys = await _subject_internal_product_keys(subject)
    confirmed_product_keys = set(product_keys)
    confirmed_merchant_ids = set()
    for key in product_keys:
        try:
            merchant_id, _, _ = parse_product_key(key)
            confirmed_merchant_ids.add(merchant_id)
        except ValueError:
            continue

    for product_key in product_keys:
        try:
            merchant_id, platform, platform_product_id = parse_product_key(product_key)
        except ValueError:
            continue
        cache_row = await _fetch_latest_cache_row(merchant_id, platform, platform_product_id)
        if not cache_row:
            continue
        product_data = _json_dict(cache_row.get("product_data"))
        if not product_data:
            continue
        offer = _merchant_offer_row(product_key, merchant_id, platform, platform_product_id, product_data)
        offers.append(
            _offer_identity_signals(
                subject,
                offer,
                confirmed_product_keys=confirmed_product_keys,
                confirmed_merchant_ids=confirmed_merchant_ids,
            )
        )
    return offers


async def _confirmed_external_seed_offers(subject: Dict[str, Any], product_keys: List[str]) -> List[Dict[str, Any]]:
    clauses: List[str] = ["status = 'active'"]
    params: Dict[str, Any] = {"market": subject.get("market") or DEFAULT_MARKET}
    market_clause = "(market = :market OR market IS NULL)"
    clauses.append(market_clause)
    external_product_id = str(subject.get("external_product_id") or "").strip()
    or_clauses: List[str] = []
    if external_product_id:
        params["external_product_id"] = external_product_id
        or_clauses.append("external_product_id = :external_product_id")
    if product_keys:
        in_clause, in_params = _named_in("product_key", product_keys)
        params.update(in_params)
        or_clauses.append(f"attached_product_key IN ({in_clause})")
    if not or_clauses:
        return []
    clauses.append("(" + " OR ".join(or_clauses) + ")")
    try:
        rows = await database.fetch_all(
            f"""
            SELECT *
            FROM external_product_seeds
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 200
            """,
            params,
        )
    except Exception:
        logger.warning(
            "_confirmed_external_seed_offers query failed; treating as empty",
            exc_info=True,
        )
        return []
    confirmed_product_keys = set(product_keys)
    return [
        _offer_identity_signals(subject, _seed_offer_row(_row_dict(row)), confirmed_product_keys=confirmed_product_keys)
        for row in rows or []
    ]


async def _external_seed_near_match_candidates(subject: Dict[str, Any], exclude_seed_ids: set) -> List[Dict[str, Any]]:
    title = subject.get("title") or subject.get("subject_ref")
    raw_tokens = _tokenize_match_text(title)
    tokens = _distinctive_match_tokens(raw_tokens)[:4] or raw_tokens[:2]
    if not tokens:
        return []
    clauses = ["status = 'active'", "(market = :market OR market IS NULL)"]
    params: Dict[str, Any] = {"market": subject.get("market") or DEFAULT_MARKET}
    token_clauses: List[str] = []
    for idx, token in enumerate(tokens):
        params[f"tok_{idx}"] = f"%{token}%"
        token_clauses.append(f"LOWER(COALESCE(title, '')) LIKE :tok_{idx}")
    clauses.append("(" + " OR ".join(token_clauses) + ")")
    try:
        rows = await database.fetch_all(
            f"""
            SELECT *
            FROM external_product_seeds
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 200
            """,
            params,
        )
    except Exception:
        logger.warning(
            "_external_seed_near_match_candidates query failed; treating as empty",
            exc_info=True,
        )
        return []

    candidates: List[Dict[str, Any]] = []
    target_brand = None
    for row in rows or []:
        data = _row_dict(row)
        if data.get("id") in exclude_seed_ids:
            continue
        if subject.get("external_product_id") and data.get("external_product_id") == subject.get("external_product_id"):
            continue
        score, reasons = _score_candidate(title, _seed_title(data), target_brand, _seed_brand(data))
        if score < 0.45:
            continue
        offer = _seed_offer_row(data, match_status="candidate")
        offer.update(
            {
                "candidate_type": "external_seed_near_match",
                "confidence": round(score, 3),
                "match_reasons": reasons or ["title_similarity"],
                "recommended_action": "review_attach_external_offer_or_reject",
                "requires_human": True,
            }
        )
        candidates.append(_offer_identity_signals(subject, offer))
    candidates.sort(key=lambda item: float(item.get("confidence") or 0), reverse=True)
    return candidates[:20]


async def _merchant_product_near_match_candidates(subject: Dict[str, Any], exclude_product_keys: set) -> List[Dict[str, Any]]:
    title = subject.get("title") or subject.get("subject_ref")
    try:
        rows = await database.fetch_all(
            """
            SELECT *
            FROM products_cache
            ORDER BY cached_at DESC, id DESC
            LIMIT 500
            """
        )
    except Exception:
        logger.warning(
            "_merchant_product_near_match_candidates products_cache scan failed; treating as empty",
            exc_info=True,
        )
        return []

    candidates: List[Dict[str, Any]] = []
    target_brand = None
    confirmed_merchant_ids = set()
    for key in exclude_product_keys:
        try:
            confirmed_merchant_id, _, _ = parse_product_key(str(key))
            confirmed_merchant_ids.add(confirmed_merchant_id)
        except ValueError:
            continue
    for row in rows or []:
        data = _row_dict(row)
        product_key = _product_key(data.get("merchant_id"), data.get("platform"), data.get("platform_product_id"))
        if product_key in exclude_product_keys:
            continue
        product_data = _json_dict(data.get("product_data"))
        summary = _extract_product_summary(product_data)
        score, reasons = _score_candidate(title, summary.get("title") or data.get("platform_product_id"), target_brand, summary.get("brand"))
        if score < 0.45:
            continue
        offer = _merchant_offer_row(
            product_key,
            str(data.get("merchant_id")),
            str(data.get("platform")),
            str(data.get("platform_product_id")),
            product_data,
            match_status="candidate",
        )
        offer.update(
            {
                "candidate_type": "merchant_product_near_match",
                "confidence": round(score, 3),
                "match_reasons": reasons or ["title_similarity"],
                "recommended_action": "review_product_group_merge_or_reject",
                "requires_human": True,
            }
        )
        candidates.append(
            _offer_identity_signals(
                subject,
                offer,
                confirmed_product_keys=exclude_product_keys,
                confirmed_merchant_ids=confirmed_merchant_ids,
            )
        )
    candidates.sort(key=lambda item: float(item.get("confidence") or 0), reverse=True)
    return candidates[:20]


def _identity_candidate_ref(candidate: Dict[str, Any]) -> str:
    return str(candidate.get("product_key") or candidate.get("id") or "").strip()


def _identity_candidate_action_set(candidate: Dict[str, Any]) -> List[str]:
    candidate_type = str(candidate.get("candidate_type") or "")
    if candidate_type == "external_seed_near_match":
        return ["attach_external_offer", "reject_candidate"]
    if candidate_type == "merchant_product_near_match":
        return ["merge_product_group", "reject_candidate"]
    return ["reject_candidate"]


def _identity_candidate_source_ref(candidate: Dict[str, Any]) -> Dict[str, Any]:
    candidate_type = str(candidate.get("candidate_type") or "")
    if candidate_type == "external_seed_near_match":
        return {
            "type": "external_seed_candidate",
            "id": candidate.get("id"),
            "url": candidate.get("canonical_url") or candidate.get("destination_url"),
            "candidate_type": candidate_type,
            "confidence": candidate.get("confidence"),
        }
    return {
        "type": "merchant_product_candidate",
        "id": candidate.get("product_key") or candidate.get("id"),
        "candidate_type": candidate_type,
        "confidence": candidate.get("confidence"),
    }


async def _identity_candidate_decision_index(pdp_id: str) -> Dict[str, Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT action, actor_id, details, created_at
        FROM pdp_audit_log
        WHERE pdp_id = :pdp_id
          AND module_key = 'identity'
          AND action IN (
            'identity_candidate_task_created',
            'identity_candidate_attached',
            'identity_candidate_merged',
            'identity_candidate_rejected'
          )
        ORDER BY created_at DESC
        LIMIT 300
        """,
        {"pdp_id": pdp_id},
    )
    decisions: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        data = _row_dict(row)
        details = _json_dict(data.get("details"))
        candidate_ref = str(details.get("candidate_ref") or "").strip()
        if not candidate_ref or candidate_ref in decisions:
            continue
        action = str(data.get("action") or "")
        if action == "identity_candidate_task_created":
            status = "pending"
            decision = "needs_review"
        elif action == "identity_candidate_rejected":
            status = "rejected"
            decision = "reject"
        else:
            status = "accepted"
            decision = "pass"
        decisions[candidate_ref] = {
            "status": status,
            "decision": decision,
            "task_id": details.get("task_id"),
            "action": action,
            "candidate_type": details.get("candidate_type"),
            "candidate_ref": candidate_ref,
            "actor_id": data.get("actor_id"),
            "created_at": _iso(data.get("created_at")),
        }
    return decisions


def _candidate_with_identity_review_state(
    candidate: Dict[str, Any],
    *,
    decisions: Dict[str, Dict[str, Any]],
    actor_role: Optional[str],
) -> Optional[Dict[str, Any]]:
    candidate_ref = _identity_candidate_ref(candidate)
    decision = decisions.get(candidate_ref)
    if decision and decision.get("status") in {"accepted", "rejected"}:
        return None
    allowed_actions: List[str] = []
    if is_employee_review_role(actor_role):
        allowed_actions.append("create_identity_review_task")
    return {
        **candidate,
        "identity_review": decision,
        "allowed_actions": allowed_actions,
    }


async def _find_offer_reconciliation_candidate(
    *,
    subject: Dict[str, Any],
    candidate_type: str,
    candidate_ref: str,
) -> Dict[str, Any]:
    internal_offers = await _confirmed_internal_seller_offers(subject)
    product_keys = [str(offer.get("product_key")) for offer in internal_offers if offer.get("product_key")]
    external_offers = await _confirmed_external_seed_offers(subject, product_keys)
    exclude_seed_ids = {offer.get("id") for offer in external_offers if offer.get("id")}
    exclude_product_keys = {offer.get("product_key") for offer in internal_offers if offer.get("product_key")}
    candidates = [
        *await _external_seed_near_match_candidates(subject, exclude_seed_ids),
        *await _merchant_product_near_match_candidates(subject, exclude_product_keys),
    ]
    for candidate in candidates:
        if str(candidate.get("candidate_type") or "") == candidate_type and _identity_candidate_ref(candidate) == candidate_ref:
            return candidate
    raise LookupError("PDP_IDENTITY_CANDIDATE_NOT_FOUND")


async def _existing_identity_candidate_task(
    *,
    subject: Dict[str, Any],
    candidate_type: str,
    candidate_ref: str,
) -> Optional[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT *
        FROM pdp_module_versions
        WHERE pdp_id = :pdp_id
          AND module_key = 'identity'
          AND stage = 'staged'
          AND superseded_at IS NULL
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"pdp_id": subject["pdp_id"]},
    )
    for row in rows or []:
        module = _serialize_module(row)
        if str(module.get("status") or "") in {"reject", "rejected"}:
            continue
        payload = _json_dict(module.get("payload"))
        review = _json_dict(payload.get("identity_review"))
        if (
            review.get("status") == "pending"
            and review.get("candidate_type") == candidate_type
            and review.get("candidate_ref") == candidate_ref
        ):
            module_with_version = {**module, "version_id": module.get("id")}
            task = await _ensure_review_task_for_module(subject, module_with_version)
            return {"module": module, "task": task}
    return None


async def _create_identity_candidate_task_from_candidate(
    *,
    subject: Dict[str, Any],
    candidate_type: str,
    candidate_ref: str,
    candidate: Dict[str, Any],
    notes: Optional[str],
    actor_role: Optional[str],
    actor_id: Optional[str],
    created_from: str,
) -> Dict[str, Any]:
    current_identity = await _current_published_version(subject["pdp_id"], "identity")
    current_payload = _json_dict((current_identity or {}).get("payload"))
    candidate = {**candidate, "candidate_type": candidate_type}
    source_refs = merge_source_refs(
        (current_identity or {}).get("source_refs") or [],
        [_identity_candidate_source_ref(candidate)],
    )
    payload = {
        **current_payload,
        "identity_review": {
            "status": "pending",
            "candidate_type": candidate_type,
            "candidate_ref": candidate_ref,
            "candidate": candidate,
            "candidate_confidence": candidate.get("confidence") or candidate.get("identity_confidence"),
            "match_reasons": candidate.get("match_reasons") or [],
            "available_actions": _identity_candidate_action_set(candidate),
            "created_from": created_from,
            "created_by_actor_id": actor_id,
            "notes": notes,
        },
    }
    module = await create_module_draft(
        pdp_id=subject["pdp_id"],
        module_key="identity",
        payload=payload,
        source_refs=source_refs,
        generated_by="identity_candidate_evidence",
        generation_ref=f"{candidate_type}:{candidate_ref}",
        actor_type=REVIEW_ACTOR_HUMAN,
        actor_id=actor_id,
        actor_role=actor_role,
    )
    task = await _ensure_review_task_for_module(subject, {**module, "version_id": module.get("id")})
    await _audit(
        pdp_id=subject["pdp_id"],
        module_key="identity",
        action="identity_candidate_task_created",
        actor_type=REVIEW_ACTOR_HUMAN,
        actor_id=actor_id,
        details={
            "task_id": (task or {}).get("task_id"),
            "version_id": module.get("id"),
            "candidate_type": candidate_type,
            "candidate_ref": candidate_ref,
            "candidate": candidate,
            "notes": notes,
            "created_from": created_from,
        },
    )
    return {"status": "success", "created": True, "module": module, "task": task}


async def create_pdp_identity_review_task(
    *,
    pdp_id: str,
    candidate_type: str,
    candidate_ref: str,
    notes: Optional[str] = None,
    actor_role: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    if not is_employee_review_role(actor_role):
        raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
    subject = await resolve_pdp_subject(pdp_id=pdp_id)
    normalized_type = str(candidate_type or "").strip()
    normalized_ref = str(candidate_ref or "").strip()
    if normalized_type not in {"external_seed_near_match", "merchant_product_near_match"} or not normalized_ref:
        raise ValueError("INVALID_PDP_IDENTITY_CANDIDATE")

    decisions = await _identity_candidate_decision_index(subject["pdp_id"])
    prior = decisions.get(normalized_ref)
    if prior and prior.get("status") in {"accepted", "rejected"}:
        raise ValueError("PDP_IDENTITY_CANDIDATE_ALREADY_RESOLVED")

    existing = await _existing_identity_candidate_task(
        subject=subject,
        candidate_type=normalized_type,
        candidate_ref=normalized_ref,
    )
    if existing:
        task = existing.get("task") or {}
        if task.get("task_id") and not (prior or {}).get("task_id"):
            await _audit(
                pdp_id=subject["pdp_id"],
                module_key="identity",
                action="identity_candidate_task_created",
                actor_type=REVIEW_ACTOR_HUMAN,
                actor_id=actor_id,
                details={
                    "task_id": task.get("task_id"),
                    "version_id": (existing.get("module") or {}).get("id"),
                    "candidate_type": normalized_type,
                    "candidate_ref": normalized_ref,
                    "recovered": True,
                    "notes": notes,
                },
            )
        return {"status": "success", "created": False, **existing}

    candidate = await _find_offer_reconciliation_candidate(
        subject=subject,
        candidate_type=normalized_type,
        candidate_ref=normalized_ref,
    )
    return await _create_identity_candidate_task_from_candidate(
        subject=subject,
        candidate_type=normalized_type,
        candidate_ref=normalized_ref,
        candidate=candidate,
        notes=notes,
        actor_role=actor_role,
        actor_id=actor_id,
        created_from="offer_reconciliation_candidate",
    )


async def _primary_product_key_for_group(product_group_id: Optional[str]) -> Optional[str]:
    if not product_group_id:
        return None
    try:
        row = await database.fetch_one(
            """
            SELECT merchant_id, platform, platform_product_id
            FROM product_group_members
            WHERE product_group_id = :product_group_id
            ORDER BY is_primary DESC, updated_at DESC
            LIMIT 1
            """,
            {"product_group_id": product_group_id},
        )
    except Exception:
        logger.warning(
            "_primary_product_key_for_group query failed for product_group_id=%s",
            product_group_id,
            exc_info=True,
        )
        return None
    data = _row_dict(row)
    if not data:
        return None
    return _product_key(data.get("merchant_id"), data.get("platform"), data.get("platform_product_id"))


async def _apply_external_candidate_attach(subject: Dict[str, Any], candidate: Dict[str, Any], target_product_key: Optional[str] = None) -> Dict[str, Any]:
    seed_id = str(candidate.get("id") or "").strip()
    if not seed_id:
        raise ValueError("PDP_IDENTITY_CANDIDATE_NOT_FOUND")
    seed = await _fetch_external_seed_by_id(seed_id)
    if not seed:
        raise LookupError("EXTERNAL_SEED_NOT_FOUND")

    attached_product_key = (target_product_key or "").strip()
    if not attached_product_key and subject.get("product_group_id"):
        attached_product_key = (
            str(subject.get("representative_product_key") or "").strip()
            if not is_external_seed_product_key(str(subject.get("representative_product_key") or ""))
            else ""
        )
        attached_product_key = attached_product_key or (await _primary_product_key_for_group(str(subject.get("product_group_id")))) or ""

    if attached_product_key:
        await database.execute(
            """
            UPDATE external_product_seeds
            SET attached_product_key = :attached_product_key,
                attached_variant_id = :attached_variant_id,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :seed_id
            """,
            {
                "seed_id": seed_id,
                "attached_product_key": attached_product_key,
                "attached_variant_id": str(candidate.get("attached_variant_id") or "∅"),
            },
        )
        return {"attached_seed_id": seed_id, "attached_product_key": attached_product_key}

    external_product_id = str(subject.get("external_product_id") or "").strip()
    if not external_product_id:
        raise ValueError("PDP_IDENTITY_ATTACH_REQUIRES_PRODUCT_KEY")
    await database.execute(
        """
        UPDATE external_product_seeds
        SET external_product_id = :external_product_id,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :seed_id
        """,
        {"seed_id": seed_id, "external_product_id": external_product_id},
    )
    return {"attached_seed_id": seed_id, "external_product_id": external_product_id}


async def _upsert_product_group_member(product_group_id: str, merchant_id: str, platform: str, platform_product_id: str) -> None:
    try:
        await database.execute(
            """
            INSERT INTO product_group_members (
              product_group_id, merchant_id, platform, platform_product_id, is_primary, updated_at
            ) VALUES (
              :product_group_id, :merchant_id, :platform, :platform_product_id, FALSE, CURRENT_TIMESTAMP
            )
            ON CONFLICT (merchant_id, platform, platform_product_id)
            DO UPDATE SET
              product_group_id = EXCLUDED.product_group_id,
              updated_at = CURRENT_TIMESTAMP
            """,
            {
                "product_group_id": product_group_id,
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
            },
        )
    except Exception:
        existing = await database.fetch_one(
            """
            SELECT 1 AS ok
            FROM product_group_members
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND platform_product_id = :platform_product_id
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
            },
        )
        if existing:
            await database.execute(
                """
                UPDATE product_group_members
                SET product_group_id = :product_group_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE merchant_id = :merchant_id
                  AND platform = :platform
                  AND platform_product_id = :platform_product_id
                """,
                {
                    "product_group_id": product_group_id,
                    "merchant_id": merchant_id,
                    "platform": platform,
                    "platform_product_id": platform_product_id,
                },
            )
        else:
            await database.execute(
                """
                INSERT INTO product_group_members (
                  product_group_id, merchant_id, platform, platform_product_id, is_primary, updated_at
                ) VALUES (
                  :product_group_id, :merchant_id, :platform, :platform_product_id, FALSE, CURRENT_TIMESTAMP
                )
                """,
                {
                    "product_group_id": product_group_id,
                    "merchant_id": merchant_id,
                    "platform": platform,
                    "platform_product_id": platform_product_id,
                },
            )


async def _product_group_member_exists(product_group_id: str, product_key: str) -> bool:
    merchant_id, platform, platform_product_id = parse_product_key(product_key)
    row = await database.fetch_one(
        """
        SELECT 1 AS ok
        FROM product_group_members
        WHERE product_group_id = :product_group_id
          AND merchant_id = :merchant_id
          AND platform = :platform
          AND platform_product_id = :platform_product_id
        LIMIT 1
        """,
        {
            "product_group_id": product_group_id,
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
        },
    )
    return bool(row)


async def _product_group_member_product_keys(product_group_id: str) -> List[str]:
    rows = await database.fetch_all(
        """
        SELECT merchant_id, platform, platform_product_id
        FROM product_group_members
        WHERE product_group_id = :product_group_id
        ORDER BY is_primary DESC, updated_at DESC
        """,
        {"product_group_id": product_group_id},
    )
    keys: List[str] = []
    for row in rows or []:
        data = _row_dict(row)
        key = _product_key(data.get("merchant_id"), data.get("platform"), data.get("platform_product_id"))
        if key not in keys:
            keys.append(key)
    return keys


async def _remove_product_group_member(product_group_id: str, product_key: str) -> None:
    merchant_id, platform, platform_product_id = parse_product_key(product_key)
    await database.execute(
        """
        DELETE FROM product_group_members
        WHERE product_group_id = :product_group_id
          AND merchant_id = :merchant_id
          AND platform = :platform
          AND platform_product_id = :platform_product_id
        """,
        {
            "product_group_id": product_group_id,
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
        },
    )


async def _set_product_group_primary(product_group_id: str, product_key: str) -> None:
    if not await _product_group_member_exists(product_group_id, product_key):
        raise LookupError("PDP_PRODUCT_GROUP_MEMBER_NOT_FOUND")
    merchant_id, platform, platform_product_id = parse_product_key(product_key)
    await database.execute(
        """
        UPDATE product_group_members
        SET is_primary = FALSE,
            updated_at = CURRENT_TIMESTAMP
        WHERE product_group_id = :product_group_id
        """,
        {"product_group_id": product_group_id},
    )
    await database.execute(
        """
        UPDATE product_group_members
        SET is_primary = TRUE,
            updated_at = CURRENT_TIMESTAMP
        WHERE product_group_id = :product_group_id
          AND merchant_id = :merchant_id
          AND platform = :platform
          AND platform_product_id = :platform_product_id
        """,
        {
            "product_group_id": product_group_id,
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
        },
    )


async def _product_summary_for_key(product_key: str) -> Dict[str, Any]:
    merchant_id, platform, platform_product_id = parse_product_key(product_key)
    row = await _fetch_latest_cache_row(merchant_id, platform, platform_product_id)
    if not row:
        raise LookupError("PDP_IDENTITY_CORRECTION_PRODUCT_NOT_LIVE")
    product_data = _json_dict(row.get("product_data"))
    if not product_data:
        raise LookupError("PDP_IDENTITY_CORRECTION_PRODUCT_NOT_LIVE")
    return _extract_product_summary(product_data)


async def _refresh_subject_after_product_group_correction(
    subject: Dict[str, Any],
    *,
    primary_product_key: Optional[str],
) -> Dict[str, Any]:
    product_group_id = str(subject.get("product_group_id") or "").strip()
    primary_key = str(primary_product_key or "").strip() or await _primary_product_key_for_group(product_group_id)
    refreshed = dict(subject)
    if primary_key:
        refreshed["representative_product_key"] = primary_key
        try:
            primary_summary = await _product_summary_for_key(primary_key)
        except Exception:
            logger.warning(
                "_product_summary_for_key failed in subject refresh for primary_key=%s",
                primary_key,
                exc_info=True,
            )
            primary_summary = {}
        if primary_summary.get("title"):
            refreshed["title"] = primary_summary.get("title")
        if primary_summary.get("image_url"):
            refreshed["image_url"] = primary_summary.get("image_url")

    internal_offers = await _confirmed_internal_seller_offers(refreshed)
    refreshed = _subject_with_effective_internal_offers(refreshed, internal_offers)
    if primary_key:
        refreshed["representative_product_key"] = primary_key
    return await _upsert_subject(refreshed)


async def _publish_identity_correction_projection(
    subject: Dict[str, Any],
    *,
    actor_role: Optional[str],
    actor_id: Optional[str],
    reason: str,
    policy_labels: List[str],
    checklist: Optional[Dict[str, Any]],
    decision_tree_path: Optional[List[str]],
    override_reason: Optional[str],
    correction_details: Dict[str, Any],
) -> List[Dict[str, Any]]:
    payloads = await _baseline_payloads(subject)
    correction_ref = {
        "type": "employee_identity_correction",
        "product_group_id": subject.get("product_group_id"),
        "actor_id": actor_id,
        "reason": reason,
    }
    always_refresh_modules = {"identity", "offers"}
    system_refresh_modules = {"copy", "variants", "external_sources", "quality"}
    published_modules: List[Dict[str, Any]] = []
    for module_key in ("identity", "copy", "variants", "offers", "external_sources", "quality"):
        current = await _current_published_version(subject["pdp_id"], module_key)
        if module_key in system_refresh_modules and current:
            generated_by = str(current.get("generated_by") or "")
            if generated_by not in {"system_baseline", "employee_identity_correction"}:
                continue
        if module_key not in always_refresh_modules and module_key not in payloads:
            continue
        payload, source_refs = payloads[module_key]
        payload = {
            **payload,
            "last_identity_correction": {
                "reason": reason,
                "policy_labels": policy_labels,
                **correction_details,
            },
        }
        module = await create_module_draft(
            pdp_id=subject["pdp_id"],
            module_key=module_key,
            payload=payload,
            source_refs=merge_source_refs(source_refs, [correction_ref]),
            generated_by="employee_identity_correction",
            generation_ref=str(correction_details.get("correction_id") or ""),
            actor_type=REVIEW_ACTOR_HUMAN,
            actor_id=actor_id,
            actor_role=actor_role,
        )
        reviewed = await review_module_version(
            pdp_id=subject["pdp_id"],
            module_key=module_key,
            version_id=module["id"],
            actor_type=REVIEW_ACTOR_HUMAN,
            actor_id=actor_id,
            actor_role=actor_role,
            decision="pass",
            notes=reason,
            checklist=checklist or {"identity_corrected": True, "source_grounded": True},
            policy_labels=policy_labels,
            decision_tree_path=decision_tree_path or ["identity", "product_group_correction", module_key, "publish"],
            override_reason=override_reason,
        )
        published_modules.append(reviewed["module"])
    return published_modules


async def correct_pdp_product_group_membership(
    *,
    pdp_id: str,
    add_product_key: Optional[str] = None,
    remove_product_keys: Optional[List[str]] = None,
    set_primary_product_key: Optional[str] = None,
    reason: Optional[str] = None,
    policy_labels: Optional[List[str]] = None,
    checklist: Optional[Dict[str, Any]] = None,
    decision_tree_path: Optional[List[str]] = None,
    override_reason: Optional[str] = None,
    actor_role: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    if not is_senior_employee_role(actor_role):
        raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise ValueError("PDP_IDENTITY_CORRECTION_REASON_REQUIRED")
    normalized_policy_labels = [str(label).strip() for label in (policy_labels or []) if str(label).strip()]
    if not normalized_policy_labels:
        raise ValueError("PDP_IDENTITY_CORRECTION_POLICY_LABEL_REQUIRED")

    subject = await resolve_pdp_subject(pdp_id=pdp_id)
    product_group_id = str(subject.get("product_group_id") or "").strip()
    if not product_group_id:
        raise ValueError("PDP_PRODUCT_GROUP_REQUIRED")

    add_key = str(add_product_key or "").strip()
    primary_key = str(set_primary_product_key or add_key or "").strip()
    remove_keys = [str(key).strip() for key in (remove_product_keys or []) if str(key).strip()]
    if not add_key and not primary_key and not remove_keys:
        raise ValueError("PDP_IDENTITY_CORRECTION_TARGET_REQUIRED")

    for key in [key for key in [add_key, primary_key, *remove_keys] if key]:
        parse_product_key(key)
    if primary_key and primary_key in set(remove_keys):
        raise ValueError("PDP_IDENTITY_CORRECTION_PRIMARY_REMOVED")
    current_member_keys = await _product_group_member_product_keys(product_group_id)
    future_member_keys = set(current_member_keys) - set(remove_keys)
    if add_key:
        future_member_keys.add(add_key)
    if not future_member_keys:
        raise ValueError("PDP_IDENTITY_CORRECTION_TARGET_REQUIRED")
    if not primary_key:
        current_primary_key = await _primary_product_key_for_group(product_group_id)
        primary_key = current_primary_key if current_primary_key in future_member_keys else sorted(future_member_keys)[0]
    if add_key:
        await _product_summary_for_key(add_key)
    if primary_key and primary_key != add_key:
        await _product_summary_for_key(primary_key)

    correction_id = f"pdpident_{uuid.uuid4().hex}"
    async with database.transaction():
        if add_key:
            merchant_id, platform, platform_product_id = parse_product_key(add_key)
            await _upsert_product_group_member(product_group_id, merchant_id, platform, platform_product_id)
        for remove_key in remove_keys:
            await _remove_product_group_member(product_group_id, remove_key)
        if primary_key:
            await _set_product_group_primary(product_group_id, primary_key)

    refreshed_subject = await _refresh_subject_after_product_group_correction(subject, primary_product_key=primary_key)
    correction_details = {
        "correction_id": correction_id,
        "product_group_id": product_group_id,
        "added_product_key": add_key or None,
        "removed_product_keys": remove_keys,
        "primary_product_key": primary_key or refreshed_subject.get("representative_product_key"),
        "actor_role": normalize_employee_role(actor_role),
    }
    published_modules = await _publish_identity_correction_projection(
        refreshed_subject,
        actor_role=actor_role,
        actor_id=actor_id,
        reason=normalized_reason,
        policy_labels=normalized_policy_labels,
        checklist=checklist,
        decision_tree_path=decision_tree_path,
        override_reason=override_reason,
        correction_details=correction_details,
    )
    await _audit(
        pdp_id=refreshed_subject["pdp_id"],
        module_key="identity",
        action="identity_product_group_corrected",
        actor_type=REVIEW_ACTOR_HUMAN,
        actor_id=actor_id,
        details={
            **correction_details,
            "reason": normalized_reason,
            "policy_labels": normalized_policy_labels,
            "checklist": checklist if isinstance(checklist, dict) else {},
            "decision_tree_path": decision_tree_path if isinstance(decision_tree_path, list) else [],
            "override_reason": override_reason,
        },
    )
    return {
        "status": "success",
        "correction": correction_details,
        "pdp": refreshed_subject,
        "published_modules": published_modules,
        "reconciliation": await get_pdp_offer_reconciliation(pdp_id=refreshed_subject["pdp_id"], actor_role=actor_role),
    }


async def _apply_merchant_candidate_merge(subject: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    product_key = str(candidate.get("product_key") or candidate.get("id") or "").strip()
    merchant_id, platform, platform_product_id = parse_product_key(product_key)
    existing_group = await _resolve_product_group(merchant_id, platform, platform_product_id)
    product_group_id = str(subject.get("product_group_id") or (existing_group or {}).get("product_group_id") or "").strip()
    if not product_group_id:
        product_group_id = f"pg_manual_{hashlib.sha256(product_key.encode('utf-8')).hexdigest()[:16]}"
    await _upsert_product_group_member(product_group_id, merchant_id, platform, platform_product_id)

    if subject.get("external_product_id"):
        await database.execute(
            """
            UPDATE external_product_seeds
            SET attached_product_key = :product_key,
                attached_variant_id = COALESCE(attached_variant_id, '∅'),
                updated_at = CURRENT_TIMESTAMP
            WHERE external_product_id = :external_product_id
              AND status = 'active'
            """,
            {"product_key": product_key, "external_product_id": subject.get("external_product_id")},
        )

    try:
        merged_subject = await _subject_from_product_key(product_key, subject.get("market") or DEFAULT_MARKET)
    except Exception:
        logger.warning(
            "_subject_from_product_key failed in candidate merge for product_key=%s; falling back to synthesized subject",
            product_key,
            exc_info=True,
        )
        merged_subject = {
            "pdp_id": make_pdp_id("product_group", product_group_id, subject.get("market") or DEFAULT_MARKET),
            "subject_type": "product_group",
            "subject_ref": product_group_id,
            "market": subject.get("market") or DEFAULT_MARKET,
            "product_group_id": product_group_id,
            "external_product_id": None,
            "representative_product_key": product_key,
            "seller_count": 1,
        }
        merged_subject = await _upsert_subject(merged_subject)
    return {
        "merged_product_key": product_key,
        "product_group_id": product_group_id,
        "target_pdp_id": merged_subject.get("pdp_id"),
    }


def _audit_snapshot_candidate(review: Dict[str, Any], candidate_type: str, candidate_ref: str) -> Dict[str, Any]:
    pools = {
        "merchant_product_near_match": review.get("merchant_candidates") or [],
        "external_seed_near_match": review.get("external_candidates") or [],
    }
    for candidate in pools.get(candidate_type, []) or []:
        candidate_dict = _json_dict(candidate)
        candidate_dict["candidate_type"] = str(candidate_dict.get("candidate_type") or candidate_type)
        if _identity_candidate_ref(candidate_dict) == candidate_ref:
            return candidate_dict
    raise LookupError("PDP_IDENTITY_CANDIDATE_NOT_FOUND")


async def _apply_product_group_identity_audit_action(
    *,
    task: Dict[str, Any],
    version: Dict[str, Any],
    payload: Dict[str, Any],
    review: Dict[str, Any],
    action: str,
    notes: Optional[str],
    reason: Optional[str],
    checklist: Optional[Dict[str, Any]],
    policy_labels: Optional[List[str]],
    decision_tree_path: Optional[List[str]],
    review_duration_ms: Optional[int],
    override_reason: Optional[str],
    candidate_type: Optional[str],
    candidate_ref: Optional[str],
    actor_role: Optional[str],
    actor_id: Optional[str],
) -> Dict[str, Any]:
    if action not in {"create_identity_candidate_task", "reject_staged_audit"}:
        raise ValueError("PDP_IDENTITY_ACTION_NOT_ALLOWED_FOR_CANDIDATE")
    normalized_policy_labels = [str(label).strip() for label in (policy_labels or []) if str(label).strip()]
    if not normalized_policy_labels:
        raise ValueError("PDP_REVIEW_POLICY_LABEL_REQUIRED")

    subject = await resolve_pdp_subject(pdp_id=task["pdp_id"])
    action_result: Dict[str, Any] = {}
    audit_action = "identity_audit_rejected"
    resolved_status = "rejected"
    review_decision = "reject"
    module_status = "rejected"

    if action == "create_identity_candidate_task":
        normalized_type = str(candidate_type or "").strip()
        normalized_ref = str(candidate_ref or "").strip()
        if normalized_type not in {"external_seed_near_match", "merchant_product_near_match"} or not normalized_ref:
            raise ValueError("INVALID_PDP_IDENTITY_CANDIDATE")
        decisions = await _identity_candidate_decision_index(subject["pdp_id"])
        prior = decisions.get(normalized_ref)
        if prior and prior.get("status") in {"accepted", "rejected"}:
            raise ValueError("PDP_IDENTITY_CANDIDATE_ALREADY_RESOLVED")
        existing = await _existing_identity_candidate_task(
            subject=subject,
            candidate_type=normalized_type,
            candidate_ref=normalized_ref,
        )
        if existing:
            action_result = {"created": False, **existing}
        else:
            candidate = _audit_snapshot_candidate(review, normalized_type, normalized_ref)
            action_result = await _create_identity_candidate_task_from_candidate(
                subject=subject,
                candidate_type=normalized_type,
                candidate_ref=normalized_ref,
                candidate=candidate,
                notes=notes,
                actor_role=actor_role,
                actor_id=actor_id,
                created_from="product_group_identity_audit",
            )
        audit_action = "identity_audit_candidate_task_created"
        resolved_status = "converted"
        review_decision = "needs_human_review"
        module_status = "resolved"
    elif not (reason or notes or "").strip():
        raise ValueError("PDP_REVIEW_REJECTION_REASON_REQUIRED")

    now = _now()
    review_payload = {
        **review,
        "status": resolved_status,
        "decision": review_decision,
        "applied_action": action,
        "action_result": action_result,
        "reviewed_by_actor_id": actor_id,
        "reviewed_at": _iso(now),
        "notes": notes,
        "reason": reason,
        "checklist": checklist if isinstance(checklist, dict) else {},
        "policy_labels": normalized_policy_labels,
        "decision_tree_path": decision_tree_path if isinstance(decision_tree_path, list) else [],
        "review_duration_ms": review_duration_ms,
        "override_reason": override_reason,
    }
    next_payload = {**payload, "identity_review": review_payload}
    rubric = {
        "decision": review_decision,
        "identity_action": action,
        "notes": notes,
        "reason": reason,
        "checklist": checklist if isinstance(checklist, dict) else {},
        "policy_labels": normalized_policy_labels,
        "decision_tree_path": decision_tree_path if isinstance(decision_tree_path, list) else [],
        "review_duration_ms": review_duration_ms,
        "override_reason": override_reason,
        "action_result": action_result,
    }
    await database.execute(
        pdp_module_versions.update()
        .where(pdp_module_versions.c.id == version["id"])
        .values(
            status=module_status,
            payload=next_payload,
            review_actor_type=REVIEW_ACTOR_HUMAN,
            review_actor_id=actor_id,
            review_decision=review_decision,
            review_confidence=None,
            review_rubric=rubric,
            risk_level=module_risk_level("identity", next_payload),
            requires_human=module_requires_human_review("identity", next_payload),
        )
    )
    await database.execute(
        pdp_review_tasks.update()
        .where(pdp_review_tasks.c.id == task["task_id"])
        .values(
            status="resolved",
            checklist=rubric["checklist"],
            policy_labels=normalized_policy_labels,
            decision_tree_path=rubric["decision_tree_path"],
            escalation_reason=reason if action == "reject_staged_audit" else None,
            override_reason=override_reason,
            review_duration_ms=review_duration_ms,
            updated_at=now,
            resolved_at=now,
        )
    )
    await _audit(
        pdp_id=task["pdp_id"],
        module_key="identity",
        action=audit_action,
        actor_type=REVIEW_ACTOR_HUMAN,
        actor_id=actor_id,
        details={
            "task_id": task["task_id"],
            "version_id": version["id"],
            "identity_action": action,
            "decision": review_decision,
            "notes": notes,
            "reason": reason,
            "policy_labels": normalized_policy_labels,
            "action_result": action_result,
        },
    )
    return {
        "status": "success",
        "decision": review_decision,
        "identity_action": action,
        "action_result": action_result,
        "task": await _review_task_by_id(task["task_id"]),
        "module": await _fetch_module_version(task["pdp_id"], "identity", version["id"]),
    }


async def apply_pdp_identity_review_action(
    *,
    task_id: str,
    action: str,
    notes: Optional[str] = None,
    reason: Optional[str] = None,
    checklist: Optional[Dict[str, Any]] = None,
    policy_labels: Optional[List[str]] = None,
    decision_tree_path: Optional[List[str]] = None,
    review_duration_ms: Optional[int] = None,
    override_reason: Optional[str] = None,
    target_product_key: Optional[str] = None,
    candidate_type: Optional[str] = None,
    candidate_ref: Optional[str] = None,
    actor_role: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    if not is_employee_review_role(actor_role):
        raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
    normalized_action = str(action or "").strip()
    if normalized_action not in {"attach_external_offer", "merge_product_group", "reject_candidate", "create_identity_candidate_task", "reject_staged_audit"}:
        raise ValueError("INVALID_PDP_IDENTITY_ACTION")
    task = await _review_task_by_id(task_id)
    if task.get("module_key") != "identity" or not task.get("version_id"):
        raise ValueError("PDP_IDENTITY_TASK_REQUIRED")
    version = await _fetch_module_version(task["pdp_id"], "identity", task.get("version_id"))
    payload = _json_dict(version.get("payload"))
    review = _json_dict(payload.get("identity_review"))
    if review.get("status") != "pending":
        raise ValueError("PDP_IDENTITY_TASK_ALREADY_RESOLVED")
    if review.get("candidate_type") == "product_group_identity_audit":
        return await _apply_product_group_identity_audit_action(
            task=task,
            version=version,
            payload=payload,
            review=review,
            action=normalized_action,
            notes=notes,
            reason=reason,
            checklist=checklist,
            policy_labels=policy_labels,
            decision_tree_path=decision_tree_path,
            review_duration_ms=review_duration_ms,
            override_reason=override_reason,
            candidate_type=candidate_type,
            candidate_ref=candidate_ref,
            actor_role=actor_role,
            actor_id=actor_id,
        )
    candidate = _json_dict(review.get("candidate"))
    candidate_type = str(review.get("candidate_type") or candidate.get("candidate_type") or "")
    candidate_ref = str(review.get("candidate_ref") or _identity_candidate_ref(candidate))
    if normalized_action not in _identity_candidate_action_set({"candidate_type": candidate_type}):
        raise ValueError("PDP_IDENTITY_ACTION_NOT_ALLOWED_FOR_CANDIDATE")
    if not policy_labels:
        raise ValueError("PDP_REVIEW_POLICY_LABEL_REQUIRED")
    if normalized_action != "reject_candidate" and not checklist_passed(checklist):
        raise ValueError("PDP_REVIEW_CHECKLIST_REQUIRED")
    if normalized_action == "reject_candidate" and not (reason or notes or "").strip():
        raise ValueError("PDP_REVIEW_REJECTION_REASON_REQUIRED")

    subject = await resolve_pdp_subject(pdp_id=task["pdp_id"])
    action_result: Dict[str, Any] = {}
    if normalized_action == "attach_external_offer":
        action_result = await _apply_external_candidate_attach(subject, candidate, target_product_key=target_product_key)
        audit_action = "identity_candidate_attached"
        review_decision = "pass"
        resolved_status = "accepted"
    elif normalized_action == "merge_product_group":
        action_result = await _apply_merchant_candidate_merge(subject, candidate)
        audit_action = "identity_candidate_merged"
        review_decision = "pass"
        resolved_status = "accepted"
    else:
        audit_action = "identity_candidate_rejected"
        review_decision = "reject"
        resolved_status = "rejected"

    now = _now()
    review_payload = {
        **review,
        "status": resolved_status,
        "decision": review_decision,
        "applied_action": normalized_action,
        "action_result": action_result,
        "reviewed_by_actor_id": actor_id,
        "reviewed_at": _iso(now),
        "notes": notes,
        "reason": reason,
        "checklist": checklist if isinstance(checklist, dict) else {},
        "policy_labels": policy_labels if isinstance(policy_labels, list) else [],
        "decision_tree_path": decision_tree_path if isinstance(decision_tree_path, list) else [],
        "review_duration_ms": review_duration_ms,
        "override_reason": override_reason,
    }
    next_payload = {**payload, "identity_review": review_payload}
    rubric = {
        "decision": review_decision,
        "identity_action": normalized_action,
        "notes": notes,
        "reason": reason,
        "checklist": checklist if isinstance(checklist, dict) else {},
        "policy_labels": policy_labels if isinstance(policy_labels, list) else [],
        "decision_tree_path": decision_tree_path if isinstance(decision_tree_path, list) else [],
        "review_duration_ms": review_duration_ms,
        "override_reason": override_reason,
        "action_result": action_result,
    }
    await database.execute(
        pdp_module_versions.update()
        .where(pdp_module_versions.c.id == version["id"])
        .values(
            status="approved" if review_decision == "pass" else "rejected",
            payload=next_payload,
            review_actor_type=REVIEW_ACTOR_HUMAN,
            review_actor_id=actor_id,
            review_decision=review_decision,
            review_confidence=1.0 if review_decision == "pass" else None,
            review_rubric=rubric,
            risk_level=module_risk_level("identity", next_payload),
            requires_human=module_requires_human_review("identity", next_payload),
        )
    )
    await database.execute(
        pdp_review_tasks.update()
        .where(pdp_review_tasks.c.id == task_id)
        .values(
            status="resolved",
            checklist=rubric["checklist"],
            policy_labels=rubric["policy_labels"],
            decision_tree_path=rubric["decision_tree_path"],
            override_reason=override_reason,
            review_duration_ms=review_duration_ms,
            updated_at=now,
            resolved_at=now,
        )
    )
    await _audit(
        pdp_id=task["pdp_id"],
        module_key="identity",
        action=audit_action,
        actor_type=REVIEW_ACTOR_HUMAN,
        actor_id=actor_id,
        details={
            "task_id": task_id,
            "version_id": version["id"],
            "candidate_type": candidate_type,
            "candidate_ref": candidate_ref,
            "identity_action": normalized_action,
            "decision": review_decision,
            "notes": notes,
            "reason": reason,
            "policy_labels": rubric["policy_labels"],
            "action_result": action_result,
        },
    )
    return {
        "status": "success",
        "decision": review_decision,
        "identity_action": normalized_action,
        "action_result": action_result,
        "task": await _review_task_by_id(task_id),
        "module": await _fetch_module_version(task["pdp_id"], "identity", version["id"]),
    }


async def get_pdp_offer_reconciliation(
    *,
    pdp_id: str,
    actor_role: Optional[str] = None,
) -> Dict[str, Any]:
    subject = await resolve_pdp_subject(pdp_id=pdp_id)
    indexed_seller_count = int(subject.get("seller_count") or 0)
    internal_offers = await _confirmed_internal_seller_offers(subject)
    subject = _subject_with_effective_internal_offers(subject, internal_offers)
    product_keys = [str(offer.get("product_key")) for offer in internal_offers if offer.get("product_key")]
    external_offers = await _confirmed_external_seed_offers(subject, product_keys)
    exclude_seed_ids = {offer.get("id") for offer in external_offers if offer.get("id")}
    exclude_product_keys = {offer.get("product_key") for offer in internal_offers if offer.get("product_key")}
    decisions = await _identity_candidate_decision_index(subject["pdp_id"])
    external_candidates = [
        candidate
        for candidate in (
            _candidate_with_identity_review_state(candidate, decisions=decisions, actor_role=actor_role)
            for candidate in await _external_seed_near_match_candidates(subject, exclude_seed_ids)
        )
        if candidate is not None
    ]
    merchant_candidates = [
        candidate
        for candidate in (
            _candidate_with_identity_review_state(candidate, decisions=decisions, actor_role=actor_role)
            for candidate in await _merchant_product_near_match_candidates(subject, exclude_product_keys)
        )
        if candidate is not None
    ]

    seller_count = len({offer.get("merchant_id") for offer in internal_offers if offer.get("merchant_id")})
    candidate_count = len(external_candidates) + len(merchant_candidates)
    return {
        "status": "success",
        "pdp": subject,
        "summary": {
            "seller_count": seller_count,
            "pdp_index_seller_count": indexed_seller_count,
            "external_only": bool(subject.get("external_only")),
            "confirmed_internal_seller_count": seller_count,
            "confirmed_external_offer_count": len(external_offers),
            "confirmed_offer_count": len(internal_offers) + len(external_offers),
            "near_match_candidate_count": candidate_count,
            "needs_identity_review": candidate_count > 0,
            "product_group_id": subject.get("product_group_id"),
            "external_product_id": subject.get("external_product_id"),
        },
        "confirmed": {
            "internal_sellers": internal_offers,
            "external_offers": external_offers,
        },
        "candidates": {
            "merchant_products": merchant_candidates,
            "external_seeds": external_candidates,
        },
        "review_guidance": [
            "Confirmed internal sellers come from product_group_members for the PDP product_group_id.",
            "Confirmed external offers come from attached external seeds or matching external_product_id.",
            "Near-match candidates are evidence only until employee/senior identity review attaches, merges, or rejects them.",
        ],
        "allowed_actions": allowed_pdp_review_actions(
            actor_role=actor_role,
            module_key="offers",
            risk_level="medium" if candidate_count else "low",
            requires_human=bool(candidate_count),
            module_status="needs_human_review" if candidate_count else "published",
        ),
    }


def _identity_audit_report(reconciliation: Dict[str, Any]) -> Dict[str, Any]:
    summary = reconciliation.get("summary") if isinstance(reconciliation.get("summary"), dict) else {}
    confirmed = reconciliation.get("confirmed") if isinstance(reconciliation.get("confirmed"), dict) else {}
    candidates = reconciliation.get("candidates") if isinstance(reconciliation.get("candidates"), dict) else {}
    internal_sellers = confirmed.get("internal_sellers") if isinstance(confirmed.get("internal_sellers"), list) else []
    external_offers = confirmed.get("external_offers") if isinstance(confirmed.get("external_offers"), list) else []
    merchant_candidates = candidates.get("merchant_products") if isinstance(candidates.get("merchant_products"), list) else []
    external_candidates = candidates.get("external_seeds") if isinstance(candidates.get("external_seeds"), list) else []
    risk_flags: List[str] = []

    indexed_count = int(summary.get("pdp_index_seller_count") or 0)
    confirmed_count = int(summary.get("confirmed_internal_seller_count") or 0)
    candidate_count = int(summary.get("near_match_candidate_count") or 0)
    if indexed_count != confirmed_count:
        _append_unique(risk_flags, "seller_count_index_live_mismatch")
    if indexed_count and confirmed_count == 0:
        _append_unique(risk_flags, "no_live_confirmed_seller_for_indexed_group")
    if candidate_count:
        _append_unique(risk_flags, "near_match_candidate_present")
    if str(summary.get("product_group_id") or "").startswith("pg:auto:title:") and candidate_count:
        _append_unique(risk_flags, "auto_title_group_has_near_matches")

    for offer in [*internal_sellers, *external_offers]:
        for flag in offer.get("risk_flags") or []:
            _append_unique(risk_flags, f"confirmed:{flag}")
        confidence = offer.get("identity_confidence")
        try:
            if confidence is not None and float(confidence) < 0.75:
                _append_unique(risk_flags, "confirmed_low_identity_confidence")
        except Exception:
            pass

    for candidate in [*merchant_candidates, *external_candidates]:
        for flag in candidate.get("risk_flags") or []:
            _append_unique(risk_flags, f"candidate:{flag}")
        if candidate.get("verification_status") == "suggested_match":
            _append_unique(risk_flags, "suggested_identity_match_needs_review")

    def compact_offer(offer: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "source": offer.get("source"),
            "match_status": offer.get("match_status"),
            "product_key": offer.get("product_key"),
            "id": offer.get("id"),
            "external_product_id": offer.get("external_product_id"),
            "merchant_id": offer.get("merchant_id"),
            "title": offer.get("title"),
            "verification_status": offer.get("verification_status"),
            "identity_confidence": offer.get("identity_confidence"),
            "risk_flags": offer.get("risk_flags") or [],
            "match_reasons": offer.get("match_reasons") or [],
        }

    return {
        "risk_flags": risk_flags,
        "summary": summary,
        "confirmed_internal_sellers": [compact_offer(offer) for offer in internal_sellers[:10]],
        "confirmed_external_offers": [compact_offer(offer) for offer in external_offers[:10]],
        "merchant_candidates": [compact_offer(candidate) for candidate in merchant_candidates[:10]],
        "external_candidates": [compact_offer(candidate) for candidate in external_candidates[:10]],
    }


async def _existing_product_group_identity_audit_task(subject: Dict[str, Any], audit_ref: str) -> Optional[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT *
        FROM pdp_module_versions
        WHERE pdp_id = :pdp_id
          AND module_key = 'identity'
          AND stage = 'staged'
          AND superseded_at IS NULL
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"pdp_id": subject["pdp_id"]},
    )
    for row in rows or []:
        module = _serialize_module(row)
        if str(module.get("status") or "") in {"reject", "rejected"}:
            continue
        payload = _json_dict(module.get("payload"))
        review = _json_dict(payload.get("identity_review"))
        if (
            review.get("status") in {"pending", "converted"}
            and review.get("candidate_type") == "product_group_identity_audit"
            and str(review.get("candidate_ref") or "") == audit_ref
        ):
            task = await _ensure_review_task_for_module(subject, {**module, "version_id": module.get("id")})
            return {"module": module, "task": task}
    return None


async def _create_product_group_identity_audit_task(
    *,
    subject: Dict[str, Any],
    report: Dict[str, Any],
    actor_type: str,
    actor_id: Optional[str],
    actor_role: Optional[str],
) -> Dict[str, Any]:
    current_identity = await _current_published_version(subject["pdp_id"], "identity")
    current_payload = _json_dict((current_identity or {}).get("payload"))
    audit_ref = str(subject.get("product_group_id") or subject.get("pdp_id") or "").strip()
    source_refs = merge_source_refs(
        (current_identity or {}).get("source_refs") or [],
        [
            {
                "type": "pdp_identity_audit",
                "id": audit_ref,
                "pdp_id": subject.get("pdp_id"),
                "product_group_id": subject.get("product_group_id"),
            }
        ],
    )
    payload = {
        **current_payload,
        "identity_review": {
            "status": "pending",
            "candidate_type": "product_group_identity_audit",
            "candidate_ref": audit_ref,
            "audit_summary": report.get("summary") or {},
            "risk_flags": report.get("risk_flags") or [],
            "confirmed_internal_sellers": report.get("confirmed_internal_sellers") or [],
            "confirmed_external_offers": report.get("confirmed_external_offers") or [],
            "merchant_candidates": report.get("merchant_candidates") or [],
            "external_candidates": report.get("external_candidates") or [],
            "available_actions": [
                "open_pdp_detail",
                "create_identity_candidate_task",
                "product_group_correction",
                "reject_staged_audit",
            ],
            "created_from": "identity_audit_job",
            "created_by_actor_id": actor_id,
        },
    }
    module = await create_module_draft(
        pdp_id=subject["pdp_id"],
        module_key="identity",
        payload=payload,
        source_refs=source_refs,
        generated_by="identity_audit_job",
        generation_ref=f"product_group_identity_audit:{audit_ref}",
        actor_type=actor_type,
        actor_id=actor_id,
        actor_role=actor_role,
    )
    task = await _ensure_review_task_for_module(subject, {**module, "version_id": module.get("id")})
    await _audit(
        pdp_id=subject["pdp_id"],
        module_key="identity",
        action="identity_audit_task_created",
        actor_type=actor_type,
        actor_id=actor_id,
        details={
            "task_id": (task or {}).get("task_id"),
            "version_id": module.get("id"),
            "audit_ref": audit_ref,
            "risk_flags": report.get("risk_flags") or [],
            "summary": report.get("summary") or {},
        },
    )
    return {"module": module, "task": task}


async def audit_pdp_identity_groups(
    *,
    limit: int = 250,
    actor_type: str = REVIEW_ACTOR_SYSTEM,
    actor_id: Optional[str] = "identity_audit_job",
    actor_role: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    if actor_type == REVIEW_ACTOR_HUMAN and not is_employee_review_role(actor_role):
        raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
    safe_limit = max(1, min(int(limit or 250), 5000))
    rows = await database.fetch_all(
        f"""
        SELECT *
        FROM pdp_subject_index
        WHERE status = 'active'
          AND subject_type = 'product_group'
          AND product_group_id IS NOT NULL
        ORDER BY updated_at DESC, created_at DESC
        {_sql_limit_clause(safe_limit)}
        """,
        _sql_limit_params(safe_limit),
    )
    scanned = 0
    created: List[Dict[str, Any]] = []
    existing: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    risk_counts: Dict[str, int] = {}

    for row in rows or []:
        subject = _serialize_subject(_row_dict(row))
        if not subject:
            continue
        scanned += 1
        try:
            await ensure_baseline_modules(subject)
            reconciliation = await get_pdp_offer_reconciliation(pdp_id=subject["pdp_id"], actor_role=actor_role)
            report = _identity_audit_report(reconciliation)
            risk_flags = [str(flag) for flag in report.get("risk_flags") or [] if str(flag)]
            for flag in risk_flags:
                risk_counts[flag] = risk_counts.get(flag, 0) + 1
            audit_ref = str(subject.get("product_group_id") or subject.get("pdp_id") or "").strip()
            if not risk_flags:
                skipped.append({"pdp_id": subject["pdp_id"], "product_group_id": subject.get("product_group_id"), "reason": "no_identity_risk_flags"})
                continue
            existing_task = await _existing_product_group_identity_audit_task(subject, audit_ref)
            if existing_task:
                existing.append(
                    {
                        "pdp_id": subject["pdp_id"],
                        "product_group_id": subject.get("product_group_id"),
                        "task_id": (existing_task.get("task") or {}).get("task_id"),
                        "risk_flags": risk_flags,
                    }
                )
                continue
            created_task = await _create_product_group_identity_audit_task(
                subject=subject,
                report=report,
                actor_type=actor_type,
                actor_id=actor_id,
                actor_role=actor_role,
            )
            created.append(
                {
                    "pdp_id": subject["pdp_id"],
                    "product_group_id": subject.get("product_group_id"),
                    "task_id": (created_task.get("task") or {}).get("task_id"),
                    "risk_flags": risk_flags,
                }
            )
        except Exception as exc:
            skipped.append({"pdp_id": subject.get("pdp_id"), "product_group_id": subject.get("product_group_id"), "reason": str(exc)[:120]})

    return {
        "status": "success",
        "scanned": scanned,
        "created_count": len(created),
        "existing_count": len(existing),
        "skipped_count": len(skipped),
        "created": created,
        "existing": existing,
        "skipped": skipped[:100],
        "risk_counts": risk_counts,
    }


def _initial_review_task_status(module_status: str) -> str:
    if module_status == "published":
        return "published_monitor"
    if module_status in {"approved", "draft", "needs_human_review"}:
        return "needs_review"
    if module_status in {"reject", "rejected"}:
        return "resolved"
    return "needs_review"


async def _ensure_review_task_for_module(subject: Dict[str, Any], module: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    version_id = module.get("version_id")
    if not version_id:
        return None
    existing = await database.fetch_one(
        """
        SELECT *
        FROM pdp_review_tasks
        WHERE pdp_id = :pdp_id
          AND module_key = :module_key
          AND version_id = :version_id
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"pdp_id": subject["pdp_id"], "module_key": module["module_key"], "version_id": version_id},
    )
    if existing:
        return _serialize_review_task(existing)

    now = _now()
    task = {
        "id": f"pdptask_{uuid.uuid4().hex}",
        "pdp_id": subject["pdp_id"],
        "module_key": module["module_key"],
        "version_id": version_id,
        "status": _initial_review_task_status(str(module.get("status") or "")),
        "assignee_actor_id": None,
        "assignee_role": None,
        "priority": "high" if module.get("risk_level") == "high" else "normal",
        "qa_sample": False,
        "checklist": {},
        "policy_labels": [],
        "decision_tree_path": [],
        "escalation_reason": None,
        "override_reason": None,
        "review_duration_ms": None,
        "created_at": now,
        "updated_at": now,
        "resolved_at": now if str(module.get("status")) == "published" else None,
    }
    await database.execute(pdp_review_tasks.insert().values(**task))
    return _serialize_review_task(task)


async def _review_task_by_id(task_id: str) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    row = await database.fetch_one(
        "SELECT * FROM pdp_review_tasks WHERE id = :task_id LIMIT 1",
        {"task_id": task_id},
    )
    task = _serialize_review_task(row)
    if not task:
        raise LookupError("PDP_REVIEW_TASK_NOT_FOUND")
    return task


async def _build_review_queue_item(
    *,
    subject: Dict[str, Any],
    module: Dict[str, Any],
    actor_role: Optional[str],
    ensure_task: bool = True,
) -> Optional[Dict[str, Any]]:
    if "current_payload" in module or "staged_payload" in module:
        current = {"payload": module.get("current_payload") or {}}
        staged = {"payload": module.get("staged_payload") or {}} if module.get("staged_payload") is not None else None
        active = {
            "payload": module.get("staged_payload") if module.get("staged_payload") is not None else module.get("current_payload") or {},
            "source_refs": module.get("source_refs") or [],
            "risk_level": module.get("risk_level"),
            "requires_human": module.get("requires_human"),
            "status": module.get("status"),
        }
    else:
        current = await _current_published_version(subject["pdp_id"], module["module_key"])
        staged = await _latest_staged_version(subject["pdp_id"], module["module_key"])
        active = staged or current or {}
    refs = active.get("source_refs") or []
    risk = module.get("risk_level") or active.get("risk_level") or module_risk_level(module["module_key"])
    requires_human = bool(module.get("requires_human") or active.get("requires_human") or module["module_key"] in HUMAN_CO_REVIEW_MODULES)
    module_status = module.get("status") or active.get("status") or "not_started"
    task = await _ensure_review_task_for_module(subject, module) if ensure_task else _synthetic_published_monitor_task(subject, module, str(risk))
    if not task:
        return None
    allowed_actions = allowed_pdp_review_actions(
        actor_role=actor_role,
        module_key=module["module_key"],
        risk_level=str(risk),
        requires_human=requires_human,
        module_status=module_status,
    )
    active_payload = _json_dict((active or {}).get("payload"))
    identity_review = _json_dict(active_payload.get("identity_review"))
    if (
        module["module_key"] == "identity"
        and identity_review.get("status") == "pending"
        and is_employee_review_role(actor_role)
    ):
        if identity_review.get("candidate_type") == "product_group_identity_audit":
            identity_actions = {"create_identity_candidate_task", "reject_staged_audit"}
        else:
            identity_actions = set(_identity_candidate_action_set({"candidate_type": identity_review.get("candidate_type")}))
        allowed_actions = sorted(
            set(allowed_actions)
            | identity_actions
        )
    if not ensure_task:
        allowed_actions = [action for action in allowed_actions if action in {"view", "rollback"}]
    return {
        **task,
        "pdp_title": subject.get("title") or subject.get("subject_ref"),
        "pdp_image_url": subject.get("image_url"),
        "subject_type": subject.get("subject_type"),
        "subject_ref": subject.get("subject_ref"),
        "market": subject.get("market"),
        "external_only": bool(subject.get("external_only")),
        "seller_count": int(subject.get("seller_count") or 0),
        "risk_level": risk,
        "requires_human": requires_human,
        "module_status": module_status,
        "review_actor_type": module.get("review_actor_type"),
        "review_decision": module.get("review_decision"),
        "sla_age_hours": _hours_since(task.get("created_at") or module.get("created_at")),
        "source_summary": _source_summary(refs),
        "diff_summary": _diff_summary((current or {}).get("payload"), (staged or {}).get("payload") if staged else None),
        "risk_reasons": _review_risk_reasons(module["module_key"], risk, requires_human, (active or {}).get("payload")),
        "allowed_actions": allowed_actions,
    }


def _review_risk_reasons(module_key: str, risk_level: str, requires_human: bool, payload: Any = None) -> List[str]:
    reasons: List[str] = []
    if requires_human:
        reasons.append("human_co_review_required")
    if module_key in HIGH_RISK_REVIEW_MODULES:
        reasons.append(f"{module_key}_high_risk_module")
    if str(risk_level) == "high":
        reasons.append("high_risk")
    text = _text_blob(payload).lower()
    for key in HIGH_RISK_PAYLOAD_KEYS:
        if key in text and f"payload:{key}" not in reasons:
            reasons.append(f"payload:{key}")
    return reasons[:6]


def _queue_item_matches_tab(item: Dict[str, Any], tab: str, actor_id: Optional[str]) -> bool:
    status = str(item.get("status") or "")
    source_types = set(((item.get("source_summary") or {}).get("by_type") or {}).keys())
    if tab == "my_queue":
        return bool(actor_id and item.get("assignee") == actor_id)
    if tab == "publish_ready":
        return "publish" in (item.get("allowed_actions") or []) and item.get("module_status") != "published"
    if tab == "escalated":
        return status == "escalated"
    if tab == "senior_review":
        return status == "escalated" or bool(item.get("requires_human")) or item.get("risk_level") == "high"
    if tab == "qa_sample":
        return bool(item.get("qa_sample"))
    if tab == "published_monitor":
        return item.get("module_status") == "published" or status == "published_monitor"
    if tab == "identity_audit":
        return (
            item.get("module_key") == "identity"
            and "pdp_identity_audit" in source_types
            and status in {"needs_review", "assigned", "escalated"}
            and item.get("module_status") != "published"
        )
    return status in {"needs_review", "assigned"} and item.get("module_status") != "published"


async def list_pdp_review_queue(
    *,
    actor_role: Optional[str],
    actor_id: Optional[str],
    tab: str = "needs_review",
    module_key: Optional[str] = None,
    risk: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    external_only: Optional[bool] = None,
    market: Optional[str] = None,
    seller_count: Optional[str] = None,
    source_type: Optional[str] = None,
    last_reviewer: Optional[str] = None,
    staleness: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    selected_tab = tab if tab in PDP_REVIEW_QUEUE_TABS else "needs_review"

    clauses = ["1 = 1"]
    params: Dict[str, Any] = {}
    if external_only is not None:
        clauses.append("external_only = :external_only")
        params["external_only"] = external_only
    if market:
        clauses.append("market = :market")
        params["market"] = market.strip().upper()

    subject_rows = await database.fetch_all(
        f"""
        SELECT *
        FROM pdp_subject_index
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC
        LIMIT :limit
        """,
        {**params, "limit": 500},
    )
    subjects = [_serialize_subject(_row_dict(row)) for row in subject_rows]
    module_summaries_by_pdp = await _list_module_summaries_for_pdp_ids([str(subject["pdp_id"]) for subject in subjects])

    all_items: List[Dict[str, Any]] = []
    for subject in subjects:
        modules = module_summaries_by_pdp.get(str(subject["pdp_id"])) or _empty_module_summaries()
        for module in modules:
            if not module.get("version_id"):
                continue
            if module_key and module.get("module_key") != module_key:
                continue
            if risk and module.get("risk_level") != risk:
                continue
            module_status = str(module.get("status") or "")
            if (
                selected_tab != "published_monitor"
                and module_status == "published"
                and not module.get("has_staged")
            ):
                continue
            if selected_tab == "published_monitor":
                if not module.get("has_current"):
                    continue
                module = {
                    **module,
                    "version_id": module.get("current_version_id") or module.get("version_id"),
                    "status": "published",
                    "risk_level": module.get("current_risk_level") or module.get("risk_level"),
                    "requires_human": bool(module.get("current_requires_human") or module.get("module_key") in HUMAN_CO_REVIEW_MODULES),
                    "review_actor_type": module.get("current_review_actor_type"),
                    "review_decision": module.get("current_review_decision"),
                    "created_at": module.get("current_created_at") or module.get("created_at"),
                    "source_refs": module.get("current_source_refs") or [],
                    "staged_payload": None,
                }
            item = await _build_review_queue_item(
                subject=subject,
                module=module,
                actor_role=actor_role,
                ensure_task=selected_tab != "published_monitor",
            )
            if not item:
                continue
            if status and item.get("status") != status and item.get("module_status") != status:
                continue
            if assignee and item.get("assignee") != assignee:
                continue
            if seller_count:
                sellers = int(item.get("seller_count") or 0)
                if seller_count == "external_only" and sellers != 0:
                    continue
                if seller_count == "single" and sellers != 1:
                    continue
                if seller_count == "multi" and sellers < 2:
                    continue
            if source_type and source_type not in (item.get("source_summary") or {}).get("by_type", {}):
                continue
            if last_reviewer:
                reviewer_match = {
                    str(item.get("review_actor_type") or ""),
                    str(item.get("review_decision") or ""),
                    str(item.get("assignee_role") or ""),
                    str(item.get("assignee") or ""),
                }
                if last_reviewer not in reviewer_match:
                    continue
            if priority and item.get("priority") != priority:
                continue
            if staleness:
                age = float(item.get("sla_age_hours") or 0.0)
                if staleness == "fresh" and age >= 24:
                    continue
                if staleness == "over_24h" and age < 24:
                    continue
                if staleness == "over_72h" and age < 72:
                    continue
            if not _queue_item_matches_tab(item, selected_tab, actor_id):
                continue
            all_items.append(item)

    all_items.sort(
        key=lambda item: (
            0 if item.get("risk_level") == "high" else 1 if item.get("risk_level") == "medium" else 2,
            -(float(item.get("sla_age_hours") or 0.0)),
            str(item.get("pdp_title") or ""),
        )
    )
    items = all_items[safe_offset : safe_offset + safe_limit]
    summary = {
        "tasks": len(all_items),
        "needs_review": sum(1 for item in all_items if item.get("status") in {"needs_review", "assigned"}),
        "publish_ready": sum(1 for item in all_items if "publish" in (item.get("allowed_actions") or []) and item.get("module_status") != "published"),
        "high_risk": sum(1 for item in all_items if item.get("risk_level") == "high"),
        "escalated": sum(1 for item in all_items if item.get("status") == "escalated"),
        "identity_audit": sum(1 for item in all_items if item.get("module_key") == "identity" and "pdp_identity_audit" in ((item.get("source_summary") or {}).get("by_type") or {})),
        "qa_sample": sum(1 for item in all_items if item.get("qa_sample")),
        "gpt55_reviewed": sum(1 for item in all_items if item.get("review_actor_type") == REVIEW_ACTOR_GPT55),
    }
    return {
        "status": "success",
        "tab": selected_tab,
        "items": items,
        "summary": summary,
        "count": len(items),
        "total": len(all_items),
        "limit": safe_limit,
        "offset": safe_offset,
        "next_offset": safe_offset + len(items),
        "has_more": safe_offset + len(items) < len(all_items),
        "scanned_subjects": len(subjects),
    }


async def get_pdp_review_task(
    *,
    task_id: str,
    actor_role: Optional[str],
) -> Dict[str, Any]:
    if task_id.startswith("published:"):
        try:
            _, pdp_id, module_key, version_id = task_id.split(":", 3)
        except ValueError as exc:
            raise LookupError("PDP_REVIEW_TASK_NOT_FOUND") from exc
        projection = await get_pdp_projection(pdp_id=pdp_id, actor_role=actor_role)
        module = next((mod for mod in projection.get("modules", []) if mod.get("module_key") == module_key), None)
        current = (module or {}).get("current") or {}
        if not module or not current:
            raise LookupError("PDP_REVIEW_TASK_NOT_FOUND")
        if version_id and current.get("id") and version_id != current.get("id"):
            raise LookupError("PDP_REVIEW_TASK_NOT_FOUND")
        item = await _build_review_queue_item(
            subject=projection["pdp"],
            module={
                "module_key": module_key,
                "version_id": current.get("id"),
                "status": "published",
                "risk_level": current.get("risk_level") or module_risk_level(module_key),
                "requires_human": bool(current.get("requires_human") or module_key in HUMAN_CO_REVIEW_MODULES),
                "review_actor_type": current.get("review_actor_type"),
                "review_decision": current.get("review_decision"),
                "created_at": current.get("created_at"),
                "published_at": current.get("published_at"),
                "source_refs": current.get("source_refs") or [],
                "current_payload": current.get("payload") or {},
                "staged_payload": None,
            },
            actor_role=actor_role,
            ensure_task=False,
        )
        return {
            "status": "success",
            "task": item,
            "pdp": projection["pdp"],
            "module": module,
            "published_payload": projection.get("published_payload") or {},
            "activity": projection.get("activity") or [],
        }

    task = await _review_task_by_id(task_id)
    projection = await get_pdp_projection(pdp_id=task["pdp_id"], actor_role=actor_role)
    module = next((mod for mod in projection.get("modules", []) if mod.get("module_key") == task["module_key"]), None)
    if not module:
        raise LookupError("PDP_REVIEW_TASK_NOT_FOUND")
    item = await _build_review_queue_item(subject=projection["pdp"], module={**module, "version_id": task.get("version_id") or module.get("staged", {}).get("id") or module.get("current", {}).get("id")}, actor_role=actor_role)
    return {"status": "success", "task": item or task, "pdp": projection["pdp"], "module": module, "published_payload": projection.get("published_payload") or {}, "activity": projection.get("activity") or []}


async def assign_pdp_review_task(
    *,
    task_id: str,
    assignee_actor_id: Optional[str],
    assignee_role: Optional[str],
    actor_role: Optional[str],
    actor_id: Optional[str],
) -> Dict[str, Any]:
    task = await _review_task_by_id(task_id)
    allowed = allowed_pdp_review_actions(actor_role=actor_role, module_key=task["module_key"], risk_level="low", requires_human=False)
    if "assign" not in allowed:
        raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
    now = _now()
    assignee = assignee_actor_id or actor_id
    await database.execute(
        pdp_review_tasks.update()
        .where(pdp_review_tasks.c.id == task_id)
        .values(
            status="assigned",
            assignee_actor_id=assignee,
            assignee_role=normalize_employee_role(assignee_role or actor_role),
            updated_at=now,
        )
    )
    await _audit(
        pdp_id=task["pdp_id"],
        module_key=task["module_key"],
        action="review_task_assigned",
        actor_type=REVIEW_ACTOR_HUMAN,
        actor_id=actor_id,
        details={"task_id": task_id, "assignee": assignee},
    )
    return {"status": "success", "task": await _review_task_by_id(task_id)}


async def update_pdp_review_task_status(
    *,
    task_id: str,
    next_status: str,
    actor_role: Optional[str],
    actor_id: Optional[str],
    reason: Optional[str] = None,
    qa_sample: Optional[bool] = None,
) -> Dict[str, Any]:
    task = await _review_task_by_id(task_id)
    if next_status not in {"needs_review", "assigned", "escalated", "skipped", "qa_sample", "resolved"}:
        raise ValueError("INVALID_PDP_REVIEW_TASK_STATUS")
    if next_status == "escalated" and not (reason or "").strip():
        raise ValueError("PDP_REVIEW_ESCALATION_REASON_REQUIRED")
    if next_status == "qa_sample" and not is_senior_employee_role(actor_role) and not is_employee_review_role(actor_role):
        raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
    now = _now()
    values: Dict[str, Any] = {
        "status": next_status,
        "updated_at": now,
        "resolved_at": now if next_status in {"skipped", "resolved"} else None,
    }
    if next_status == "escalated":
        values["escalation_reason"] = (reason or "").strip()
    if qa_sample is not None or next_status == "qa_sample":
        values["qa_sample"] = True if qa_sample is None else bool(qa_sample)
    await database.execute(pdp_review_tasks.update().where(pdp_review_tasks.c.id == task_id).values(**values))
    await _audit(
        pdp_id=task["pdp_id"],
        module_key=task["module_key"],
        action=f"review_task_{next_status}",
        actor_type=REVIEW_ACTOR_HUMAN,
        actor_id=actor_id,
        details={"task_id": task_id, "reason": reason},
    )
    return {"status": "success", "task": await _review_task_by_id(task_id)}


async def list_pdp_subjects(
    *,
    module_status: Optional[str] = None,
    review_actor: Optional[str] = None,
    risk: Optional[str] = None,
    external_only: Optional[bool] = None,
    market: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    clauses = ["1 = 1"]
    params: Dict[str, Any] = {}
    if external_only is not None:
        clauses.append("external_only = :external_only")
        params["external_only"] = external_only
    if market:
        clauses.append("market = :market")
        params["market"] = market.strip().upper()

    count_row = await database.fetch_one(
        f"""
        SELECT COUNT(*) AS total
        FROM pdp_subject_index
        WHERE {' AND '.join(clauses)}
        """,
        params,
    )
    total = int((_row_dict(count_row)).get("total") or 0)

    module_filters = bool(module_status or review_actor or risk)
    batch_size = safe_limit if not module_filters else min(max(safe_limit * 2, 50), 200)
    cursor = safe_offset
    items: List[Dict[str, Any]] = []
    scanned = 0

    while len(items) < safe_limit and cursor < total:
        query_params = {**params, "limit": batch_size, "offset": cursor}
        rows = await database.fetch_all(
            f"""
        SELECT *
        FROM pdp_subject_index
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC
        LIMIT :limit
        OFFSET :offset
        """,
            query_params,
        )
        if not rows:
            break
        cursor += len(rows)
        scanned += len(rows)
        subjects = [_serialize_subject(_row_dict(row)) for row in rows]
        module_summaries_by_pdp = await _list_module_summaries_for_pdp_ids(
            [str(subject["pdp_id"]) for subject in subjects if subject.get("pdp_id")]
        )
        for subject in subjects:
            modules = module_summaries_by_pdp.get(str(subject["pdp_id"])) or _empty_module_summaries()
            if module_status and not any(module.get("status") == module_status for module in modules):
                continue
            if review_actor and not any(module.get("review_actor_type") == review_actor for module in modules):
                continue
            if risk and not any(module.get("risk_level") == risk for module in modules):
                continue
            items.append(
                {
                    **subject,
                    "modules": [
                        {
                            "module_key": module["module_key"],
                            "status": module["status"],
                            "risk_level": module["risk_level"],
                            "requires_human": module["requires_human"],
                            "review_actor_type": module["review_actor_type"],
                            "review_decision": module["review_decision"],
                        }
                        for module in modules
                    ],
                }
            )
            if len(items) >= safe_limit:
                break

    return {
        "status": "success",
        "items": items,
        "count": len(items),
        "limit": safe_limit,
        "offset": safe_offset,
        "next_offset": cursor,
        "has_more": cursor < total,
        "total": total,
        "scanned": scanned,
    }


async def get_pdp_subject_index_stats() -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    totals_row = await database.fetch_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN external_only THEN 1 ELSE 0 END) AS external_only,
               SUM(CASE WHEN external_only THEN 0 ELSE 1 END) AS internal_or_multi_merchant,
               MAX(updated_at) AS last_updated_at
        FROM pdp_subject_index
        """
    )
    market_rows = await database.fetch_all(
        """
        SELECT market,
               COUNT(*) AS total,
               SUM(CASE WHEN external_only THEN 1 ELSE 0 END) AS external_only,
               SUM(CASE WHEN external_only THEN 0 ELSE 1 END) AS internal_or_multi_merchant
        FROM pdp_subject_index
        GROUP BY market
        ORDER BY COUNT(*) DESC, market ASC
        """
    )
    totals = _row_dict(totals_row)
    return {
        "status": "success",
        "total": int(totals.get("total") or 0),
        "external_only": int(totals.get("external_only") or 0),
        "internal_or_multi_merchant": int(totals.get("internal_or_multi_merchant") or 0),
        "last_updated_at": _iso(totals.get("last_updated_at")),
        "markets": [
            {
                "market": row_data.get("market") or DEFAULT_MARKET,
                "total": int(row_data.get("total") or 0),
                "external_only": int(row_data.get("external_only") or 0),
                "internal_or_multi_merchant": int(row_data.get("internal_or_multi_merchant") or 0),
            }
            for row_data in (_row_dict(row) for row in market_rows or [])
        ],
    }


def _normalize_hydration_limit(limit: Optional[int], default: int = 500) -> Optional[int]:
    raw_limit = default if limit is None else int(limit)
    if raw_limit <= 0:
        return None
    return max(1, min(raw_limit, 50000))


def _sql_limit_clause(limit: Optional[int]) -> str:
    return "LIMIT :limit" if limit is not None else ""


def _sql_limit_params(limit: Optional[int]) -> Dict[str, int]:
    return {"limit": int(limit)} if limit is not None else {}


async def hydrate_pdp_subject_index(
    *,
    limit: int = 500,
    actor_type: str = REVIEW_ACTOR_SYSTEM,
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    safe_limit = _normalize_hydration_limit(limit)
    before = await get_pdp_subject_index_stats()
    await seed_recent_pdp_subjects(limit=0 if safe_limit is None else safe_limit)
    after = await get_pdp_subject_index_stats()
    await _audit(
        pdp_id="pdp_subject_index",
        module_key=None,
        action="pdp_subject_index_hydrated",
        actor_type=actor_type,
        actor_id=actor_id,
        details={
            "limit": safe_limit,
            "limit_mode": "all" if safe_limit is None else "per_source_cap",
            "before_total": before.get("total"),
            "after_total": after.get("total"),
            "delta_total": int(after.get("total") or 0) - int(before.get("total") or 0),
        },
    )
    return {
        "status": "success",
        "limit": safe_limit,
        "limit_mode": "all" if safe_limit is None else "per_source_cap",
        "before": before,
        "after": after,
        "delta_total": int(after.get("total") or 0) - int(before.get("total") or 0),
    }


def _pdp_gallery_bucket() -> str:
    return _first_env("PDP_GALLERY_S3_BUCKET", "PHOTO_UPLOAD_BUCKET", "S3_BUCKET", "AWS_S3_BUCKET", default="")


def _pdp_gallery_prefix() -> str:
    return _first_env("PDP_GALLERY_S3_PREFIX", default="pdp-gallery").strip().strip("/")


def _pdp_gallery_public_base_url() -> str:
    return _first_env("PDP_GALLERY_PUBLIC_BASE_URL", "PHOTO_UPLOAD_PUBLIC_BASE_URL", "S3_PUBLIC_BASE_URL", default="").rstrip("/")


def _pdp_gallery_asset_public_base_url() -> str:
    return _first_env(
        "PDP_GALLERY_ASSET_PUBLIC_BASE_URL",
        "PUBLIC_API_BASE_URL",
        "PUBLIC_BASE_URL",
        default=resolve_public_api_base_url(),
    ).rstrip("/")


def _pdp_gallery_s3_endpoint_url() -> Optional[str]:
    value = _first_env("PDP_GALLERY_S3_ENDPOINT_URL", "PHOTO_UPLOAD_ENDPOINT_URL", "AWS_ENDPOINT_URL", "S3_ENDPOINT_URL", default="")
    return value or None


def _pdp_gallery_s3_region() -> Optional[str]:
    return _first_env("PDP_GALLERY_S3_REGION", "PHOTO_UPLOAD_REGION", "AWS_REGION", "AWS_DEFAULT_REGION", default="") or None


def _pdp_gallery_s3_client():
    try:
        import boto3
        from botocore.client import Config
    except Exception:
        return None

    endpoint_url = _pdp_gallery_s3_endpoint_url()
    endpoint_lc = (endpoint_url or "").lower()
    is_r2 = bool(endpoint_url and ("cloudflarestorage.com" in endpoint_lc or ".r2." in endpoint_lc))

    try:
        config_kwargs: Dict[str, Any] = {"signature_version": "s3v4"}
        if endpoint_url:
            config_kwargs["s3"] = {"addressing_style": "path"}

        access_key_id = _first_env("PDP_GALLERY_S3_ACCESS_KEY_ID", "PHOTO_UPLOAD_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY", default="")
        secret_access_key = _first_env("PDP_GALLERY_S3_SECRET_ACCESS_KEY", "PHOTO_UPLOAD_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_SECRET_KEY", default="")
        session_token = _first_env("PDP_GALLERY_S3_SESSION_TOKEN", "PHOTO_UPLOAD_SESSION_TOKEN", "AWS_SESSION_TOKEN", default="") or None
        if is_r2:
            session_token = None

        region_name = _pdp_gallery_s3_region()
        if is_r2:
            region_name = "auto"

        client_kwargs: Dict[str, Any] = {
            "region_name": region_name,
            "endpoint_url": endpoint_url,
            "config": Config(**config_kwargs),
        }
        if access_key_id and secret_access_key:
            client_kwargs.update(
                {
                    "aws_access_key_id": access_key_id,
                    "aws_secret_access_key": secret_access_key,
                    **({"aws_session_token": session_token} if session_token else {}),
                }
            )
        return boto3.client("s3", **client_kwargs)
    except Exception:
        return None


def _gallery_file_ext(filename: str, content_type: str) -> str:
    name = (filename or "").lower()
    ct = (content_type or "").lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"):
        if name.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "gif" in ct:
        return ".gif"
    if "heic" in ct or "heif" in ct:
        return ".heic"
    return ".jpg"


def _gallery_images_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("images") or payload.get("gallery") or []
    images: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, str):
                url = item.strip()
                if url:
                    images.append({"id": f"gallery_img_existing_{index}", "url": url, "role": "gallery"})
            elif isinstance(item, dict):
                url = str(item.get("url") or item.get("image_url") or item.get("src") or "").strip()
                if url:
                    images.append({**item, "id": item.get("id") or f"gallery_img_existing_{index}", "url": url})
    primary_url = str(payload.get("primary_image_url") or payload.get("image_url") or "").strip()
    if primary_url and not any(str(img.get("url")) == primary_url for img in images):
        images.insert(0, {"id": "gallery_img_primary_existing", "url": primary_url, "role": "primary", "is_primary": True})
    return images


async def store_pdp_gallery_asset(
    *,
    pdp_id: str,
    filename: str,
    content_type: str,
    blob: bytes,
    actor_type: str,
    actor_id: Optional[str],
) -> Dict[str, Any]:
    asset_id = f"pdp_gallery_asset_{uuid.uuid4().hex}"
    await database.execute(
        pdp_gallery_assets.insert().values(
            id=asset_id,
            pdp_id=pdp_id,
            filename=filename,
            content_type=content_type,
            byte_size=len(blob),
            data=blob,
            created_by_actor_type=actor_type,
            created_by_actor_id=actor_id,
            created_at=_now(),
        )
    )
    base_url = _pdp_gallery_asset_public_base_url()
    return {
        "id": asset_id,
        "url": f"{base_url}/employee/pdps/gallery-assets/{asset_id}",
        "storage": {
            "type": "database",
            "asset_id": asset_id,
            "content_type": content_type,
            "byte_size": len(blob),
        },
    }


async def get_pdp_gallery_asset(asset_id: str) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    row = await database.fetch_one(pdp_gallery_assets.select().where(pdp_gallery_assets.c.id == asset_id))
    asset = _row_dict(row)
    if not asset:
        raise ValueError("PDP_GALLERY_ASSET_NOT_FOUND")
    return asset


async def upload_pdp_gallery_image(
    *,
    pdp_id: str,
    filename: str,
    content_type: str,
    blob: bytes,
    alt_text: Optional[str] = None,
    role: str = "gallery",
    variant_id: Optional[str] = None,
    rights_status: str = "owned_or_licensed",
    source_note: Optional[str] = None,
    actor_type: str = REVIEW_ACTOR_HUMAN,
    actor_id: Optional[str] = None,
    actor_role: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    subject = await resolve_pdp_subject(pdp_id=pdp_id)

    ct = (content_type or "").strip().lower()
    if not ct.startswith("image/"):
        raise ValueError("UNSUPPORTED_GALLERY_IMAGE_TYPE")
    max_bytes = int(os.getenv("PDP_GALLERY_IMAGE_MAX_BYTES") or str(10 * 1024 * 1024))
    if len(blob or b"") <= 0:
        raise ValueError("EMPTY_GALLERY_IMAGE")
    if len(blob) > max_bytes:
        raise ValueError("GALLERY_IMAGE_TOO_LARGE")

    image_id = f"gallery_img_{uuid.uuid4().hex}"
    bucket = _pdp_gallery_bucket()
    public_base = _pdp_gallery_public_base_url()
    if bucket and public_base:
        client = _pdp_gallery_s3_client()
        if client is None:
            raise RuntimeError("PDP_GALLERY_STORAGE_CLIENT_UNAVAILABLE")

        ext = _gallery_file_ext(filename, ct)
        prefix = _pdp_gallery_prefix()
        key = f"{prefix}/{subject['pdp_id']}/{image_id}{ext}" if prefix else f"{subject['pdp_id']}/{image_id}{ext}"

        try:
            client.put_object(Bucket=bucket, Key=key, Body=blob, ContentType=ct)
        except Exception as exc:
            raise RuntimeError(f"PDP_GALLERY_STORAGE_UPLOAD_FAILED:{type(exc).__name__}") from exc

        image_url = f"{public_base}/{key}"
        storage = {
            "type": "s3",
            "bucket": bucket,
            "object_key": key,
            "content_type": ct,
            "byte_size": len(blob),
        }
    else:
        stored = await store_pdp_gallery_asset(
            pdp_id=subject["pdp_id"],
            filename=filename,
            content_type=ct,
            blob=blob,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        image_url = stored["url"]
        storage = stored["storage"]

    normalized_role = role if role in GALLERY_IMAGE_ROLES else "gallery"
    normalized_rights = rights_status if rights_status in GALLERY_RIGHTS_STATUSES else "unknown"

    staged = await _latest_staged_version(subject["pdp_id"], "gallery")
    current = await _current_published_version(subject["pdp_id"], "gallery")
    base_payload = _json_dict((staged or current or {}).get("payload"))
    images = _gallery_images_from_payload(base_payload)

    make_primary = normalized_role == "primary" or not images
    if make_primary:
        images = [{**img, "is_primary": False, "role": "gallery" if img.get("role") == "primary" else img.get("role", "gallery")} for img in images]

    image = {
        "id": image_id,
        "url": image_url,
        "alt": (alt_text or "").strip(),
        "role": normalized_role,
        "is_primary": make_primary,
        "variant_id": (variant_id or "").strip() or None,
        "source_type": "employee_upload",
        "rights_status": normalized_rights,
        "source_note": (source_note or "").strip() or None,
        "uploaded_by_actor_type": actor_type,
        "uploaded_by_actor_id": actor_id,
        "uploaded_at": _now().isoformat(),
        "storage": storage,
    }
    images.append(image)
    primary = next((img for img in images if img.get("is_primary")), images[0] if images else image)
    payload = {
        **base_payload,
        "images": images,
        "primary_image_url": primary.get("url"),
        "gallery_source": "employee_curated",
    }
    source_refs = merge_source_refs(
        (staged or current or {}).get("source_refs") or [],
        [
            {
                "type": "employee_gallery_upload",
                "id": image_id,
                "url": image_url,
                "rights_status": normalized_rights,
                "source_note": (source_note or "").strip() or None,
            }
        ],
    )
    module = await create_module_draft(
        pdp_id=subject["pdp_id"],
        module_key="gallery",
        payload=payload,
        source_refs=source_refs,
        generated_by="employee_gallery_upload",
        generation_ref=image_id,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_role=actor_role,
    )
    return {"status": "success", "image": image, "module": module}


def _empty_module_summaries() -> List[Dict[str, Any]]:
    return [
        {
            "module_key": module_key,
            "status": "needs_human_review" if module_key in HUMAN_CO_REVIEW_MODULES else "not_started",
            "risk_level": "high" if module_key in HUMAN_CO_REVIEW_MODULES else "low",
            "requires_human": module_key in HUMAN_CO_REVIEW_MODULES,
            "review_actor_type": None,
            "review_decision": None,
        }
        for module_key in PDP_MODULE_KEYS
    ]


async def _list_module_summaries_for_pdp_ids(pdp_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    clean_ids = [pdp_id for pdp_id in dict.fromkeys(pdp_ids) if pdp_id]
    if not clean_ids:
        return {}

    params: Dict[str, Any]
    if IS_POSTGRES:
        where = "pdp_id = ANY(:pdp_ids)"
        params = {"pdp_ids": clean_ids}
    else:
        params = {f"pdp_id_{idx}": pdp_id for idx, pdp_id in enumerate(clean_ids)}
        where = "pdp_id IN (" + ", ".join(f":pdp_id_{idx}" for idx in range(len(clean_ids))) + ")"

    rows = await database.fetch_all(
        f"""
        SELECT *
        FROM pdp_module_versions
        WHERE {where}
          AND (
            stage = 'staged'
            OR (stage = 'published' AND status = 'published' AND superseded_at IS NULL)
          )
        ORDER BY pdp_id, module_key, stage, version DESC, created_at DESC
        """,
        params,
    )
    selected: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in rows or []:
        data = _serialize_module(row)
        key = (str(data.get("pdp_id") or ""), str(data.get("module_key") or ""), str(data.get("stage") or ""))
        if key[0] and key[1] and key[2] and key not in selected:
            selected[key] = data

    summaries: Dict[str, List[Dict[str, Any]]] = {}
    empty_summaries = {summary["module_key"]: summary for summary in _empty_module_summaries()}
    for pdp_id in clean_ids:
        module_summaries: List[Dict[str, Any]] = []
        for module_key in PDP_MODULE_KEYS:
            current = selected.get((pdp_id, module_key, "published"))
            staged = selected.get((pdp_id, module_key, "staged"))
            active = staged or current
            if not active:
                module_summaries.append(dict(empty_summaries[module_key]))
                continue
            module_summaries.append(
                {
                    "module_key": module_key,
                    "version_id": active.get("id"),
                    "status": active.get("status") or "not_started",
                    "risk_level": active.get("risk_level") or module_risk_level(module_key),
                    "requires_human": bool(active.get("requires_human") or module_key in HUMAN_CO_REVIEW_MODULES),
                    "review_actor_type": active.get("review_actor_type"),
                    "review_decision": active.get("review_decision"),
                    "current_review_actor_type": (current or {}).get("review_actor_type"),
                    "current_review_decision": (current or {}).get("review_decision"),
                    "last_reviewer": active.get("last_reviewer"),
                    "created_at": active.get("created_at"),
                    "current_created_at": (current or {}).get("created_at"),
                    "source_count": len(active.get("source_refs") or []),
                    "source_refs": active.get("source_refs") or [],
                    "current_source_refs": (current or {}).get("source_refs") or [],
                    "current_version_id": (current or {}).get("id"),
                    "current_risk_level": (current or {}).get("risk_level"),
                    "current_requires_human": (current or {}).get("requires_human"),
                    "current_payload": (current or {}).get("payload") or {},
                    "staged_payload": (staged or {}).get("payload") if staged else None,
                    "published_at": (current or {}).get("published_at"),
                    "has_current": bool(current),
                    "has_staged": bool(staged),
                }
            )
        summaries[pdp_id] = module_summaries
    return summaries


async def seed_recent_pdp_subjects(limit: Optional[int] = 50) -> None:
    """Hydrate dashboard subjects from product groups, ungrouped merchant products, and external seeds."""
    safe_limit = _normalize_hydration_limit(limit, default=50)
    if IS_POSTGRES:
        await _seed_recent_pdp_subjects_postgres(safe_limit)
        return

    limit_clause = _sql_limit_clause(safe_limit)
    limit_params = _sql_limit_params(safe_limit)

    try:
        groups = await database.fetch_all(
            f"""
            SELECT product_group_id,
                   COUNT(DISTINCT merchant_id) AS seller_count,
                   MAX(updated_at) AS updated_at
            FROM product_group_members
            WHERE product_group_id IS NOT NULL
            GROUP BY product_group_id
            ORDER BY MAX(updated_at) DESC
            {limit_clause}
            """,
            limit_params,
        )
    except Exception:
        logger.warning(
            "seed_recent_pdp_subjects: product_group_members aggregate failed; treating as empty",
            exc_info=True,
        )
        groups = []

    for group in groups or []:
        group_data = _row_dict(group)
        product_group_id = str(group_data.get("product_group_id") or "")
        if not product_group_id:
            continue
        try:
            primary = await database.fetch_one(
                """
                SELECT merchant_id, platform, platform_product_id
                FROM product_group_members
                WHERE product_group_id = :product_group_id
                ORDER BY is_primary DESC, updated_at DESC
                LIMIT 1
                """,
                {"product_group_id": product_group_id},
            )
        except Exception:
            logger.warning(
                "seed_recent_pdp_subjects: primary-member lookup failed for product_group_id=%s",
                product_group_id,
                exc_info=True,
            )
            primary = None
        if not primary:
            continue
        representative_product_key = f"{primary['merchant_id']}|{primary['platform']}|{primary['platform_product_id']}"
        product_data = {}
        try:
            cache_row = await _fetch_latest_cache_row(
                str(primary["merchant_id"]),
                str(primary["platform"]),
                str(primary["platform_product_id"]),
            )
            product_data = _json_dict(cache_row.get("product_data")) if cache_row else {}
        except Exception:
            logger.warning(
                "seed_recent_pdp_subjects: product cache lookup failed for primary=%s",
                representative_product_key,
                exc_info=True,
            )
            product_data = {}
        summary = _extract_product_summary(product_data)
        try:
            await _upsert_subject(
                {
                    "pdp_id": make_pdp_id("product_group", product_group_id, DEFAULT_MARKET),
                    "subject_type": "product_group",
                    "subject_ref": product_group_id,
                    "market": DEFAULT_MARKET,
                    "product_group_id": product_group_id,
                    "external_product_id": None,
                    "representative_product_key": representative_product_key,
                    "title": summary.get("title") or product_group_id,
                    "image_url": summary.get("image_url"),
                    "seller_count": int(group_data.get("seller_count") or 1),
                    "external_only": False,
                    "status": "active",
                }
            )
        except Exception:
            continue

    try:
        canonical_rows = await database.fetch_all(
            f"""
            SELECT *
            FROM canonical_products cp
            WHERE cp.merchant_id IS NOT NULL
              AND cp.platform IS NOT NULL
              AND cp.platform_product_id IS NOT NULL
              AND (cp.expires_at IS NULL OR cp.expires_at > CURRENT_TIMESTAMP)
              AND NOT EXISTS (
                SELECT 1
                FROM product_group_members pgm
                WHERE pgm.merchant_id = cp.merchant_id
                  AND pgm.platform = cp.platform
                  AND pgm.platform_product_id = cp.platform_product_id
                  AND pgm.product_group_id IS NOT NULL
              )
            ORDER BY cp.source_recorded_at DESC, cp.updated_at DESC
            {limit_clause}
            """,
            limit_params,
        )
    except Exception:
        logger.warning(
            "seed_recent_pdp_subjects: canonical_products query failed; treating as empty",
            exc_info=True,
        )
        canonical_rows = []

    for row in canonical_rows or []:
        product = _row_dict(row)
        merchant_id = str(product.get("merchant_id") or "")
        platform = str(product.get("platform") or "")
        platform_product_id = str(product.get("platform_product_id") or "")
        if not merchant_id or not platform or not platform_product_id:
            continue
        product_key = f"{merchant_id}|{platform}|{platform_product_id}"
        product_data = _json_dict(product.get("standard_product_data"))
        summary = _extract_product_summary(product_data)
        try:
            await _upsert_subject(
                {
                    "pdp_id": make_pdp_id("merchant_product", product_key, DEFAULT_MARKET),
                    "subject_type": "merchant_product",
                    "subject_ref": product_key,
                    "market": DEFAULT_MARKET,
                    "product_group_id": None,
                    "external_product_id": None,
                    "representative_product_key": product_key,
                    "title": product.get("title") or summary.get("title") or platform_product_id,
                    "image_url": product.get("default_image_url") or summary.get("image_url"),
                    "seller_count": 1,
                    "external_only": False,
                    "status": "active",
                }
            )
        except Exception:
            continue

    try:
        merchant_rows = await database.fetch_all(
            f"""
            SELECT pc.*
            FROM products_cache pc
            WHERE pc.merchant_id IS NOT NULL
              AND pc.platform IS NOT NULL
              AND pc.platform_product_id IS NOT NULL
              AND (pc.expires_at IS NULL OR pc.expires_at > CURRENT_TIMESTAMP)
              AND NOT EXISTS (
                SELECT 1
                FROM canonical_products cp
                WHERE cp.merchant_id = pc.merchant_id
                  AND cp.platform = pc.platform
                  AND cp.platform_product_id = pc.platform_product_id
                  AND (cp.expires_at IS NULL OR cp.expires_at > CURRENT_TIMESTAMP)
              )
              AND NOT EXISTS (
                SELECT 1
                FROM product_group_members pgm
                WHERE pgm.merchant_id = pc.merchant_id
                  AND pgm.platform = pc.platform
                  AND pgm.platform_product_id = pc.platform_product_id
                  AND pgm.product_group_id IS NOT NULL
              )
              AND pc.id = (
                SELECT pc2.id
                FROM products_cache pc2
                WHERE pc2.merchant_id = pc.merchant_id
                  AND pc2.platform = pc.platform
                  AND pc2.platform_product_id = pc.platform_product_id
                ORDER BY pc2.cached_at DESC, pc2.id DESC
                LIMIT 1
              )
            ORDER BY pc.cached_at DESC, pc.id DESC
            {limit_clause}
            """,
            limit_params,
        )
    except Exception:
        logger.warning(
            "seed_recent_pdp_subjects: products_cache (ungrouped merchant) query failed; treating as empty",
            exc_info=True,
        )
        merchant_rows = []

    for row in merchant_rows or []:
        product = _row_dict(row)
        merchant_id = str(product.get("merchant_id") or "")
        platform = str(product.get("platform") or "")
        platform_product_id = str(product.get("platform_product_id") or "")
        if not merchant_id or not platform or not platform_product_id:
            continue
        product_key = f"{merchant_id}|{platform}|{platform_product_id}"
        summary = _extract_product_summary(_json_dict(product.get("product_data")))
        try:
            await _upsert_subject(
                {
                    "pdp_id": make_pdp_id("merchant_product", product_key, DEFAULT_MARKET),
                    "subject_type": "merchant_product",
                    "subject_ref": product_key,
                    "market": DEFAULT_MARKET,
                    "product_group_id": None,
                    "external_product_id": None,
                    "representative_product_key": product_key,
                    "title": summary.get("title") or platform_product_id,
                    "image_url": summary.get("image_url"),
                    "seller_count": 1,
                    "external_only": False,
                    "status": "active",
                }
            )
        except Exception:
            continue

    try:
        external_rows = await database.fetch_all(
            f"""
            SELECT *
            FROM external_product_seeds
            WHERE status = 'active'
            ORDER BY updated_at DESC, created_at DESC
            {limit_clause}
            """,
            limit_params,
        )
    except Exception:
        logger.warning(
            "seed_recent_pdp_subjects: external_product_seeds query failed; treating as empty",
            exc_info=True,
        )
        external_rows = []

    for seed in external_rows or []:
        seed_data = _row_dict(seed)
        external_product_id = str(seed_data.get("external_product_id") or seed_data.get("id") or "")
        if not external_product_id:
            continue
        try:
            await _upsert_subject(
                _subject_from_external_seed(
                    seed_data,
                    external_product_id=external_product_id,
                    market=str(seed_data.get("market") or DEFAULT_MARKET).upper(),
                )
            )
        except Exception:
            continue


async def _seed_recent_pdp_subjects_postgres(safe_limit: Optional[int]) -> None:
    """Hydrate the PDP dashboard index without per-subject DB round-trips."""
    limit_clause = _sql_limit_clause(safe_limit)
    limit_params = _sql_limit_params(safe_limit)

    group_rows = await database.fetch_all(
        f"""
        WITH ranked_groups AS (
            SELECT product_group_id,
                   COUNT(DISTINCT merchant_id) AS seller_count,
                   MAX(updated_at) AS updated_at
            FROM product_group_members
            WHERE product_group_id IS NOT NULL
            GROUP BY product_group_id
            ORDER BY MAX(updated_at) DESC
            {limit_clause}
        ),
        primary_members AS (
            SELECT DISTINCT ON (pgm.product_group_id)
                   pgm.product_group_id,
                   pgm.merchant_id,
                   pgm.platform,
                   pgm.platform_product_id
            FROM product_group_members pgm
            JOIN ranked_groups rg ON rg.product_group_id = pgm.product_group_id
            ORDER BY pgm.product_group_id, pgm.is_primary DESC, pgm.updated_at DESC
        ),
        latest_cache AS (
            SELECT DISTINCT ON (pc.merchant_id, pc.platform, pc.platform_product_id)
                   pc.merchant_id,
                   pc.platform,
                   pc.platform_product_id,
                   pc.product_data
            FROM products_cache pc
            JOIN primary_members pm
              ON pm.merchant_id = pc.merchant_id
             AND pm.platform = pc.platform
             AND pm.platform_product_id = pc.platform_product_id
            ORDER BY pc.merchant_id, pc.platform, pc.platform_product_id, pc.cached_at DESC, pc.id DESC
        )
        SELECT rg.product_group_id,
               rg.seller_count,
               pm.merchant_id,
               pm.platform,
               pm.platform_product_id,
               lc.product_data
        FROM ranked_groups rg
        JOIN primary_members pm ON pm.product_group_id = rg.product_group_id
        LEFT JOIN latest_cache lc
          ON lc.merchant_id = pm.merchant_id
         AND lc.platform = pm.platform
         AND lc.platform_product_id = pm.platform_product_id
        ORDER BY rg.updated_at DESC
        """,
        limit_params,
    )

    subjects: List[Dict[str, Any]] = []
    for row in group_rows or []:
        group_data = _row_dict(row)
        product_group_id = str(group_data.get("product_group_id") or "")
        merchant_id = str(group_data.get("merchant_id") or "")
        platform = str(group_data.get("platform") or "")
        platform_product_id = str(group_data.get("platform_product_id") or "")
        if not product_group_id or not merchant_id or not platform or not platform_product_id:
            continue
        product_data = _json_dict(group_data.get("product_data"))
        summary = _extract_product_summary(product_data)
        representative_product_key = f"{merchant_id}|{platform}|{platform_product_id}"
        subjects.append(
            {
                "pdp_id": make_pdp_id("product_group", product_group_id, DEFAULT_MARKET),
                "subject_type": "product_group",
                "subject_ref": product_group_id,
                "market": DEFAULT_MARKET,
                "product_group_id": product_group_id,
                "external_product_id": None,
                "representative_product_key": representative_product_key,
                "title": summary.get("title") or product_group_id,
                "image_url": summary.get("image_url"),
                "seller_count": int(group_data.get("seller_count") or 1),
                "external_only": False,
                "status": "active",
            }
        )

    canonical_rows = await database.fetch_all(
        f"""
        SELECT cp.*
        FROM canonical_products cp
        WHERE cp.merchant_id IS NOT NULL
          AND cp.platform IS NOT NULL
          AND cp.platform_product_id IS NOT NULL
          AND (cp.expires_at IS NULL OR cp.expires_at > NOW())
          AND NOT EXISTS (
            SELECT 1
            FROM product_group_members pgm
            WHERE pgm.merchant_id = cp.merchant_id
              AND pgm.platform = cp.platform
              AND pgm.platform_product_id = cp.platform_product_id
              AND pgm.product_group_id IS NOT NULL
          )
        ORDER BY cp.source_recorded_at DESC NULLS LAST, cp.updated_at DESC NULLS LAST
        {limit_clause}
        """,
        limit_params,
    )

    for row in canonical_rows or []:
        product = _row_dict(row)
        merchant_id = str(product.get("merchant_id") or "")
        platform = str(product.get("platform") or "")
        platform_product_id = str(product.get("platform_product_id") or "")
        if not merchant_id or not platform or not platform_product_id:
            continue
        product_key = f"{merchant_id}|{platform}|{platform_product_id}"
        product_data = _json_dict(product.get("standard_product_data"))
        summary = _extract_product_summary(product_data)
        subjects.append(
            {
                "pdp_id": make_pdp_id("merchant_product", product_key, DEFAULT_MARKET),
                "subject_type": "merchant_product",
                "subject_ref": product_key,
                "market": DEFAULT_MARKET,
                "product_group_id": None,
                "external_product_id": None,
                "representative_product_key": product_key,
                "title": product.get("title") or summary.get("title") or platform_product_id,
                "image_url": product.get("default_image_url") or summary.get("image_url"),
                "seller_count": 1,
                "external_only": False,
                "status": "active",
            }
        )

    merchant_rows = await database.fetch_all(
        f"""
        WITH latest_products AS (
            SELECT DISTINCT ON (pc.merchant_id, pc.platform, pc.platform_product_id)
                   pc.id,
                   pc.merchant_id,
                   pc.platform,
                   pc.platform_product_id,
                   pc.product_data,
                   pc.cached_at
            FROM products_cache pc
            WHERE pc.merchant_id IS NOT NULL
              AND pc.platform IS NOT NULL
              AND pc.platform_product_id IS NOT NULL
              AND (pc.expires_at IS NULL OR pc.expires_at > NOW())
              AND NOT EXISTS (
                SELECT 1
                FROM canonical_products cp
                WHERE cp.merchant_id = pc.merchant_id
                  AND cp.platform = pc.platform
                  AND cp.platform_product_id = pc.platform_product_id
                  AND (cp.expires_at IS NULL OR cp.expires_at > NOW())
              )
              AND NOT EXISTS (
                SELECT 1
                FROM product_group_members pgm
                WHERE pgm.merchant_id = pc.merchant_id
                  AND pgm.platform = pc.platform
                  AND pgm.platform_product_id = pc.platform_product_id
                  AND pgm.product_group_id IS NOT NULL
              )
            ORDER BY pc.merchant_id, pc.platform, pc.platform_product_id, pc.cached_at DESC NULLS LAST, pc.id DESC
        )
        SELECT *
        FROM latest_products
        ORDER BY cached_at DESC NULLS LAST, id DESC
        {limit_clause}
        """,
        limit_params,
    )

    for row in merchant_rows or []:
        product = _row_dict(row)
        merchant_id = str(product.get("merchant_id") or "")
        platform = str(product.get("platform") or "")
        platform_product_id = str(product.get("platform_product_id") or "")
        if not merchant_id or not platform or not platform_product_id:
            continue
        product_key = f"{merchant_id}|{platform}|{platform_product_id}"
        summary = _extract_product_summary(_json_dict(product.get("product_data")))
        subjects.append(
            {
                "pdp_id": make_pdp_id("merchant_product", product_key, DEFAULT_MARKET),
                "subject_type": "merchant_product",
                "subject_ref": product_key,
                "market": DEFAULT_MARKET,
                "product_group_id": None,
                "external_product_id": None,
                "representative_product_key": product_key,
                "title": summary.get("title") or platform_product_id,
                "image_url": summary.get("image_url"),
                "seller_count": 1,
                "external_only": False,
                "status": "active",
            }
        )

    external_rows = await database.fetch_all(
        f"""
        SELECT *
        FROM external_product_seeds
        WHERE status = 'active'
        ORDER BY updated_at DESC, created_at DESC
        {limit_clause}
        """,
        limit_params,
    )

    for seed in external_rows or []:
        seed_data = _row_dict(seed)
        external_product_id = str(seed_data.get("external_product_id") or seed_data.get("id") or "")
        if not external_product_id:
            continue
        subjects.append(
            _subject_from_external_seed(
                seed_data,
                external_product_id=external_product_id,
                market=str(seed_data.get("market") or DEFAULT_MARKET).upper(),
            )
        )

    await _bulk_upsert_subjects_postgres(subjects)


async def _bulk_upsert_subjects_postgres(subjects: List[Dict[str, Any]]) -> None:
    if not subjects:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    deduped_subjects = {
        str(subject.get("pdp_id") or ""): subject
        for subject in subjects
        if subject.get("pdp_id")
    }
    if not deduped_subjects:
        return

    now = _now()
    rows = [
        {
            "pdp_id": subject["pdp_id"],
            "subject_type": subject["subject_type"],
            "subject_ref": subject["subject_ref"],
            "market": subject.get("market") or DEFAULT_MARKET,
            "product_group_id": subject.get("product_group_id"),
            "external_product_id": subject.get("external_product_id"),
            "representative_product_key": subject.get("representative_product_key"),
            "title": subject.get("title"),
            "image_url": subject.get("image_url"),
            "seller_count": int(subject.get("seller_count") or 0),
            "external_only": bool(subject.get("external_only")),
            "status": subject.get("status") or "active",
            "created_at": now,
            "updated_at": now,
        }
        for subject in deduped_subjects.values()
    ]
    chunk_size = 1000
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        insert_stmt = pg_insert(pdp_subject_index).values(chunk)
        update_columns = {
            column.name: getattr(insert_stmt.excluded, column.name)
            for column in pdp_subject_index.c
            if column.name not in {"pdp_id", "created_at"}
        }
        await database.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=[pdp_subject_index.c.pdp_id],
                set_=update_columns,
            )
        )


async def _next_module_version(pdp_id: str, module_key: str) -> int:
    row = await database.fetch_one(
        """
        SELECT MAX(version) AS max_version
        FROM pdp_module_versions
        WHERE pdp_id = :pdp_id AND module_key = :module_key
        """,
        {"pdp_id": pdp_id, "module_key": module_key},
    )
    data = _row_dict(row)
    return int(data.get("max_version") or 0) + 1


def _validate_module_key(module_key: str) -> None:
    if module_key not in PDP_MODULE_KEYS:
        raise ValueError("INVALID_PDP_MODULE")


async def create_module_draft(
    *,
    pdp_id: str,
    module_key: str,
    payload: Dict[str, Any],
    source_refs: Optional[List[Dict[str, Any]]] = None,
    generated_by: Optional[str] = None,
    generation_ref: Optional[str] = None,
    actor_type: str = REVIEW_ACTOR_HUMAN,
    actor_id: Optional[str] = None,
    actor_role: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    _validate_module_key(module_key)
    subject = await resolve_pdp_subject(pdp_id=pdp_id)
    payload = payload if isinstance(payload, dict) else {}
    source_refs = source_refs if isinstance(source_refs, list) else []
    # STAGE-time guardrails. The payload arriving here is free-form (a merchant
    # contribution or an LLM candidate both land in it unvalidated), so this is where a
    # price or an identity key riding inside a copy edit is refused — before it is
    # persisted as a version anyone can then approve.
    _enforce_module_write_guardrails(
        pdp_id=subject["pdp_id"], module_key=module_key, payload=payload
    )
    version = await _next_module_version(subject["pdp_id"], module_key)
    requires_human = module_requires_human_review(module_key, payload)
    risk_level = module_risk_level(module_key, payload)
    if actor_type == REVIEW_ACTOR_HUMAN:
        allowed = allowed_pdp_review_actions(
            actor_role=actor_role,
            module_key=module_key,
            risk_level=risk_level,
            requires_human=requires_human,
            module_status="draft",
        )
        if "edit_draft" not in allowed:
            raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
    row = {
        "id": f"pdpmod_{uuid.uuid4().hex}",
        "pdp_id": subject["pdp_id"],
        "module_key": module_key,
        "stage": "staged",
        "version": version,
        "status": "needs_human_review" if requires_human else "draft",
        "payload": payload,
        "source_refs": source_refs,
        "review_actor_type": None,
        "review_actor_id": None,
        "review_model": None,
        "review_decision": None,
        "review_confidence": None,
        "review_rubric": None,
        "risk_level": risk_level,
        "requires_human": requires_human,
        "generated_by": generated_by,
        "generation_ref": generation_ref,
        "created_by_employee_id": actor_id if actor_type == REVIEW_ACTOR_HUMAN else None,
        "created_at": _now(),
        "published_at": None,
        "superseded_at": None,
    }
    await database.execute(pdp_module_versions.insert().values(**row))
    await _audit(
        pdp_id=subject["pdp_id"],
        module_key=module_key,
        action="module_draft_created",
        actor_type=actor_type,
        actor_id=actor_id,
        details={"version_id": row["id"], "generated_by": generated_by},
    )
    return _serialize_module(row)


def run_gpt55_quality_gate(
    *,
    module_key: str,
    payload: Dict[str, Any],
    source_refs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    _validate_module_key(module_key)
    payload = payload if isinstance(payload, dict) else {}
    source_refs = source_refs if isinstance(source_refs, list) else []
    reasons: List[str] = []
    checks: Dict[str, Any] = {
        "source_grounded": bool(source_refs),
        "seller_entity_checkout_not_confused": True,
        "variant_market_consistent": True,
        "no_medical_regulated_promo_or_fake_review_claim": True,
        "machine_publish_allowed_module": module_key in MACHINE_PUBLISH_MODULES,
    }

    text = _text_blob(payload).lower()
    for pattern, reason in UNSUPPORTED_CLAIM_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            reasons.append(reason)
            checks["no_medical_regulated_promo_or_fake_review_claim"] = False

    if not checks["source_grounded"]:
        reasons.append("missing_source_refs")

    if module_requires_human_review(module_key, payload):
        reasons.append("module_or_payload_requires_human_co_review")

    if module_key not in MACHINE_PUBLISH_MODULES:
        reasons.append("module_not_machine_publishable")

    if any(reason in reasons for reason in {"medical_or_regulated_claim", "guarantee_claim", "unverified_popularity_claim", "promotion_or_time_sensitive_claim", "unsupported_review_expression", "seller_or_checkout_ownership_confusion"}):
        decision = "reject"
        confidence = 0.96
    elif "missing_source_refs" in reasons:
        decision = "reject"
        confidence = 0.9
    elif "module_or_payload_requires_human_co_review" in reasons or "module_not_machine_publishable" in reasons:
        decision = "needs_human_review"
        confidence = 0.92
    else:
        decision = "pass"
        confidence = 0.91

    return {
        "review_actor_type": REVIEW_ACTOR_GPT55,
        "review_model": GPT55_REVIEW_MODEL,
        "decision": decision,
        "confidence": confidence,
        "reasons": reasons,
        "checks": checks,
        "evidence_refs": source_refs,
    }


def _force_codex_artifact_required(local: Dict[str, Any], reason: str) -> Dict[str, Any]:
    decision = "reject" if local.get("decision") == "reject" else "needs_human_review"
    reasons = list(dict.fromkeys([*(local.get("reasons") or []), reason]))
    return {
        **local,
        "decision": decision,
        "confidence": min(float(local.get("confidence") or 0.0), 0.85) or 0.85,
        "reasons": reasons,
        "codex_review_artifact_required": True,
        "local_policy_artifact": local,
    }


def _normalize_codex_gpt55_rubric(rubric: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(rubric, dict):
        raise ValueError("GPT55_RUBRIC_MUST_BE_OBJECT")
    decision = str(rubric.get("decision") or "").strip()
    if decision not in {"pass", "reject", "needs_human_review"}:
        raise ValueError("GPT55_RUBRIC_INVALID_DECISION")
    confidence_raw = rubric.get("confidence")
    try:
        confidence = float(confidence_raw)
    except Exception as exc:
        raise ValueError("GPT55_RUBRIC_INVALID_CONFIDENCE") from exc
    if confidence < 0 or confidence > 1:
        raise ValueError("GPT55_RUBRIC_INVALID_CONFIDENCE")

    reasons = rubric.get("reasons") or []
    if not isinstance(reasons, list):
        raise ValueError("GPT55_RUBRIC_REASONS_MUST_BE_LIST")

    checks = rubric.get("checks") or {}
    if not isinstance(checks, dict):
        raise ValueError("GPT55_RUBRIC_CHECKS_MUST_BE_OBJECT")
    missing = sorted(GPT55_RUBRIC_REQUIRED_CHECKS - set(checks.keys()))
    if missing:
        raise ValueError(f"GPT55_RUBRIC_MISSING_CHECKS:{','.join(missing)}")

    evidence_refs = rubric.get("evidence_refs") or []
    if not isinstance(evidence_refs, list):
        raise ValueError("GPT55_RUBRIC_EVIDENCE_REFS_MUST_BE_LIST")

    return {
        "decision": decision,
        "confidence": confidence,
        "reasons": [str(reason) for reason in reasons],
        "checks": {key: bool(value) for key, value in checks.items()},
        "evidence_refs": evidence_refs,
        "review_notes": rubric.get("review_notes"),
        "reviewed_in": rubric.get("reviewed_in") or "codex_external_window",
    }


def _codex_gpt55_pass_blockers(codex_rubric: Dict[str, Any]) -> List[str]:
    if codex_rubric.get("decision") != "pass":
        return []

    checks = codex_rubric.get("checks") if isinstance(codex_rubric.get("checks"), dict) else {}
    failed = sorted(key for key in GPT55_RUBRIC_REQUIRED_CHECKS if checks.get(key) is not True)
    blockers: List[str] = []
    if failed:
        blockers.append(f"codex_pass_failed_checks:{','.join(failed)}")
    if not codex_rubric.get("evidence_refs"):
        blockers.append("codex_pass_missing_evidence_refs")
    if codex_rubric.get("reviewed_in") != "codex_external_window":
        blockers.append("codex_pass_invalid_review_channel")
    return blockers


def _merge_codex_rubric_with_local_gate(local: Dict[str, Any], codex_rubric: Dict[str, Any]) -> Dict[str, Any]:
    decision_rank = {"pass": 0, "needs_human_review": 1, "reject": 2}
    local_decision = str(local.get("decision") or "reject")
    codex_decision = str(codex_rubric.get("decision") or "reject")
    stricter_decision = local_decision
    if decision_rank.get(codex_decision, 2) > decision_rank.get(local_decision, 2):
        stricter_decision = codex_decision

    local_reasons = [str(reason) for reason in local.get("reasons") or []]
    codex_reasons = [str(reason) for reason in codex_rubric.get("reasons") or []]
    pass_blockers = _codex_gpt55_pass_blockers(codex_rubric)
    if pass_blockers and stricter_decision == "pass":
        stricter_decision = "needs_human_review"
    reasons = list(dict.fromkeys([*local_reasons, *codex_reasons, *pass_blockers]))

    local_checks = local.get("checks") if isinstance(local.get("checks"), dict) else {}
    codex_checks = codex_rubric.get("checks") if isinstance(codex_rubric.get("checks"), dict) else {}
    checks = {**codex_checks}
    for key, value in local_checks.items():
        checks[key] = bool(checks.get(key, True)) and bool(value)

    if stricter_decision == local_decision and local_decision != codex_decision:
        confidence = float(local.get("confidence") or 0.0)
    else:
        confidence = min(
            float(local.get("confidence") or 0.0),
            float(codex_rubric.get("confidence") or local.get("confidence") or 0.0),
        ) or float(codex_rubric.get("confidence") or local.get("confidence") or 0.0)
    return {
        "review_actor_type": REVIEW_ACTOR_GPT55,
        "review_model": GPT55_REVIEW_MODEL,
        "decision": stricter_decision,
        "confidence": confidence,
        "reasons": reasons,
        "checks": checks,
        "evidence_refs": codex_rubric.get("evidence_refs") or local.get("evidence_refs") or [],
        "codex_gpt55_artifact": {
            "decision": codex_decision,
            "confidence": codex_rubric.get("confidence"),
            "reasons": codex_reasons,
            "checks": codex_checks,
            "publish_blockers": pass_blockers,
            "review_notes": codex_rubric.get("review_notes"),
            "reviewed_in": codex_rubric.get("reviewed_in"),
        },
        "local_policy_artifact": local,
    }


def build_codex_gpt55_quality_gate_result(
    *,
    module_key: str,
    payload: Dict[str, Any],
    source_refs: Optional[List[Dict[str, Any]]] = None,
    external_rubric: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    local = run_gpt55_quality_gate(module_key=module_key, payload=payload, source_refs=source_refs)
    if external_rubric is None:
        return _force_codex_artifact_required(local, "codex_gpt55_review_artifact_required")
    try:
        normalized = _normalize_codex_gpt55_rubric(external_rubric)
    except Exception as exc:
        return _force_codex_artifact_required(local, str(exc)[:120] or "invalid_codex_gpt55_review_artifact")
    return _merge_codex_rubric_with_local_gate(local, normalized)


async def _fetch_module_version(pdp_id: str, module_key: str, version_id: Optional[str] = None) -> Dict[str, Any]:
    if version_id:
        row = await database.fetch_one(
            """
            SELECT *
            FROM pdp_module_versions
            WHERE id = :id AND pdp_id = :pdp_id AND module_key = :module_key
            LIMIT 1
            """,
            {"id": version_id, "pdp_id": pdp_id, "module_key": module_key},
        )
    else:
        row = await database.fetch_one(
            """
            SELECT *
            FROM pdp_module_versions
            WHERE pdp_id = :pdp_id
              AND module_key = :module_key
              AND stage = 'staged'
              AND superseded_at IS NULL
            ORDER BY version DESC, created_at DESC
            LIMIT 1
            """,
            {"pdp_id": pdp_id, "module_key": module_key},
        )
    if not row:
        raise LookupError("PDP_MODULE_VERSION_NOT_FOUND")
    return _serialize_module(row)


async def review_module_version(
    *,
    pdp_id: str,
    module_key: str,
    version_id: Optional[str] = None,
    actor_type: str,
    actor_id: Optional[str] = None,
    actor_role: Optional[str] = None,
    decision: Optional[str] = None,
    notes: Optional[str] = None,
    external_rubric: Optional[Dict[str, Any]] = None,
    checklist: Optional[Dict[str, Any]] = None,
    policy_labels: Optional[List[str]] = None,
    decision_tree_path: Optional[List[str]] = None,
    escalation_reason: Optional[str] = None,
    review_duration_ms: Optional[int] = None,
    override_reason: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    _validate_module_key(module_key)
    version = await _fetch_module_version(pdp_id, module_key, version_id)
    payload = _json_dict(version.get("payload"))
    source_refs = _json_list(version.get("source_refs"))

    if actor_type == REVIEW_ACTOR_GPT55:
        rubric = build_codex_gpt55_quality_gate_result(
            module_key=module_key,
            payload=payload,
            source_refs=source_refs,
            external_rubric=external_rubric,
        )
        review_decision = rubric["decision"]
        confidence = rubric["confidence"]
        actor_id = actor_id or GPT55_REVIEW_MODEL
        review_model = GPT55_REVIEW_MODEL
    else:
        review_decision = decision or "needs_human_review"
        confidence = 1.0 if review_decision == "pass" else None
        review_model = None
        rubric = {
            "decision": review_decision,
            "notes": notes,
            "reviewer_standard": "human_employee_quality_gate",
        }

    if review_decision not in {"pass", "reject", "needs_human_review"}:
        raise ValueError("INVALID_REVIEW_DECISION")

    if actor_type == REVIEW_ACTOR_HUMAN:
        risk = module_risk_level(module_key, payload)
        requires_human = module_requires_human_review(module_key, payload)
        allowed = allowed_pdp_review_actions(
            actor_role=actor_role,
            module_key=module_key,
            risk_level=risk,
            requires_human=requires_human,
            module_status=version.get("status"),
        )
        if review_decision == "pass" and "publish" not in allowed:
            raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
        if review_decision == "reject" and "reject" not in allowed:
            raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
        if override_reason and "override" not in allowed:
            raise PermissionError("PDP_REVIEW_OVERRIDE_FORBIDDEN")
        if review_decision == "pass" and is_outsourced_review_role(actor_role):
            if not checklist_passed(checklist):
                raise ValueError("PDP_REVIEW_CHECKLIST_REQUIRED")
            if not policy_labels:
                raise ValueError("PDP_REVIEW_POLICY_LABEL_REQUIRED")

    status = "approved" if review_decision == "pass" else review_decision
    audit_review_context = {
        "checklist": checklist if isinstance(checklist, dict) else {},
        "policy_labels": policy_labels if isinstance(policy_labels, list) else [],
        "decision_tree_path": decision_tree_path if isinstance(decision_tree_path, list) else [],
        "escalation_reason": escalation_reason,
        "override_reason": override_reason,
        "review_duration_ms": review_duration_ms,
        "actor_role": normalize_employee_role(actor_role),
    }
    if actor_type == REVIEW_ACTOR_HUMAN:
        rubric = {**rubric, **audit_review_context}
    await database.execute(
        pdp_module_versions.update()
        .where(pdp_module_versions.c.id == version["id"])
        .values(
            status=status,
            review_actor_type=actor_type,
            review_actor_id=actor_id,
            review_model=review_model,
            review_decision=review_decision,
            review_confidence=confidence,
            review_rubric=rubric,
            risk_level=module_risk_level(module_key, payload),
            requires_human=module_requires_human_review(module_key, payload),
        )
    )
    await _audit(
        pdp_id=pdp_id,
        module_key=module_key,
        action="module_reviewed",
        actor_type=actor_type,
        actor_id=actor_id,
        details={"version_id": version["id"], "decision": review_decision, "notes": notes, **audit_review_context},
    )
    await database.execute(
        pdp_review_tasks.update()
        .where(pdp_review_tasks.c.version_id == version["id"])
        .values(
            checklist=audit_review_context["checklist"],
            policy_labels=audit_review_context["policy_labels"],
            decision_tree_path=audit_review_context["decision_tree_path"],
            escalation_reason=escalation_reason,
            override_reason=override_reason,
            review_duration_ms=review_duration_ms,
            status="resolved" if review_decision in {"pass", "reject"} else "needs_review",
            resolved_at=_now() if review_decision in {"pass", "reject"} else None,
            updated_at=_now(),
        )
    )

    can_publish = review_decision == "pass" and (
        actor_type == REVIEW_ACTOR_HUMAN
        or (
            actor_type == REVIEW_ACTOR_GPT55
            and module_key in MACHINE_PUBLISH_MODULES
            and not module_requires_human_review(module_key, payload)
        )
    )

    if can_publish:
        published = await publish_module_version(
            pdp_id=pdp_id,
            module_key=module_key,
            version_id=version["id"],
            actor_type=actor_type,
            actor_id=actor_id,
            review_model=review_model,
            review_rubric=rubric,
            review_confidence=confidence,
        )
        return {"status": "success", "decision": review_decision, "published": True, "module": published, "rubric": rubric}

    return {
        "status": "success",
        "decision": review_decision,
        "published": False,
        "module": await _fetch_module_version(pdp_id, module_key, version["id"]),
        "rubric": rubric,
    }


# ---------------------------------------------------------------------------
# SKU Optimization OS -- hybrid publish path (overlay materialization).
# Flag-gated, OFF by default. When ON, publishing a governance module also flattens
# its approved payload into merchant_product_overlay so the public PDP merge hook
# (PIVOTA-Agent enrichProductWithCatalogPdpContentFields) can serve it at request time.
# v1 scope: the "copy" module only. Add modules to _OVERLAY_FIELD_MAP to widen coverage.
# ---------------------------------------------------------------------------
SKU_OPT_OVERLAY_V1_ENABLED = os.getenv("SKU_OPT_OVERLAY_V1", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# actor_type -> overlay provenance label
_OVERLAY_PROVENANCE_BY_ACTOR = {
    REVIEW_ACTOR_HUMAN: "ops_approved",
}

# Retry budget for the overlay supersede+insert pair vs the active unique index
# under concurrent publishes (see materialize_overlay_from_module).
_OVERLAY_MATERIALIZE_MAX_ATTEMPTS = 3


def _overlay_copy_description(payload: Dict[str, Any]) -> Optional[str]:
    """Extract the description body from a published 'copy' module payload."""
    for key in ("pdp_description_raw", "description", "body", "text", "copy"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# module_key -> list of (overlay field_key, extractor(payload) -> Optional[value])
_OVERLAY_FIELD_MAP: Dict[str, List[Tuple[str, Any]]] = {
    "copy": [("pdp_description_raw", _overlay_copy_description)],
}


async def materialize_overlay_from_module(
    *,
    pdp_id: str,
    module_key: str,
    published_version_id: str,
    payload: Dict[str, Any],
    actor_type: str,
    actor_id: Optional[str],
) -> int:
    """Flatten an approved governance module payload into merchant_product_overlay rows.

    Returns the number of overlay rows written. Best-effort and bounded to modules in
    _OVERLAY_FIELD_MAP -- callers MUST guard so a failure here never blocks a publish.
    """
    field_specs = _OVERLAY_FIELD_MAP.get(module_key)
    if not field_specs:
        return 0
    subject = await database.fetch_one(
        pdp_subject_index.select().where(pdp_subject_index.c.pdp_id == pdp_id)
    )
    product_key = dict(subject).get("representative_product_key") if subject else None
    if not product_key:
        return 0
    provenance = _OVERLAY_PROVENANCE_BY_ACTOR.get(actor_type, "agent_approved")
    now = _now()
    written = 0
    for field_key, extractor in field_specs:
        value = extractor(payload)
        if value is None:
            continue
        # Supersede prior active overlay + insert the new one. The partial unique
        # index uq_merchant_product_overlay_active enforces at most one active row
        # per (product_key, module_key, field_key). The transaction makes each
        # supersede+insert atomic; the retry handles the residual race where two
        # concurrent publishers both supersede then both insert -> the loser hits
        # the unique index, retries (re-supersedes the winner's row, inserts its
        # own), and the table converges to exactly one active row.
        for attempt in range(_OVERLAY_MATERIALIZE_MAX_ATTEMPTS):
            try:
                async with database.transaction():
                    await database.execute(
                        merchant_product_overlay.update()
                        .where(
                            (merchant_product_overlay.c.product_key == product_key)
                            & (merchant_product_overlay.c.module_key == module_key)
                            & (merchant_product_overlay.c.field_key == field_key)
                            & (merchant_product_overlay.c.approval_status == "active")
                        )
                        .values(approval_status="superseded", updated_at=now)
                    )
                    await database.execute(
                        merchant_product_overlay.insert().values(
                            overlay_id=f"ovl_{uuid.uuid4().hex}",
                            product_key=product_key,
                            content_key=None,
                            module_key=module_key,
                            field_key=field_key,
                            value_jsonb=value,
                            provenance=provenance,
                            source_version_id=published_version_id,
                            approval_status="active",
                            approved_by=actor_id,
                            approved_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                break
            except Exception:
                if attempt + 1 >= _OVERLAY_MATERIALIZE_MAX_ATTEMPTS:
                    raise
                # lost the race on the active unique index; retry the pair
                continue
        written += 1
    return written


async def publish_module_version(
    *,
    pdp_id: str,
    module_key: str,
    version_id: str,
    actor_type: str,
    actor_id: Optional[str],
    review_model: Optional[str] = None,
    review_rubric: Optional[Dict[str, Any]] = None,
    review_confidence: Optional[float] = None,
) -> Dict[str, Any]:
    version = await _fetch_module_version(pdp_id, module_key, version_id)
    payload = _json_dict(version.get("payload"))
    source_refs = _json_list(version.get("source_refs"))
    if actor_type == REVIEW_ACTOR_GPT55 and module_requires_human_review(module_key, payload):
        raise PermissionError("PDP_MODULE_REQUIRES_HUMAN_REVIEW")

    # APPLY-time guardrails, before the first write. Re-checked here rather than trusted
    # from staging for two reasons: the limits may have been tightened while this
    # version sat staged, and a version's payload can be rewritten in place by the
    # identity-review path without passing through create_module_draft again.
    _enforce_module_write_guardrails(
        pdp_id=pdp_id,
        module_key=module_key,
        payload=payload,
        before=_json_dict((await _current_published_version(pdp_id, module_key) or {}).get("payload")),
        actor_type=actor_type,
        at_apply=True,
    )

    now = _now()
    await database.execute(
        pdp_module_versions.update()
        .where(
            (pdp_module_versions.c.pdp_id == pdp_id)
            & (pdp_module_versions.c.module_key == module_key)
            & (pdp_module_versions.c.stage == "published")
            & (pdp_module_versions.c.superseded_at.is_(None))
        )
        .values(status="superseded", superseded_at=now)
    )
    published_row = {
        "id": f"pdpmod_{uuid.uuid4().hex}",
        "pdp_id": pdp_id,
        "module_key": module_key,
        "stage": "published",
        "version": await _next_module_version(pdp_id, module_key),
        "status": "published",
        "payload": payload,
        "source_refs": source_refs,
        "review_actor_type": actor_type,
        "review_actor_id": actor_id,
        "review_model": review_model,
        "review_decision": "pass",
        "review_confidence": review_confidence,
        "review_rubric": review_rubric or {},
        "risk_level": module_risk_level(module_key, payload),
        "requires_human": module_requires_human_review(module_key, payload),
        "generated_by": version.get("generated_by"),
        "generation_ref": version.get("generation_ref"),
        "created_by_employee_id": version.get("created_by_employee_id"),
        "created_at": now,
        "published_at": now,
        "superseded_at": None,
    }
    await database.execute(pdp_module_versions.insert().values(**published_row))
    await database.execute(
        pdp_module_versions.update()
        .where(pdp_module_versions.c.id == version_id)
        .values(status="published_from_staged", superseded_at=now)
    )
    await _audit(
        pdp_id=pdp_id,
        module_key=module_key,
        action="module_published",
        actor_type=actor_type,
        actor_id=actor_id,
        details={"source_version_id": version_id, "published_version_id": published_row["id"]},
    )
    if SKU_OPT_OVERLAY_V1_ENABLED:
        try:
            await materialize_overlay_from_module(
                pdp_id=pdp_id,
                module_key=module_key,
                published_version_id=published_row["id"],
                payload=payload,
                actor_type=actor_type,
                actor_id=actor_id,
            )
        except Exception:  # overlay materialization must never block a publish
            import logging

            logging.getLogger(__name__).exception(
                "merchant_product_overlay materialization failed for pdp_id=%s module=%s",
                pdp_id,
                module_key,
            )
    return _serialize_module(published_row)


async def rollback_module(
    *,
    pdp_id: str,
    module_key: str,
    target_version_id: str,
    actor_type: str = REVIEW_ACTOR_HUMAN,
    actor_id: Optional[str] = None,
    actor_role: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_pdp_governance_tables()
    _validate_module_key(module_key)
    if actor_type == REVIEW_ACTOR_HUMAN and not is_senior_employee_role(actor_role):
        raise PermissionError("PDP_REVIEW_ACTION_FORBIDDEN")
    target = await _fetch_module_version(pdp_id, module_key, target_version_id)
    if target.get("stage") != "published":
        raise ValueError("ROLLBACK_TARGET_MUST_BE_PUBLISHED")
    now = _now()
    await database.execute(
        pdp_module_versions.update()
        .where(
            (pdp_module_versions.c.pdp_id == pdp_id)
            & (pdp_module_versions.c.module_key == module_key)
            & (pdp_module_versions.c.stage == "published")
            & (pdp_module_versions.c.superseded_at.is_(None))
        )
        .values(status="superseded", superseded_at=now)
    )
    rollback_row = {
        "id": f"pdpmod_{uuid.uuid4().hex}",
        "pdp_id": pdp_id,
        "module_key": module_key,
        "stage": "published",
        "version": await _next_module_version(pdp_id, module_key),
        "status": "published",
        "payload": _json_dict(target.get("payload")),
        "source_refs": _json_list(target.get("source_refs")),
        "review_actor_type": actor_type,
        "review_actor_id": actor_id,
        "review_model": None,
        "review_decision": "pass",
        "review_confidence": 1.0,
        "review_rubric": {"rollback_from_version_id": target_version_id},
        "risk_level": target.get("risk_level") or module_risk_level(module_key, _json_dict(target.get("payload"))),
        "requires_human": bool(target.get("requires_human")),
        "generated_by": target.get("generated_by"),
        "generation_ref": target.get("generation_ref"),
        "created_by_employee_id": actor_id if actor_type == REVIEW_ACTOR_HUMAN else None,
        "created_at": now,
        "published_at": now,
        "superseded_at": None,
    }
    await database.execute(pdp_module_versions.insert().values(**rollback_row))
    await _audit(
        pdp_id=pdp_id,
        module_key=module_key,
        action="module_rolled_back",
        actor_type=actor_type,
        actor_id=actor_id,
        details={"target_version_id": target_version_id, "published_version_id": rollback_row["id"]},
    )
    return _serialize_module(rollback_row)


async def create_merchant_contribution(
    *,
    product_key: str,
    merchant_id: str,
    module_key: str,
    payload: Dict[str, Any],
    notes: Optional[str] = None,
    market: str = DEFAULT_MARKET,
) -> Dict[str, Any]:
    _validate_module_key(module_key)
    product_merchant_id, _, _ = parse_product_key(product_key)
    if product_merchant_id != merchant_id:
        raise PermissionError("MERCHANT_PRODUCT_FORBIDDEN")
    projection = await get_pdp_projection(product_key=product_key, market=market)
    pdp_id = projection["pdp"]["pdp_id"]
    contribution_id = f"pdpcontrib_{uuid.uuid4().hex}"
    now = _now()
    await database.execute(
        merchant_pdp_contributions.insert().values(
            id=contribution_id,
            pdp_id=pdp_id,
            product_key=product_key,
            merchant_id=merchant_id,
            module_key=module_key,
            payload=payload,
            notes=notes,
            status="submitted",
            reviewed_by_actor_type=None,
            reviewed_by_actor_id=None,
            review_decision=None,
            review_notes=None,
            created_at=now,
            updated_at=now,
        )
    )
    draft = await create_module_draft(
        pdp_id=pdp_id,
        module_key=module_key,
        payload=payload,
        source_refs=[{"type": "merchant_contribution", "id": contribution_id, "product_key": product_key}],
        generated_by="merchant_contribution",
        generation_ref=contribution_id,
        actor_type="merchant",
        actor_id=merchant_id,
    )
    await _audit(
        pdp_id=pdp_id,
        module_key=module_key,
        action="merchant_contribution_submitted",
        actor_type="merchant",
        actor_id=merchant_id,
        details={"contribution_id": contribution_id, "draft_version_id": draft["id"]},
    )
    return {
        "status": "success",
        "contribution": {
            "id": contribution_id,
            "pdp_id": pdp_id,
            "product_key": product_key,
            "merchant_id": merchant_id,
            "module_key": module_key,
            "status": "submitted",
            "notes": notes,
            "created_at": _iso(now),
        },
        "draft": draft,
    }

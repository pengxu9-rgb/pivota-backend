from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

POLICY_NAME = "text_rules_v1"

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
    return merged

"""Layer 2 of the merchant-audit output-quality assurance: an LLM REVIEW GATE
for the fuzzy long tail that deterministic invariants (Layer 1) cannot state.

It reads three things — the merchant IDENTITY, the merchant-facing CLAIMS (the
rendered prose), and the EVIDENCE — and returns a pass/flag verdict. It is a
GATE, never an editor: it never returns rewritten prose. On a flag (or any
error/timeout/missing-key) the caller withholds the LLM-generated surface and
falls back to the deterministic-safe rendering, which Layer 1 already guarantees
is contradiction-free. So the gate can only ever SUBTRACT a surface, never
produce different run-to-run content (deterministic-by-fallback).

Default-OFF and key-gated (mirrors the strategic brief): with the flag off or no
provider key, the gate is skipped and output ships unchanged. Scope at launch
(founder-locked): prose surfaces only — the strategic brief and the
sku_intelligence money-shot headline. brand_report numbers are Layer 1's job.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, MutableMapping, Optional

from services.llm_synthesis import (
    LLMSynthesisError,
    configured_key_for_provider,
    default_model_for_provider,
    normalize_provider,
    synthesize,
)

try:  # the safety lexicon is shared with the brief; tolerate import drift
    from services.strategic_brief import _SAFETY_SENSITIVE_TERMS as _SAFETY_TERMS
except Exception:  # noqa: BLE001
    _SAFETY_TERMS = {"kids", "children", "infant", "pregnant", "prenatal", "nursing",
                     "diabetic", "diabetics", "medication"}

logger = logging.getLogger(__name__)

VERDICT_PASS = "pass"
VERDICT_FLAG = "flag"
VERDICT_ERROR = "error"
VERDICT_SKIPPED = "skipped"

_DEFAULT_PROVIDER = "deepseek"


def _enabled() -> bool:
    return os.getenv("AUDIT_REVIEW_GATE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    surface: str = ""
    findings: tuple = ()
    severity: str = "none"
    note: str = ""

    def should_withhold(self) -> bool:
        """Deterministic-by-fallback: withhold the LLM surface on a flag or an
        error while the gate is enabled. pass/skipped keep the surface."""
        return self.verdict in (VERDICT_FLAG, VERDICT_ERROR)


_SYSTEM = (
    "You are a strict fact-checking GATE for merchant-facing AI-commerce audit "
    "copy. You are NOT an editor: never rewrite, never suggest text. You are "
    "given the merchant IDENTITY, the merchant-facing CLAIMS, and the EVIDENCE "
    "the claims must trace to. Decide if every claim is supported by the "
    "evidence.\n"
    "FLAG (verdict='flag') if ANY claim:\n"
    "- asserts a competitor LACKS / doesn't offer / is the only one without "
    "something (absence is never proven by the evidence);\n"
    "- names a channel, community, subreddit, influencer/handle, or brand not "
    "present in the evidence;\n"
    "- cites a number, rank, or percentage not in the evidence;\n"
    "- describes the merchant's OWN site/brand as a competitor or controller;\n"
    "- uses a lane/phrase that is not grounded in the evidence's attribute "
    "words or lanes;\n"
    "- makes an unsafe health claim (e.g. for " + ", ".join(sorted(_SAFETY_TERMS)[:6]) + ", etc.).\n"
    "Otherwise verdict='pass'. Judge ONLY grounding, not style.\n"
    "Respond with STRICT JSON only: "
    '{"verdict":"pass"|"flag","severity":"none"|"low"|"high",'
    '"findings":[{"claim":"...","issue":"..."}]}'
)


def _build_user(*, surface: str, identity: Mapping[str, Any], claims: Any, evidence: Any) -> str:
    payload = {
        "surface": surface,
        "merchant_identity": identity,
        "claims": claims,
        "evidence": evidence,
    }
    return (
        "Review this surface. CLAIMS must trace to EVIDENCE; absence is never "
        "proof of a competitor lacking something.\n\n"
        + json.dumps(payload, ensure_ascii=False, default=str)[:12000]
    )


def _parse_verdict(text: str) -> Optional[Dict[str, Any]]:
    # W3: shared tolerant parse; domain gate (a valid verdict value) unchanged.
    from services.llm_io import parse_llm_object

    obj = parse_llm_object(text, label="audit_review_gate")
    if obj and str(obj.get("verdict", "")).lower() in (VERDICT_PASS, VERDICT_FLAG):
        return obj
    return None


async def review_merchant_surface(
    *,
    surface: str,
    rendered_output: Any,
    identity: Mapping[str, Any],
    claims: Any,
    evidence: Any,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> ReviewVerdict:
    """Review one merchant-facing prose surface. Returns a verdict only; never
    rewrites. Default-off + key-gated → VERDICT_SKIPPED (caller keeps the
    surface). Error/unparseable → VERDICT_ERROR (caller withholds). Never raises.
    """
    if not _enabled():
        return ReviewVerdict(VERDICT_SKIPPED, surface=surface, note="gate_disabled")
    try:
        prov = normalize_provider(provider or _DEFAULT_PROVIDER)
    except LLMSynthesisError:
        return ReviewVerdict(VERDICT_SKIPPED, surface=surface, note="bad_provider")
    if not configured_key_for_provider(prov):
        return ReviewVerdict(VERDICT_SKIPPED, surface=surface, note="no_key")
    mdl = str(model or "").strip() or default_model_for_provider(prov)

    # The claims to review default to the rendered output itself.
    review_claims = claims if claims is not None else rendered_output
    user = _build_user(surface=surface, identity=identity, claims=review_claims, evidence=evidence)
    try:
        result = await synthesize(
            system=_SYSTEM, user=user, provider=prov, model=mdl, max_tokens=600,
        )
    except LLMSynthesisError as exc:
        logger.warning("AUDIT_REVIEW_GATE_LLM_ERROR surface=%s err=%s", surface, exc)
        return ReviewVerdict(VERDICT_ERROR, surface=surface, note="llm_error")
    except Exception as exc:  # noqa: BLE001 — the gate must never crash the audit
        logger.warning("AUDIT_REVIEW_GATE_UNEXPECTED surface=%s err=%s", surface, exc)
        return ReviewVerdict(VERDICT_ERROR, surface=surface, note="unexpected")

    parsed = _parse_verdict(result.get("text"))
    if parsed is None:
        logger.warning("AUDIT_REVIEW_GATE_UNPARSEABLE surface=%s", surface)
        return ReviewVerdict(VERDICT_ERROR, surface=surface, note="unparseable")
    verdict = str(parsed.get("verdict", "")).lower()
    findings = tuple(
        f for f in (parsed.get("findings") or []) if isinstance(f, Mapping)
    )
    severity = str(parsed.get("severity") or ("high" if verdict == VERDICT_FLAG else "none"))
    if verdict == VERDICT_FLAG:
        logger.warning(
            "AUDIT_REVIEW_GATE_FLAG surface=%s severity=%s findings=%s",
            surface, severity, json.dumps(list(findings), default=str)[:800],
        )
    return ReviewVerdict(verdict, surface=surface, findings=findings, severity=severity)


def _get(d: Any, *keys: str) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def _identity_dict(payload: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        from services.audit_invariants import resolve_merchant_identity
        ident = resolve_merchant_identity(payload)
        return {"hosts": sorted(ident.hosts), "merchant_name": ident.merchant_name}
    except Exception:  # noqa: BLE001
        brand = payload.get("brand_report") if isinstance(payload, Mapping) else {}
        brand = brand if isinstance(brand, Mapping) else {}
        return {"hosts": [h for h in [brand.get("merchant_domain")] if h],
                "merchant_name": brand.get("merchant_name")}


def _money_shot_evidence(sku: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "buyer_path_verdict": _get(payload, "brand_report", "aggregate", "buyer_path_verdict"),
        "top_open_lanes": sku.get("top_open_lanes"),
        "substitution_alert": sku.get("substitution_alert"),
        "intent_ladder": sku.get("intent_ladder"),
    }


def _brief_evidence(sku: Mapping[str, Any]) -> Dict[str, Any]:
    brief = _get(sku, "next_best_action", "strategic_brief")
    return {
        "grounding_notes": brief.get("grounding_notes") if isinstance(brief, Mapping) else None,
        "intent_ladder": sku.get("intent_ladder"),
        "top_open_lanes": sku.get("top_open_lanes"),
        "substitution_alert": sku.get("substitution_alert"),
        "hero_sku": sku.get("hero_sku"),
    }


async def apply_audit_review_gate(
    payload: MutableMapping[str, Any],
    *,
    run_id: Optional[str] = None,
) -> Dict[str, str]:
    """Review the prose surfaces (money-shot + strategic brief) AFTER Layer 1,
    and withhold any surface the gate flags. Default-off: returns immediately
    with no payload change when disabled/no-key. Mutates in place; never raises.
    Returns {surface: verdict} for telemetry."""
    out: Dict[str, str] = {}
    if not _enabled() or not isinstance(payload, MutableMapping):
        return out
    sku = payload.get("sku_intelligence")
    if not isinstance(sku, MutableMapping):
        return out
    identity = _identity_dict(payload)

    try:
        headline = sku.get("headline")
        if isinstance(headline, str) and headline.strip():
            v = await review_merchant_surface(
                surface="money_shot", rendered_output=headline, identity=identity,
                claims=headline, evidence=_money_shot_evidence(sku, payload),
            )
            out["money_shot"] = v.verdict
            if v.should_withhold():
                sku["headline"] = None
                logger.error("AUDIT_REVIEW_GATE_WITHHELD surface=money_shot run_id=%s verdict=%s",
                             run_id, v.verdict)

        brief = _get(sku, "next_best_action", "strategic_brief")
        if isinstance(brief, Mapping) and brief:
            v = await review_merchant_surface(
                surface="strategic_brief", rendered_output=brief, identity=identity,
                claims=brief, evidence=_brief_evidence(sku),
            )
            out["strategic_brief"] = v.verdict
            if v.should_withhold():
                nba = sku.get("next_best_action")
                if isinstance(nba, MutableMapping):
                    nba["strategic_brief"] = None
                logger.error("AUDIT_REVIEW_GATE_WITHHELD surface=strategic_brief run_id=%s verdict=%s",
                             run_id, v.verdict)
    except Exception as exc:  # noqa: BLE001 — the gate must never crash the audit
        logger.warning("AUDIT_REVIEW_GATE_APPLY_FAILED run_id=%s err=%s", run_id, exc)
    return out

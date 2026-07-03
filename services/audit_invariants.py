"""Layer 1 of the merchant-audit output-quality assurance: a DETERMINISTIC
invariant/sanity layer over the final merchant-facing payload.

Two-layer architecture (design `~/dev/Markato/audit_output_quality_two_layer_design.md`):
  Layer 1 (this module) — hard, statable assertions that FAIL LOUD on
    contradictions a merchant should never see (the merchant's OWN host labelled
    a third-party controller; cross-field inconsistency). Pure + deterministic.
  Layer 2 (separate) — an LLM review gate for the fuzzy long tail (prose that
    outruns evidence). Never auto-edits; deterministic-by-fallback.

`check_audit_invariants(payload, identity)` is the SINGLE entry point, used by
BOTH a runtime adapter (degrade the affected surface + alert on critical) AND a
CI test whose keystone is the exact URL-audit P0 (the merchant's own domain in
`top_controllers` with `merchant_owned_count == 0`).

Scope note (learned from the P0 investigation): the merchant-facing payload
STRIPS first-party hosts from the displayed `sources`/controller lists, and the
strategic brief is prose, so "merchant cited in grounding but ownership 0" and
brief-prose misattribution are NOT cleanly statable from the final payload —
those belong to Layer 2. What IS statable, and what this layer asserts, is that
the merchant's own host must never APPEAR as a controller/competitor anywhere,
plus cross-field numeric consistency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Shared host normalizer + redirector set — import the SAME ones the audit
# pipeline scores with so this layer can't disagree on host identity.
from services.agent_center_bd_report_service import (
    normalize_host,
    _VERTEX_REDIRECTOR_HOSTS,
)
# W1 RunFacts — the same fold the report assembly stamps with, so the
# rollup-reconciliation invariant can never drift from the stamping code.
from services.audit_facts import aggregate_run_facts

SEVERITY_CRITICAL = "critical"
SEVERITY_WARN = "warn"

SURFACE_BRAND = "brand_report"
SURFACE_SKU = "sku_intelligence"
SURFACE_BRIEF = "strategic_brief"
# Rendered-copy violations are ALARM-ONLY: they log at ERROR (alertable) but
# never degrade a surface — detection belongs here, prevention belongs to the
# generation layer (shared LLM envelope parsing / schema-constrained output).
SURFACE_RENDERED = "rendered_copy"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MerchantIdentity:
    """The audited merchant's own identity, resolved from the final payload."""
    hosts: frozenset
    merchant_domain: Optional[str] = None
    merchant_name: Optional[str] = None


@dataclass(frozen=True)
class Violation:
    code: str
    severity: str
    surface: str
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InvariantReport:
    violations: Tuple[Violation, ...] = ()

    def critical(self) -> Tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity == SEVERITY_CRITICAL)

    def warnings(self) -> Tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity == SEVERITY_WARN)

    def for_surface(self, surface: str) -> Tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.surface == surface)

    def has_critical(self) -> bool:
        return any(v.severity == SEVERITY_CRITICAL for v in self.violations)


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------
def _norm(host: Any) -> Optional[str]:
    if not host or not isinstance(host, str):
        return None
    return normalize_host(host) or None


def _is_own_host(host: Any, identity: MerchantIdentity) -> bool:
    h = _norm(host)
    if not h:
        return False
    if h in identity.hosts:
        return True
    # A subdomain of the merchant's own host is still first-party
    # (shop.bblab.shop), but never over-match (notbblab.shop must NOT match).
    return any(h == own or h.endswith("." + own) for own in identity.hosts if own)


def _is_redirector(host: Any) -> bool:
    h = _norm(host)
    return bool(h and h in _VERTEX_REDIRECTOR_HOSTS)


def _looks_like_host(value: Any) -> bool:
    """A controller entry should be a domain, not a redirector URI or a display
    name. True only for a clean host token."""
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    if not text or " " in text or "/" in text:
        return False
    return "." in text and _norm(text) is not None


def _as_host_list(value: Any) -> List[str]:
    """A controller field may be a single host or a list of hosts."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


def _get(d: Any, *keys: str) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------
def resolve_merchant_identity(payload: Mapping[str, Any]) -> MerchantIdentity:
    """Build the merchant's first-party host set from the final payload.

    Mirrors the field set of `_first_party_url_candidates`
    (services/agent_center_bd_report_service.py) so this layer and the
    first-party detection used in scoring agree on what counts as the merchant's
    own host — the drift between those two notions is what created the P0.
    """
    payload = payload or {}
    brand = payload.get("brand_report") if isinstance(payload, Mapping) else None
    brand = brand if isinstance(brand, Mapping) else {}

    candidates: List[Any] = [
        payload.get("audited_url"),
        brand.get("merchant_domain"),
        _get(payload, "sku_intelligence", "hero_sku", "pdp_url"),
    ]
    for prod in (payload.get("audited_products") or []):
        if isinstance(prod, Mapping):
            candidates += [prod.get("pdp_url"), prod.get("url"),
                           prod.get("canonical_url"), prod.get("pivota_canonical_url")]
    for prod in (brand.get("per_product") or []):
        if isinstance(prod, Mapping):
            candidates += [prod.get("canonical_url"), prod.get("pivota_canonical_url"),
                           prod.get("pdp_url"), prod.get("url")]

    hosts = frozenset(h for h in (_norm(c) for c in candidates) if h)
    merchant_domain = _norm(brand.get("merchant_domain"))
    name = brand.get("merchant_name")
    merchant_name = str(name).strip() if isinstance(name, str) and name.strip() else None
    return MerchantIdentity(hosts=hosts, merchant_domain=merchant_domain, merchant_name=merchant_name)


# ---------------------------------------------------------------------------
# Controller-host collection (drives A1 / A2 / D1)
# ---------------------------------------------------------------------------
def _buyer_path_verdict(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    bpv = _get(payload, "brand_report", "aggregate", "buyer_path_verdict")
    return bpv if isinstance(bpv, Mapping) else {}


def _collect_controller_entries(payload: Mapping[str, Any]) -> List[Tuple[str, str, str]]:
    """Every host presented to the merchant as a third-party controller/owner,
    as (surface, location, host). These are the lists that must NEVER contain
    the merchant's own host."""
    out: List[Tuple[str, str, str]] = []

    for host in _as_host_list(_buyer_path_verdict(payload).get("top_controllers")):
        out.append((SURFACE_BRAND, "aggregate.buyer_path_verdict.top_controllers", host))

    sku = payload.get("sku_intelligence")
    sku = sku if isinstance(sku, Mapping) else {}
    for i, row in enumerate(sku.get("prompt_matrix") or []):
        if not isinstance(row, Mapping):
            continue
        for host in _as_host_list(row.get("who_owns")):
            out.append((SURFACE_SKU, f"prompt_matrix[{i}].who_owns", host))
        bpa = row.get("buyer_path_action")
        bpa = bpa if isinstance(bpa, Mapping) else {}
        for host in _as_host_list(bpa.get("controllers")):
            out.append((SURFACE_SKU, f"prompt_matrix[{i}].buyer_path_action.controllers", host))
        profile = bpa.get("controller_profile")
        profile = profile if isinstance(profile, Mapping) else {}
        for key in ("controllers", "leading_controllers", "known_retail_controllers",
                    "source_authority_controllers"):
            for host in _as_host_list(profile.get(key)):
                out.append((SURFACE_SKU, f"prompt_matrix[{i}].buyer_path_action.controller_profile.{key}", host))
    for i, lane in enumerate(sku.get("top_open_lanes") or []):
        if not isinstance(lane, Mapping):
            continue
        for host in _as_host_list(lane.get("controllers")):
            out.append((SURFACE_SKU, f"top_open_lanes[{i}].controllers", host))
        for host in _as_host_list(lane.get("who_owns")):
            out.append((SURFACE_SKU, f"top_open_lanes[{i}].who_owns", host))
    return out


def _strategic_brief(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    sb = _get(payload, "sku_intelligence", "next_best_action", "strategic_brief")
    return sb if isinstance(sb, Mapping) else {}


# ---------------------------------------------------------------------------
# Invariants (each pure; returns a list of Violations)
# ---------------------------------------------------------------------------
def _inv_own_host_as_controller(payload, identity) -> List[Violation]:
    """A1 (the P0) / A2 — the merchant's own host must never appear as a
    third-party controller/owner on any surface. D1 — controllers must be clean
    host tokens, never redirector URIs or non-hosts."""
    out: List[Violation] = []
    for surface, location, host in _collect_controller_entries(payload):
        if _is_own_host(host, identity):
            code = "OWN_HOST_AS_CONTROLLER" if surface == SURFACE_BRAND else "OWN_HOST_AS_COMPETITOR_SKU"
            out.append(Violation(
                code=code, severity=SEVERITY_CRITICAL, surface=surface,
                message=(f"merchant's own host {_norm(host)!r} appears as a third-party "
                         f"controller at {location}"),
                evidence={"host": host, "location": location, "own_hosts": sorted(identity.hosts)},
            ))
        elif _is_redirector(host) or not _looks_like_host(host):
            out.append(Violation(
                code="REDIRECTOR_OR_NONHOST_CONTROLLER", severity=SEVERITY_CRITICAL,
                surface=surface,
                message=(f"controller entry {host!r} at {location} is not a clean host "
                         f"(redirector URI or display name leaked into a controller list)"),
                evidence={"host": host, "location": location},
            ))
    return out


def _inv_strategic_brief_channels(payload, identity) -> List[Violation]:
    """A3 — when the brief carries STRUCTURED grounding channels (not always
    present; the display brief is prose → Layer 2's job), the merchant's own
    host must not be listed as a third-party evidenced channel or named as the
    competitor."""
    out: List[Violation] = []
    notes = _strategic_brief(payload).get("grounding_notes")
    notes = notes if isinstance(notes, Mapping) else {}
    for i, ch in enumerate(notes.get("evidenced_channels") or []):
        if isinstance(ch, Mapping) and _is_own_host(ch.get("host"), identity):
            out.append(Violation(
                code="OWN_HOST_AS_THIRD_PARTY_BRIEF", severity=SEVERITY_CRITICAL,
                surface=SURFACE_BRIEF,
                message=f"merchant's own host listed as a third-party evidenced channel",
                evidence={"host": ch.get("host"), "location": f"grounding_notes.evidenced_channels[{i}]"},
            ))
    comp = _get(notes, "competitor_attributes", "competitor")
    if comp and (_is_own_host(comp, identity)
                 or (identity.merchant_name and str(comp).strip().lower() == identity.merchant_name.lower())):
        out.append(Violation(
            code="OWN_AS_COMPETITOR_BRIEF", severity=SEVERITY_CRITICAL, surface=SURFACE_BRIEF,
            message=f"brief names the merchant itself as the competitor: {comp!r}",
            evidence={"competitor": comp},
        ))
    return out


def _inv_state_vs_counts(payload, identity) -> List[Violation]:
    """C1 — buyer-path state must be consistent with the owned/controlled counts
    and the controller list."""
    out: List[Violation] = []
    bpv = _buyer_path_verdict(payload)
    if not bpv:
        return out
    state = bpv.get("state")
    owned = bpv.get("merchant_owned_count")
    controllers = _as_host_list(bpv.get("top_controllers"))
    if state == "merchant_controlled" and isinstance(owned, int) and owned <= 0:
        out.append(Violation(
            code="STATE_VS_OWNED_COUNT", severity=SEVERITY_CRITICAL, surface=SURFACE_BRAND,
            message="state is 'merchant_controlled' but merchant_owned_count <= 0",
            evidence={"state": state, "merchant_owned_count": owned},
        ))
    if state == "third_party_controlled":
        non_own = [c for c in controllers if not _is_own_host(c, identity)]
        if not non_own:
            out.append(Violation(
                code="STATE_VS_CONTROLLERS", severity=SEVERITY_CRITICAL, surface=SURFACE_BRAND,
                message="state is 'third_party_controlled' but no non-merchant controller is listed",
                evidence={"state": state, "top_controllers": controllers},
            ))
    return out


def _inv_count_overflow(payload, identity) -> List[Violation]:
    """C2 — counts must be non-negative and not exceed the prompt count."""
    out: List[Violation] = []
    bpv = _buyer_path_verdict(payload)
    owned = bpv.get("merchant_owned_count")
    third = bpv.get("third_party_controlled_count")
    prompts = bpv.get("prompt_count")
    nums = {"merchant_owned_count": owned, "third_party_controlled_count": third,
            "prompt_count": prompts}
    for name, val in nums.items():
        if isinstance(val, int) and val < 0:
            out.append(Violation(
                code="NEGATIVE_COUNT", severity=SEVERITY_CRITICAL, surface=SURFACE_BRAND,
                message=f"{name} is negative ({val})", evidence={name: val},
            ))
    if all(isinstance(v, int) for v in (owned, third, prompts)):
        if owned + third > prompts:
            out.append(Violation(
                code="COUNT_OVERFLOW", severity=SEVERITY_CRITICAL, surface=SURFACE_BRAND,
                message=(f"merchant_owned_count + third_party_controlled_count "
                         f"({owned}+{third}) exceeds prompt_count ({prompts})"),
                evidence=dict(nums),
            ))
    return out


def _inv_self_substitution(payload, identity) -> List[Violation]:
    """C3 — the substitution alert must never name the merchant itself as the
    competitor that substitutes for it."""
    out: List[Violation] = []
    alert = _get(payload, "sku_intelligence", "substitution_alert")
    alert = alert if isinstance(alert, Mapping) else {}
    if not alert.get("present"):
        return out
    by = alert.get("substituted_by")
    if not by:
        return out
    is_self = _is_own_host(by, identity) or (
        identity.merchant_name and str(by).strip().lower() == identity.merchant_name.lower()
    )
    if is_self:
        out.append(Violation(
            code="SELF_SUBSTITUTION", severity=SEVERITY_CRITICAL, surface=SURFACE_SKU,
            message=f"substitution alert names the merchant itself as the substitute: {by!r}",
            evidence={"substituted_by": by, "merchant_name": identity.merchant_name},
        ))
    return out


def _inv_strong_but_unowned(payload, identity) -> List[Violation]:
    """C4 (WARN) — branded-transactional intent reads strong while the merchant
    owns 0 lanes. Legitimate for a strong brand with thin owned-lane coverage, so
    surface for review, never degrade."""
    out: List[Violation] = []
    bpv = _buyer_path_verdict(payload)
    owned = bpv.get("merchant_owned_count")
    if not (isinstance(owned, int) and owned == 0):
        return out
    branded = _get(payload, "sku_intelligence", "intent_ladder", "branded_transactional")
    branded = branded if isinstance(branded, Mapping) else {}
    score = branded.get("score")
    if isinstance(score, (int, float)) and score >= 80:
        out.append(Violation(
            code="STRONG_BUT_UNOWNED", severity=SEVERITY_WARN, surface=SURFACE_SKU,
            message=(f"branded-transactional intent score {score} is strong but "
                     f"merchant_owned_count is 0 — verify the merchant truly owns nothing"),
            evidence={"branded_transactional_score": score, "merchant_owned_count": owned},
        ))
    return out


# ---------------------------------------------------------------------------
# Rendered-copy invariant (W7.1) — no machine text in merchant-facing strings
# ---------------------------------------------------------------------------
# Keys whose SUBTREES legitimately hold raw model/upstream output (probe runs,
# debug traces, the merchant's own typed input). Everything else in the payload
# is merchant-renderable and must never contain a markdown code fence, an
# upstream error marker, or a raw structured-probe envelope. This is the
# class-wide net for the 2026-07-03 "category winner rendered ```json {...}"
# leak — that string lived in competitor_intel.known_for, a rendered field.
_MACHINE_ONLY_KEYS = frozenset({
    "raw", "raw_runs", "raw_output", "parsed",
    "brief_debug", "grounding_failures",
    "grounding_chunks", "grounding_sources",
    "error", "error_jsonb", "traceback_truncated",
    "probe_runs", "per_sku_probe_runs",
    "partial_result_jsonb", "cost_summary_jsonb",
    # The merchant's own typed input, echoed back — not our copy.
    "custom_prompts",
})

# (marker substring, violation code). Envelope-key fragments catch a raw JSON
# probe response leaking into prose even when its code fence was stripped.
_MACHINE_TEXT_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("```", "MARKDOWN_FENCE_IN_COPY"),
    ("__error__", "UPSTREAM_ERROR_MARKER_IN_COPY"),
    ('"product_visible"', "RAW_LLM_ENVELOPE_IN_COPY"),
    ('"evidence_excerpt"', "RAW_LLM_ENVELOPE_IN_COPY"),
    ('"competitors_listed"', "RAW_LLM_ENVELOPE_IN_COPY"),
    ('"grounding_sources"', "RAW_LLM_ENVELOPE_IN_COPY"),
)

_RENDERED_COPY_MAX_VIOLATIONS = 10
_RENDERED_COPY_MAX_DEPTH = 40


def _inv_no_machine_text_in_copy(payload, identity) -> List[Violation]:
    """W7.1 — walk every merchant-renderable string in the payload and flag
    machine text (markdown fences, upstream `__error__` markers, raw probe
    envelopes). Subtrees under _MACHINE_ONLY_KEYS are skipped: raw runs and
    debug fields legitimately hold that content. Capped so a systemic leak
    reports a sample, not thousands of violations."""
    out: List[Violation] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if len(out) >= _RENDERED_COPY_MAX_VIOLATIONS or depth > _RENDERED_COPY_MAX_DEPTH:
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key) in _MACHINE_ONLY_KEYS:
                    continue
                walk(value, f"{path}.{key}" if path else str(key), depth + 1)
        elif isinstance(node, (list, tuple)):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]", depth + 1)
        elif isinstance(node, str):
            for marker, code in _MACHINE_TEXT_MARKERS:
                if marker in node:
                    out.append(Violation(
                        code=code,
                        severity=SEVERITY_CRITICAL,
                        surface=SURFACE_RENDERED,
                        message=(
                            f"machine text {marker!r} found in a merchant-"
                            f"facing string at {path}"
                        ),
                        evidence={"path": path, "snippet": node[:160]},
                    ))
                    break  # one violation per string is enough

    walk(payload, "", 0)
    return out


# ---------------------------------------------------------------------------
# W1 RunFacts invariant — displayed counters must reconcile to the fact layer
# ---------------------------------------------------------------------------
# Counter fields a rolled-up run_facts dict must agree on with the fold of its
# children's run_facts stamps (aggregate_run_facts is the SAME function the
# assembly used, so a mismatch means the stamps were mutated after assembly or
# a child was added/dropped without re-folding).
_RUNFACTS_FOLD_FIELDS = (
    "own_url_cited_runs",
    "brand_mentioned_runs",
    "run_count",
    "runs_with_citations",
    "prompt_count",
)


def _runfacts_internal_violations(rf: Mapping[str, Any], location: str) -> List[Violation]:
    """A stamped run_facts dict must be internally coherent: tier counters are
    over RUNS and can never exceed run_count; prompt-class and provider splits
    must sum to their totals. WARN severity during the phase-1 parity window —
    surfaced for review, never degrades a surface."""
    out: List[Violation] = []
    run_count = rf.get("run_count")
    if not isinstance(run_count, int):
        return out

    def _warn(code: str, message: str, **evidence: Any) -> None:
        out.append(Violation(
            code=code, severity=SEVERITY_WARN, surface=SURFACE_BRAND,
            message=f"{message} at {location}",
            evidence={"location": location, **evidence},
        ))

    for name in ("own_url_cited_runs", "brand_mentioned_runs", "runs_with_citations"):
        val = rf.get(name)
        if isinstance(val, int) and not (0 <= val <= run_count):
            _warn("RUNFACTS_COUNTER_OVERFLOW",
                  f"run_facts.{name} ({val}) outside [0, run_count={run_count}]",
                  **{name: val, "run_count": run_count})
    prompt_count = rf.get("prompt_count")
    by_class = rf.get("prompt_count_by_class")
    if isinstance(prompt_count, int) and isinstance(by_class, Mapping) and by_class:
        class_sum = sum(int(v or 0) for v in by_class.values())
        if class_sum != prompt_count:
            _warn("RUNFACTS_CLASS_SPLIT_MISMATCH",
                  f"prompt_count_by_class sums to {class_sum} but prompt_count is {prompt_count}",
                  prompt_count=prompt_count, prompt_count_by_class=dict(by_class))
    coverage = rf.get("provider_coverage")
    if isinstance(coverage, Mapping) and coverage:
        coverage_sum = sum(int(v or 0) for v in coverage.values())
        if coverage_sum != run_count:
            _warn("RUNFACTS_PROVIDER_COVERAGE_MISMATCH",
                  f"provider_coverage sums to {coverage_sum} but run_count is {run_count}",
                  run_count=run_count, provider_coverage=dict(coverage))
    return out


def _inv_counters_match_run_facts(payload, identity) -> List[Violation]:
    """W1/W7 — every stamped ``run_facts`` block must be internally coherent,
    and a brand-level rollup must equal the fold of its children's stamps
    (legacy mode: brand_report.run_facts vs per_product[*].run_facts; per-SKU
    mode: brand_rollup.run_facts vs per_sku_reports[*].run_facts). Tolerant of
    absent stamps (pre-W1 payloads yield no violations); skips the fold check
    when any child lacks a stamp (mixed-era payloads)."""
    out: List[Violation] = []
    brand = payload.get("brand_report")
    brand = brand if isinstance(brand, Mapping) else {}

    rollup_sites = (
        ("brand_report.run_facts", brand.get("run_facts"),
         "brand_report.per_product", brand.get("per_product")),
        ("brand_report.brand_rollup.run_facts",
         _get(brand, "brand_rollup", "run_facts"),
         "brand_report.per_sku_reports", brand.get("per_sku_reports")),
    )
    for rollup_loc, rollup, children_loc, children in rollup_sites:
        if isinstance(rollup, Mapping):
            out.extend(_runfacts_internal_violations(rollup, rollup_loc))
        child_facts: List[Any] = []
        for i, child in enumerate(children or []):
            if not isinstance(child, Mapping):
                continue
            rf = child.get("run_facts")
            child_facts.append(rf)
            if isinstance(rf, Mapping):
                out.extend(
                    _runfacts_internal_violations(rf, f"{children_loc}[{i}].run_facts")
                )
        if (
            isinstance(rollup, Mapping)
            and child_facts
            and all(isinstance(rf, Mapping) for rf in child_facts)
        ):
            refold = aggregate_run_facts(child_facts)
            for name in _RUNFACTS_FOLD_FIELDS:
                if rollup.get(name) != refold.get(name):
                    out.append(Violation(
                        code="RUNFACTS_ROLLUP_MISMATCH", severity=SEVERITY_WARN,
                        surface=SURFACE_BRAND,
                        message=(
                            f"{rollup_loc}.{name} ({rollup.get(name)!r}) does not equal "
                            f"the fold of {children_loc}[*].run_facts ({refold.get(name)!r})"
                        ),
                        evidence={"field": name, "rollup": rollup.get(name),
                                  "refold": refold.get(name), "location": rollup_loc},
                    ))
    return out


_INVARIANTS = (
    _inv_own_host_as_controller,
    _inv_strategic_brief_channels,
    _inv_state_vs_counts,
    _inv_count_overflow,
    _inv_self_substitution,
    _inv_strong_but_unowned,
    _inv_no_machine_text_in_copy,
    _inv_counters_match_run_facts,
)


# ---------------------------------------------------------------------------
# Runtime adapter — log + degrade (NOT scrub). Founder-locked: a critical
# violation withholds the contradicted surface (honest fallback) and ships the
# rest, plus a high-severity alertable log. Mutates the payload in place.
# ---------------------------------------------------------------------------
def _degrade_brand_buyer_path(payload: MutableMapping[str, Any]) -> None:
    agg = _get(payload, "brand_report", "aggregate")
    if not isinstance(agg, MutableMapping):
        return
    prior = agg.get("buyer_path_verdict") if isinstance(agg.get("buyer_path_verdict"), Mapping) else {}
    agg["buyer_path_verdict"] = {
        "state": "not_measured",
        "label_display": "Buyer path not confirmed",
        "explanation": ("We could not confirm the buyer-path controllers for this "
                        "audit, so this section is withheld."),
        "prompt_count": prior.get("prompt_count"),
        "merchant_owned_count": None,
        "third_party_controlled_count": None,
        "primary_lane": None,
        "top_controllers": [],
        "quality_gate": {"shareable": False, "reason": "invariant_violation"},
    }


def _suppress_money_shot(payload: MutableMapping[str, Any]) -> None:
    sku = payload.get("sku_intelligence")
    if isinstance(sku, MutableMapping):
        sku["headline"] = None


def _degrade_sku_intelligence(payload: MutableMapping[str, Any], codes: Sequence[str]) -> None:
    existing = payload.get("sku_intelligence")
    hero = existing.get("hero_sku") if isinstance(existing, Mapping) else None
    payload["sku_intelligence"] = {
        "hero_sku": hero or {"title": None, "pdp_url": None, "vendor": None},
        "headline": None,
        "intent_ladder": {},
        "top_open_lanes": [],
        "prompt_matrix": [],
        "substitution_alert": {"present": False},
        "demand_state_summary": None,
        "coverage": {},
        "next_best_action": None,
        "is_empty": True,
        "quality_gate": {
            "shareable": False,
            "reason": "invariant_violation",
            "violations": list(codes),
            "merchant_copy_allowed": False,
        },
    }


def _degrade_strategic_brief(payload: MutableMapping[str, Any]) -> None:
    nba = _get(payload, "sku_intelligence", "next_best_action")
    if isinstance(nba, MutableMapping) and "strategic_brief" in nba:
        nba["strategic_brief"] = None


def enforce_audit_invariants(
    payload: MutableMapping[str, Any],
    *,
    run_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    identity: Optional[MerchantIdentity] = None,
) -> InvariantReport:
    """Runtime gate. Logs every violation (criticals at ERROR — alertable) and,
    for each surface with a CRITICAL, degrades that surface to an honest fallback
    in place (withhold, never scrub-and-ship). Returns the report. Never raises."""
    if not isinstance(payload, MutableMapping):
        return InvariantReport(())
    report = check_audit_invariants(payload, identity)
    if not report.violations:
        return report

    base = {"run_id": run_id, "merchant_id": merchant_id}
    for v in report.warnings():
        logger.warning(
            "AUDIT_INVARIANT_WARN code=%s surface=%s msg=%s", v.code, v.surface, v.message,
            extra={"audit_invariant": {**base, "code": v.code, "surface": v.surface,
                                       "severity": v.severity, "evidence": v.evidence}},
        )
    criticals = report.critical()
    for v in criticals:
        logger.error(
            "AUDIT_INVARIANT_CRITICAL code=%s surface=%s run_id=%s msg=%s",
            v.code, v.surface, run_id, v.message,
            extra={"audit_invariant": {**base, "code": v.code, "surface": v.surface,
                                       "severity": v.severity, "evidence": v.evidence}},
        )
    if not criticals:
        return report

    surfaces = {v.surface for v in criticals}
    codes = sorted({v.code for v in criticals})
    try:
        if SURFACE_BRAND in surfaces:
            _degrade_brand_buyer_path(payload)
            _suppress_money_shot(payload)  # money-shot is derived from the buyer path
        if SURFACE_SKU in surfaces:
            _degrade_sku_intelligence(payload, codes)
        if SURFACE_BRIEF in surfaces:
            _degrade_strategic_brief(payload)
    except Exception as exc:  # noqa: BLE001 — degrade must never crash the audit
        logger.error("AUDIT_INVARIANT_DEGRADE_FAILED run_id=%s err=%s", run_id, exc)
    return report


def check_audit_invariants(
    payload: Mapping[str, Any],
    identity: Optional[MerchantIdentity] = None,
) -> InvariantReport:
    """Run every invariant over the final merchant-facing payload. Pure +
    deterministic; tolerant of missing keys (an absent surface yields no
    violations, never an exception)."""
    if not isinstance(payload, Mapping):
        return InvariantReport(())
    if identity is None:
        identity = resolve_merchant_identity(payload)
    violations: List[Violation] = []
    for inv in _INVARIANTS:
        try:
            violations.extend(inv(payload, identity))
        except Exception as exc:  # noqa: BLE001 — an invariant bug must never crash the audit
            violations.append(Violation(
                code="INVARIANT_INTERNAL_ERROR", severity=SEVERITY_WARN, surface="invariant_layer",
                message=f"invariant {getattr(inv, '__name__', '?')} raised: {exc}",
                evidence={"invariant": getattr(inv, "__name__", "?")},
            ))
    return InvariantReport(tuple(violations))

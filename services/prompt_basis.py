"""W2 — pinned measurement basis for per-SKU audit prompts (2026-07-04).

The LLM-generated discovery prompts (value-prop "winnable" + P4a scenario
elicitation) were regenerated fresh on every run: temperature + provider drift
meant the same SKU was measured against a DIFFERENT question set each time, so
scores were non-comparable by construction (the 82→50 swing on identical URLs)
and the re-audit delta had to mask the noise with a ±15 threshold.

This module makes the prompt set an ASSET OF THE SKU:
  - the first audit generates the basis and stamps it (with a stable
    `prompt_set_id`) on the per-SKU report;
  - re-runs RELOAD the prior basis instead of regenerating — same questions,
    comparable scores, zero extra LLM spend (mirrors
    services/retailer_evidence.load_prior_retailer_evidence, which already
    recycles prior-run evidence the same way);
  - refreshing the basis is an explicit, versioned event, never silent drift:
    bumping PROMPT_BASIS_VERSION (a deliberate generator change) or passing
    `refresh=True` (a deliberate caller action) regenerates; nothing else does.

W2 (winnable/scenario) pins only the stochastic LLM lists. W2.1 pins the FULL
SELECTED probe set — the exact `{query, axis, source, …}` records that were
actually probed. The deterministic sidewalk/base prompts are a pure function of
the attribute graph, BUT that graph is mutated between runs by retailer-evidence
recycling, so the FINAL selected set still drifted a slot or two (prod pair
2026-07-04: conditioner 14/14 identical, shampoo 13/14 with one oscillating
slot). Persisting the selected set closes that residual: a re-run probes the
exact prior queries, so the whole measurement basis — not just the LLM lists —
is identical.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

# Bump when the prompt GENERATORS change materially (new shapes, new mining
# strategy). A stored basis from an older version is ignored, so every SKU
# regenerates ONCE on its next audit — that regeneration is the explicit,
# deploy-visible "measurement basis updated" event.
# v2 (#1281): spec-matched discovery-prompt generators changed materially —
# bidirectional category-noun redundancy guard (kills "best headphones for bone
# conduction headphones"), vertical-gated "what helps with X" (off for
# electronics), and a budget reorder floating LLM winnable/scenario prompts ahead
# of the generic category-template tail. SKUs pinned to a v1 basis (already-
# audited partners) must regenerate to actually receive the fixed prompts.
# v3 (#1521): branded (navigational/trust — product/brand-naming) prompts are now
# CAPPED at a minority share of the per-SKU budget (default 30%, floor 2) with the
# remainder rebalanced to unbranded discovery/category/problem/sidewalk shapes.
# This changes the generated/selected set materially, so SKUs pinned to a v2
# branded-heavy basis must regenerate to receive the rebalanced mix (otherwise a
# re-audit would replay the old ~50%-branded set and mask the change).
PROMPT_BASIS_VERSION = 3

_MAX_PROMPTS_PER_LIST = 12
_MAX_PROMPT_CHARS = 300
# Probe budget per SKU: wedge 14, standard readiness 40, deep landscape 80. Cap
# above the deepest tier (80 + headroom) so a pinned set can never bloat the
# stored report, and drop malformed records. At 64 a deep-tier selected set
# would silently truncate and the re-pinned basis would drift from what was
# actually probed.
_MAX_SELECTED_SPECS = 96
# Record keys worth preserving on a pinned spec — enough to reprobe identically
# AND keep the downstream sidewalk-metadata seam faithful (axis + attribute
# basis + evidence + weight). Anything else on a record is regenerable.
_SELECTED_SPEC_KEYS = ("query", "axis", "source", "attribute_basis", "evidence", "intent_weight")

# Audit depth tiers (deep-tier spec 2026-07-21). The tier is a NAMED product
# unit, never a raw merchant-facing count; it decides the per-SKU probe budget
# and how much of the LLM discovery lists (winnable/scenario) survives the cap.
# The basis is pinned PER TIER: a deep run never reuses a standard basis (or
# vice versa) — different question sets are non-comparable by construction, so
# a tier switch is an explicit baseline reset, not a delta.
AUDIT_TIER_STANDARD = "standard"
AUDIT_TIER_DEEP = "deep"
_TIER_PROMPTS_PER_SKU = {AUDIT_TIER_STANDARD: 40, AUDIT_TIER_DEEP: 80}
_TIER_MAX_PROMPTS_PER_LIST = {AUDIT_TIER_STANDARD: _MAX_PROMPTS_PER_LIST, AUDIT_TIER_DEEP: 18}


def normalize_audit_tier(value: Any) -> str:
    """Coerce any launch-option/stored value to a known tier; unknown → standard."""
    tier = str(value or "").strip().lower()
    return tier if tier in _TIER_PROMPTS_PER_SKU else AUDIT_TIER_STANDARD


def prompts_per_sku_for_tier(audit_tier: Any) -> int:
    return _TIER_PROMPTS_PER_SKU[normalize_audit_tier(audit_tier)]


def max_prompts_per_list_for_tier(audit_tier: Any) -> int:
    return _TIER_MAX_PROMPTS_PER_LIST[normalize_audit_tier(audit_tier)]

# Namespace of the URL-wedge synthetic SKU keys (routes/merchant_audit_routes
# mints them via _synthetic_url_sku_key, deterministic on (merchant_id,
# pdp_url)). The namespace doubles as the run-kind signal: a wedge SKU's prior
# basis lives in prior subject_type='merchant_url' runs, a catalog SKU's in
# subject_type='merchant' runs — scanning the other kind can never match the
# sku_key and only burns scan-window slots.
URL_WEDGE_SKU_PREFIX = "urlwedge:"


def subject_type_for_sku_key(sku_key: str) -> str:
    """The audit-run subject_type whose reports can carry this SKU's basis."""
    return (
        "merchant_url"
        if str(sku_key or "").startswith(URL_WEDGE_SKU_PREFIX)
        else "merchant"
    )


def _clean_prompts(values: Any, max_prompts: int = _MAX_PROMPTS_PER_LIST) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values if isinstance(values, (list, tuple)) else []:
        text = str(value or "").strip()[:_MAX_PROMPT_CHARS]
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
        if len(out) >= max_prompts:
            break
    return out


def clean_selected_specs(records: Any) -> List[Dict[str, Any]]:
    """Sanitize the final selected query records for durable storage: keep only
    the reprobe-relevant keys, JSON-safe, deduped by query, order-preserving.
    A record with no query is dropped."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for record in records if isinstance(records, (list, tuple)) else []:
        if not isinstance(record, Mapping):
            continue
        query = str(record.get("query") or "").strip()[:_MAX_PROMPT_CHARS]
        key = query.lower()
        if not query or key in seen:
            continue
        seen.add(key)
        spec: Dict[str, Any] = {"query": query}
        axis = record.get("axis")
        spec["axis"] = str(axis).strip() if axis else "intent"
        source = record.get("source")
        if source:
            spec["source"] = str(source).strip()
        # Merchant-prompt scope ("sku" vs "brand") must survive pinning: a
        # pinned per-SKU custom re-probes on later runs and the brand-level
        # "Your prompts" panel excludes it by this key.
        custom_scope = record.get("custom_scope")
        if custom_scope:
            spec["custom_scope"] = str(custom_scope).strip()
        basis = record.get("attribute_basis")
        if isinstance(basis, (list, tuple)):
            spec["attribute_basis"] = [str(b) for b in basis if b]
        evidence = record.get("evidence")
        if isinstance(evidence, (list, tuple)):
            spec["evidence"] = [str(e) for e in evidence if e][:6]
        weight = record.get("intent_weight")
        if isinstance(weight, (int, float)):
            spec["intent_weight"] = float(weight)
        out.append(spec)
        if len(out) >= _MAX_SELECTED_SPECS:
            break
    return out


def build_prompt_set_id(winnable: List[str], scenario: List[str]) -> str:
    """Stable identity of the LLM-list basis — same prompts (order-sensitive,
    case-preserving) → same id, across runs and processes."""
    payload = json.dumps(
        {"w": list(winnable), "s": list(scenario)},
        ensure_ascii=False, sort_keys=True,
    )
    return "ps_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_selected_set_id(specs: List[Mapping[str, Any]]) -> str:
    """Stable identity of the FULL probed set — the (query, axis) sequence that
    was actually measured. This, not prompt_set_id, is the true "were we
    measured the same way?" key for the re-audit delta."""
    payload = json.dumps(
        [[str(s.get("query") or ""), str(s.get("axis") or "")] for s in specs],
        ensure_ascii=False,
    )
    return "sel_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def set_selected_specs_on_meta(
    meta: Optional[Mapping[str, Any]],
    built_specs: Any,
) -> Dict[str, Any]:
    """Record the queries ACTUALLY probed this run onto the basis meta.

    Called after probing (pinned or fresh) so the chain never breaks: a pinned
    run re-records the same set; a fresh run establishes it. Returns a new meta
    dict with `selected_specs` + `selected_set_id`; a no-op (returns meta
    unchanged) when there's nothing to record."""
    out = dict(meta) if isinstance(meta, Mapping) else {}
    specs = clean_selected_specs(built_specs)
    if not specs:
        return out
    out["selected_specs"] = specs
    out["selected_set_id"] = build_selected_set_id(specs)
    return out


def harvest_prompt_basis(
    report_jsonb: Mapping[str, Any],
    *,
    sku_key: str,
    audit_tier: str = AUDIT_TIER_STANDARD,
) -> Optional[Dict[str, Any]]:
    """Pull the pinned prompt basis for `sku_key` out of one prior run's
    report. Returns the stored basis dict, or None when absent/unusable
    (wrong version, wrong tier, empty, malformed). Pure + synchronous.

    A basis stamped before tiers existed carries no `audit_tier` and reads as
    standard — so already-audited SKUs keep their pinned standard basis, while
    a first deep run regenerates (the deliberate tier-switch baseline reset)."""
    report = report_jsonb if isinstance(report_jsonb, Mapping) else {}
    brand_report = report.get("brand_report")
    if isinstance(brand_report, Mapping):
        report = brand_report
    per_sku = report.get("per_sku_reports")
    if not isinstance(per_sku, list):
        return None
    for sku_report in per_sku:
        if not isinstance(sku_report, Mapping):
            continue
        if str(sku_report.get("sku_key") or "") != str(sku_key):
            continue
        basis = sku_report.get("prompt_basis")
        if not isinstance(basis, Mapping):
            return None
        if int(basis.get("basis_version") or 0) != PROMPT_BASIS_VERSION:
            return None  # generators changed — explicit regeneration event
        tier = normalize_audit_tier(audit_tier)
        if normalize_audit_tier(basis.get("audit_tier")) != tier:
            return None  # tier switch — explicit baseline reset, never pin across
        list_cap = max_prompts_per_list_for_tier(tier)
        winnable = _clean_prompts(basis.get("winnable"), max_prompts=list_cap)
        scenario = _clean_prompts(basis.get("scenario"), max_prompts=list_cap)
        selected_specs = clean_selected_specs(basis.get("selected_specs"))
        if not winnable and not scenario and not selected_specs:
            return None  # never pin an empty basis (e.g. a prior LLM outage)
        return {
            "audit_tier": tier,
            "winnable": winnable,
            "scenario": scenario,
            # W2.1: the full probed set from the prior run (may be empty for a
            # basis stamped before W2.1 shipped — then only the LLM lists pin,
            # and this run re-records the selected set to establish it).
            "selected_specs": selected_specs,
            "selected_set_id": (
                str(basis.get("selected_set_id") or "")
                or (build_selected_set_id(selected_specs) if selected_specs else None)
            ),
            "prompt_set_id": (
                str(basis.get("prompt_set_id") or "")
                or build_prompt_set_id(winnable, scenario)
            ),
            "created_at": basis.get("created_at"),
        }
    return None


async def load_prior_prompt_basis(
    *,
    merchant_id: str,
    sku_key: str,
    max_runs: int = 3,
    audit_tier: str = AUDIT_TIER_STANDARD,
) -> Optional[Dict[str, Any]]:
    """Best-effort: scan the merchant's most recent completed runs (newest
    first) and return the first pinned basis found for this sku_key, tagged
    with the run it came from. Never raises."""
    if not merchant_id or not sku_key:
        return None
    try:
        from db.merchant_audit_runs import (
            COMPLETED_RUN_STATUSES,
            fetch_audit_run_by_id,
            recent_runs_for_merchant,
        )

        runs = await recent_runs_for_merchant(
            merchant_id=merchant_id, limit=max(1, max_runs) + 2,
            # Scope the scan to the run kind that can carry this SKU: URL-wedge
            # synthetic SKUs live only in merchant_url runs, catalog SKUs only
            # in merchant runs. Without this, a merchant alternating wedge and
            # catalog audits fills the scan window with runs that can't match.
            subject_type=subject_type_for_sku_key(sku_key),
        )
        for run in runs or []:
            # Completion is a STATUS question ('succeeded', legacy
            # 'completed'). recent_runs_for_merchant rows carry no `stage`
            # key at all — the old `stage == 'completed'` filter here skipped
            # every run, so pinning silently never engaged (every audit
            # regenerated a fresh basis; the tracking chart drew every point
            # as its own basis segment).
            if str(run.get("status") or "") not in COMPLETED_RUN_STATUSES:
                continue
            row = await fetch_audit_run_by_id(run_id=str(run.get("run_id")))
            if not row:
                continue
            report = row.get("report_jsonb")
            if not isinstance(report, Mapping):
                continue
            basis = harvest_prompt_basis(
                report, sku_key=sku_key, audit_tier=audit_tier,
            )
            if basis:
                basis["pinned_from_run_id"] = str(run.get("run_id") or "")
                return basis
    except Exception:  # noqa: BLE001 — pinning must never block probing
        logger.warning(
            "prompt-basis load failed for merchant=%s sku=%s",
            merchant_id, sku_key, exc_info=True,
        )
    return None


_PROBE_RUN_META_KEY = "prompt_basis_meta"


def attach_basis_meta_to_probe_runs(
    probe_runs: Any,
    meta: Mapping[str, Any],
) -> None:
    """Ride the basis meta on the PERSISTED probe payload (first run dict).

    The probing phase and the report phase use DIFFERENT sku_ctx instances —
    run_brand_report resets the context cache and reloads, so anything stashed
    on the probing-phase ctx silently vanishes by report time (the prod
    validation pair of 2026-07-04 caught exactly that: run #1 stamped
    prompt_basis=None and run #2 regenerated). The probe runs are what the
    report phase durably reloads (load_per_sku_probe_runs), so the meta rides
    there. An extra key on a run dict is inert to every extractor (they .get()
    known fields) and survives worker restarts/resume."""
    if not isinstance(meta, Mapping):
        return
    for run in probe_runs if isinstance(probe_runs, list) else []:
        if isinstance(run, dict):
            run[_PROBE_RUN_META_KEY] = dict(meta)
            return


def basis_meta_from_probe_runs(probe_runs: Any) -> Optional[Dict[str, Any]]:
    """Recover the basis meta attached by attach_basis_meta_to_probe_runs."""
    for run in probe_runs if isinstance(probe_runs, list) else []:
        if isinstance(run, Mapping):
            meta = run.get(_PROBE_RUN_META_KEY)
            if isinstance(meta, Mapping):
                return dict(meta)
    return None


async def resolve_prompt_basis(
    *,
    merchant_id: str,
    sku_key: str,
    generate_winnable: Callable[[], Awaitable[List[str]]],
    generate_scenario: Callable[[], Awaitable[List[str]]],
    refresh: bool = False,
    audit_tier: str = AUDIT_TIER_STANDARD,
) -> Dict[str, Any]:
    """THE prompt-basis decision for one SKU in one run.

    Pinned path (default): a prior completed run stamped a same-version,
    non-empty basis for this SKU → reuse it verbatim. No LLM calls, identical
    measurement basis, comparable scores.

    Fresh path: no usable prior basis, or `refresh=True` (an explicit caller
    action) → run the generators (each best-effort, mirroring the previous
    inline behavior: a generator failure yields an empty list, never blocks
    probing) and mint a new prompt_set_id.

    Returns {"winnable": [...], "scenario": [...], "meta": {...}} where meta is
    the JSON-safe `prompt_basis` payload to stamp on the per-SKU report.
    """
    tier = normalize_audit_tier(audit_tier)
    if not refresh:
        prior = await load_prior_prompt_basis(
            merchant_id=merchant_id, sku_key=sku_key, audit_tier=tier,
        )
        if prior:
            selected_specs = prior.get("selected_specs") or []
            meta = {
                "prompt_set_id": prior["prompt_set_id"],
                "basis_version": PROMPT_BASIS_VERSION,
                "audit_tier": tier,
                "source": "pinned",
                "winnable": prior["winnable"],
                "scenario": prior["scenario"],
                # Identity of the basis is its ORIGIN — created_at carries
                # through so a chain of re-runs shares one basis birthdate.
                "created_at": prior.get("created_at"),
                "pinned_from_run_id": prior.get("pinned_from_run_id"),
            }
            logger.info(
                "prompt-basis: pinned %s (%d winnable, %d scenario, %d "
                "selected) from run %s for sku=%s",
                meta["prompt_set_id"], len(prior["winnable"]),
                len(prior["scenario"]), len(selected_specs),
                prior.get("pinned_from_run_id"), sku_key,
            )
            return {
                "winnable": prior["winnable"],
                "scenario": prior["scenario"],
                # W2.1: when the prior run stored the full probed set, the
                # caller reprobes EXACTLY these — no spec rebuild, no drift.
                "selected_specs": selected_specs,
                "meta": meta,
            }

    winnable: List[str] = []
    scenario: List[str] = []
    list_cap = max_prompts_per_list_for_tier(tier)
    try:
        winnable = _clean_prompts(await generate_winnable(), max_prompts=list_cap)
    except Exception:  # noqa: BLE001 — never block probing on the LLM step
        logger.warning("winnable-prompt extraction skipped", exc_info=True)
    try:
        scenario = _clean_prompts(await generate_scenario(), max_prompts=list_cap)
    except Exception:  # noqa: BLE001 — never block probing on the LLM step
        logger.warning("scenario elicitation skipped", exc_info=True)
    meta = {
        "prompt_set_id": build_prompt_set_id(winnable, scenario),
        "basis_version": PROMPT_BASIS_VERSION,
        "audit_tier": tier,
        "source": "refreshed" if refresh else "fresh",
        "winnable": winnable,
        "scenario": scenario,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pinned_from_run_id": None,
    }
    # Fresh run: no prior selected set to reprobe. The caller builds specs
    # normally, then records what was actually probed via
    # set_selected_specs_on_meta so the NEXT run can pin it.
    return {
        "winnable": winnable,
        "scenario": scenario,
        "selected_specs": [],
        "meta": meta,
    }

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

The deterministic sidewalk prompts are NOT pinned here — they are already a
pure function of the attribute graph. Only the stochastic LLM lists are.
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
PROMPT_BASIS_VERSION = 1

_MAX_PROMPTS_PER_LIST = 12
_MAX_PROMPT_CHARS = 300


def _clean_prompts(values: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values if isinstance(values, (list, tuple)) else []:
        text = str(value or "").strip()[:_MAX_PROMPT_CHARS]
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
        if len(out) >= _MAX_PROMPTS_PER_LIST:
            break
    return out


def build_prompt_set_id(winnable: List[str], scenario: List[str]) -> str:
    """Stable identity of a prompt basis — same prompts (order-sensitive,
    case-preserving) → same id, across runs and processes."""
    payload = json.dumps(
        {"w": list(winnable), "s": list(scenario)},
        ensure_ascii=False, sort_keys=True,
    )
    return "ps_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def harvest_prompt_basis(
    report_jsonb: Mapping[str, Any],
    *,
    sku_key: str,
) -> Optional[Dict[str, Any]]:
    """Pull the pinned prompt basis for `sku_key` out of one prior run's
    report. Returns the stored basis dict, or None when absent/unusable
    (wrong version, empty, malformed). Pure + synchronous."""
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
        winnable = _clean_prompts(basis.get("winnable"))
        scenario = _clean_prompts(basis.get("scenario"))
        if not winnable and not scenario:
            return None  # never pin an empty basis (e.g. a prior LLM outage)
        return {
            "winnable": winnable,
            "scenario": scenario,
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
) -> Optional[Dict[str, Any]]:
    """Best-effort: scan the merchant's most recent completed runs (newest
    first) and return the first pinned basis found for this sku_key, tagged
    with the run it came from. Never raises."""
    if not merchant_id or not sku_key:
        return None
    try:
        from db.merchant_audit_runs import (
            fetch_audit_run_by_id,
            recent_runs_for_merchant,
        )

        runs = await recent_runs_for_merchant(
            merchant_id=merchant_id, limit=max(1, max_runs) + 2,
        )
        for run in runs or []:
            if str(run.get("stage") or "") != "completed":
                continue
            row = await fetch_audit_run_by_id(run_id=str(run.get("run_id")))
            if not row:
                continue
            report = row.get("report_jsonb")
            if not isinstance(report, Mapping):
                continue
            basis = harvest_prompt_basis(report, sku_key=sku_key)
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
    if not refresh:
        prior = await load_prior_prompt_basis(
            merchant_id=merchant_id, sku_key=sku_key,
        )
        if prior:
            meta = {
                "prompt_set_id": prior["prompt_set_id"],
                "basis_version": PROMPT_BASIS_VERSION,
                "source": "pinned",
                "winnable": prior["winnable"],
                "scenario": prior["scenario"],
                # Identity of the basis is its ORIGIN — created_at carries
                # through so a chain of re-runs shares one basis birthdate.
                "created_at": prior.get("created_at"),
                "pinned_from_run_id": prior.get("pinned_from_run_id"),
            }
            logger.info(
                "prompt-basis: pinned %s (%d winnable, %d scenario) from run "
                "%s for sku=%s",
                meta["prompt_set_id"], len(prior["winnable"]),
                len(prior["scenario"]), prior.get("pinned_from_run_id"),
                sku_key,
            )
            return {
                "winnable": prior["winnable"],
                "scenario": prior["scenario"],
                "meta": meta,
            }

    winnable: List[str] = []
    scenario: List[str] = []
    try:
        winnable = _clean_prompts(await generate_winnable())
    except Exception:  # noqa: BLE001 — never block probing on the LLM step
        logger.warning("winnable-prompt extraction skipped", exc_info=True)
    try:
        scenario = _clean_prompts(await generate_scenario())
    except Exception:  # noqa: BLE001 — never block probing on the LLM step
        logger.warning("scenario elicitation skipped", exc_info=True)
    meta = {
        "prompt_set_id": build_prompt_set_id(winnable, scenario),
        "basis_version": PROMPT_BASIS_VERSION,
        "source": "refreshed" if refresh else "fresh",
        "winnable": winnable,
        "scenario": scenario,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pinned_from_run_id": None,
    }
    return {"winnable": winnable, "scenario": scenario, "meta": meta}

"""Response-level selection contract. Never infer absence from an excerpt.

The source-visibility proxy and explicit answer mention are separate signals.
Historical reports without response observations are unknown, never zero.
Wilson intervals describe this sample under an independence assumption; repeated
queries/providers may correlate, so they do not license before/after claims.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from collections import Counter
from typing import Any, Mapping

from services.audit_facts import (
    BRANDED_INTENTS, DISCOVERY_INTENTS, compute_run_facts, intent_axis_for,
    run_errored, run_is_internal_comparison,
)
from services.brand_alias import text_mentions_brand
from services.consumer_answer_evidence import answer_mention, PREDICATE as CONSUMER_PREDICATE

VERSION = "4"
MENTION_PREDICATE = "explicit_answer_brand_mentioned_v1"
TIERS = ("branded", "unbranded", "dupe")
DIAGNOSTIC_SCAN_MODES = {
    "open_product_visibility_test", "merchant_store_attribution_test",
    "pivota_pdp_attribution_test", "search_grounded_product_discovery_test",
    "category_visibility_test",
}


def selection_tier(query: str, axis: str, brand: str | None) -> str:
    # Dupe questions can name the merchant; classify them BEFORE branded.
    if axis in {"dupe", "alternative", "alternatives", "substitute"} or re.search(
        r"\b(dupes?|alternatives? to|substitute for)\b", query, re.I
    ):
        return "dupe"
    if brand and text_mentions_brand(query.lower(), (brand.lower(),)):
        return "branded"
    if not axis or axis in {"custom", "unclassified"}:
        return "unknown"
    intent = intent_axis_for(query, axis)
    if intent in BRANDED_INTENTS:
        return "branded"
    # Do not let intent_axis_for's unknown->category fallback classify garbage.
    if axis in DISCOVERY_INTENTS | {"category", "attribute", "sidewalk"}:
        return "unbranded"
    return "unknown"


def response_observations(runs, *, sku_key, merchant_host, merchant_brand, merchant_vendors=()):
    out = []
    for index, run in enumerate(runs):
        if not isinstance(run, dict) or run_is_internal_comparison(run):
            continue
        query = str(run.get("normalized_query") or run.get("query") or "").strip()
        meta = run.get("axis_metadata") or {}
        axis = str(meta.get("axis") or "").lower() if isinstance(meta, dict) else ""
        answer_provider = run.get("answer", {}).get("provider") if isinstance(run.get("answer"), dict) else None
        provider = str(run.get("_provider") or run.get("provider") or answer_provider or "unknown")
        failed = run_errored(run)
        parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
        # A provider may explicitly report this boolean. correct_sku=False,
        # product_visible=False, missing citations and truncated snippets do NOT
        # establish brand absence. Do not parse raw model envelopes as prose.
        mention = parsed.get("brand_mentioned")
        # Merchant-context prompts request diagnostic JSON. Even an explicit
        # model-supplied boolean in that JSON is not consumer-answer evidence.
        modes = (run.get("_scan_mode"), run.get("scan_mode"))
        invalid_mode = any(mode is not None and not isinstance(mode, str) for mode in modes)
        diagnostic = (run.get("evidence_kind") == "merchant_context_diagnostic"
                      or run.get("prompt_contract") == "merchant_context_diagnostic_v1"
                      or any(isinstance(mode, str) and mode in DIAGNOSTIC_SCAN_MODES for mode in modes))
        if type(mention) is not bool or failed or diagnostic or invalid_mode:
            mention = None
        mention_basis = MENTION_PREDICATE if mention is not None else "unavailable"
        answer_unknown_reason = None
        if run.get("evidence_kind") == "consumer_answer":
            mention, answer_unknown_reason = answer_mention(run, merchant_brand)
            if failed or diagnostic or invalid_mode:
                mention, answer_unknown_reason = None, "incompatible_or_failed_probe"
            mention_basis = CONSUMER_PREDICATE if mention is not None else "unavailable"
        facts = compute_run_facts(
            [run], merchant_host=merchant_host, merchant_brand=merchant_brand,
            merchant_vendors=merchant_vendors,
        )
        has_sources = isinstance(run.get("grounding_sources"), list) or isinstance(run.get("grounding_chunks"), list)
        source_visible = bool(facts.brand_mentioned_runs) if has_sources and not failed else None
        identity = [sku_key, run.get("_probe_run_id"), index, provider, query]
        out.append({
            # `default=str` because `_probe_run_id` is whatever the probe layer
            # put there — a UUID or a datetime are both routine and neither is
            # JSON-serializable. Without it json.dumps raises TypeError, and
            # this loop's caller wraps the run_facts stamp in the SAME try, so
            # one such id silently dropped run_facts for the entire run.
            "observation_id": hashlib.sha256(
                json.dumps(identity, default=str).encode()
            ).hexdigest(),
            "product_key": sku_key, "query": query, "provider": provider,
            "tier": selection_tier(query, axis, merchant_brand),
            "status": "provider_failed" if failed else "answered",
            "brand_mentioned": mention, "source_visible": source_visible,
            "mention_basis": mention_basis,
            "answer_unknown_reason": answer_unknown_reason,
            "evidence_kind": run.get("evidence_kind"),
            **({"answer_evidence": run.get("answer"), "cited_sources": run.get("grounding_sources") or [],
                "prompt_contract": run.get("prompt_contract"), "measured_brand": merchant_brand}
               if run.get("evidence_kind") == "consumer_answer" else {}),
        })
    return out


def _estimate(positive, n):
    if not n:
        return {"positive": 0, "n": 0, "rate": None, "ci95": None}
    p, z = positive / n, 1.959963984540054
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {"positive": positive, "n": n, "rate": p,
            "ci95": [max(0, center - half), min(1, center + half)]}


def selection_measurement(observations):
    unique = {r["observation_id"]: r for r in observations or []
              if isinstance(r, dict) and r.get("observation_id")}
    rows = list(unique.values())
    consumer_rows = [r for r in rows if r.get("evidence_kind") == "consumer_answer"]
    # Never blend prompted diagnostics and natural answers into one rate.
    excluded_diagnostics = len(rows) - len(consumer_rows) if consumer_rows else 0
    if consumer_rows:
        rows = consumer_rows
    conditions = Counter((r.get('provider','unknown'), r.get('prompt_contract','unknown')) for r in consumer_rows)
    mixed = {provider for provider, _ in conditions if sum(p == provider for p, _ in conditions) > 1}
    tiers = {}
    for tier in TIERS:
        group = [r for r in rows if r.get("tier") == tier]
        bucket = {"attempted": len(group), "provider_failed": sum(r.get("status") == "provider_failed" for r in group)}
        for field in ("brand_mentioned", "source_visible"):
            eligible = [r[field] for r in group if r.get("status") == "answered" and type(r.get(field)) is bool
                        and not (field == 'brand_mentioned' and r.get('provider') in mixed)]
            bucket[field] = {**_estimate(sum(eligible), len(eligible)),
                             "unknown": len(group) - bucket["provider_failed"] - len(eligible)}
        tiers[tier] = bucket
    return {
        "version": VERSION, "mention_predicate": CONSUMER_PREDICATE if consumer_rows else MENTION_PREDICATE,
        "excluded_diagnostics": excluded_diagnostics,
        "execution_conditions": [{"provider":provider,"contract":contract,"observations":count} for (provider,contract),count in sorted(conditions.items())],
        "mixed_execution_providers": sorted(mixed),
        "answers": [{"observation_id": r["observation_id"], "query": r.get("query"),
                     "provider": r.get("provider"), "prompt_contract": r.get("prompt_contract"), "brand_mentioned": r.get("brand_mentioned"),
                     "unknown_reason": r.get("answer_unknown_reason"),
                     "evidence": r.get("answer_evidence"), "cited_sources": r.get("cited_sources") or []}
                    for r in consumer_rows],
        "unit": "product_provider_query_response", "tiers": tiers,
        "observations": len(rows), "unclassified": sum(r.get("tier") not in TIERS for r in rows),
        "providers": dict(Counter(r.get("provider", "unknown") for r in rows)),
        "unavailable_reason": None if rows else "This report did not retain response-level observations.",
        "interval_method": "Wilson 95%, conditional on observed responses",
        "limitation": "Describes completed, cited answers in this sample only; answers without verifiable citations are excluded and may differ systematically. Repeated queries and providers may be correlated; these intervals do not establish improvement or population coverage. Unknown answers are excluded, not counted as misses.",
    }


def report_observations(report: Mapping[str, Any]):
    """Read retained observations without inventing historical answer evidence."""
    rows = []
    containers = list(report.get("per_sku_reports") or [])
    containers.append({"selection_observations": report.get("consumer_selection_observations") or []})
    for sku in containers:
        if not isinstance(sku, dict):
            continue
        for original in sku.get("selection_observations") or []:
            if not isinstance(original, dict):
                continue
            row = dict(original)
            mention, reason = answer_mention(
                {**row, "answer": row.get("answer_evidence")}, row.get("measured_brand"),
            )
            if row.get("status") != "answered":
                mention, reason = None, "incompatible_or_failed_probe"
            row.update(brand_mentioned=mention,
                       mention_basis=CONSUMER_PREDICATE if mention is not None else "unavailable",
                       answer_unknown_reason=reason)
            rows.append(row)
    return rows


def upgrade_retained_measurement(value):
    """Old canonical findings must not acquire new validity from a cache rebuild."""
    if not isinstance(value, dict):
        return selection_measurement([])
    result = deepcopy(value)
    if result.get("version") == VERSION:
        return result
    for bucket in (result.get("tiers") or {}).values():
        if not isinstance(bucket, dict):
            continue
        attempted = bucket.get("attempted", 0)
        failed = bucket.get("provider_failed", 0)
        unknown = max(0, attempted - failed) if isinstance(attempted, int) and isinstance(failed, int) else 0
        bucket["brand_mentioned"] = {**_estimate(0, 0), "unknown": unknown}
    result.update(version=VERSION, answers=[], mention_predicate=CONSUMER_PREDICATE,
                  limitation="Historical answer provenance was not retained; answer mentions are unknown. " + str(result.get("limitation") or ""))
    return result

"""Response-level selection contract. Never infer absence from an excerpt.

The source-visibility proxy and explicit answer mention are separate signals.
Historical reports without response observations are unknown, never zero.
Wilson intervals describe this sample under an independence assumption; repeated
queries/providers may correlate, so they do not license before/after claims.
"""
from __future__ import annotations

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

VERSION = "1"
MENTION_PREDICATE = "explicit_answer_brand_mentioned_v1"
TIERS = ("branded", "unbranded", "dupe")


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
        provider = str(run.get("_provider") or run.get("provider") or "unknown")
        failed = run_errored(run)
        parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
        # A provider may explicitly report this boolean. correct_sku=False,
        # product_visible=False, missing citations and truncated snippets do NOT
        # establish brand absence. Do not parse raw model envelopes as prose.
        mention = parsed.get("brand_mentioned")
        if type(mention) is not bool or failed:
            mention = None
        facts = compute_run_facts(
            [run], merchant_host=merchant_host, merchant_brand=merchant_brand,
            merchant_vendors=merchant_vendors,
        )
        has_sources = isinstance(run.get("grounding_sources"), list) or isinstance(run.get("grounding_chunks"), list)
        source_visible = bool(facts.brand_mentioned_runs) if has_sources and not failed else None
        identity = [sku_key, run.get("_probe_run_id"), index, provider, query]
        out.append({
            "observation_id": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
            "product_key": sku_key, "query": query, "provider": provider,
            "tier": selection_tier(query, axis, merchant_brand),
            "status": "provider_failed" if failed else "answered",
            "brand_mentioned": mention, "source_visible": source_visible,
            "mention_basis": MENTION_PREDICATE if mention is not None else "unavailable",
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
    tiers = {}
    for tier in TIERS:
        group = [r for r in rows if r.get("tier") == tier]
        bucket = {"attempted": len(group), "provider_failed": sum(r.get("status") == "provider_failed" for r in group)}
        for field in ("brand_mentioned", "source_visible"):
            eligible = [r[field] for r in group if r.get("status") == "answered" and type(r.get(field)) is bool]
            bucket[field] = {**_estimate(sum(eligible), len(eligible)),
                             "unknown": len(group) - bucket["provider_failed"] - len(eligible)}
        tiers[tier] = bucket
    return {
        "version": VERSION, "mention_predicate": MENTION_PREDICATE,
        "unit": "product_provider_query_response", "tiers": tiers,
        "observations": len(rows), "unclassified": sum(r.get("tier") not in TIERS for r in rows),
        "providers": dict(Counter(r.get("provider", "unknown") for r in rows)),
        "unavailable_reason": None if rows else "This report did not retain response-level observations.",
        "interval_method": "Wilson 95%, conditional on observed responses",
        "limitation": "Describes this sample only. Repeated queries and providers may be correlated; these intervals do not establish improvement or population coverage. Unknown answers are excluded, not counted as misses.",
    }


def report_observations(report: Mapping[str, Any]):
    return [r for sku in report.get("per_sku_reports") or [] if isinstance(sku, dict)
            for r in sku.get("selection_observations") or [] if isinstance(r, dict)]

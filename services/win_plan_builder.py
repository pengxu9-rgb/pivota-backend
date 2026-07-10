"""Fix 4 — per-SKU win-plan: how to win the AI category recommendation.

Turns the Fix 1/2/3 *diagnosis* ("where you stand with AI agents") into the
recognizable, per-SKU *instruction* a merchant can act on: for each
category/discovery query a SKU is **losing**, the exact independent hosts AI
grounds its answer in (the targets to get cited in), the competitor who wins
that query today (the benchmark), and the honest outreach path to each target.

The causal model this encodes: AI answers "best X" by **grounding** in
independent web sources it retrieves and recommends the SKUs cited there —
never from the merchant's own listing. So winning = get into the exact
independent endorsement hosts AI grounds on for the specific category queries
you're losing. On a *failing* category query, that query's grounding sources
are precisely the hosts AI built the answer from while the merchant was absent —
i.e. the target list.

The hard part: this host<->query linkage is NOT preserved in `authority_map`.
`build_authority_map` aggregates every cited host to a per-host
"cited on *some* category query" boolean and pops the underlying query set, so
"for THIS query, AI cited THESE hosts" is gone. This module **re-derives** it
per query by joining each losing query's raw grounding `uri` back to the
authority_map's already-resolved host rows (each row carries the
`evidence_urls` it was cited at). That reuses the Fix-1 redirector resolution
and Fix-2 role classification *exactly as the report computed them* — no
re-implementation, and no import of the report module (so no import cycle).

Guardrails inherited from Fix 2/3: **no fabrication** (only real grounded hosts
and competitors; when a target has no known outreach recipient, say so — never
invent one) and **no inflation** (findability roles — own site / own
marketplace listing — are never a win target).

This module adds no probes and reads no DB. Pure + defensive: every field
degrades to an honest "not available" / stated limit rather than guessing.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from services.audit_playbook_engine import build_pitch_draft_for_host
from services.cited_host_classifier import (
    classify_host,
    is_endorsement_role,
    is_findability_role,
)

# The non-branded discovery axis — the only queries where "AI recommends you for
# the category" is the thing at stake. Kept local (mirrors
# merchant_narrative_builder) so this module has no import cycle with
# agent_center_bd_report_service, which imports it.
_CATEGORY_DISCOVERY_AXES = frozenset({"category"})

_MAX_LOSING_QUERIES_PER_SKU = 10
_MAX_TARGETS_PER_QUERY = 6
_MAX_COMPETITORS_PER_QUERY = 6

# Outreach states, honest about how actionable each target is (no fabrication):
#   draft_ready    — registry has a pitch email -> a one-click pitch is possible
#                    (the rendered draft body is wired in Phase 2).
#   submission_only — only a submission form/url -> no email pitch; longer cycle.
#   target_only    — the right target, but no known recipient yet (a BD/registry
#                    coverage gap, surfaced rather than hidden).
OUTREACH_DRAFT_READY = "draft_ready"
OUTREACH_SUBMISSION_ONLY = "submission_only"
OUTREACH_TARGET_ONLY = "target_only"


def _is_category_axis(axis: Optional[str]) -> bool:
    return str(axis or "").strip().lower() in _CATEGORY_DISCOVERY_AXES


def _authority_hosts_by_sku(authority_map: Dict[str, Any]) -> Dict[Any, List[Dict[str, Any]]]:
    out: Dict[Any, List[Dict[str, Any]]] = {}
    for sku in (authority_map.get("skus") if isinstance(authority_map, dict) else None) or []:
        if not isinstance(sku, dict):
            continue
        hosts = sku.get("authority_hosts")
        out[sku.get("sku_key")] = hosts if isinstance(hosts, list) else []
    return out


def _uri_to_host_row(authority_hosts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index every raw grounding `uri` -> the resolved host row that was cited at
    it. This is the join key that recovers the per-query linkage the aggregate
    threw away: a losing query's raw sources carry the same `uri` strings the
    authority map recorded in each host's `evidence_urls`."""
    index: Dict[str, Dict[str, Any]] = {}
    for row in authority_hosts or []:
        if not isinstance(row, dict):
            continue
        for uri in row.get("evidence_urls") or []:
            if uri and uri not in index:
                index[uri] = row
    return index


def _resolve_query_hosts(
    grounding_sources: List[Dict[str, Any]],
    uri_index: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The distinct resolved host rows AI grounded THIS query's answer in, via
    the uri join. Order-preserving + deduped by host. Sources that the report
    couldn't resolve (dropped from the authority map) are simply absent — we
    never name a host we couldn't resolve (no fabrication)."""
    seen: set = set()
    rows: List[Dict[str, Any]] = []
    for source in grounding_sources or []:
        if not isinstance(source, dict):
            continue
        row = uri_index.get(source.get("uri") or "")
        if not row:
            continue
        host = row.get("host")
        if not host or host in seen:
            continue
        seen.add(host)
        rows.append(row)
    return rows


def _outreach_from_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """The honest outreach path to one target host, read from the BD cited-host
    registry (`classify_host`). State degrades draft_ready -> submission_only ->
    target_only by what recipient the registry actually has — never invents one."""
    recipient = meta.get("pitch_recipient")
    recipient = recipient if isinstance(recipient, dict) else None
    email = (recipient or {}).get("email")
    submission_url = (recipient or {}).get("submission_url")
    if email:
        state = OUTREACH_DRAFT_READY
    elif submission_url:
        state = OUTREACH_SUBMISSION_ONLY
    else:
        state = OUTREACH_TARGET_ONLY
    return {
        "state": state,
        "hint": meta.get("outreach_hint"),
        "cycle_weeks": meta.get("expected_outreach_cycle_weeks"),
        "recipient": {
            "email": email,
            "submission_url": submission_url,
            "note": (recipient or {}).get("note"),
        } if recipient else None,
        # Phase 2: the rendered one-click pitch, attached below for draft_ready.
        "pitch_draft": None,
    }


def _target(
    row: Dict[str, Any],
    *,
    merchant_name: Optional[str],
    merchant_category: Optional[str],
    query: str,
    competitors_named: List[str],
) -> Dict[str, Any]:
    host = row.get("host")
    meta = classify_host(host, merchant_category=merchant_category)
    outreach = _outreach_from_meta(meta)
    # Attach the one-click pitch email when the host actually supports it (a real
    # email recipient + a playbook pitch template). Keyed to THIS losing query +
    # its competitor benchmark so the draft is specific, not generic. Reuses the
    # playbook engine (no re-implementation); stays None when not renderable, so
    # submission_only / target_only targets honestly show no draft button.
    if outreach["state"] == OUTREACH_DRAFT_READY:
        outreach["pitch_draft"] = build_pitch_draft_for_host(
            meta,
            merchant_name=merchant_name,
            merchant_category=merchant_category,
            example_query=query,
            competitors_named=competitors_named,
        )
    return {
        "host": host,
        "role": row.get("citation_role"),
        "tier": meta.get("tier"),
        "applies_to_merchant_category": meta.get("applies_to_merchant_category"),
        "outreach": outreach,
    }


def _competitor_benchmark(competitors_named: Any) -> List[str]:
    # Shared competitor-name dedup (case variants, 0-vs-o typos, product-names
    # onto their observed brand) so the win-plan benchmark matches the
    # opportunity panels — was a plain lowercase dedupe that kept "H20 Audio"
    # beside "H2O Audio" across panels.
    from services.competitor_brand_filter import normalize_competitor_list

    names = normalize_competitor_list(list(competitors_named or []))
    return names[:_MAX_COMPETITORS_PER_QUERY]


def _win_condition(query: str, targets: List[Dict[str, Any]]) -> Optional[str]:
    hosts = [t["host"] for t in targets[:3] if t.get("host")]
    if not hosts:
        return None
    return f'Get cited in {" / ".join(hosts)} for "{query}".'


def _losing_query_plan(
    failing_prompt: Dict[str, Any],
    uri_index: Dict[str, Dict[str, Any]],
    *,
    merchant_name: Optional[str],
    merchant_category: Optional[str],
) -> Dict[str, Any]:
    query = str(failing_prompt.get("query") or "").strip()
    resolved = _resolve_query_hosts(failing_prompt.get("grounding_sources") or [], uri_index)
    competitor_benchmark = _competitor_benchmark(failing_prompt.get("competitors_named"))

    # The targets are the independent ENDORSEMENT hosts AI grounded this answer
    # in (editorial / review / creator / community / independent retailer).
    # Findability (own site / own listing) is never a win target (no inflation);
    # competitor storefronts and unclassified hosts are not pitchable targets.
    endorsement_rows = [r for r in resolved if is_endorsement_role(r.get("citation_role"))]
    targets = [
        _target(
            r,
            merchant_name=merchant_name,
            merchant_category=merchant_category,
            query=query,
            competitors_named=competitor_benchmark,
        )
        for r in endorsement_rows
    ]
    # Tier-then-host so the highest-leverage publisher leads even at a single
    # cite (ADR-004 open Q2); unknown tier sorts last.
    targets.sort(key=lambda t: (t.get("tier") is None, t.get("tier") or 0, t.get("host") or ""))
    targets = targets[:_MAX_TARGETS_PER_QUERY]

    limit: Optional[str] = None
    win_path = "publisher" if targets else "own_content"
    win_condition = _win_condition(query, targets)
    if not targets:
        # No independent target we can name for this query. Be specific about
        # WHY using only real resolved hosts — never invent a target. But a
        # missing publisher is NOT a dead end: it means no publisher CONTROLS
        # this answer, which is exactly when the merchant's own content can
        # win it. Emit the own-content win condition instead of null — the old
        # null dead-ended precisely on the specific long-tail queries the
        # audit generates for the merchant to win.
        non_endorsement = [
            r.get("host")
            for r in resolved
            if r.get("host") and not is_endorsement_role(r.get("citation_role"))
        ]
        if non_endorsement:
            shown = ", ".join(dict.fromkeys(non_endorsement))  # dedupe, keep order
            kind = (
                "your own / retail listings"
                if all(is_findability_role(r.get("citation_role")) for r in resolved)
                else "sources that aren't independent publishers we can target"
            )
            limit = (
                f"AI grounds this answer in {shown} ({kind}); no independent "
                "publisher to pitch here."
            )
        else:
            limit = (
                "AI returned no resolvable independent source for this query."
            )
        win_condition = (
            "No publisher controls this answer — win it with your own "
            f'content: publish a spec-complete product page that answers "{query}" '
            "so AI can ground on you directly."
        )

    return {
        "query": query,
        "axis": failing_prompt.get("axis"),
        # Engines whose runs failed this query (merged upstream in _sku_plan) —
        # replaces the unlabeled per-provider duplicate rows.
        "providers": sorted(
            str(p).strip().lower()
            for p in (failing_prompt.get("providers") or [])
            if str(p or "").strip()
        ),
        "grounds_in": targets,
        "competitor_benchmark": competitor_benchmark,
        "win_condition": win_condition,
        "win_path": win_path,
        "limit": limit,
    }


def _sku_plan(
    report: Dict[str, Any],
    authority_hosts: List[Dict[str, Any]],
    *,
    merchant_name: Optional[str],
    merchant_category: Optional[str],
) -> Optional[Dict[str, Any]]:
    failing = report.get("failing_prompts")
    failing = failing if isinstance(failing, list) else []
    losing_category = [
        fp for fp in failing if isinstance(fp, dict) and _is_category_axis(fp.get("axis"))
    ]
    if not losing_category:
        return None

    # MERGE per-provider failing rows for the same query into ONE plan row.
    # failing_prompts emits one entry per provider run, which rendered as
    # unlabeled duplicate losing queries with contradictory limits ("no
    # publisher to pitch here" beside "get cited in techradar" for the same
    # query — live run 7420c2b5). One row per query: grounding-source union
    # (a publisher target found by ANY engine wins), competitor-benchmark
    # union (deduped downstream), and a `providers` label listing the engines
    # that failed it.
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for fp in losing_category:
        query_key = re.sub(r"\s+", " ", str(fp.get("query") or "").strip().lower())
        if not query_key:
            continue
        row = merged.get(query_key)
        if row is None:
            row = {
                "query": fp.get("query"),
                "axis": fp.get("axis"),
                "grounding_sources": [],
                "competitors_named": [],
                "providers": [],
            }
            merged[query_key] = row
            order.append(query_key)
        row["grounding_sources"].extend(fp.get("grounding_sources") or [])
        row["competitors_named"].extend(fp.get("competitors_named") or [])
        provider = str(fp.get("provider") or "").strip().lower()
        if provider and provider not in row["providers"]:
            row["providers"].append(provider)

    uri_index = _uri_to_host_row(authority_hosts)
    losing_queries = [
        _losing_query_plan(
            merged[query_key],
            uri_index,
            merchant_name=merchant_name,
            merchant_category=merchant_category,
        )
        for query_key in order[:_MAX_LOSING_QUERIES_PER_SKU]
    ]

    with_target = [q for q in losing_queries if q["grounds_in"]]
    draft_ready = sum(
        1
        for q in losing_queries
        if any(t["outreach"]["state"] == OUTREACH_DRAFT_READY for t in q["grounds_in"])
    )
    return {
        "sku_key": report.get("sku_key"),
        "sku_title": report.get("sku_title"),
        "losing_queries": losing_queries,
        "coverage": {
            "losing_category_queries": len(losing_queries),
            "queries_with_grounded_target": len(with_target),
            "queries_with_draft_ready_target": draft_ready,
        },
    }


def build_win_plan(
    *,
    per_sku_reports: List[Dict[str, Any]],
    authority_map: Optional[Dict[str, Any]] = None,
    merchant_name: Optional[str] = None,
    merchant_category: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the per-SKU win-plan from already-built report data.

    For every category/discovery query a SKU is losing, names the independent
    endorsement hosts AI grounds the answer in (the targets), the competitor
    benchmark for that query, the win condition, and the honest outreach state
    per target. Pure + defensive — degrades to an honest `available=False` note
    when there are no losing category queries, and to a per-query `limit` when a
    losing query has no nameable independent target.
    """
    authority_map = authority_map if isinstance(authority_map, dict) else {}
    hosts_by_sku = _authority_hosts_by_sku(authority_map)

    sku_plans: List[Dict[str, Any]] = []
    for report in per_sku_reports or []:
        if not isinstance(report, dict):
            continue
        plan = _sku_plan(
            report,
            hosts_by_sku.get(report.get("sku_key")) or [],
            merchant_name=merchant_name,
            merchant_category=merchant_category,
        )
        if plan:
            sku_plans.append(plan)

    losing_total = sum(p["coverage"]["losing_category_queries"] for p in sku_plans)
    hosts_to_win: set = set()
    draft_ready_hosts: set = set()
    pitch_ready_hosts: set = set()
    for p in sku_plans:
        for q in p["losing_queries"]:
            for t in q["grounds_in"]:
                hosts_to_win.add(t["host"])
                if t["outreach"]["state"] == OUTREACH_DRAFT_READY:
                    draft_ready_hosts.add(t["host"])
                if t["outreach"].get("pitch_draft"):
                    pitch_ready_hosts.add(t["host"])

    available = bool(sku_plans)
    note: Optional[str] = None
    if not available:
        note = (
            "No losing category queries for the audited SKUs — there is no "
            "category recommendation to win back (or the discovery axis was not "
            "probed)."
        )
    elif not hosts_to_win:
        note = (
            "These SKUs are losing category queries, but the grounded answers "
            "named no independent publisher to target — the play is to earn a "
            "first independent category citation rather than pitch a known host."
        )

    return {
        "available": available,
        "note": note,
        "sku_plans": sku_plans,
        "rollup": {
            "losing_category_queries": losing_total,
            "independent_hosts_to_win": sorted(hosts_to_win),
            # draft_ready_hosts = an emailable recipient exists (registry fact);
            # pitch_ready_hosts = a one-click draft actually rendered (email +
            # matching playbook template). The latter is the true "act now" count.
            "draft_ready_hosts": sorted(draft_ready_hosts),
            "pitch_ready_hosts": sorted(pitch_ready_hosts),
        },
    }

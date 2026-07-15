"""Audit→action→outcome loop: what changed at the hosts you were targeting.

When a merchant re-audits, their PRIOR report told them which hosts to get
cited in (win_plan losing-query targets + narrative outreach moves). This
module answers, per target, what the CURRENT run observed at that host —
"hwahae now names you on 'best hair care'" / "goodhousekeeping still grounds
this query without naming you" — without ever claiming the merchant's
outreach *caused* the change.

Purity contract (same as services/audit_delta.py): the caller fetches the
reports and passes them in; this module does no I/O. Measurement-basis
comparability is NOT re-derived here — the caller passes the
``measurement_basis`` dict that ``build_reaudit_delta`` already computed
(services/audit_delta._measurement_basis), so the two layers can never
disagree about whether query-level comparison is licensed.

Honesty rules encoded here:
  (a) No causation. There IS task-completion state (db/merchant_tasks.py,
      status='done' + evidence_jsonb.target_host), so the caller may pass
      ``completed_actions``; a matching target is annotated with the
      marked-done fact ("you marked this done N days before this run"), but
      the outcome copy never says the pitch worked.
  (b) Query-level claims ONLY when measurement_basis["same"] is True (the W2
      pinned prompt set). same=False or same=None → every query-keyed target
      degrades to "not_comparable"; only basis-independent endorsement
      transitions are still reported as wins.
  (c) A host that vanished from a query's grounding is "no_longer_grounded"
      — the engine sampled different sources; neither a win nor a loss.
  (d) prompts_cited_count deltas are surfaced as raw signals but NEVER drive
      classification — on 1-cite baselines they are noise. Only categorical
      transitions classify: query recovered, host entered the endorsement
      set, host started naming the merchant.

Stated limit: ``failing_prompts`` is capped upstream (20/SKU), so "query no
longer failing" is corroborated against the probed-prompt list
(opportunity.per_prompt) when present; a query absent from BOTH is reported
as reason="query_not_probed" under "no_longer_grounded", not as a win.

Payload contract — attached at merchant_view["outreach_outcomes"] on legacy
per-product reports (_attach_reaudit_delta) and TOP-LEVEL as
report["outreach_outcomes"] on per-SKU brand reports, beside win_plan /
merchant_narrative (_attach_outreach_outcomes_per_sku; the per-SKU report has
no brand merchant_view). Same shape in both places:
  {
    "is_first_audit": bool,        # True → no prior targets; targets == []
    "available": bool,             # False when either side lacks the data
    "note": str | None,            # why unavailable, or the no-causation framing
    "comparable": bool | None,     # measurement_basis["same"], echoed
    "basis_note": str | None,      # measurement_basis["note"], echoed
    "targets": [
      {
        "host": str,
        "query": str | None,       # None for host-only (outreach-move) targets
        "axis": str | None,
        "target_source": "win_plan" | "outreach_move" | "win_plan+outreach_move",
        "outcome": "won" | "progress" | "no_change"
                   | "no_longer_grounded" | "not_comparable",
        "reason": str,             # machine key, e.g. "query_now_cited",
                                   # "host_now_endorses", "host_now_names_you",
                                   # "still_grounds_without_naming",
                                   # "absent_from_query_grounding",
                                   # "absent_from_run_grounding",
                                   # "query_not_probed", "basis_changed"
        "what_changed": str,       # merchant-facing copy (observational only)
        "signals": {
          "prior":   {"endorsed": bool, "names_you": bool,
                      "citation_role": str|None, "prompts_cited_count": int|None},
          "current": {"endorsed": bool, "names_you": bool,
                      "citation_role": str|None, "prompts_cited_count": int|None,
                      "grounds_this_query": bool|None,   # query-keyed only
                      "query_still_losing": bool|None},  # query-keyed only
        },
        "merchant_action": {       # only when completed_actions matched
          "title": str, "marked_done_at": str|None,
          "days_before_current_run": int|None, "note": str,
        } | None,
      }, ...
    ],
    "summary": {"won": n, "progress": n, "no_change": n,
                "no_longer_grounded": n, "not_comparable": n},
    "closed_channels_excluded": [str, ...],  # prior closed doors, never targets
  }
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

# Mirror the win-plan's uri→host join exactly (it re-derives the per-query
# host linkage authority_map aggregates away) — reuse, don't reinvent.
from services.win_plan_builder import _resolve_query_hosts, _uri_to_host_row

OUTCOME_WON = "won"
OUTCOME_PROGRESS = "progress"
OUTCOME_NO_CHANGE = "no_change"
OUTCOME_NO_LONGER_GROUNDED = "no_longer_grounded"
OUTCOME_NOT_COMPARABLE = "not_comparable"

_OUTCOME_ORDER = {
    OUTCOME_WON: 0,
    OUTCOME_PROGRESS: 1,
    OUTCOME_NO_CHANGE: 2,
    OUTCOME_NO_LONGER_GROUNDED: 3,
    OUTCOME_NOT_COMPARABLE: 4,
}

_NO_CAUSATION_NOTE = (
    "What changed at the hosts your last audit told you to target. These are "
    "observed re-audit facts — they do not prove any outreach you ran caused "
    "them."
)


def build_outreach_outcomes(
    *,
    current_report: Optional[Mapping[str, Any]],
    prior_report: Optional[Mapping[str, Any]],
    measurement_basis: Optional[Mapping[str, Any]],
    completed_actions: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Classify, per target the PRIOR run surfaced, what the CURRENT run
    observed at that host. See module docstring for the payload contract."""
    basis = measurement_basis if isinstance(measurement_basis, Mapping) else {}
    same_basis = basis.get("same")
    base = {
        "is_first_audit": prior_report is None,
        "available": False,
        "note": None,
        "comparable": same_basis,
        "basis_note": basis.get("note"),
        "targets": [],
        "summary": _summary([]),
        "closed_channels_excluded": [],
    }

    if prior_report is None:
        base["note"] = (
            "First audit — this run establishes the targets; outcomes appear "
            "on your next re-audit."
        )
        return base

    prior_root = _root(prior_report)
    current_root = _root(current_report)

    targets, closed_hosts = _prior_targets(prior_root)
    base["closed_channels_excluded"] = closed_hosts
    if not targets:
        base["note"] = (
            "Your previous audit surfaced no outreach targets, so there is "
            "nothing to compare host-by-host."
        )
        return base

    current_facts = _current_facts(current_root)
    if not current_facts["has_host_data"]:
        base["note"] = (
            "The current run carries no host-level grounding data, so target "
            "outcomes can't be measured this time."
        )
        return base

    prior_facts = _prior_facts(prior_root)
    done_by_host = _completed_by_host(completed_actions)
    run_ts = _parse_ts(current_root.get("timestamp"))

    rows = [
        _classify_target(
            target,
            same_basis=same_basis,
            prior_facts=prior_facts,
            current_facts=current_facts,
            done_by_host=done_by_host,
            run_ts=run_ts,
        )
        for target in targets
    ]
    rows.sort(
        key=lambda r: (
            _OUTCOME_ORDER.get(r["outcome"], 9),
            r["host"],
            r["query"] or "",
        )
    )

    base["available"] = True
    base["note"] = _NO_CAUSATION_NOTE
    base["targets"] = rows
    base["summary"] = _summary(rows)
    return base


# ---------------------------------------------------------------------------
# Prior-run targets


def _prior_targets(
    prior_root: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Union of the prior run's win-plan losing-query targets (keyed
    host+query) and narrative outreach-move hosts (host-only, unless the host
    already appears query-keyed). Closed channels — hosts the prior report
    explicitly named as doors no pitch can open — are excluded and returned
    separately so their absence is a stated fact, not a silent drop."""
    closed_hosts = sorted(
        {
            h
            for c in _where_losing(prior_root).get("closed_channels") or []
            if isinstance(c, Mapping)
            for h in [_norm_host(c.get("host"))]
            if h
        }
    )
    closed = set(closed_hosts)

    by_key: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}
    query_hosts: Set[str] = set()

    win_plan = _as_mapping(prior_root.get("win_plan"))
    for plan in _as_list(win_plan.get("sku_plans")):
        if not isinstance(plan, Mapping):
            continue
        for lq in _as_list(plan.get("losing_queries")):
            if not isinstance(lq, Mapping):
                continue
            query = str(lq.get("query") or "").strip()
            qkey = _norm_query(query)
            if not qkey:
                continue
            for t in _as_list(lq.get("grounds_in")):
                if not isinstance(t, Mapping):
                    continue
                host = _norm_host(t.get("host"))
                if not host or host in closed:
                    continue
                key = (host, qkey)
                if key not in by_key:
                    by_key[key] = {
                        "host": host,
                        "query": query,
                        "qkey": qkey,
                        "axis": lq.get("axis"),
                        "target_source": "win_plan",
                    }
                query_hosts.add(host)

    for move in _as_list(_where_losing(prior_root).get("outreach_moves")):
        if not isinstance(move, Mapping):
            continue
        host = _norm_host(move.get("host"))
        if not host or host in closed:
            continue
        if host in query_hosts:
            # Already a query-keyed target — record the dual provenance there.
            for row in by_key.values():
                if row["host"] == host:
                    row["target_source"] = "win_plan+outreach_move"
            continue
        key = (host, None)
        if key not in by_key:
            by_key[key] = {
                "host": host,
                "query": None,
                "qkey": None,
                "axis": None,
                "target_source": "outreach_move",
            }

    return list(by_key.values()), closed_hosts


# ---------------------------------------------------------------------------
# Per-run host/query facts


def _prior_facts(prior_root: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "endorsement": _endorsement_hosts(prior_root),
        "hosts": _authority_rows_by_host(prior_root),
    }


def _current_facts(current_root: Mapping[str, Any]) -> Dict[str, Any]:
    hosts = _authority_rows_by_host(current_root)
    failing = _current_failing_queries(current_root)
    probed = _probed_query_keys(current_root)
    return {
        "has_host_data": bool(hosts) or bool(failing),
        "endorsement": _endorsement_hosts(current_root),
        "endorsement_category": _endorsement_hosts(
            current_root, key="endorsement_category_hosts"
        ),
        "hosts": hosts,
        # qkey -> set of hosts grounding that (still failing) query
        "failing_query_hosts": failing,
        # normalized probed queries; empty set = unknown coverage
        "probed_queries": probed,
    }


def _endorsement_hosts(
    root: Mapping[str, Any], *, key: str = "endorsement_hosts"
) -> Set[str]:
    summary = _as_mapping(_as_mapping(root.get("authority_map")).get(
        "host_attribution_summary"
    ))
    hosts = {_norm_host(h) for h in _as_list(summary.get(key))}
    if not hosts and key == "endorsement_hosts":
        run_facts = _as_mapping(_as_mapping(root.get("brand_rollup")).get("run_facts"))
        hosts = {_norm_host(h) for h in _as_list(run_facts.get(key))}
    return {h for h in hosts if h}


def _authority_rows_by_host(root: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for row in _as_list(_as_mapping(root.get("authority_map")).get("hosts")):
        if not isinstance(row, Mapping):
            continue
        host = _norm_host(row.get("host"))
        if host and host not in out:
            out[host] = row
    return out


def _current_failing_queries(root: Mapping[str, Any]) -> Dict[str, Set[str]]:
    """qkey → the hosts the CURRENT run grounded that still-failing query in,
    via the same evidence_urls uri join the win plan uses. A failing query
    whose sources didn't resolve keeps an empty host set (still counted as
    failing)."""
    authority_by_sku = {
        sku.get("sku_key"): _as_list(sku.get("authority_hosts"))
        for sku in _as_list(_as_mapping(root.get("authority_map")).get("skus"))
        if isinstance(sku, Mapping)
    }
    out: Dict[str, Set[str]] = {}
    for report in _as_list(root.get("per_sku_reports")):
        if not isinstance(report, Mapping):
            continue
        uri_index = _uri_to_host_row(
            [
                r
                for r in authority_by_sku.get(report.get("sku_key")) or []
                if isinstance(r, dict)
            ]
        )
        for fp in _as_list(report.get("failing_prompts")):
            if not isinstance(fp, Mapping):
                continue
            qkey = _norm_query(fp.get("query"))
            if not qkey:
                continue
            hosts = out.setdefault(qkey, set())
            resolved = _resolve_query_hosts(
                [s for s in _as_list(fp.get("grounding_sources")) if isinstance(s, dict)],
                uri_index,
            )
            hosts.update(h for h in (_norm_host(r.get("host")) for r in resolved) if h)
    return out


def _probed_query_keys(root: Mapping[str, Any]) -> Set[str]:
    """Every query the current run demonstrably probed (opportunity.per_prompt
    + failing_prompts). Used only to corroborate a 'won' claim — empty means
    coverage is unknown and the pinned basis is trusted alone."""
    keys: Set[str] = set()
    for report in _as_list(root.get("per_sku_reports")):
        if not isinstance(report, Mapping):
            continue
        for row in _as_list(_as_mapping(report.get("opportunity")).get("per_prompt")):
            if isinstance(row, Mapping):
                keys.add(_norm_query(row.get("normalized_query") or row.get("query")))
        for fp in _as_list(report.get("failing_prompts")):
            if isinstance(fp, Mapping):
                keys.add(_norm_query(fp.get("query")))
    keys.discard("")
    return keys


# ---------------------------------------------------------------------------
# Classification


def _classify_target(
    target: Mapping[str, Any],
    *,
    same_basis: Optional[bool],
    prior_facts: Mapping[str, Any],
    current_facts: Mapping[str, Any],
    done_by_host: Mapping[str, Mapping[str, Any]],
    run_ts: Optional[datetime],
) -> Dict[str, Any]:
    host = target["host"]
    query = target["query"]
    qkey = target["qkey"]

    prior_row = _as_mapping(prior_facts["hosts"].get(host))
    current_row = _as_mapping(current_facts["hosts"].get(host))
    prior_endorsed = host in prior_facts["endorsement"]
    current_endorsed = host in current_facts["endorsement"]
    prior_names = prior_endorsed or _names_merchant(prior_row)
    current_names = current_endorsed or _names_merchant(current_row)
    endorsement_transition = current_endorsed and not prior_endorsed

    failing = current_facts["failing_query_hosts"]
    probed = current_facts["probed_queries"]
    query_still_losing: Optional[bool] = (qkey in failing) if qkey else None
    grounds_this_query: Optional[bool] = (
        host in failing.get(qkey, set()) if qkey else None
    )

    outcome: str
    reason: str
    if same_basis is not True:
        # Basis changed (or one side predates pinning): no per-query claims.
        # The one basis-independent fact still worth naming: the host entered
        # the endorsement set — it now recommends the merchant.
        if endorsement_transition:
            outcome, reason = OUTCOME_WON, "host_now_endorses"
        else:
            outcome, reason = OUTCOME_NOT_COMPARABLE, "basis_changed"
    elif qkey is not None:
        if not query_still_losing:
            if probed and qkey not in probed:
                # Stated limit: the query is absent from the run's probed set,
                # so "no longer failing" would be a coverage artifact.
                outcome, reason = OUTCOME_NO_LONGER_GROUNDED, "query_not_probed"
            else:
                outcome, reason = OUTCOME_WON, "query_now_cited"
        elif endorsement_transition:
            outcome, reason = OUTCOME_WON, "host_now_endorses"
        elif current_names and not prior_names:
            outcome, reason = OUTCOME_PROGRESS, "host_now_names_you"
        elif grounds_this_query:
            outcome, reason = OUTCOME_NO_CHANGE, "still_grounds_without_naming"
        else:
            outcome, reason = (
                OUTCOME_NO_LONGER_GROUNDED,
                "absent_from_query_grounding",
            )
    else:
        # Host-only target (outreach move without query context).
        if endorsement_transition:
            outcome, reason = OUTCOME_WON, "host_now_endorses"
        elif current_names and not prior_names:
            outcome, reason = OUTCOME_PROGRESS, "host_now_names_you"
        elif current_row:
            outcome, reason = OUTCOME_NO_CHANGE, "still_grounds_without_naming"
        else:
            outcome, reason = (
                OUTCOME_NO_LONGER_GROUNDED,
                "absent_from_run_grounding",
            )

    return {
        "host": host,
        "query": query,
        "axis": target.get("axis"),
        "target_source": target["target_source"],
        "outcome": outcome,
        "reason": reason,
        "what_changed": _what_changed(
            outcome,
            reason,
            host=host,
            query=query,
            category_endorsed=host in current_facts["endorsement_category"],
        ),
        "signals": {
            "prior": _host_signals(prior_row, endorsed=prior_endorsed, names=prior_names),
            "current": {
                **_host_signals(
                    current_row, endorsed=current_endorsed, names=current_names
                ),
                "grounds_this_query": grounds_this_query,
                "query_still_losing": query_still_losing,
            },
        },
        "merchant_action": _merchant_action(done_by_host.get(host), run_ts),
    }


def _what_changed(
    outcome: str,
    reason: str,
    *,
    host: str,
    query: Optional[str],
    category_endorsed: bool,
) -> str:
    # Observational copy only — never "your pitch worked" (no causation claim).
    if outcome == OUTCOME_WON and reason == "query_now_cited":
        return (
            f'"{query}" — lost at your last audit — now surfaces you in the '
            "grounded answer."
        )
    if outcome == OUTCOME_WON:
        where = " on a category query" if category_endorsed else ""
        return f"{host} now names you{where} — it moved into your endorsement set."
    if outcome == OUTCOME_PROGRESS:
        tail = f', but "{query}" is still lost.' if query else "."
        return f"{host} now cites your product on at least one query{tail}"
    if outcome == OUTCOME_NO_CHANGE:
        if query:
            return f'{host} still grounds "{query}" without naming you.'
        return f"{host} is still cited in your answers without naming you."
    if outcome == OUTCOME_NO_LONGER_GROUNDED:
        if reason == "query_not_probed":
            return (
                f'"{query}" was not sampled in this run\'s probe set, so no '
                "claim is made either way."
            )
        where = f' the grounding for "{query}"' if query else " this run's grounding"
        return (
            f"{host} no longer appears in{where} — the engine sampled "
            "different sources; neither a win nor a loss."
        )
    return (
        "The prompt set changed between these runs, so no per-query claim is "
        f"made for {host}."
    )


def _host_signals(
    row: Mapping[str, Any], *, endorsed: bool, names: bool
) -> Dict[str, Any]:
    return {
        "endorsed": endorsed,
        "names_you": names,
        "citation_role": row.get("citation_role") if row else None,
        # Raw count for context only — count deltas never classify (a
        # 1→2 move on a 1-cite baseline is noise, per audit_delta's
        # materiality stance).
        "prompts_cited_count": _int_or_none(row.get("prompts_cited_count"))
        if row
        else None,
    }


def _names_merchant(row: Mapping[str, Any]) -> bool:
    return bool(row.get("cites_exact_sku") or row.get("cites_near_variant"))


# ---------------------------------------------------------------------------
# Merchant-action annotation (fact, not causation)


def _completed_by_host(
    completed_actions: Optional[List[Mapping[str, Any]]],
) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for row in completed_actions or []:
        if not isinstance(row, Mapping):
            continue
        host = _norm_host(row.get("host") or row.get("target_host"))
        if host and host not in out:
            out[host] = row
    return out


def _merchant_action(
    row: Optional[Mapping[str, Any]], run_ts: Optional[datetime]
) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    done_at = _parse_ts(row.get("completed_at") or row.get("marked_done_at"))
    days: Optional[int] = None
    if done_at and run_ts and run_ts >= done_at:
        days = (run_ts - done_at).days
    title = str(row.get("title") or "").strip() or "this outreach task"
    when = f" {days} day{'s' if days != 1 else ''} before this run" if days is not None else ""
    return {
        "title": title,
        "marked_done_at": done_at.isoformat() if done_at else None,
        "days_before_current_run": days,
        # Fair to surface the merchant's own record; still not a causal claim.
        "note": f"You marked “{title}” done{when}.",
    }


# ---------------------------------------------------------------------------
# Shape tolerance + small utils


def _root(report: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not isinstance(report, Mapping):
        return {}
    brand = report.get("brand_report")
    return brand if isinstance(brand, Mapping) else report


def _where_losing(root: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(
        _as_mapping(root.get("merchant_narrative")).get("where_youre_losing")
    )


def _summary(rows: List[Mapping[str, Any]]) -> Dict[str, int]:
    counts = {k: 0 for k in _OUTCOME_ORDER}
    for row in rows:
        outcome = str(row.get("outcome") or "")
        if outcome in counts:
            counts[outcome] += 1
    return counts


def _norm_host(value: Any) -> str:
    return str(value or "").strip().lower()


def _norm_query(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]

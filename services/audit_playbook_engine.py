"""
Phase C-4 (PR-G): per-cited-host action playbook engine.

PR-E classified the cited hosts. PR-F attributed which URL won which
failed query, with which competitor brands. PR-G is the synthesis
layer: turn that evidence into specific, named, ordered action items
the merchant can execute.

For each cited host that's relevant to the merchant's category, this
module:

  1. Selects the most-specific matching playbook from
     `data/playbooks.json`. Subtype matches beat type-only matches;
     a fallback `generic_<type>` covers known-type-unknown-subtype;
     a `generic_unclassified` covers everything else.

  2. Renders the playbook's title + body templates with this host's
     real data: the host name, the registry's coverage_note, the
     registry's outreach_hint, and (when available) an example failed
     query that targeted this host plus the competitor brands Gemini
     named in that query.

  3. Emits an action dict in the same shape `_generate_action_items`
     uses (severity / title / body / evidence) plus four new fields:
     `playbook_step_id`, `target_host`, `lever`,
     `expected_timeline_weeks`. Frontend can group / filter on those
     without needing to parse the body text.

The output list is sorted by severity (critical > high > medium >
low) then by `times_cited` descending, and capped at `cap` entries
(default 5) so the merchant sees the highest-leverage moves first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import logger

_PLAYBOOK_PATH = Path(__file__).resolve().parent.parent / "data" / "playbooks.json"
_PLAYBOOK_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _load_playbooks() -> Dict[str, Dict[str, Any]]:
    """Lazy-load the playbook registry on first call. Returns an empty
    dict on read/parse failure so the audit never crashes because BD
    happened to ship malformed JSON — the engine just emits no
    playbook actions."""
    global _PLAYBOOK_CACHE
    if _PLAYBOOK_CACHE is not None:
        return _PLAYBOOK_CACHE
    try:
        with open(_PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        logger.warning(
            "playbook registry not found at %s — playbook actions disabled.",
            _PLAYBOOK_PATH,
        )
        _PLAYBOOK_CACHE = {}
        return _PLAYBOOK_CACHE
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "playbook registry failed to load (%s) — playbook actions disabled.",
            exc,
        )
        _PLAYBOOK_CACHE = {}
        return _PLAYBOOK_CACHE

    raw = doc.get("playbooks") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        logger.warning("playbook registry has no 'playbooks' object — disabled.")
        _PLAYBOOK_CACHE = {}
        return _PLAYBOOK_CACHE

    _PLAYBOOK_CACHE = {k: v for k, v in raw.items() if isinstance(v, dict)}
    return _PLAYBOOK_CACHE


def reset_playbook_cache() -> None:
    """Test hook — drop the in-memory cache so the next lookup reads
    from disk again. Pairs with monkeypatching `_PLAYBOOK_PATH`."""
    global _PLAYBOOK_CACHE
    _PLAYBOOK_CACHE = None


def _select_playbook_for_host(
    host_classification: Dict[str, Any],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Find the most-specific playbook matching this host. Returns
    (playbook_id, playbook_dict) or None if nothing matches.

    Priority order:
      1. Exact (type, subtype) match
      2. Type-only match (any playbook with applies_when={type: T} and
         no subtype constraint) — these are the `generic_<type>` rows
      3. unclassified fallback (`generic_unclassified`)
    """
    host_type = (host_classification or {}).get("type")
    host_subtype = (host_classification or {}).get("subtype")
    if not host_type:
        return None

    playbooks = _load_playbooks()
    if not playbooks:
        return None

    # 1. Exact (type, subtype) match.
    if host_subtype:
        for pid, pb in playbooks.items():
            aw = pb.get("applies_when") or {}
            if aw.get("type") == host_type and aw.get("subtype") == host_subtype:
                return (pid, pb)

    # 2. Type-only match (no subtype constraint on the playbook).
    for pid, pb in playbooks.items():
        aw = pb.get("applies_when") or {}
        if aw.get("type") == host_type and not aw.get("subtype"):
            return (pid, pb)

    return None


def _render_template(template: str, ctx: Dict[str, Any]) -> str:
    """str.format with a defaulting Mapping so missing placeholders
    render as empty rather than raising. Trims double-spaces created
    by empty replacements."""
    class _DefaultDict(dict):
        def __missing__(self, key):  # type: ignore[override]
            return ""

    rendered = template.format_map(_DefaultDict(ctx))
    # Collapse repeated whitespace introduced by empty fields.
    while "  " in rendered:
        rendered = rendered.replace("  ", " ")
    return rendered.strip()


def _example_query_for_host(
    host: str,
    failed_queries_detailed: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Find the first failed-query entry whose top_cited_host matches
    this host. Returns the entry or None if no failed query targets
    this host. Used to enrich the playbook body with a concrete
    example ('for "best pajamas under $100", they listed Lunya, ...')."""
    if not host:
        return None
    h_lower = host.strip().lower()
    for fq in failed_queries_detailed or []:
        cited = (fq.get("top_cited_host") or "").lower()
        if cited == h_lower:
            return fq
    return None


def _competitors_phrase(competitors: List[str]) -> str:
    """Render a sentence fragment naming up to 3 competitor brands
    Gemini cited. Empty string when no competitors named."""
    names = [c for c in (competitors or []) if isinstance(c, str) and c.strip()]
    if not names:
        return ""
    if len(names) == 1:
        return f"They listed {names[0]}; your brand absent. "
    if len(names) == 2:
        return f"They listed {names[0]} and {names[1]}; your brand absent. "
    return (
        f"They listed {', '.join(names[:3])}; your brand absent. "
    )


def _example_phrase(example_query: Optional[Dict[str, Any]]) -> str:
    """Render a sentence fragment naming the specific failed query
    that hit this host. Empty when no example available."""
    if not example_query:
        return ""
    q = (example_query.get("query") or "").strip()
    if not q:
        return ""
    if len(q) > 70:
        q = q[:67].rstrip() + "..."
    return f'For "{q}", they were the cited URL. '


def select_playbooks(
    *,
    cited_hosts_detailed: List[Dict[str, Any]],
    failed_queries_detailed: List[Dict[str, Any]],
    cap: int = 5,
) -> List[Dict[str, Any]]:
    """Produce per-host playbook actions for the merchant audit's
    `merchant_view.actions` extension. Skips hosts where
    `applies_to_merchant_category` is False — they're cited but
    irrelevant to this merchant (e.g. a beauty-only host appearing
    in a sleepwear audit).

    Each output entry mirrors `_generate_action_items` shape and adds:
      playbook_step_id          : the matched playbook key
      target_host               : the cited host this action targets
      lever                     : editorial_outreach / wholesale_onboarding / ...
      expected_timeline_weeks   : [low, high]
    """
    actions: List[Dict[str, Any]] = []

    for entry in cited_hosts_detailed or []:
        host = (entry.get("host") or "").strip()
        if not host:
            continue
        applies = entry.get("applies_to_merchant_category")
        # When applies is explicitly False, skip — this host doesn't
        # cover the merchant's category. None (registry didn't know
        # merchant_category) and True both pass through.
        if applies is False:
            continue

        selection = _select_playbook_for_host(entry)
        if not selection:
            continue
        pid, pb = selection
        timeline = pb.get("expected_timeline_weeks") or [0, 0]
        try:
            tl_low = int(timeline[0])
            tl_high = int(timeline[1])
        except (ValueError, TypeError, IndexError):
            tl_low = tl_high = 0

        example = _example_query_for_host(host, failed_queries_detailed)
        competitors_named = (example or {}).get("competitors_named") or []

        ctx = {
            "host": host,
            "coverage_note": entry.get("coverage_note") or "",
            "outreach_hint": entry.get("outreach_hint") or "",
            "competitors_phrase": _competitors_phrase(competitors_named),
            "example_phrase": _example_phrase(example),
            "timeline_low": tl_low,
            "timeline_high": tl_high,
        }

        title = _render_template(pb.get("title_template") or "", ctx)
        body = _render_template(pb.get("body_template") or "", ctx)
        if not title or not body:
            continue

        # `concrete_next_step` (playbook schema_v2) is the BD-curated
        # 1-sentence "this week" task — what the merchant should DO
        # first, with specifics (URLs, who to email, sample sizes,
        # required documents). Distinct from `body` which describes
        # the strategic rationale.
        concrete_next_step_tpl = pb.get("concrete_next_step") or ""
        concrete_next_step = (
            _render_template(concrete_next_step_tpl, ctx)
            if concrete_next_step_tpl else None
        )

        actions.append({
            "severity": pb.get("severity") or "medium",
            "title": title,
            "body": body,
            "concrete_next_step": concrete_next_step,
            "evidence": {
                "host": host,
                "times_cited": entry.get("times_cited") or 0,
                "competitors_named": list(competitors_named),
                "example_failed_query": (example or {}).get("query"),
            },
            "playbook_step_id": pid,
            "target_host": host,
            "lever": pb.get("lever"),
            "expected_timeline_weeks": [tl_low, tl_high],
        })

    # Sort: severity ascending (critical first) then times_cited descending.
    actions.sort(
        key=lambda a: (
            _SEVERITY_RANK.get((a.get("severity") or "low"), 99),
            -(a.get("evidence", {}) or {}).get("times_cited", 0),
        )
    )
    return actions[:cap]

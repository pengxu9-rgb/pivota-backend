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


def _competitors_phrase_short(competitors: List[str]) -> str:
    """Email-friendly variant — names competitors without the "your
    brand absent" closer (which makes sense in body prose but reads
    harshly in a pitch email)."""
    names = [c for c in (competitors or []) if isinstance(c, str) and c.strip()]
    if not names:
        return "other brands in this category"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:2])}, and {names[2]}"


def _example_phrase_short(example_query: Optional[Dict[str, Any]]) -> str:
    """Email-friendly variant — references the query as a parenthetical
    rather than a full sentence."""
    if not example_query:
        return ""
    q = (example_query.get("query") or "").strip()
    if not q:
        return ""
    if len(q) > 70:
        q = q[:67].rstrip() + "..."
    return f' (e.g. for "{q}")'


def _build_pitch_draft(
    *,
    pb: Dict[str, Any],
    host_entry: Dict[str, Any],
    ctx: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Phase A: render the pre-filled email pitch the merchant can
    one-click into their mail client. Returns None when either side
    of the contract is missing (playbook has no template, or host
    has no email recipient).

    Output shape:
      {
        "subject": "...",
        "body": "...",
        "recipient_email": "tips@nymag.com",
        "recipient_note":  "..." | None,
      }

    Email-only path. Hosts without a published editorial email (e.g.
    Wirecutter's submission form) are intentionally skipped — we only
    render the "Draft pitch email" CTA when it actually opens a
    pre-filled mail client in one click. Half-working fallbacks
    (open-the-form-yourself) erode trust in the execution layer; we
    prefer honest coverage gaps.
    """
    pitch_tpl = pb.get("pitch_template") or {}
    subject_tpl = pitch_tpl.get("subject_template") or ""
    body_tpl = pitch_tpl.get("body_template") or ""
    if not subject_tpl or not body_tpl:
        return None

    recipient = host_entry.get("pitch_recipient") or {}
    recipient_email = recipient.get("email") if isinstance(recipient, dict) else None
    recipient_note = recipient.get("note") if isinstance(recipient, dict) else None

    if not recipient_email:
        return None

    subject = _render_template(subject_tpl, ctx)
    body = _render_template(body_tpl, ctx)
    if not subject or not body:
        return None

    return {
        "subject": subject,
        "body": body,
        "recipient_email": recipient_email,
        "recipient_note": recipient_note,
    }


# A host cited only once in the audit's grounding sources is weak
# evidence for any host-targeted action — a single citation could be
# noise (one tangential mention) rather than a real capture pattern.
# The PR-8 quality review flagged a `creator_partnerships: youtube.com`
# action that fired off 2 cites (defensible) but noted that at 1 cite
# the same action reads as flimsy. The threshold gates host-targeted
# playbook actions on at least this many citations. Callers can lower
# it to 1 to restore the prior all-hosts behavior.
_DEFAULT_MIN_TIMES_CITED = 2


def select_playbooks(
    *,
    cited_hosts_detailed: List[Dict[str, Any]],
    failed_queries_detailed: List[Dict[str, Any]],
    merchant_name: Optional[str] = None,
    merchant_category: Optional[str] = None,
    attribution_score: Optional[int] = None,
    category_score: Optional[int] = None,
    cap: int = 5,
    min_times_cited: int = _DEFAULT_MIN_TIMES_CITED,
) -> List[Dict[str, Any]]:
    """Produce per-host playbook actions for the merchant audit's
    `merchant_view.actions` extension. Skips hosts where
    `applies_to_merchant_category` is False — they're cited but
    irrelevant to this merchant (e.g. a beauty-only host appearing
    in a sleepwear audit).

    `min_times_cited` (default 2): hosts with fewer citations than
    this in the audit's grounding sources are skipped — a single
    citation is too weak to justify a host-targeted action on the
    merchant queue. Pass `min_times_cited=1` to restore the prior
    all-cited-hosts behavior.

    Each output entry mirrors `_generate_action_items` shape and adds:
      playbook_step_id          : the matched playbook key
      target_host               : the cited host this action targets
      lever                     : editorial_outreach / wholesale_onboarding / ...
      expected_timeline_weeks   : [low, high]
      pitch_draft (optional)    : pre-filled pitch email — see Phase A
                                  spec. Present when both the playbook
                                  has `pitch_template` and the host
                                  has at least an outreach destination
                                  (`email` or `submission_url`).
    """
    actions: List[Dict[str, Any]] = []
    # Guard against a caller passing 0 / negative — treat anything
    # below 1 as "no threshold" rather than skipping every host.
    effective_min_cited = max(1, int(min_times_cited))

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

        # Min-citation gate. A host cited fewer than `effective_min_cited`
        # times is too thin to anchor a host-targeted action. `times_cited`
        # missing / non-int is treated as 0 → skipped (we don't emit
        # actions for hosts whose citation count we can't establish).
        raw_cited = entry.get("times_cited")
        times_cited = raw_cited if isinstance(raw_cited, int) else 0
        if times_cited < effective_min_cited:
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

        # Phase A: render the pitch_draft when playbook + host both
        # have the right metadata. Use a richer ctx so subject/body
        # templates can reference merchant_name + category in addition
        # to the existing host / competitors / example fields. Uses
        # _short variants of the phrases so they fit naturally inside
        # email-shape sentences.
        competitors_short = _competitors_phrase_short(competitors_named)
        example_short = _example_phrase_short(example)
        category_label = (merchant_category or "category").strip() or "category"
        merchant_label = (merchant_name or "[brand]").strip() or "[brand]"

        ctx = {
            "host": host,
            "coverage_note": entry.get("coverage_note") or "",
            "outreach_hint": entry.get("outreach_hint") or "",
            "competitors_phrase": _competitors_phrase(competitors_named),
            "example_phrase": _example_phrase(example),
            "timeline_low": tl_low,
            "timeline_high": tl_high,
            # Pitch-template-only fields (used only by pitch_template
            # render below, but harmless to include in the broader
            # ctx — _render_template ignores extras).
            "merchant_name": merchant_label,
            "category": category_label,
            "competitors_phrase_short": competitors_short,
            "example_phrase_short": example_short,
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

        # Phase A pitch_draft. Emit when the playbook has a
        # pitch_template AND the host has a pitch_recipient (email
        # OR submission_url). Without either side, no pitch is
        # rendered (no draft button).
        pitch_draft = _build_pitch_draft(
            pb=pb,
            host_entry=entry,
            ctx=ctx,
        )

        # Q-P1-6: route the playbook's authored severity through the
        # central scorer so it can be calibrated against the actual
        # audit evidence. Pre-fix the playbook's severity flowed
        # directly into the action (e.g. "Pitch whowhatwear.com" was
        # `high` even when competitors_named=[] and example=null —
        # the playbook authors picked "high" as the default, the
        # audit had nothing to back it up, but the merchant saw
        # "high" anyway).
        from services.audit_severity import compute_action_severity

        # score_gap_pct: the discovery → attribution gap. When both
        # scores are present we use category - attribution (the
        # "category visible but attribution missing" pattern).
        # Falls back to "100 - attribution" when only attribution is
        # known (closer to goal-state framing).
        score_gap_pct: Optional[int] = None
        if isinstance(attribution_score, int):
            if isinstance(category_score, int):
                score_gap_pct = max(0, category_score - attribution_score)
            else:
                score_gap_pct = max(0, 100 - attribution_score)

        host_type = (entry.get("type") or "").strip().lower() or None
        example_query_str = (example or {}).get("query")
        has_failed_query_example = bool(
            example_query_str and str(example_query_str).strip()
        )
        has_competitors_named = bool(competitors_named)

        scored_severity, severity_reason = compute_action_severity(
            score_gap_pct=score_gap_pct,
            host_type=host_type,
            has_failed_query_example=has_failed_query_example,
            has_competitors_named=has_competitors_named,
            base_severity=pb.get("severity") or "medium",
        )

        actions.append({
            "severity": scored_severity,
            "severity_reason": severity_reason,
            "title": title,
            "body": body,
            "concrete_next_step": concrete_next_step,
            "evidence": {
                "host": host,
                "times_cited": entry.get("times_cited") or 0,
                "competitors_named": list(competitors_named),
                "example_failed_query": example_query_str,
            },
            "playbook_step_id": pid,
            "target_host": host,
            "lever": pb.get("lever"),
            "expected_timeline_weeks": [tl_low, tl_high],
            "pitch_draft": pitch_draft,
        })

    # Consolidate editorial pitches into one action before sort + cap.
    actions = _consolidate_editorial_actions(actions, merchant_category)

    # Sort: severity ascending (critical first) then times_cited descending.
    actions.sort(
        key=lambda a: (
            _SEVERITY_RANK.get((a.get("severity") or "low"), 99),
            -(a.get("evidence", {}) or {}).get("times_cited", 0),
        )
    )
    return actions[:cap]


# Editorial-pitch playbooks all carry lever="editorial_outreach". A
# D2C beauty audit routinely cites forbes.com + whowhatwear.com +
# marieclaire.com + ... — pre-consolidation that's one "Pitch X"
# action PER host, which reads as repetitive noise on the merchant
# queue (the PR-8 quality review's N7 finding). One consolidated
# "Pitch N editorial sites" action is the right granularity; the
# per-host pitch_draft + target_host are preserved inside
# `evidence.editorial_hosts` so the merchant can still act per-host.
_EDITORIAL_LEVER = "editorial_outreach"


def _consolidate_editorial_actions(
    actions: List[Dict[str, Any]],
    merchant_category: Optional[str],
) -> List[Dict[str, Any]]:
    """Collapse 2+ editorial-outreach playbook actions into a single
    consolidated action. 0 or 1 editorial actions pass through
    unchanged — a lone editorial pitch doesn't need the consolidation
    wrapper (it'd just add a layer of indirection for one host).

    Non-editorial actions (retailer / creator / research / wholesale)
    are never touched. Order within `actions` is otherwise preserved;
    the caller re-sorts by severity afterward.
    """
    editorial = [a for a in actions if a.get("lever") == _EDITORIAL_LEVER]
    if len(editorial) < 2:
        return actions

    non_editorial = [a for a in actions if a.get("lever") != _EDITORIAL_LEVER]

    # Per-host detail preserved so the merchant can still act per-host
    # — the pitch_draft (pre-filled email) is the load-bearing part.
    editorial_hosts: List[Dict[str, Any]] = []
    for a in editorial:
        editorial_hosts.append({
            "host": a.get("target_host"),
            "pitch_draft": a.get("pitch_draft"),
            "playbook_step_id": a.get("playbook_step_id"),
            "title": a.get("title"),
            "concrete_next_step": a.get("concrete_next_step"),
            "expected_timeline_weeks": a.get("expected_timeline_weeks"),
            "times_cited": (a.get("evidence", {}) or {}).get("times_cited", 0),
            # Per-host severity preserved so a per-host renderer can
            # show each site's individual tier — and so the
            # consolidated severity (max of the group) is auditable.
            "severity": a.get("severity"),
        })

    # Consolidated severity = the strongest (lowest rank value) among
    # the grouped actions — if any single editorial pitch was `high`,
    # the consolidated action is `high`.
    consolidated_severity = min(
        (a.get("severity") or "low" for a in editorial),
        key=lambda s: _SEVERITY_RANK.get(s, 99),
    )

    # Widest timeline span across the group.
    lows = [
        a.get("expected_timeline_weeks", [0, 0])[0]
        for a in editorial
        if isinstance(a.get("expected_timeline_weeks"), list)
        and len(a.get("expected_timeline_weeks") or []) == 2
    ]
    highs = [
        a.get("expected_timeline_weeks", [0, 0])[1]
        for a in editorial
        if isinstance(a.get("expected_timeline_weeks"), list)
        and len(a.get("expected_timeline_weeks") or []) == 2
    ]
    timeline = [min(lows), max(highs)] if lows and highs else [0, 0]

    hosts = [h["host"] for h in editorial_hosts if h.get("host")]
    host_list = ", ".join(hosts)
    category_label = (merchant_category or "").strip() or "your category"
    n = len(editorial_hosts)

    consolidated = {
        "severity": consolidated_severity,
        "severity_reason": "editorial_pitches_consolidated",
        "title": f"Pitch {n} editorial sites for {category_label} visibility",
        "body": (
            f"{n} editorial sites surfaced in your audit's grounding "
            f"sources are worth a pitch: {host_list}. Each is a place "
            f"AI assistants already look for {category_label} "
            f"recommendations — a placement there feeds back into "
            f"grounded answers. Per-site pitch drafts are attached "
            f"below; the sites are ordered by how often they were "
            f"cited in your audit."
        ),
        "concrete_next_step": (
            f"This week: start with the most-cited site ({hosts[0]}) — "
            f"send its pitch draft, then work down the list. Editorial "
            f"pitches need a story angle, not just a product spec."
        ),
        "evidence": {
            "editorial_hosts": editorial_hosts,
            "host_count": n,
            # Sum of citations across the grouped hosts — keeps the
            # severity-sort tiebreaker meaningful for the merged action.
            "times_cited": sum(h.get("times_cited", 0) for h in editorial_hosts),
        },
        "playbook_step_id": "editorial_pitch_consolidated",
        "target_host": None,  # multi-host — see evidence.editorial_hosts
        "lever": _EDITORIAL_LEVER,
        "expected_timeline_weeks": timeline,
        "pitch_draft": None,  # per-host drafts live in evidence.editorial_hosts
    }

    return non_editorial + [consolidated]

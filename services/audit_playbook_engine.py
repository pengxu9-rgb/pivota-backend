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
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import logger

_PLAYBOOK_PATH = Path(__file__).resolve().parent.parent / "data" / "playbooks.json"
_PLAYBOOK_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Priority for the get-indexed gateway action: ranks above every other
# content_revision action. Large but finite so it stays JSONB-serializable
# (float('inf') would break json/asyncpg serialization of report_jsonb).
_BLOCKED_GATEWAY_PRIORITY = 1e9


# Levers whose playbook actions are suppressed from merchant output until
# the data / contact path behind them is real. The `creator_partnership`
# video + social playbooks (creator_partnership_video,
# creator_partnership_social) render category-agnostic boilerplate with
# invented rate ranges ("$50-500/video", "$200-2000/post") and no actual
# creator data or outreach path behind them. That's the same gap that
# keeps the creator *matchmaker* (services/creator_matcher.py →
# build_creator_partnership_action) emitting nothing today: with no
# mass creator-data access, outreach isn't executable, so we surface zero
# rather than fabricate. Drop a lever from this set once its actions carry
# real, executable creator data + a contact path.
_SUPPRESSED_LEVERS = frozenset({"creator_partnership"})


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

    def _stringify(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(str(v) for v in value if v is not None)
        return str(value)

    if "{{" in (template or ""):
        rendered = re.sub(
            r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}",
            lambda m: _stringify(ctx.get(m.group(1), "")),
            template,
        )
    else:
        rendered = template.format_map(_DefaultDict({
            k: _stringify(v) for k, v in (ctx or {}).items()
        }))
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
    from services.win_plan_builder import is_broad_head_query

    h_lower = host.strip().lower()
    matches = [
        fq
        for fq in failed_queries_detailed or []
        if (fq.get("top_cited_host") or "").lower() == h_lower
    ]
    # Niche-first example: a specific failed query makes the pitch concrete
    # ("for <specific ask>, they listed X"); a head baseline makes the pitch
    # generic. Head examples survive only when they're the only match.
    for fq in matches:
        if not is_broad_head_query(
            fq.get("query"), prompt_source=fq.get("prompt_source")
        ):
            return fq
    return matches[0] if matches else None


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
    allow_submission_channel: bool = False,
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

    Two honest channels, never a fake mailto (P3-8):
      - channel="email": a published editorial email exists — the UI may
        render a one-click pre-filled mail client CTA.
      - channel="submission_form" (only when allow_submission_channel=True —
        the win-plan surface, whose renderer handles email-less drafts): no
        email, but a published submission / contact page exists — the UI
        renders "copy this draft + open their form", never a mailto with no
        recipient. (The original email-only rule was about not shipping
        half-working mailto CTAs; a paste-ready draft next to the real form
        link keeps that promise while unlocking form-only hosts like
        Wirecutter or Rtings.) Callers whose renderers assume an email CTA
        (select_playbooks → the ai-readiness DraftPitchButton mailto) keep the
        default and stay email-only.
    Hosts with NEITHER recipient stay None — the honest coverage gap.
    """
    pitch_tpl = pb.get("pitch_template") or {}
    subject_tpl = pitch_tpl.get("subject_template") or ""
    body_tpl = pitch_tpl.get("body_template") or ""
    if not subject_tpl or not body_tpl:
        return None

    recipient = host_entry.get("pitch_recipient") or {}
    recipient = recipient if isinstance(recipient, dict) else {}
    recipient_email = recipient.get("email")
    submission_url = recipient.get("submission_url")
    recipient_note = recipient.get("note")

    if not recipient_email and not (allow_submission_channel and submission_url):
        return None

    subject = _render_template(subject_tpl, ctx)
    body = _render_template(body_tpl, ctx)
    if not subject or not body:
        return None

    return {
        "subject": subject,
        "body": body,
        "channel": "email" if recipient_email else "submission_form",
        "recipient_email": recipient_email,
        "submission_url": submission_url,
        "recipient_note": recipient_note,
    }


def build_pitch_draft_for_host(
    host_entry: Dict[str, Any],
    *,
    merchant_name: Optional[str] = None,
    merchant_category: Optional[str] = None,
    example_query: Optional[str] = None,
    competitors_named: Optional[List[str]] = None,
    allow_submission_channel: bool = False,
) -> Optional[Dict[str, Any]]:
    """Render the one-click pitch email for a SINGLE target host, keyed to a
    specific failing query + its competitor benchmark.

    `host_entry` is a `cited_host_classifier.classify_host` result (carries
    `type`/`subtype` for playbook selection and `pitch_recipient` for the
    recipient). Reuses the exact playbook selection + template rendering +
    recipient contract `select_playbooks` uses — the per-host seam the win-plan
    layer (Fix 4) calls instead of re-implementing the email.

    Returns the pitch_draft dict (`subject`/`body`/`recipient_email`/
    `recipient_note`) or None when the matched playbook has no `pitch_template`
    or the host has no email recipient — the honest "no one-click draft" case
    the caller degrades to submission_only / target_only.
    """
    if not isinstance(host_entry, dict):
        return None
    selection = _select_playbook_for_host(host_entry)
    if not selection:
        return None
    _pid, pb = selection
    # Defense in depth: suppressed levers (e.g. creator_partnership) must not
    # emit a one-click pitch draft either. Today this is moot — the creator
    # playbooks carry no pitch_template, so we'd return None below anyway — but
    # gating here keeps select_playbooks() and this seam consistent if a
    # pitch_template is ever added. See _SUPPRESSED_LEVERS.
    if (pb.get("lever") or "") in _SUPPRESSED_LEVERS:
        return None
    timeline = pb.get("expected_timeline_weeks") or [0, 0]
    try:
        tl_low, tl_high = int(timeline[0]), int(timeline[1])
    except (ValueError, TypeError, IndexError):
        tl_low = tl_high = 0
    comps = [c for c in (competitors_named or []) if isinstance(c, str) and c.strip()]
    example = {"query": example_query} if example_query else None
    ctx = {
        "host": host_entry.get("host") or "",
        "coverage_note": host_entry.get("coverage_note") or "",
        "outreach_hint": host_entry.get("outreach_hint") or "",
        "competitors_phrase": _competitors_phrase(comps),
        "example_phrase": _example_phrase(example),
        "timeline_low": tl_low,
        "timeline_high": tl_high,
        "merchant_name": (merchant_name or "[brand]").strip() or "[brand]",
        "category": (merchant_category or "category").strip() or "category",
        "competitors_phrase_short": _competitors_phrase_short(comps),
        "example_phrase_short": _example_phrase_short(example),
    }
    return _build_pitch_draft(
        pb=pb, host_entry=host_entry, ctx=ctx,
        allow_submission_channel=allow_submission_channel,
    )


# A host cited only once in the audit's grounding sources is weak
# evidence for any host-targeted action — a single citation could be
# noise (one tangential mention) rather than a real capture pattern.
# The PR-8 quality review flagged a `creator_partnerships: youtube.com`
# action that fired off 2 cites (defensible) but noted that at 1 cite
# the same action reads as flimsy. The threshold gates host-targeted
# playbook actions on at least this many citations. Callers can lower
# it to 1 to restore the prior all-hosts behavior.
_DEFAULT_MIN_TIMES_CITED = 2


def _content_revision_playbooks() -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    for pid, pb in _load_playbooks().items():
        if (pb.get("lever") or "") != "content_revision":
            continue
        triggers = pb.get("triggers") or {}
        if triggers.get("failing_dimension") and triggers.get("failing_bucket"):
            out.append((pid, pb))
    return out


def _score_for_dimension(per_sku_report: Dict[str, Any], dimension: str) -> Optional[int]:
    score = ((per_sku_report.get("scores") or {}).get(dimension) or {}).get("score")
    try:
        return int(score) if score is not None else None
    except (TypeError, ValueError):
        return None


def _content_gap_candidates(
    per_sku_report: Dict[str, Any],
    *,
    vertical: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return failing-dimension/bucket candidates for content_revision.

    Exact `primary_gaps` from the report win. We also map the scorecard
    bucket vocabulary to playbook trigger vocabulary so the scorer can
    stay faithful to spec A while the playbooks stay phrased around
    concrete content modules merchants can understand.
    """
    candidates: List[Dict[str, Any]] = []
    seen = set()

    def _add(dimension: str, bucket: str, reason: Optional[str] = None) -> None:
        key = (dimension, bucket)
        if key in seen:
            return
        seen.add(key)
        candidates.append({
            "dimension": dimension,
            "bucket": bucket,
            "reason": reason,
        })

    # Blocked / un-indexed SKUs: the #1 step is getting indexed. Emit this
    # gateway candidate first and unconditionally so a blocked SKU never
    # dead-ends without a next step. A blocked SKU has no probe evidence, so
    # this candidate is also exempt from the evidence-run-id gate downstream.
    if str(per_sku_report.get("band") or "") == "blocked":
        _add(
            "routability",
            "blocked_sku_gateway",
            "This product isn't indexed yet, so AI can't find it.",
        )

    for gap in per_sku_report.get("primary_gaps") or []:
        dimension = gap.get("dimension")
        bucket = gap.get("bucket")
        if dimension and bucket:
            # `why` is the merchant-safe gap copy (raw `reason` was internal
            # scoring vocabulary and is no longer emitted in primary_gaps).
            _add(str(dimension), str(bucket), gap.get("why"))

    content_score = _score_for_dimension(per_sku_report, "content_richness")
    if content_score is None or content_score >= 70:
        return candidates

    prompt_axes = {
        (p.get("axis") or "").strip()
        for p in per_sku_report.get("failing_prompts") or []
        if isinstance(p, dict)
    }
    _add("content_richness", "answer_shaped_modules", "content score below ready threshold")
    if "comparison" in prompt_axes:
        _add("content_richness", "comparison_block", "comparison prompts failed")
    if prompt_axes & {"intent", "persona", "concern", "use_case"}:
        _add("content_richness", "intent_header", "intent-shaped prompts failed")

    breakdown = ((per_sku_report.get("scores") or {}).get("content_richness") or {}).get("breakdown") or {}
    # The ingredient/dose playbook's copy is formulation-specific (INCI,
    # ingredient modules) — the vertical_structure/safety_claims buckets it
    # keys on are generic (specs and safety claims fail for electronics too),
    # so gate on an explicitly-resolved beauty vertical or a drone SKU gets
    # INCI advice (founder scan 2026-07-22). Default-closed: unknown vertical
    # skips it rather than risking formulation copy on the wrong category.
    if str(vertical or "").strip().lower() == "beauty" and any(
        isinstance(detail, dict)
        and int(detail.get("points") or 0) < int(detail.get("max") or 0)
        for name, detail in breakdown.items()
        if name in {"vertical_structure", "safety_claims"}
    ):
        _add("content_richness", "ingredient_dose_module", "vertical structure or claims evidence is incomplete")
    if any(
        isinstance(detail, dict)
        and int(detail.get("points") or 0) < int(detail.get("max") or 0)
        for name, detail in breakdown.items()
        if name in {"freshness_raw_pdp", "model_readiness", "enrichment_coverage"}
    ):
        _add("content_richness", "schema_markup", "structured answer/readiness signals are incomplete")
    return candidates


def _authority_by_sku(authority_map: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for entry in (authority_map or {}).get("skus") or []:
        if isinstance(entry, dict) and entry.get("sku_key"):
            out[str(entry["sku_key"])] = entry
    return out


def _sku_title(sku_ctx: Dict[str, Any], per_sku_report: Dict[str, Any]) -> str:
    product = sku_ctx.get("product") if isinstance(sku_ctx.get("product"), dict) else {}
    sku = sku_ctx.get("sku") if isinstance(sku_ctx.get("sku"), dict) else {}
    return (
        per_sku_report.get("sku_title")
        or sku.get("title")
        or product.get("title")
        or per_sku_report.get("product_key")
        or per_sku_report.get("sku_key")
        or "this SKU"
    )


def _failing_prompt_rows(per_sku_report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = [
        row for row in per_sku_report.get("failing_prompts") or []
        if isinstance(row, dict) and (row.get("query") or "").strip()
    ]
    if rows:
        return rows
    # Fallback for tests or older reports: derive a prompt-ish row
    # from grounding evidence, but only if it carries a run id.
    out: List[Dict[str, Any]] = []
    for ev in per_sku_report.get("verbatim_grounding_evidence") or []:
        if not isinstance(ev, dict) or not ev.get("probe_run_id"):
            continue
        q = (ev.get("query") or "").strip()
        if q:
            out.append({
                "query": q,
                "evidence_run_id": ev.get("probe_run_id"),
                "grounding_sources": ev.get("grounding_sources") or [],
            })
    return out


def _evidence_run_ids(per_sku_report: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for row in _failing_prompt_rows(per_sku_report):
        rid = row.get("evidence_run_id") or row.get("probe_run_id")
        if rid and str(rid) not in ids:
            ids.append(str(rid))
    for ev in per_sku_report.get("verbatim_grounding_evidence") or []:
        if not isinstance(ev, dict):
            continue
        rid = ev.get("probe_run_id") or ev.get("evidence_run_id")
        if rid and str(rid) not in ids:
            ids.append(str(rid))
    for rid in per_sku_report.get("evidence_run_ids") or []:
        if rid and str(rid) not in ids:
            ids.append(str(rid))
    return ids


def _competitor_evidence(per_sku_report: Dict[str, Any]) -> str:
    authority_hosts = per_sku_report.get("authority_hosts") or []
    phrases: List[str] = []
    for host in authority_hosts:
        if not isinstance(host, dict):
            continue
        competitors = [
            c for c in host.get("competitors_named") or []
            if isinstance(c, str) and c.strip()
        ]
        if competitors:
            phrases.append(f"{host.get('host')} cited {', '.join(competitors[:3])}")
        elif host.get("host"):
            phrases.append(f"{host.get('host')} was cited")
        if len(phrases) >= 3:
            break
    if phrases:
        return "; ".join(phrases)

    competitors_seen: List[str] = []
    for row in _failing_prompt_rows(per_sku_report):
        for name in row.get("competitors_named") or []:
            if isinstance(name, str) and name.strip() and name not in competitors_seen:
                competitors_seen.append(name)
    return ", ".join(competitors_seen[:5]) if competitors_seen else "no competitor evidence captured"


def _priority_for_content_action(per_sku_report: Dict[str, Any], bucket: str) -> float:
    for item in per_sku_report.get("priority_queue") or []:
        if item.get("bucket") == bucket:
            try:
                return float(item.get("priority_score") or 0)
            except (TypeError, ValueError):
                return 0.0
    content_score = _score_for_dimension(per_sku_report, "content_richness")
    impact = per_sku_report.get("impact_proxy") or 1.0
    try:
        return float(impact) * max(0, 100 - int(content_score or 0))
    except (TypeError, ValueError):
        return 0.0


def render_content_revision_action(
    playbook: Dict[str, Any],
    sku_ctx: Dict[str, Any],
    per_sku_report: Dict[str, Any],
) -> Dict[str, Any]:
    """Render a merchant-safe content_revision action with evidence ids."""
    template = playbook.get("template") or {}
    failing_rows = _failing_prompt_rows(per_sku_report)
    prompt_texts = [row.get("query") for row in failing_rows if row.get("query")]
    evidence_run_ids = _evidence_run_ids(per_sku_report)
    ctx = {
        "sku_title": _sku_title(sku_ctx or {}, per_sku_report or {}),
        "sku_key": per_sku_report.get("sku_key") or (sku_ctx or {}).get("sku_key") or "",
        "top_failing_prompts": "; ".join(prompt_texts[:3]),
        "failing_prompt_list": "; ".join(prompt_texts[:8]),
        "competitor_evidence": _competitor_evidence(per_sku_report),
    }
    title = _render_template(template.get("title") or playbook.get("title_template") or "", ctx)
    what = _render_template(template.get("what") or playbook.get("body_template") or "", ctx)
    evidence = _render_template(template.get("evidence") or "", ctx)
    concrete_next_step = _render_template(
        template.get("concrete_next_step") or playbook.get("concrete_next_step") or "",
        ctx,
    )
    body = what
    if evidence:
        body = f"{body} Evidence: {evidence}" if body else evidence
    return {
        "severity": playbook.get("severity") or "high",
        "title": title,
        "body": body,
        "concrete_next_step": concrete_next_step or None,
        "evidence": {
            "sku_key": per_sku_report.get("sku_key"),
            "failing_prompts": prompt_texts[:8],
            "competitor_evidence": ctx["competitor_evidence"],
        },
        "playbook_step_id": playbook.get("playbook_id") or playbook.get("_playbook_id"),
        "target_sku_key": per_sku_report.get("sku_key"),
        "lever": "content_revision",
        "owner": playbook.get("owner") or "pivota",
        "expected_timeline_days": playbook.get("estimated_days") or [3, 7],
        "evidence_run_ids": evidence_run_ids,
    }


def _select_content_revision_actions(
    *,
    per_sku_reports: Optional[List[Dict[str, Any]]],
    authority_map: Optional[Dict[str, Any]],
    sku_contexts_by_sku: Optional[Dict[str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    if not per_sku_reports:
        return []
    playbooks = _content_revision_playbooks()
    if not playbooks:
        return []
    authority_lookup = _authority_by_sku(authority_map)
    actions: List[Dict[str, Any]] = []
    for report in per_sku_reports:
        if not isinstance(report, dict):
            continue
        sku_key = str(report.get("sku_key") or "")
        enriched_report = dict(report)
        if sku_key in authority_lookup:
            enriched_report["authority_hosts"] = authority_lookup[sku_key].get("authority_hosts") or []
        sku_ctx = (sku_contexts_by_sku or {}).get(sku_key, {})
        # NOTE for future wiring: the ingredient gate below is default-closed —
        # a caller that passes per_sku_reports but omits sku_contexts_by_sku
        # (or builds contexts without a vertical) silently drops the ingredient
        # playbook for beauty merchants. Read the canonical "vertical" with a
        # resolved_vertical fallback so alternate context shapes still resolve.
        candidates = _content_gap_candidates(
            enriched_report,
            vertical=str(
                (sku_ctx or {}).get("vertical")
                or (sku_ctx or {}).get("resolved_vertical")
                or ""
            ).strip().lower() or None,
        )
        for dimension, bucket, reason in (
            (c.get("dimension"), c.get("bucket"), c.get("reason"))
            for c in candidates
        ):
            for pid, pb in playbooks:
                triggers = pb.get("triggers") or {}
                if (
                    triggers.get("failing_dimension") != dimension
                    or triggers.get("failing_bucket") != bucket
                ):
                    continue
                render_pb = dict(pb)
                render_pb["_playbook_id"] = pid
                render_pb.setdefault("playbook_id", pid)
                action = render_content_revision_action(render_pb, sku_ctx, enriched_report)
                # Hard lesson: no merchant-facing content-revision
                # recommendation without a traceable probe evidence id —
                # EXCEPT the get-indexed gateway, which fires precisely
                # because the SKU couldn't be probed (it isn't indexed yet).
                is_blocked_gateway = bucket == "blocked_sku_gateway"
                if not action.get("evidence_run_ids") and not is_blocked_gateway:
                    continue
                action["failing_dimension"] = dimension
                action["failing_bucket"] = bucket
                if is_blocked_gateway:
                    # Always the merchant's first step — rank above every
                    # other content action. Finite sentinel (JSONB-safe).
                    action["priority_score"] = _BLOCKED_GATEWAY_PRIORITY
                else:
                    action["priority_score"] = _priority_for_content_action(enriched_report, str(bucket))
                if reason:
                    action["evidence"]["gap_reason"] = reason
                actions.append(action)
    actions.sort(key=lambda a: a.get("priority_score") or 0, reverse=True)
    return actions


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
    per_sku_reports: Optional[List[Dict[str, Any]]] = None,
    authority_map: Optional[Dict[str, Any]] = None,
    sku_contexts_by_sku: Optional[Dict[str, Dict[str, Any]]] = None,
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

    When `per_sku_reports` is supplied, also emits `content_revision`
    actions keyed by failing dimension + bucket. Those actions are
    sorted by their SKU priority score and always carry
    `evidence_run_ids`.
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

        # Suppress levers whose actions have no real data / contact path
        # behind them (e.g. creator_partnership — see _SUPPRESSED_LEVERS).
        # These never reach the merchant `actions` array.
        if (pb.get("lever") or "") in _SUPPRESSED_LEVERS:
            continue

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

    # Sort host actions: severity ascending (critical first) then
    # times_cited descending. Per-SKU content_revision actions are
    # appended after the cap below (new per-SKU audit callers only).

    actions.sort(
        key=lambda a: (
            _SEVERITY_RANK.get((a.get("severity") or "low"), 99),
            -(a.get("evidence", {}) or {}).get("times_cited", 0),
        )
    )
    content_actions = _select_content_revision_actions(
        per_sku_reports=per_sku_reports,
        authority_map=authority_map,
        sku_contexts_by_sku=sku_contexts_by_sku,
    )
    return actions[:cap] + content_actions[:cap]


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

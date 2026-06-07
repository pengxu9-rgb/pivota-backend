"""Polished markdown renderer v2 (PR-9a).

Consumes the full enriched audit payload (executive_summary,
evidence_quotes, action_items v2 with owner/KPI/outcome,
implementation_roadmap, pivota_commitments, cited_hosts_detailed
with tier/cadence, industry_context with depth, cohort_form_factor)
and emits markdown matching the polished hand-written Grüns PDF
structure.

**Section ordering** (intentionally identical to the polished PDF):

  1. Title + meta
  2. Executive Summary (paradox framing) — PR-8a
  3. Headline Metrics table — verdict_label_display + scores
  4. Strategic Context (3 trends) — industry_context depth
  5. Detailed Findings (with quote boxes) — evidence_quotes
  6. Competitive Analysis — cohort_form_factor + competitor_brands
  7. Editorial Publisher Analysis — cited_hosts_detailed (tier+cadence)
  8. Strategic Implications — derived from executive_summary
  9. Recommendations — action_items v2 with owner/KPI/outcome
 10. Implementation Roadmap — phases from PR-8c
 11. Pivota Commitments — deliver / don't promise from PR-8d
 12. Methodology
 13. Appendix: raw citation data

**Feature flag.** The cold-start audit export route honors env var
`AUDIT_RENDERER_VERSION`:
  - `v1` (default) — existing `render_brand_markdown` in
    agent_center_bd_report_service.py
  - `v2` — this module

Feature flag mitigates blast radius for the renderer change. Once
v2 has soaked in production, default flips to v2.

**Defensive against missing fields.** The renderer uses
`.get(...)` for every payload lookup so an audit response that
predates a particular PR (e.g. an audit run before PR-7e shipped)
renders cleanly without the missing section, no crashes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Owner display labels — convert internal enum to merchant-facing text
# ---------------------------------------------------------------------------

_OWNER_DISPLAY: Dict[str, str] = {
    "pivota_ops": "Pivota Ops",
    "merchant_brand_team": "Merchant brand team",
    "merchant_growth_team": "Merchant growth team",
    "merchant_tech_team": "Merchant tech team",
    "joint": "Joint (Pivota + merchant)",
}


_PHASE_DISPLAY: Dict[str, str] = {
    "week_1_to_4": "Weeks 1-4",
    "week_4_to_12": "Weeks 4-12",
    "week_12_to_24": "Weeks 12-24",
}


_TIER_DISPLAY: Dict[int, str] = {
    1: "Tier 1",
    2: "Tier 2",
    3: "Tier 3",
}


_CADENCE_DISPLAY: Dict[str, str] = {
    "continuous": "Continuous",
    "quarterly": "Quarterly refresh",
    "biannual": "Biannual",
    "annual": "Annual",
    "oneoff": "One-off",
}


_GROUNDING_WEIGHT_DISPLAY: Dict[str, str] = {
    "high": "High AI-grounding influence",
    "medium": "Medium",
    "low": "Low",
}


def _section_title(text: str, level: int = 2) -> str:
    return f"\n{'#' * level} {text}\n\n"


def _render_executive_summary(es: Optional[Dict[str, Any]]) -> str:
    if not es or not es.get("opening_paragraphs"):
        return ""
    out: List[str] = [_section_title("Executive Summary")]
    for paragraph in es["opening_paragraphs"]:
        out.append(paragraph + "\n\n")
    pill = (es.get("verdict_pill_text") or "").strip()
    if pill:
        out.append(f"**Verdict:** {pill}\n\n")
    return "".join(out)


def _render_headline_metrics(report: Dict[str, Any]) -> str:
    verdict = report.get("verdict") or {}
    headline = verdict.get("label_display") or verdict.get("label") or "(unknown)"
    out: List[str] = [_section_title("Headline Metrics")]
    out.append(f"**Verdict:** {headline}\n\n")
    explanation = (verdict.get("explanation") or "").strip()
    if explanation:
        out.append(explanation + "\n\n")
    rows = [
        "| Dimension | Score | Notes |",
        "|---|---|---|",
    ]
    vis = verdict.get("visibility_score")
    attr = verdict.get("attribution_score")
    cat = verdict.get("category_visibility_score")
    rows.append(
        f"| Named-product visibility (Layer 1) | {_score_cell(vis)} | "
        f"Direct named-product queries |"
    )
    rows.append(
        f"| First-party attribution (Layer 1) | {_score_cell(attr)} | "
        f"Merchant URL cited in grounding |"
    )
    rows.append(
        f"| Category discoverability | {_score_cell(cat)} | "
        f"Brand surfaces in category-open queries |"
    )
    out.append("\n".join(rows) + "\n")
    return "".join(out)


def _score_cell(score: Optional[int]) -> str:
    if score is None:
        return "_(not measured)_"
    return f"**{score}/100**"


def _render_strategic_context(industry: Optional[Dict[str, Any]]) -> str:
    if not industry:
        return ""
    out: List[str] = [_section_title("Strategic Context")]
    blurb = (industry.get("blurb") or "").strip()
    if blurb:
        out.append(blurb + "\n\n")
    market_size = industry.get("market_size_billions_usd")
    market_year = industry.get("market_size_year")
    horizon = industry.get("growth_horizon_years")
    if market_size:
        size_line = f"**Category market size:** ~${market_size}B"
        if market_year:
            size_line += f" ({market_year})"
        if horizon:
            size_line += f" · projection horizon {horizon}"
        out.append(size_line + "\n\n")
    sub_trends = industry.get("sub_category_trends") or []
    if sub_trends:
        out.append("**Sub-category trends:**\n\n")
        for trend in sub_trends[:5]:
            sub = trend.get("sub", "")
            growth = trend.get("growth_pct")
            why = (trend.get("why") or "").strip()
            line = f"- **{sub}**"
            if growth:
                line += f" — ~{growth}% YoY"
            if why:
                line += f". {why}"
            out.append(line + "\n")
        out.append("\n")
    comparison = (industry.get("comparison_to_other_verticals") or "").strip()
    if comparison:
        out.append(f"**Vertical positioning:** {comparison}\n\n")
    return "".join(out)


def _render_evidence_quotes(quotes: Optional[List[Dict[str, Any]]]) -> str:
    if not quotes:
        return ""
    out: List[str] = [_section_title("Evidence Quotes")]
    out.append(
        "Verbatim text from AI grounded answers that named the brand "
        "directly, with editorial-grade corroboration:\n\n"
    )
    for q in quotes:
        excerpt = (q.get("excerpt_text") or "").strip()
        if not excerpt:
            continue
        sources = q.get("source_labels") or []
        query = (q.get("query") or "").strip()
        out.append(f"> {excerpt}\n>\n")
        meta_parts: List[str] = []
        if sources:
            meta_parts.append(f"_via {', '.join(sources)}_")
        if query:
            meta_parts.append(f'_query: "{query}"_')
        if meta_parts:
            out.append(f"> {' · '.join(meta_parts)}\n\n")
        else:
            out.append("\n")
    return "".join(out)


def _render_competitive_analysis(report: Dict[str, Any]) -> str:
    cohort_ff = report.get("cohort_form_factor") or {}
    cv = report.get("category_visibility") or {}
    competitor_brands = (cv.get("competitor_brands") or [])[:15]
    if not (competitor_brands or cohort_ff.get("form_factor_summary")):
        return ""
    out: List[str] = [_section_title("Competitive Analysis")]
    # Form-factor summary
    summary = cohort_ff.get("form_factor_summary") or {}
    if summary:
        merchant_ff = cohort_ff.get("merchant_form_factor")
        unique = cohort_ff.get("merchant_owns_unique_form_factor")
        if merchant_ff and unique:
            out.append(
                f"**Form-factor positioning:** the audited brand is the "
                f"only one in the cohort with form factor `{merchant_ff}` — "
                f"a structural moat in editorial subcategories.\n\n"
            )
        elif merchant_ff:
            shared = cohort_ff.get("competitors_in_merchant_form_factor") or []
            if shared:
                out.append(
                    f"**Form-factor positioning:** the audited brand "
                    f"shares form factor `{merchant_ff}` with: "
                    f"{', '.join(shared[:5])}.\n\n"
                )
        # Form-factor breakdown table
        if any(brands for brands in summary.values()):
            out.append("**Cohort form-factor breakdown:**\n\n")
            rows = ["| Form factor | Brands |", "|---|---|"]
            for ff, brands in summary.items():
                if brands:
                    rows.append(f"| `{ff}` | {', '.join(brands[:8])} |")
            out.append("\n".join(rows) + "\n\n")
    # Competitor brands list
    if competitor_brands:
        out.append("**Top competitor brands surfaced (by AI mention share):**\n\n")
        rows = ["| Brand | Times mentioned |", "|---|---|"]
        for entry in competitor_brands[:15]:
            name = entry.get("name", "?")
            times = entry.get("times_cited", 0)
            rows.append(f"| {name} | {times} |")
        out.append("\n".join(rows) + "\n\n")
    return "".join(out)


def _render_publisher_analysis(report: Dict[str, Any]) -> str:
    mv = report.get("merchant_view") or {}
    receipts = mv.get("receipts") or {}
    cited = receipts.get("cited_hosts_detailed") or []
    if not cited:
        return ""
    # Filter to editorial publishers only — retailer/marketplace
    # don't fit the "outreach to editor" narrative. Renderer can
    # show those separately if needed.
    editorial = [c for c in cited if (c.get("type") == "editorial")]
    if not editorial:
        return ""
    out: List[str] = [_section_title("Editorial Publisher Analysis")]
    out.append(
        "Publishers cited by AI grounded retrieval for this brand's "
        "category — ranked by frequency. Tier reflects authority, "
        "cadence reflects editorial refresh frequency, AI-grounding "
        "weight reflects observed citation frequency in our audit "
        "history.\n\n"
    )
    rows = [
        "| Publisher | Cites | Tier | Cadence | Outreach cycle | "
        "Pitch path |",
        "|---|---|---|---|---|---|",
    ]
    for p in editorial[:10]:
        host = p.get("host", "?")
        cites = p.get("times_cited", 0)
        tier = p.get("tier")
        tier_str = _TIER_DISPLAY.get(tier or 0, "—")
        cadence = p.get("editorial_cadence")
        cadence_str = _CADENCE_DISPLAY.get(cadence or "", "—")
        cycle = p.get("expected_outreach_cycle_weeks")
        cycle_str = (
            f"{cycle[0]}-{cycle[1]} weeks"
            if isinstance(cycle, list) and len(cycle) == 2
            else "—"
        )
        # Pitch path: prefer email; fall back to submission URL note
        pitch = p.get("pitch_recipient") or {}
        pitch_str = "—"
        if isinstance(pitch, dict):
            email = pitch.get("email")
            if email:
                pitch_str = f"`{email}`"
            elif pitch.get("submission_url"):
                pitch_str = "submission form"
        rows.append(
            f"| `{host}` | {cites} | {tier_str} | {cadence_str} | "
            f"{cycle_str} | {pitch_str} |"
        )
    out.append("\n".join(rows) + "\n\n")
    return "".join(out)


def _render_recommendations(action_items: Optional[List[Dict[str, Any]]]) -> str:
    if not action_items:
        return ""
    out: List[str] = [_section_title("Recommendations")]
    out.append(
        "Prioritized actions with owner, expected outcome, KPI to "
        "track, and time-window phase.\n\n"
    )
    for action in action_items:
        title = (action.get("title") or "").strip()
        if not title:
            continue
        priority = action.get("priority_order")
        severity = (action.get("severity") or "").upper()
        owner = _OWNER_DISPLAY.get(action.get("owner", ""), "—")
        phase = _PHASE_DISPLAY.get(action.get("phase", ""), "—")
        body = (action.get("body") or "").strip()
        kpi = (action.get("kpi_to_track") or "").strip()
        outcome = (action.get("expected_outcome") or "").strip()
        next_step = (action.get("concrete_next_step") or "").strip()
        timeline = action.get("expected_timeline_weeks")
        out.append(
            f"### {priority}. {title}\n\n"
            f"**Severity:** {severity} · **Owner:** {owner} · "
            f"**Phase:** {phase}"
        )
        if isinstance(timeline, list) and len(timeline) == 2:
            out.append(f" · **Timeline:** {timeline[0]}-{timeline[1]} weeks")
        out.append("\n\n")
        if body:
            out.append(body + "\n\n")
        if next_step:
            out.append(f"**This week:** {next_step}\n\n")
        if outcome:
            out.append(f"**Expected outcome:** {outcome}\n\n")
        if kpi:
            out.append(f"**KPI to track:** {kpi}\n\n")
        # Pitch draft (when present from playbook engine)
        pitch_draft = action.get("pitch_draft")
        if isinstance(pitch_draft, dict) and pitch_draft.get("body"):
            recipient = pitch_draft.get("recipient_email") or ""
            subject = pitch_draft.get("subject") or ""
            out.append(f"**Suggested outreach** (recipient: `{recipient}`):\n\n")
            if subject:
                out.append(f"> **Subject:** {subject}\n>\n")
            for line in pitch_draft["body"].split("\n"):
                out.append(f"> {line}\n")
            out.append("\n")
    return "".join(out)


def _chip_values(items: Any, *, key: str, limit: int = 4) -> List[str]:
    values: List[str] = []
    if not isinstance(items, list):
        return values
    for item in items:
        value = ""
        if isinstance(item, dict):
            value = str(item.get(key) or "").strip()
        else:
            value = str(item or "").strip()
        if value and value not in values:
            values.append(value)
        if len(values) >= limit:
            break
    return values


def _render_next_best_action(next_best_action: Optional[Dict[str, Any]]) -> str:
    if not isinstance(next_best_action, dict):
        return ""
    headline = str(next_best_action.get("headline") or "").strip()
    why = str(next_best_action.get("why_this_first") or "").strip()
    first_move = str(next_best_action.get("first_move") or "").strip()
    evidence_summary = str(next_best_action.get("evidence_summary") or "").strip()
    evidence_chips = [
        str(chip).strip()
        for chip in (next_best_action.get("evidence_chips") or [])
        if str(chip).strip()
    ][:4]
    self_serve = [
        str(action).strip()
        for action in (
            next_best_action.get("self_serve")
            or next_best_action.get("self_serve_actions")
            or []
        )
        if str(action).strip()
    ][:2]
    pivota_items = [
        str(action).strip()
        for action in (
            next_best_action.get("pivota_assisted")
            or [next_best_action.get("pivota_path")]
        )
        if str(action).strip()
    ][:1]
    if not any([headline, why, first_move, self_serve, pivota_items]):
        return ""

    evidence = (
        next_best_action.get("evidence")
        if isinstance(next_best_action.get("evidence"), dict)
        else next_best_action.get("evidence_used")
    )
    evidence = evidence if isinstance(evidence, dict) else {}
    hosts = _chip_values(
        (
            evidence.get("retailer_hosts")
            or evidence.get("source_hosts")
            or evidence.get("cited_hosts")
        ),
        key="host",
    )
    competitors = _chip_values(evidence.get("competitors_named"), key="name")
    queries = _chip_values(evidence.get("failed_query_examples"), key="query", limit=3)
    secondary_moves = [
        move for move in (next_best_action.get("secondary_moves") or [])
        if isinstance(move, dict) and str(move.get("title") or "").strip()
    ][:2]
    tracking_metrics = [
        str(metric).strip()
        for metric in (
            next_best_action.get("tracking_metrics")
            or next_best_action.get("how_to_track")
            or []
        )
        if str(metric).strip()
    ][:3]

    out: List[str] = [_section_title("What Should You Do Next?")]
    if headline:
        out.append(f"**{headline}**\n\n")
    if why:
        out.append(why + "\n\n")
    if first_move:
        out.append(f"**First move:** {first_move}\n\n")
    if evidence_summary:
        out.append(f"**Why this is the leak:** {evidence_summary}\n\n")
    if evidence_chips:
        out.append("**Gap read:** " + " · ".join(evidence_chips) + "\n\n")
    evidence_bits: List[str] = []
    if queries:
        evidence_bits.append("queries: " + ", ".join(f"`{q}`" for q in queries))
    if hosts:
        evidence_bits.append("cited hosts: " + ", ".join(f"`{h}`" for h in hosts))
    if competitors:
        evidence_bits.append("competitors: " + ", ".join(competitors))
    if evidence_bits:
        out.append("**Evidence:** " + " · ".join(evidence_bits) + "\n\n")
    if self_serve:
        out.append("**Do yourself this week:**\n\n")
        for action in self_serve:
            out.append(f"- {action}\n")
        out.append("\n")
    if pivota_items:
        out.append("**Use Pivota for:**\n\n")
        for action in pivota_items:
            out.append(f"- {action}\n")
        out.append("\n")
    if secondary_moves:
        out.append("**Secondary moves:**\n\n")
        for move in secondary_moves:
            reason = str(move.get("reason") or "").strip()
            out.append(f"- {move['title']}")
            if reason:
                out.append(f" — {reason}")
            out.append("\n")
        out.append("\n")
    if tracking_metrics:
        out.append("**How to track:**\n\n")
        for metric in tracking_metrics:
            out.append(f"- {metric}\n")
        out.append("\n")
    cta = next_best_action.get("cta")
    if isinstance(cta, dict):
        label = str(cta.get("label") or "").strip()
        trust_note = str(cta.get("trust_note") or "").strip()
        if label or trust_note:
            out.append("**CTA:** ")
            if label:
                out.append(label)
            if trust_note:
                out.append(f" — {trust_note}")
            out.append("\n\n")
    return "".join(out)


def _render_reaudit_delta(delta: Optional[Dict[str, Any]]) -> str:
    if not isinstance(delta, dict):
        return ""
    headline = str(delta.get("headline") or "").strip()
    if not headline:
        return ""
    out: List[str] = [_section_title("Since your last audit")]
    out.append(headline + "\n\n")
    material = [
        movement for movement in (delta.get("movements") or [])
        if isinstance(movement, dict) and movement.get("is_material")
    ]
    if material:
        out.append("**Material movement:**\n\n")
        for movement in material:
            label = str(movement.get("label") or movement.get("signal") or "").strip()
            direction = str(movement.get("direction") or "changed").strip()
            out.append(
                f"- {label}: {movement.get('from')} → {movement.get('to')} ({direction})\n"
            )
        out.append("\n")
    tracked = [
        row for row in (delta.get("tracked_metric_results") or [])
        if isinstance(row, dict) and str(row.get("metric") or "").strip()
    ]
    if tracked:
        out.append("**Tracking read:**\n\n")
        for row in tracked:
            metric = str(row.get("metric") or "").strip()
            status = str(row.get("status") or "").strip() or "not_measurable"
            note = str(row.get("note") or "").strip()
            suffix = f" — {note}" if note else ""
            out.append(f"- {metric}: {status}{suffix}\n")
        out.append("\n")
    return "".join(out)


def _render_owned_buyer_path_play(next_best_action: Optional[Dict[str, Any]]) -> str:
    if not isinstance(next_best_action, dict):
        return ""
    play = next_best_action.get("canonical_page_play")
    if not isinstance(play, dict):
        return ""
    lane = (play.get("lane") or "").strip()
    moves = [
        move for move in (play.get("moves") or [])
        if isinstance(move, dict) and (move.get("operator_action") or "").strip()
    ]
    if not lane and not moves:
        return ""

    strategy = (
        play.get("controller_strategy_label")
        or play.get("controller_strategy")
        or "Buyer-path repair"
    )
    controllers = [
        str(controller).strip()
        for controller in (play.get("controllers") or [])
        if str(controller).strip()
    ][:3]
    profile = play.get("controller_profile") if isinstance(play.get("controller_profile"), dict) else {}
    focus = (profile.get("operator_focus") or "").strip()
    exposure_read = (
        str(play.get("exposure_read") or profile.get("exposure_read") or "").strip()
    )
    out: List[str] = [_section_title("Owned Buyer Path Play")]
    out.append(f"**Strategy:** {strategy}\n\n")
    if lane:
        out.append(f"**Lane to win back:** `{lane}`\n\n")
    if controllers:
        out.append(f"**Controllers evidenced:** {', '.join(f'`{h}`' for h in controllers)}\n\n")
    if focus:
        out.append(f"**Operator read:** {focus}\n\n")
    if exposure_read:
        out.append(f"**Exposure read:** {exposure_read}\n\n")
    wedge = next_best_action.get("sideways_wedge")
    if isinstance(wedge, dict):
        beachhead = wedge.get("recommended_beachhead_lane")
        beachhead_query = (
            str(beachhead.get("query") or "").strip()
            if isinstance(beachhead, dict) else ""
        )
        why_wedge = str(wedge.get("why_this_lane_not_the_head_prompt") or "").strip()
        do_not = [
            item for item in (wedge.get("do_not_chase_yet") or [])
            if isinstance(item, dict) and str(item.get("query") or "").strip()
        ][:3]
        if beachhead_query or why_wedge or do_not:
            out.append("**Sideways demand wedge:**\n\n")
            if beachhead_query:
                out.append(f"- Beachhead lane: `{beachhead_query}`\n")
            if why_wedge:
                out.append(f"- Why this first: {why_wedge}\n")
            if do_not:
                deferred = ", ".join(
                    f"`{str(item.get('query') or '').strip()}`"
                    for item in do_not
                )
                out.append(f"- Do not chase yet: {deferred}\n")
            out.append("\n")
    if moves:
        out.append("**Operator checklist:**\n\n")
        for idx, move in enumerate(moves[:5], start=1):
            action = (move.get("operator_action") or "").strip()
            why = (move.get("why") or "").strip()
            move_type = (move.get("type") or f"move_{idx}").replace("_", " ")
            out.append(f"{idx}. **{move_type.title()}** — {action}\n")
            if why:
                out.append(f"   - Why: {why}\n")
        out.append("\n")
    checkout = (play.get("checkout_readiness") or "").strip()
    if checkout:
        out.append(f"**Agent-checkout readiness:** {checkout}\n\n")
    economics = (play.get("economics_policy") or "").strip()
    if economics:
        out.append(f"**Economics guard:** {economics}\n\n")
    return "".join(out)


def _render_implementation_roadmap(roadmap: Optional[Dict[str, Any]]) -> str:
    if not roadmap:
        return ""
    phases = roadmap.get("phases") or []
    if not phases:
        return ""
    out: List[str] = [_section_title("Implementation Roadmap")]
    rows = [
        "| Phase | Weeks | Owners | Activities | Expected outcome |",
        "|---|---|---|---|---|",
    ]
    for phase in phases:
        label = phase.get("label", "")
        weeks = phase.get("weeks", "")
        owners = phase.get("owners") or []
        owners_str = ", ".join(
            _OWNER_DISPLAY.get(o, o) for o in owners
        ) if owners else "—"
        activities = phase.get("activities") or []
        activity_count = phase.get("activity_count", len(activities))
        outcome = (phase.get("expected_outcome") or "").strip()
        # Truncate outcome for table cell
        if len(outcome) > 200:
            outcome = outcome[:197].rstrip() + "..."
        rows.append(
            f"| **{label}** | {weeks} | {owners_str} | "
            f"{activity_count} activities | {outcome} |"
        )
    out.append("\n".join(rows) + "\n\n")
    return "".join(out)


def _render_pivota_commitments(commitments: Optional[Dict[str, Any]]) -> str:
    if not commitments:
        return ""
    out: List[str] = [_section_title("Pivota's Commitment")]
    summary = (commitments.get("platform_capability_summary") or "").strip()
    if summary:
        out.append(f"**Platform capability:** {summary}\n\n")
    delivers_1_4 = commitments.get("delivers_weeks_1_to_4") or []
    if delivers_1_4:
        out.append("### Weeks 1-4: Onboarding execution\n\n")
        for item in delivers_1_4:
            out.append(f"- {item}\n")
        out.append("\n")
    delivers_continuous = commitments.get("delivers_continuous") or []
    if delivers_continuous:
        out.append("### Continuous operations\n\n")
        for item in delivers_continuous:
            out.append(f"- {item}\n")
        out.append("\n")
    does_not = commitments.get("does_not_promise") or []
    if does_not:
        out.append("### What Pivota does not promise\n\n")
        for item in does_not:
            out.append(f"- {item}\n")
        out.append("\n")
    return "".join(out)


def _render_methodology(report: Dict[str, Any]) -> str:
    out: List[str] = [_section_title("Methodology")]
    upstream = report.get("upstream_status") or {}
    requested = upstream.get("requested_provider", "?")
    actual = upstream.get("visibility_provider", "?")
    out.append(
        f"**Probe source.** Audit issued via `{requested}` provider; "
        f"upstream resolved to `{actual}`. Each probe parses the "
        f"structured grounded response including explicit grounding "
        f"sources (URIs and titles) the LLM cited.\n\n"
    )
    out.append(
        "**Probe modes.** Three modes mirror the standard audit "
        "structure:\n\n"
        "- *Open product visibility test* — buyer-style queries to "
        "measure named-product visibility\n"
        "- *Merchant store attribution test* — same queries scored "
        "against whether the merchant URL appears in grounding "
        "chunks\n"
        "- *Category visibility test* — category-open queries that "
        "do not name the brand, to measure organic discoverability\n\n"
    )
    out.append(
        "**Scoring.** Category visibility credits a query when ANY of: "
        "(a) merchant URL in grounding chunks, (b) merchant brand in "
        "a grounded source title, OR (c) brand in evidence excerpt + "
        "LLM self-report + at least one grounding source — the "
        "editorial-citation path. Excerpt-only LLM paraphrases (no "
        "self-report or no grounding) do NOT credit, defending against "
        "hallucinated brand mentions.\n\n"
    )
    out.append(
        "**Probe failures.** When the upstream returns empty/"
        "unparseable response, the run is excluded from the score "
        "denominator rather than counted as a brand miss. Failed runs "
        "are surfaced separately for transparency.\n\n"
    )
    return "".join(out)


def _render_appendix(report: Dict[str, Any]) -> str:
    out: List[str] = [_section_title("Appendix: Citation Data", level=2)]
    mv = report.get("merchant_view") or {}
    receipts = mv.get("receipts") or {}
    cited = receipts.get("cited_hosts_detailed") or []
    if cited:
        out.append("**All grounding sources cited across probes:**\n\n")
        rows = ["| Source | Times cited | Type |", "|---|---|---|"]
        for c in cited[:25]:
            rows.append(
                f"| `{c.get('host', '?')}` | {c.get('times_cited', 0)} "
                f"| {c.get('type', 'unclassified')} |"
            )
        out.append("\n".join(rows) + "\n\n")
    return "".join(out)


def render_brand_markdown_v2(
    brand_report: Dict[str, Any],
    *,
    discovery: Optional[Dict[str, Any]] = None,
) -> str:
    """Render the polished v2 markdown matching the hand-written
    Grüns PDF structure. Consumes the rich payload from PR-7e + 7a-d
    + 8a-d + 11. Defensive against missing fields (older audits).

    Selects the first per_product report as the primary report when
    multiple products were audited (the discovery + competitive
    sections are brand-level; per-product detail can land in an
    appendix in a future iteration).
    """
    sections: List[str] = []

    merchant_name = brand_report.get("merchant_name") or "Unknown brand"
    merchant_domain = brand_report.get("merchant_domain") or ""
    timestamp = brand_report.get("timestamp") or ""
    per_product = brand_report.get("per_product") or []

    # Pick the primary per-product report (first one) for narrative
    # sections — executive summary, evidence quotes, recommendations,
    # roadmap, commitments are all per-product fields.
    primary = per_product[0] if per_product else {}

    # 1. Title + meta
    sections.append(f"# AI Commerce Readiness Audit\n")
    sections.append(f"## {merchant_name}\n\n")
    meta_parts: List[str] = []
    if timestamp:
        meta_parts.append(f"**Generated:** {timestamp}")
    if merchant_domain:
        meta_parts.append(f"**Domain:** `{merchant_domain}`")
    if discovery:
        method = discovery.get("discovery_method")
        platform = discovery.get("platform")
        if method:
            disc_parts: List[str] = [f"discovery: {method}"]
            if platform:
                disc_parts.append(f"platform: {platform}")
            meta_parts.append(f"**Catalog:** {', '.join(disc_parts)}")
    if meta_parts:
        sections.append(" · ".join(meta_parts) + "\n\n---\n")

    # 2. Executive Summary (PR-8a)
    sections.append(_render_executive_summary(primary.get("executive_summary")))
    # 3. Headline Metrics
    sections.append(_render_headline_metrics(primary))
    # 4. Strategic Context (industry depth from PR-7d)
    sections.append(_render_strategic_context(primary.get("industry_context")))
    # 5. Evidence Quotes (PR-7e)
    sections.append(_render_evidence_quotes(primary.get("evidence_quotes")))
    # 6. Competitive Analysis (form factor from PR-7b + competitor brands)
    sections.append(_render_competitive_analysis(primary))
    # 7. Editorial Publisher Analysis (publisher tier/cadence from PR-7c)
    sections.append(_render_publisher_analysis(primary))
    mv = primary.get("merchant_view") or {}
    # 8. What should you do next? (merchant-facing decision layer)
    sections.append(_render_reaudit_delta(mv.get("reaudit_delta")))
    sections.append(_render_next_best_action(mv.get("next_best_action")))
    # 9. Recommendations (action items v2 from PR-8b)
    sections.append(_render_recommendations(mv.get("actions")))
    # 10. Owned buyer-path play (canonical page + route strategy)
    sections.append(_render_owned_buyer_path_play(mv.get("next_best_action")))
    # 11. Implementation Roadmap (PR-8c)
    sections.append(_render_implementation_roadmap(
        primary.get("implementation_roadmap")
    ))
    # 12. Pivota Commitments (PR-8d)
    sections.append(_render_pivota_commitments(
        primary.get("pivota_commitments")
    ))
    # 13. Methodology
    sections.append(_render_methodology(primary))
    # 14. Appendix
    sections.append(_render_appendix(primary))

    return "".join(sections)


def select_renderer():
    """Returns the renderer function based on the
    `AUDIT_RENDERER_VERSION` env var. Default v1; opt-in v2."""
    import os
    version = (os.getenv("AUDIT_RENDERER_VERSION") or "v1").strip().lower()
    if version == "v2":
        return render_brand_markdown_v2
    # Lazy import to avoid circular dependency
    from services.agent_center_bd_report_service import render_brand_markdown
    return render_brand_markdown

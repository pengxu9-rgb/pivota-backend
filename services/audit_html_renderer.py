"""HTML renderer + PDF conversion pipeline (PR-9b).

Generates print-quality HTML for the audit report, matching the
visual style of the polished hand-written Grüns PDF (the one
manually crafted at /tmp/gruns_report.html earlier in the project).
Then optionally converts to PDF via weasyprint (pure-Python, single
binary dep) or returns HTML directly when PDF conversion isn't
available.

**Two endpoints surface this:**

  - `GET /api/agent-center/bd/cold-start-audit/export?format=html`
    — returns rendered HTML as text/html. Works always; no extra
    dependency required.
  - `GET /api/agent-center/bd/cold-start-audit/export?format=pdf`
    — returns rendered PDF as application/pdf. Requires weasyprint
    installed; falls back to HTML download with a 200 + warning
    header when conversion not available, so the route never 500s.

**Design choices:**

  - **HTML structure mirrors v2 markdown sections.** Same content,
    same ordering — markdown renderer (PR-9a) and HTML renderer
    consume the same payload fields. They're sister renderers, not
    independent rebuilds. This means as Track A/B add fields, both
    renderers automatically benefit.

  - **Inline CSS in `<style>` block.** Print CSS is page-size +
    page-break-aware. Color palette matches the hand-written Grüns
    PDF (slate / blue-headers / amber-callouts).

  - **PDF conversion is lazy-imported.** weasyprint imports a heavy
    binary stack (cairo, pango); we don't pay that cost on every
    request when only HTML is needed. Lazy import + try/except so
    HTML rendering works even without weasyprint installed.
"""

from __future__ import annotations

import html
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Display labels (mirror the v2 markdown renderer for consistency)
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

_TIER_DISPLAY: Dict[int, str] = {1: "Tier 1", 2: "Tier 2", 3: "Tier 3"}

_CADENCE_DISPLAY: Dict[str, str] = {
    "continuous": "Continuous",
    "quarterly": "Quarterly",
    "biannual": "Biannual",
    "annual": "Annual",
    "oneoff": "One-off",
}


def _escape(text: Any) -> str:
    """HTML-escape any value, returning empty string for None.
    quote=False keeps apostrophes readable in text content (e.g.
    "Pivota's Commitment" stays literal in <h2>). Attributes are
    rendered with double quotes throughout, so single-quote escaping
    isn't needed for safety."""
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def _print_styles() -> str:
    """Print-optimized CSS. Letter paper, page-break-aware section
    dividers, color theme matching the hand-written Grüns PDF."""
    return """
<style>
  @page { size: letter; margin: 0.75in 0.7in; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.55;
    color: #1a1a1a;
    margin: 0;
    padding: 0;
  }
  h1 {
    font-size: 22pt;
    margin: 0 0 4px 0;
    color: #0d2538;
    border-bottom: 3px solid #0d2538;
    padding-bottom: 8px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  h2 {
    font-size: 15pt;
    margin: 28px 0 10px 0;
    color: #0d2538;
    font-weight: 700;
    border-bottom: 1px solid #cbd5e1;
    padding-bottom: 4px;
    page-break-after: avoid;
  }
  h3 {
    font-size: 12pt;
    margin: 18px 0 6px 0;
    color: #1e3a5f;
    font-weight: 600;
    page-break-after: avoid;
  }
  h4 {
    font-size: 11pt;
    margin: 14px 0 4px 0;
    color: #2c4a6a;
    font-weight: 600;
  }
  .subtitle {
    font-size: 14pt;
    color: #475569;
    font-weight: 400;
    margin-top: -2px;
    margin-bottom: 4px;
  }
  .meta {
    font-size: 9.5pt;
    color: #64748b;
    margin: 6px 0 18px 0;
    padding-bottom: 14px;
    border-bottom: 1px solid #e2e8f0;
  }
  .meta strong { color: #1a1a1a; }
  p { margin: 8px 0; }
  strong { color: #0d2538; font-weight: 600; }
  hr { border: 0; border-top: 1px solid #cbd5e1; margin: 24px 0; }
  ul, ol { margin: 8px 0; padding-left: 22px; }
  li { margin: 4px 0; }
  table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0 16px 0;
    font-size: 9.5pt;
    page-break-inside: avoid;
  }
  th {
    background: #0d2538;
    color: white;
    text-align: left;
    padding: 8px 10px;
    font-weight: 600;
    font-size: 9pt;
    letter-spacing: 0.02em;
  }
  td {
    padding: 7px 10px;
    border-bottom: 1px solid #e2e8f0;
    vertical-align: top;
  }
  tr:nth-child(even) td { background: #f8fafc; }
  blockquote {
    margin: 12px 0;
    padding: 10px 16px;
    border-left: 3px solid #2563eb;
    background: #eff6ff;
    color: #1e3a5f;
    font-size: 10pt;
  }
  code {
    background: #f1f5f9;
    padding: 1px 5px;
    border-radius: 3px;
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 9pt;
    color: #1e3a5f;
  }
  .callout {
    margin: 14px 0;
    padding: 12px 16px;
    background: #fef3c7;
    border-left: 4px solid #d97706;
    border-radius: 2px;
  }
  .summary-box {
    background: #f0f9ff;
    border: 1px solid #bae6fd;
    border-radius: 4px;
    padding: 14px 18px;
    margin: 14px 0;
  }
  .verdict-pill {
    display: inline-block;
    padding: 6px 14px;
    background: #fb923c;
    color: white;
    font-weight: 700;
    border-radius: 20px;
    font-size: 10pt;
    letter-spacing: 0.02em;
    margin: 6px 0;
  }
  .footer {
    margin-top: 32px;
    padding-top: 14px;
    border-top: 1px solid #cbd5e1;
    font-size: 8.5pt;
    color: #64748b;
    text-align: center;
  }
  .no-break { page-break-inside: avoid; }
</style>
"""


def _h(level: int, text: str) -> str:
    return f"<h{level}>{_escape(text)}</h{level}>\n"


def _render_executive_summary_html(es: Optional[Dict[str, Any]]) -> str:
    if not es or not es.get("opening_paragraphs"):
        return ""
    out: List[str] = [_h(2, "Executive Summary")]
    for paragraph in es["opening_paragraphs"]:
        # Preserve simple emphasis from markdown style if present
        text = _escape(paragraph)
        out.append(f"<p>{text}</p>\n")
    pill = (es.get("verdict_pill_text") or "").strip()
    if pill:
        out.append(
            f'<p><span class="verdict-pill">Verdict: {_escape(pill)}</span></p>\n'
        )
    return "".join(out)


def _render_headline_metrics_html(report: Dict[str, Any]) -> str:
    verdict = report.get("verdict") or {}
    headline = (
        verdict.get("label_display")
        or verdict.get("label")
        or "(unknown)"
    )
    out: List[str] = [_h(2, "Headline Metrics")]
    out.append(f'<p><strong>Verdict:</strong> {_escape(headline)}</p>\n')
    explanation = (verdict.get("explanation") or "").strip()
    if explanation:
        out.append(f"<p>{_escape(explanation)}</p>\n")
    rows: List[str] = []
    rows.append(
        "<table><thead><tr><th>Dimension</th><th>Score</th>"
        "<th>Notes</th></tr></thead><tbody>"
    )
    rows.append(_metric_row(
        "Named-product visibility (Layer 1)",
        verdict.get("visibility_score"),
        "Direct named-product queries",
    ))
    rows.append(_metric_row(
        "First-party attribution (Layer 1)",
        verdict.get("attribution_score"),
        "Merchant URL cited in grounding",
    ))
    rows.append(_metric_row(
        "Category discoverability",
        verdict.get("category_visibility_score"),
        "Brand surfaces in category-open queries",
    ))
    rows.append("</tbody></table>")
    out.append("".join(rows) + "\n")
    return "".join(out)


def _metric_row(dim: str, score: Optional[int], notes: str) -> str:
    score_str = (
        f"<strong>{score}/100</strong>" if score is not None
        else "<em>(not measured)</em>"
    )
    return (
        f"<tr><td>{_escape(dim)}</td><td>{score_str}</td>"
        f"<td>{_escape(notes)}</td></tr>"
    )


def _render_strategic_context_html(industry: Optional[Dict[str, Any]]) -> str:
    if not industry:
        return ""
    out: List[str] = [_h(2, "Strategic Context")]
    blurb = (industry.get("blurb") or "").strip()
    if blurb:
        out.append(f"<p>{_escape(blurb)}</p>\n")
    market_size = industry.get("market_size_billions_usd")
    market_year = industry.get("market_size_year")
    horizon = industry.get("growth_horizon_years")
    if market_size:
        size_line = f"<strong>Category market size:</strong> ~${market_size}B"
        if market_year:
            size_line += f" ({_escape(market_year)})"
        if horizon:
            size_line += f" &middot; projection horizon {_escape(horizon)}"
        out.append(f"<p>{size_line}</p>\n")
    sub_trends = industry.get("sub_category_trends") or []
    if sub_trends:
        out.append("<p><strong>Sub-category trends:</strong></p>\n<ul>\n")
        for trend in sub_trends[:5]:
            sub = trend.get("sub", "")
            growth = trend.get("growth_pct")
            why = (trend.get("why") or "").strip()
            line = f"<strong>{_escape(sub)}</strong>"
            if growth:
                line += f" &mdash; ~{growth}% YoY"
            if why:
                line += f". {_escape(why)}"
            out.append(f"  <li>{line}</li>\n")
        out.append("</ul>\n")
    comparison = (industry.get("comparison_to_other_verticals") or "").strip()
    if comparison:
        out.append(
            f"<p><strong>Vertical positioning:</strong> "
            f"{_escape(comparison)}</p>\n"
        )
    return "".join(out)


def _render_evidence_quotes_html(quotes: Optional[List[Dict[str, Any]]]) -> str:
    if not quotes:
        return ""
    out: List[str] = [_h(2, "Evidence Quotes")]
    out.append(
        "<p>Verbatim text from AI grounded answers that named the brand "
        "directly, with editorial-grade corroboration:</p>\n"
    )
    for q in quotes:
        excerpt = (q.get("excerpt_text") or "").strip()
        if not excerpt:
            continue
        sources = q.get("source_labels") or []
        query = (q.get("query") or "").strip()
        meta_parts: List[str] = []
        if sources:
            meta_parts.append(f"<em>via {_escape(', '.join(sources))}</em>")
        if query:
            meta_parts.append(f'<em>query: "{_escape(query)}"</em>')
        meta = " &middot; ".join(meta_parts) if meta_parts else ""
        out.append(
            f"<blockquote>{_escape(excerpt)}"
            + (f"<br><br>{meta}" if meta else "")
            + "</blockquote>\n"
        )
    return "".join(out)


def _render_competitive_analysis_html(report: Dict[str, Any]) -> str:
    cohort_ff = report.get("cohort_form_factor") or {}
    cv = report.get("category_visibility") or {}
    competitor_brands = (cv.get("competitor_brands") or [])[:15]
    if not (competitor_brands or cohort_ff.get("form_factor_summary")):
        return ""
    out: List[str] = [_h(2, "Competitive Analysis")]
    summary = cohort_ff.get("form_factor_summary") or {}
    if summary:
        merchant_ff = cohort_ff.get("merchant_form_factor")
        unique = cohort_ff.get("merchant_owns_unique_form_factor")
        if merchant_ff and unique:
            out.append(
                f'<div class="summary-box"><strong>Form-factor positioning:'
                f"</strong> the audited brand is the only one in the cohort "
                f"with form factor <code>{_escape(merchant_ff)}</code> "
                f"&mdash; a structural moat in editorial subcategories."
                f"</div>\n"
            )
        elif merchant_ff:
            shared = cohort_ff.get("competitors_in_merchant_form_factor") or []
            if shared:
                out.append(
                    f"<p><strong>Form-factor positioning:</strong> the audited "
                    f"brand shares form factor <code>{_escape(merchant_ff)}</code> "
                    f"with: {_escape(', '.join(shared[:5]))}.</p>\n"
                )
        if any(brands for brands in summary.values()):
            out.append("<p><strong>Cohort form-factor breakdown:</strong></p>\n")
            out.append("<table><thead><tr><th>Form factor</th>"
                       "<th>Brands</th></tr></thead><tbody>\n")
            for ff, brands in summary.items():
                if brands:
                    out.append(
                        f"<tr><td><code>{_escape(ff)}</code></td>"
                        f"<td>{_escape(', '.join(brands[:8]))}</td></tr>\n"
                    )
            out.append("</tbody></table>\n")
    if competitor_brands:
        out.append("<p><strong>Top competitor brands surfaced "
                   "(by AI mention share):</strong></p>\n")
        out.append("<table><thead><tr><th>Brand</th>"
                   "<th>Times mentioned</th></tr></thead><tbody>\n")
        for entry in competitor_brands[:15]:
            out.append(
                f"<tr><td>{_escape(entry.get('name', '?'))}</td>"
                f"<td>{_escape(entry.get('times_cited', 0))}</td></tr>\n"
            )
        out.append("</tbody></table>\n")
    return "".join(out)


def _render_publisher_analysis_html(report: Dict[str, Any]) -> str:
    mv = report.get("merchant_view") or {}
    receipts = mv.get("receipts") or {}
    cited = receipts.get("cited_hosts_detailed") or []
    editorial = [c for c in cited if c.get("type") == "editorial"]
    if not editorial:
        return ""
    out: List[str] = [_h(2, "Editorial Publisher Analysis")]
    out.append(
        "<p>Publishers cited by AI grounded retrieval for this brand's "
        "category &mdash; ranked by frequency.</p>\n"
    )
    out.append(
        "<table><thead><tr>"
        "<th>Publisher</th><th>Cites</th><th>Tier</th>"
        "<th>Cadence</th><th>Outreach cycle</th><th>Pitch path</th>"
        "</tr></thead><tbody>\n"
    )
    for p in editorial[:10]:
        host = p.get("host", "?")
        cites = p.get("times_cited", 0)
        tier = p.get("tier")
        tier_str = _TIER_DISPLAY.get(tier or 0, "&mdash;")
        cadence = p.get("editorial_cadence")
        cadence_str = _CADENCE_DISPLAY.get(cadence or "", "&mdash;")
        cycle = p.get("expected_outreach_cycle_weeks")
        cycle_str = (
            f"{cycle[0]}-{cycle[1]} weeks"
            if isinstance(cycle, list) and len(cycle) == 2
            else "&mdash;"
        )
        pitch = p.get("pitch_recipient") or {}
        pitch_str = "&mdash;"
        if isinstance(pitch, dict):
            email = pitch.get("email")
            if email:
                pitch_str = f"<code>{_escape(email)}</code>"
            elif pitch.get("submission_url"):
                pitch_str = "submission form"
        out.append(
            f"<tr><td><code>{_escape(host)}</code></td>"
            f"<td>{_escape(cites)}</td>"
            f"<td>{tier_str}</td>"
            f"<td>{cadence_str}</td>"
            f"<td>{cycle_str}</td>"
            f"<td>{pitch_str}</td></tr>\n"
        )
    out.append("</tbody></table>\n")
    return "".join(out)


def _render_recommendations_html(action_items: Optional[List[Dict[str, Any]]]) -> str:
    if not action_items:
        return ""
    out: List[str] = [_h(2, "Recommendations")]
    out.append(
        "<p>Prioritized actions with owner, expected outcome, KPI to "
        "track, and time-window phase.</p>\n"
    )
    for action in action_items:
        title = (action.get("title") or "").strip()
        if not title:
            continue
        priority = action.get("priority_order")
        severity = (action.get("severity") or "").upper()
        owner = _OWNER_DISPLAY.get(action.get("owner", ""), "&mdash;")
        phase = _PHASE_DISPLAY.get(action.get("phase", ""), "&mdash;")
        body = (action.get("body") or "").strip()
        kpi = (action.get("kpi_to_track") or "").strip()
        outcome = (action.get("expected_outcome") or "").strip()
        next_step = (action.get("concrete_next_step") or "").strip()
        timeline = action.get("expected_timeline_weeks")
        out.append(
            f'<div class="no-break">\n'
            f"<h3>{_escape(priority)}. {_escape(title)}</h3>\n"
        )
        meta = (
            f"<p><strong>Severity:</strong> {_escape(severity)} "
            f"&middot; <strong>Owner:</strong> {owner} "
            f"&middot; <strong>Phase:</strong> {phase}"
        )
        if isinstance(timeline, list) and len(timeline) == 2:
            meta += (
                f" &middot; <strong>Timeline:</strong> "
                f"{timeline[0]}-{timeline[1]} weeks"
            )
        meta += "</p>\n"
        out.append(meta)
        if body:
            out.append(f"<p>{_escape(body)}</p>\n")
        if next_step:
            out.append(
                f"<p><strong>This week:</strong> {_escape(next_step)}</p>\n"
            )
        if outcome:
            out.append(
                f"<p><strong>Expected outcome:</strong> "
                f"{_escape(outcome)}</p>\n"
            )
        if kpi:
            out.append(
                f"<p><strong>KPI to track:</strong> {_escape(kpi)}</p>\n"
            )
        pitch_draft = action.get("pitch_draft")
        if isinstance(pitch_draft, dict) and pitch_draft.get("body"):
            recipient = pitch_draft.get("recipient_email") or ""
            subject = pitch_draft.get("subject") or ""
            out.append(
                f'<p><strong>Suggested outreach</strong> (recipient: '
                f"<code>{_escape(recipient)}</code>):</p>\n"
            )
            out.append("<blockquote>")
            if subject:
                out.append(f"<strong>Subject:</strong> {_escape(subject)}<br><br>")
            out.append(_escape(pitch_draft["body"]).replace("\n", "<br>"))
            out.append("</blockquote>\n")
        out.append("</div>\n")
    return "".join(out)


def _render_owned_buyer_path_play_html(next_best_action: Optional[Dict[str, Any]]) -> str:
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

    out: List[str] = [_h(2, "Owned Buyer Path Play")]
    out.append('<div class="summary-box no-break">\n')
    out.append(f"<p><strong>Strategy:</strong> {_escape(strategy)}</p>\n")
    if lane:
        out.append(f"<p><strong>Lane to win back:</strong> <code>{_escape(lane)}</code></p>\n")
    if controllers:
        controller_html = ", ".join(f"<code>{_escape(host)}</code>" for host in controllers)
        out.append(f"<p><strong>Controllers evidenced:</strong> {controller_html}</p>\n")
    if focus:
        out.append(f"<p><strong>Operator read:</strong> {_escape(focus)}</p>\n")
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
            out.append("<p><strong>Sideways demand wedge:</strong></p>\n<ul>\n")
            if beachhead_query:
                out.append(
                    f"<li>Beachhead lane: <code>{_escape(beachhead_query)}</code></li>\n"
                )
            if why_wedge:
                out.append(f"<li>Why this first: {_escape(why_wedge)}</li>\n")
            if do_not:
                deferred = ", ".join(
                    f"<code>{_escape(str(item.get('query') or '').strip())}</code>"
                    for item in do_not
                )
                out.append(f"<li>Do not chase yet: {deferred}</li>\n")
            out.append("</ul>\n")
    if moves:
        out.append("<p><strong>Operator checklist:</strong></p>\n<ol>\n")
        for move in moves[:5]:
            action = (move.get("operator_action") or "").strip()
            why = (move.get("why") or "").strip()
            move_type = (move.get("type") or "move").replace("_", " ").title()
            out.append(
                f"<li><strong>{_escape(move_type)}</strong> — {_escape(action)}"
            )
            if why:
                out.append(f"<br><em>Why:</em> {_escape(why)}")
            out.append("</li>\n")
        out.append("</ol>\n")
    checkout = (play.get("checkout_readiness") or "").strip()
    if checkout:
        out.append(
            f"<p><strong>Agent-checkout readiness:</strong> {_escape(checkout)}</p>\n"
        )
    economics = (play.get("economics_policy") or "").strip()
    if economics:
        out.append(f"<p><strong>Economics guard:</strong> {_escape(economics)}</p>\n")
    out.append("</div>\n")
    return "".join(out)


def _render_implementation_roadmap_html(roadmap: Optional[Dict[str, Any]]) -> str:
    if not roadmap:
        return ""
    phases = roadmap.get("phases") or []
    if not phases:
        return ""
    out: List[str] = [_h(2, "Implementation Roadmap")]
    out.append(
        "<table><thead><tr><th>Phase</th><th>Weeks</th><th>Owners</th>"
        "<th>Activities</th><th>Expected outcome</th></tr></thead><tbody>\n"
    )
    for phase in phases:
        label = phase.get("label", "")
        weeks = phase.get("weeks", "")
        owners = phase.get("owners") or []
        owners_str = (
            ", ".join(_OWNER_DISPLAY.get(o, o) for o in owners)
            if owners else "&mdash;"
        )
        activity_count = phase.get("activity_count", 0)
        outcome = (phase.get("expected_outcome") or "").strip()
        if len(outcome) > 200:
            outcome = outcome[:197].rstrip() + "..."
        out.append(
            f"<tr><td><strong>{_escape(label)}</strong></td>"
            f"<td>{_escape(weeks)}</td>"
            f"<td>{owners_str}</td>"
            f"<td>{_escape(activity_count)} activities</td>"
            f"<td>{_escape(outcome)}</td></tr>\n"
        )
    out.append("</tbody></table>\n")
    return "".join(out)


def _render_pivota_commitments_html(commitments: Optional[Dict[str, Any]]) -> str:
    if not commitments:
        return ""
    out: List[str] = [_h(2, "Pivota's Commitment")]
    summary = (commitments.get("platform_capability_summary") or "").strip()
    if summary:
        out.append(
            f"<p><strong>Platform capability:</strong> "
            f"{_escape(summary)}</p>\n"
        )
    delivers_1_4 = commitments.get("delivers_weeks_1_to_4") or []
    if delivers_1_4:
        out.append(_h(3, "Weeks 1-4: Onboarding execution"))
        out.append("<ul>\n")
        for item in delivers_1_4:
            out.append(f"  <li>{_escape(item)}</li>\n")
        out.append("</ul>\n")
    delivers_continuous = commitments.get("delivers_continuous") or []
    if delivers_continuous:
        out.append(_h(3, "Continuous operations"))
        out.append("<ul>\n")
        for item in delivers_continuous:
            out.append(f"  <li>{_escape(item)}</li>\n")
        out.append("</ul>\n")
    does_not = commitments.get("does_not_promise") or []
    if does_not:
        out.append(_h(3, "What Pivota does not promise"))
        out.append("<ul>\n")
        for item in does_not:
            out.append(f"  <li>{_escape(item)}</li>\n")
        out.append("</ul>\n")
    return "".join(out)


def _render_methodology_html(report: Dict[str, Any]) -> str:
    out: List[str] = [_h(2, "Methodology")]
    upstream = report.get("upstream_status") or {}
    requested = upstream.get("requested_provider", "?")
    actual = upstream.get("visibility_provider", "?")
    out.append(
        f"<p><strong>Probe source.</strong> Audit issued via "
        f"<code>{_escape(requested)}</code> provider; upstream resolved to "
        f"<code>{_escape(actual)}</code>.</p>\n"
    )
    out.append(
        "<p><strong>Probe modes:</strong> three modes mirror the standard "
        "audit structure &mdash; open product visibility (named-product "
        "queries), merchant store attribution (URL-cited check), category "
        "visibility (category-open queries that don't name the brand).</p>\n"
    )
    out.append(
        "<p><strong>Scoring.</strong> Category visibility credits a query "
        "when ANY of: (a) merchant URL in grounding chunks, (b) merchant "
        "brand in a grounded source title, OR (c) brand in evidence "
        "excerpt + LLM self-report + at least one grounding source &mdash; "
        "the editorial-citation path. Excerpt-only LLM paraphrases (no "
        "self-report or no grounding) do NOT credit, defending against "
        "hallucinated brand mentions.</p>\n"
    )
    out.append(
        "<p><strong>Probe failures.</strong> When the upstream returns "
        "empty/unparseable response, the run is excluded from the score "
        "denominator rather than counted as a brand miss.</p>\n"
    )
    return "".join(out)


def _render_appendix_html(report: Dict[str, Any]) -> str:
    out: List[str] = [_h(2, "Appendix: Citation Data")]
    mv = report.get("merchant_view") or {}
    receipts = mv.get("receipts") or {}
    cited = receipts.get("cited_hosts_detailed") or []
    if cited:
        out.append("<p><strong>All grounding sources cited across "
                   "probes:</strong></p>\n")
        out.append(
            "<table><thead><tr><th>Source</th><th>Times cited</th>"
            "<th>Type</th></tr></thead><tbody>\n"
        )
        for c in cited[:25]:
            out.append(
                f"<tr><td><code>{_escape(c.get('host', '?'))}</code></td>"
                f"<td>{_escape(c.get('times_cited', 0))}</td>"
                f"<td>{_escape(c.get('type', 'unclassified'))}</td></tr>\n"
            )
        out.append("</tbody></table>\n")
    return "".join(out)


def render_brand_html_v2(
    brand_report: Dict[str, Any],
    *,
    discovery: Optional[Dict[str, Any]] = None,
) -> str:
    """Render the full audit report as print-quality HTML matching
    the polished Grüns PDF visual style. Same payload + sections as
    the v2 markdown renderer (PR-9a) — sister implementation."""
    merchant_name = brand_report.get("merchant_name") or "Unknown brand"
    merchant_domain = brand_report.get("merchant_domain") or ""
    timestamp = brand_report.get("timestamp") or ""
    per_product = brand_report.get("per_product") or []
    primary = per_product[0] if per_product else {}

    body_sections: List[str] = []
    body_sections.append("<h1>AI Commerce Readiness Audit</h1>\n")
    body_sections.append(f'<div class="subtitle">{_escape(merchant_name)}</div>\n')

    meta_parts: List[str] = []
    if timestamp:
        meta_parts.append(f"<strong>Generated:</strong> {_escape(timestamp)}")
    if merchant_domain:
        meta_parts.append(
            f"<strong>Domain:</strong> <code>{_escape(merchant_domain)}</code>"
        )
    if discovery:
        method = discovery.get("discovery_method")
        platform = discovery.get("platform")
        if method:
            disc_parts: List[str] = [f"discovery: {_escape(method)}"]
            if platform:
                disc_parts.append(f"platform: {_escape(platform)}")
            meta_parts.append(
                f"<strong>Catalog:</strong> {', '.join(disc_parts)}"
            )
    if meta_parts:
        body_sections.append(
            f'<div class="meta">{" &middot; ".join(meta_parts)}</div>\n'
        )

    body_sections.append(_render_executive_summary_html(primary.get("executive_summary")))
    body_sections.append(_render_headline_metrics_html(primary))
    body_sections.append(_render_strategic_context_html(primary.get("industry_context")))
    body_sections.append(_render_evidence_quotes_html(primary.get("evidence_quotes")))
    body_sections.append(_render_competitive_analysis_html(primary))
    body_sections.append(_render_publisher_analysis_html(primary))
    mv = primary.get("merchant_view") or {}
    body_sections.append(_render_recommendations_html(mv.get("actions")))
    body_sections.append(_render_owned_buyer_path_play_html(
        mv.get("next_best_action")
    ))
    body_sections.append(_render_implementation_roadmap_html(
        primary.get("implementation_roadmap")
    ))
    body_sections.append(_render_pivota_commitments_html(
        primary.get("pivota_commitments")
    ))
    body_sections.append(_render_methodology_html(primary))
    body_sections.append(_render_appendix_html(primary))

    body_sections.append(
        '<div class="footer">Prepared by Pivota. Audit reference: '
        f'{_escape(merchant_domain or merchant_name)} | '
        f'{_escape(timestamp)}</div>\n'
    )

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        f'<title>AI Commerce Readiness Audit — {_escape(merchant_name)}</title>\n'
        f"{_print_styles()}"
        "</head>\n<body>\n"
        + "".join(body_sections)
        + "</body>\n</html>\n"
    )


def html_to_pdf_bytes(html_str: str) -> Optional[bytes]:
    """Convert HTML → PDF using weasyprint. Returns the PDF bytes,
    or None when weasyprint isn't installed (caller can fall back
    to returning the HTML directly).

    Lazy import — weasyprint pulls in cairo / pango binaries; we
    don't pay that cost on every request when only HTML is needed.
    """
    try:
        from weasyprint import HTML  # type: ignore
    except ImportError:
        logger.info(
            "weasyprint not installed; PDF conversion unavailable. "
            "Install via `pip install weasyprint` to enable. "
            "Falling back to HTML-only rendering."
        )
        return None
    try:
        return HTML(string=html_str).write_pdf()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "weasyprint failed to render PDF: %s. Falling back to HTML.",
            exc,
        )
        return None

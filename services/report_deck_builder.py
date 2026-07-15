"""Leadership deck (PPTX) export for the Report Summary Contract v1.

Renders a completed audit's `report_summary` into a short, boardroom-ready
deck: cover (score + band + as-of date), verdict + competitive snapshot, an
optional LLM-written executive summary, one slide per top action (with the
measured prompt evidence as captions), and a methodology/honesty slide.

Pricing (PR-4 decision): the ONLY LLM step is the executive summary; it bills
on ACTUAL token usage at DECK_TOKEN_PRICE_MULTIPLE (1.6x measured token COGS —
deliberately above the 1.2 probe flat_multiple: the deck is a premium,
leadership-facing artifact). Everything else is deterministic rendering of the
contract, so a deck without the LLM step (no key / LLM down) costs 0 credits.

Honesty rules carried over from the contract: every number and finding on the
deck is verbatim contract data; the LLM bullets may only rephrase for an
executive audience and are prompted to add no new facts, numbers, or claims;
the deck prints its "data as of" date and the run's honest_limits.

python-pptx is imported lazily (mirrors the weasyprint pattern in
audit_html_renderer): build_report_deck returns None when it isn't installed
so the route can 503 with a clear message instead of crashing at import time.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

# Customer price = measured token COGS x this multiple (user-set pricing:
# 1.6x actual consumption), converted to credits by credits_for_tokens.
DECK_TOKEN_PRICE_MULTIPLE = Decimal("1.6")

DECK_LLM_PROVIDER = "deepseek"
_EXEC_SUMMARY_MAX_TOKENS = 400
_EXEC_SUMMARY_TIMEOUT_S = 30.0

_EXEC_SUMMARY_SYSTEM = (
    "You write one executive-summary slide for a brand's AI-readiness audit "
    "deck. Audience: marketing leadership deciding what to fund next. Input "
    "is the audit's summary JSON. Write 3-4 short bullets (max ~18 words "
    "each): the current state, what it costs the brand, and what to do first. "
    "STRICT: use only facts, numbers, and names present in the JSON — never "
    "invent, extrapolate, or soften. Plain business English, no jargon, no "
    "emoji. Output one bullet per line, no numbering, no markdown."
)

# Midnight-executive palette (skill-vetted): navy dominates the cover +
# closing, ice-blue accents, white content slides. Safe fonts only.
_NAVY = (0x1E, 0x27, 0x61)
_ICE = (0xCA, 0xDC, 0xFC)
_WHITE = (0xFF, 0xFF, 0xFF)
_INK = (0x21, 0x21, 0x21)
_MUTED = (0x5A, 0x5F, 0x73)
_RED = (0xB4, 0x23, 0x18)
_AMBER = (0xA1, 0x62, 0x07)
_GREEN = (0x1E, 0x6B, 0x3C)

_BAND_DECK_LABEL = {
    "needs_work": ("Needs work", _AMBER),
    "pass": ("Pass", _NAVY),
    "good": ("Good", _GREEN),
    "excellent": ("Excellent", _GREEN),
}

_SEVERITY_COLOR = {"high": _RED, "medium": _AMBER, "info": _MUTED}


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


async def generate_executive_summary(
    summary: Mapping[str, Any],
) -> Optional[Tuple[List[str], int, int]]:
    """LLM executive-summary bullets grounded ONLY in the contract JSON.

    Returns (bullets, prompt_tokens, completion_tokens), or None when no API
    key is configured / the provider returned nothing usable — the deck then
    ships without this slide and nothing is billed. Raises on transport errors
    (caller catches). Isolated so tests can monkeypatch it.
    """
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        return None
    # Only the fields leadership copy may draw from — strips sku_summaries etc.
    grounding = {
        "score": _as_dict(summary.get("score")),
        "verdict": _as_dict(summary.get("verdict")),
        "top_findings": _as_list(summary.get("top_findings")),
        "top_actions": [
            {
                k: v
                for k, v in _as_dict(a).items()
                if k in ("headline", "why_this_first", "first_move", "sku_title")
            }
            for a in _as_list(summary.get("top_actions"))
        ],
        "competitive_snapshot": _as_dict(summary.get("competitive_snapshot")),
    }
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _EXEC_SUMMARY_SYSTEM},
            {"role": "user", "content": json.dumps(grounding, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": _EXEC_SUMMARY_MAX_TOKENS,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    base_url = settings.deepseek_api_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=_EXEC_SUMMARY_TIMEOUT_S) as client:
        response = await client.post(
            f"{base_url}/v1/chat/completions", json=payload, headers=headers
        )
        response.raise_for_status()
        data = response.json()
    content = (data["choices"][0]["message"]["content"] or "").strip()
    bullets = [
        line.strip().lstrip("-•*").strip()
        for line in content.splitlines()
        if line.strip()
    ][:4]
    if not bullets:
        return None
    usage = data.get("usage") or {}
    return (
        bullets,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )


def _fmt_display(score: Mapping[str, Any]) -> str:
    display = score.get("display")
    if not isinstance(display, (int, float)):
        return "—"
    return f"{display:.1f}"


def build_report_deck(
    summary: Mapping[str, Any],
    *,
    executive_bullets: Optional[List[str]] = None,
    preview_only: bool = False,
) -> Optional[bytes]:
    """Render the summary contract into PPTX bytes.

    preview_only=True (free tier) → a single watermarked cover slide: enough
    to share, honest about being a preview, and the upgrade hook. Returns None
    when python-pptx isn't installed (route surfaces a clear 503).
    """
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.text import PP_ALIGN
        from pptx.util import Emu, Inches, Pt
    except ImportError:
        logger.warning("python-pptx not installed; deck export unavailable")
        return None

    summary = _as_dict(summary)
    score = _as_dict(summary.get("score"))
    verdict = _as_dict(summary.get("verdict"))
    meta = _as_dict(summary.get("meta"))
    subject = _as_dict(summary.get("subject"))
    brand = subject.get("merchant_name") or "Your brand"

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    def rgb(t):  # (r,g,b) -> RGBColor
        return RGBColor(*t)

    def add_slide(bg=_WHITE):
        slide = prs.slides.add_slide(blank)
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = rgb(bg)
        return slide

    def text_box(
        slide,
        x,
        y,
        w,
        h,
        lines,
        *,
        align=PP_ALIGN.LEFT,
    ):
        """lines: list of (text, size_pt, bold, color, space_after_pt)."""
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Emu(0)
        first = True
        for text, size, bold, color, space_after in lines:
            para = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            para.alignment = align
            para.space_after = Pt(space_after)
            run = para.add_run()
            run.text = text
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.name = "Calibri"
            run.font.color.rgb = rgb(color)
        return box

    band = str(score.get("band") or "")
    band_label, _band_color = _BAND_DECK_LABEL.get(band, ("", _MUTED))
    scale_max = score.get("scale_max") or 10
    generated_at = str(summary.get("generated_at") or "")[:10]

    # ── Cover: navy, giant score callout, verdict headline. ────────────────
    cover = add_slide(_NAVY)
    text_box(
        cover,
        0.9,
        0.7,
        11.5,
        0.6,
        [(f"{brand} — AI-readiness report", 20, True, _ICE, 0)],
    )
    text_box(
        cover,
        0.9,
        1.9,
        11.5,
        2.2,
        [
            (
                f"{_fmt_display(score)} / {scale_max}"
                + (f"   ·   {band_label}" if band_label else ""),
                66,
                True,
                _WHITE,
                6,
            )
        ],
    )
    cover_lines = []
    if verdict.get("headline"):
        cover_lines.append((str(verdict["headline"]), 22, False, _ICE, 10))
    delta = _as_dict(score.get("delta"))
    if isinstance(delta.get("raw"), (int, float)):
        moved = float(delta["raw"]) / 10
        cover_lines.append(
            (f"{moved:+.1f} on the {scale_max}-point scale since the last audit", 14, False, _ICE, 0)
        )
    if cover_lines:
        text_box(cover, 0.9, 4.1, 11.5, 1.8, cover_lines)
    footer = f"Pivota AI-readiness audit · data as of {generated_at}" if generated_at else "Pivota AI-readiness audit"
    text_box(cover, 0.9, 6.7, 11.5, 0.4, [(footer, 11, False, _ICE, 0)])

    if preview_only:
        # Watermark + upgrade hook, then stop at one slide.
        text_box(
            cover,
            0.9,
            5.6,
            11.5,
            0.9,
            [
                (
                    "PREVIEW — upgrade your Pivota plan to export the full deck "
                    "(findings, competitive snapshot, and the action plan).",
                    16,
                    True,
                    _WHITE,
                    0,
                )
            ],
        )
        import io

        buf = io.BytesIO()
        prs.save(buf)
        return buf.getvalue()

    # ── What we found: findings + who AI cites instead. ────────────────────
    findings = [
        f for f in (_as_dict(x) for x in _as_list(summary.get("top_findings")))
        if f.get("title") or f.get("evidence_summary")
    ]
    snapshot = _as_dict(summary.get("competitive_snapshot"))
    slide = add_slide()
    text_box(slide, 0.9, 0.6, 11.5, 0.8, [("What we found", 34, True, _NAVY, 0)])
    y = 1.7
    for f in findings[:3]:
        sev_color = _SEVERITY_COLOR.get(str(f.get("severity") or ""), _MUTED)
        lines = []
        if f.get("title"):
            lines.append((str(f["title"]), 20, True, sev_color, 3))
        if f.get("evidence_summary"):
            lines.append((str(f["evidence_summary"]), 15, False, _INK, 0))
        text_box(slide, 0.9, y, 11.5, 1.3, lines)
        y += 1.45
    if snapshot.get("available"):
        snap_lines = [("Who AI cites instead", 16, True, _NAVY, 3)]
        hosts = [h for h in _as_list(snapshot.get("top_cited_hosts")) if h]
        comps = [c for c in _as_list(snapshot.get("competitors_named")) if c]
        if hosts:
            snap_lines.append(
                ("Sources: " + ", ".join(str(h) for h in hosts[:6]), 14, False, _INK, 2)
            )
        if comps:
            snap_lines.append(
                ("Competitors named: " + ", ".join(str(c) for c in comps[:6]), 14, False, _INK, 0)
            )
        if len(snap_lines) > 1:
            text_box(slide, 0.9, y, 11.5, 1.4, snap_lines)

    # ── Executive summary (LLM, optional). ──────────────────────────────────
    if executive_bullets:
        slide = add_slide()
        text_box(slide, 0.9, 0.6, 11.5, 0.8, [("Executive summary", 34, True, _NAVY, 0)])
        text_box(
            slide,
            0.9,
            1.8,
            11.5,
            4.6,
            [(f"•  {b}", 19, False, _INK, 14) for b in executive_bullets[:4]],
        )
        text_box(
            slide,
            0.9,
            6.8,
            11.5,
            0.4,
            [
                (
                    "Written from this audit's measured findings only.",
                    11,
                    False,
                    _MUTED,
                    0,
                )
            ],
        )

    # ── One slide per top action. ───────────────────────────────────────────
    actions = [
        a for a in (_as_dict(x) for x in _as_list(summary.get("top_actions")))
        if a.get("headline")
    ]
    for i, action in enumerate(actions[:3], start=1):
        slide = add_slide()
        text_box(
            slide,
            0.9,
            0.6,
            11.5,
            1.2,
            [(f"Move {i} — {action['headline']}", 30, True, _NAVY, 0)],
        )
        body = []
        if action.get("why_this_first"):
            body.append(("Why this first", 15, True, _MUTED, 2))
            body.append((str(action["why_this_first"]), 16, False, _INK, 10))
        if action.get("first_move"):
            body.append(("First move", 15, True, _MUTED, 2))
            body.append((str(action["first_move"]), 16, False, _INK, 0))
        if body:
            text_box(slide, 0.9, 2.0, 11.5, 3.0, body)
        prompts = [
            p for p in (_as_dict(x) for x in _as_list(action.get("supporting_prompts")))
            if p.get("query")
        ]
        if prompts and (action.get("supporting_prompts_basis") or "none") != "none":
            ev = [("What the AI answers showed", 13, True, _MUTED, 3)]
            for p in prompts[:3]:
                bits = [f"“{p['query']}”"]
                if p.get("provider"):
                    bits.append(str(p["provider"]))
                if p.get("reason"):
                    bits.append(str(p["reason"]))
                ev.append(("  ·  ".join(bits), 12, False, _MUTED, 2))
            text_box(slide, 0.9, 5.3, 11.5, 1.7, ev)

    # ── Methodology / honesty close (navy bookend). ─────────────────────────
    slide = add_slide(_NAVY)
    text_box(slide, 0.9, 0.6, 11.5, 0.8, [("How this was measured", 34, True, _WHITE, 0)])
    limits = [str(l) for l in _as_list(meta.get("honest_limits")) if l]
    lines = [(f"•  {l}", 14, False, _ICE, 8) for l in limits[:5]]
    facts = []
    providers = [str(p) for p in _as_list(meta.get("providers")) if p]
    if providers:
        facts.append("Probed on " + ", ".join(providers))
    if isinstance(meta.get("products_audited"), int):
        facts.append(f"{meta['products_audited']} product(s) audited")
    if generated_at:
        facts.append(f"data as of {generated_at}")
    if facts:
        lines.append((" · ".join(facts), 13, False, _ICE, 0))
    if lines:
        text_box(slide, 0.9, 1.9, 11.5, 4.6, lines)
    text_box(
        slide,
        0.9,
        6.7,
        11.5,
        0.4,
        [("Generated by Pivota · pivota.cc", 12, False, _ICE, 0)],
    )

    import io

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()

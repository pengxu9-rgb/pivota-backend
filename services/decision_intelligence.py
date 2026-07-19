"""Decision-dossier copy: grounded bullet_points + usage_scenarios.

THE GAP (2026-07-19 prod census): agent_pdp_view carries the structured decision
columns bullet_points / usage_scenarios / evidence_profile / required_disclaimers,
but across the serving brand-official cohort they are ~0% populated. Per ADR-002
a served record must be a "verified decision dossier — graded claims, honest
pros/cons", not a commodity CATALOG PDP. This module authors the two *copy*
fields of that dossier; the evidence/claims side is produced by
services.beauty_enrichment_persist (INCI-substantiated, non-LLM) and reused by
the cohort runner.

DESIGN: EXTRACTIVE, not generatively-validated. Validating free LLM prose is
structurally leaky — a bag-of-tokens or even span check lets a fabricated clause
("...that reverses sun damage") ride a real fragment, and lets grade-escalation
("clinically proven", "24 hours") through. So the LLM never *asserts*; it only
SELECTS/RANKS which owned content to surface, and we publish SOURCE-TRUTH text,
never the model's paraphrase. There are exactly two bullet sources:

  1. EFFICACY / benefit bullets -> we publish the matched SUBSTANTIATED evidence
     item's claim_text VERBATIM (from beauty_evidence.derive_substantiated_claims
     ∪ beauty_enrichment_persist._inci_substantiated_claims). No substantiated
     item -> no efficacy bullet. This kills grade-overclaim (we publish the
     hedged, graded phrasing) AND the leaky efficacy allowlist (evidence items
     ARE the only efficacy source).
  2. DESCRIPTIVE bullets -> survive only if the model line is a WHOLE-BULLET
     contiguous QUOTE of the owned source (every content stem inside one grounded
     contiguous run, not a single 3-gram rider). We publish the source span.

Belt-and-suspenders guards on top of extractive publish: a POLARITY guard
(negation / `-free` / `without`), an ATTRIBUTE-SWAP guard (skin-type / gentleness
value swaps: source "oily skin" vs bullet "dry skin"), and a GRADE gate (any
strength-escalation token in the model line — clinically/proven/guaranteed/
instantly/quantified durations/percentages — must be present in the published
source-truth text, else drop).

The LLM call is dependency-injected (GenerateFn) exactly like
official_match_judge.judge_fn, so tests run with a fake model and no network. The
closed-context model choice (DeepSeek) follows that doctrine: the grounding
source is the product's OWNED text, so this is a closed selection task.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

# Output caps mirror the E1 parser (services.executor_agents.canonical_pdp_enrichment)
# so both writers of product_enrichment.bullet_points/usage_scenarios agree on shape.
_MAX_BULLETS = 8
_MAX_SCENARIOS = 4
_MIN_BULLETS = 3  # below this the dossier isn't worth publishing (thin/ungroundable)

# A generated line shorter than this is a fragment, longer than this is a
# paragraph masquerading as a bullet — both are rejected before grounding.
_MIN_LINE_CHARS = 8
_MAX_LINE_CHARS = 240

# Descriptive quote needs at least this many content tokens (a 1-word "quote" is
# worthless). Efficacy -> substantiated-claim match floor.
_MIN_QUOTE_CONTENT = 2
_EFFICACY_MIN_SHARED = 2
_EFFICACY_MIN_RATIO = 0.5

_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")

# Stopwords for CONTENT tokenization only. Negation words are deliberately NOT
# here — polarity is parsed from the raw token stream (see _polarity_map), so a
# "not"/"free" can never hide. Kept small + generic (English marketing glue).
_STOPWORDS: frozenset = frozenset(
    """
    a an and or the this that these those for with your you our their its it is are be to of in on at
    as by from into onto over under out up down off can will may help helps use uses used using
    made make makes designed perfect great best more most very just also all any each every when what
    how why where which while than then them they we us my me if so but yes new
    """.split()
)

# --- polarity vocabulary ----------------------------------------------------
_NEG_FOLLOW: frozenset = frozenset(
    {"not", "no", "never", "without", "cannot", "avoid", "excludes", "exclude", "minus"}
)
_NEG_WINDOW = 3  # how many following content tokens a "not"/"without" negates

# --- efficacy classification (raw words; stemmed below through _stem so the gate
# and the vocabulary can never miss on inflection — fixes the minimize->minimiz
# drift). This lexicon only CLASSIFIES a line as a claim; it is NOT the control.
# The control is: a claim publishes ONLY a matched evidence item's claim_text. So
# over-inclusion here is safe (it just forces evidence-backing), and a missed
# term still can't fabricate because descriptive quotes are whole-bullet exact.
_EFFICACY_WORDS = (
    "reduce", "reduces", "reducing", "minimize", "minimise", "minimizes", "diminish",
    "reverse", "reverses", "restore", "repair", "brighten", "brightens", "whiten",
    "lighten", "fade", "even", "evens", "soothe", "soothes", "calm", "hydrate",
    "hydrates", "moisturize", "moisturise", "plump", "firm", "tighten", "lift",
    "smooth", "smooths", "exfoliate", "clear", "clears", "heal", "heals", "treat",
    "treats", "prevent", "prevents", "protect", "defend", "nourish", "strengthen",
    "boost", "renew", "refine", "resurface", "control", "controls", "mattify",
    "rejuvenate", "revitalize", "correct", "combat", "fight", "banish", "eliminate",
    "unclog", "decongest", "balance", "improve", "enhance", "depuff", "puff",
    "puffiness", "aging", "antiaging", "wrinkle", "wrinkles", "acne", "blemish",
    "breakout", "hyperpigmentation", "redness", "eczema", "rosacea", "elasticity",
    "detoxify", "purify", "shrink", "erase", "cure", "cures", "regenerate",
)

# --- attribute-value groups (antonym / skin-type swaps). Any group member a
# bullet asserts must ALSO be in the source, else it's an invented attribute.
_ATTRIBUTE_WORDS: Tuple[Tuple[str, ...], ...] = (
    ("oily", "dry", "combination", "normal", "sensitive"),  # skin type
    ("gentle", "harsh", "strong", "mild"),                  # strength/gentleness
)

# --- grade-escalation tokens: must appear in the published source-truth text or
# the bullet is dropped. Quantified strength (digits/%/durations) is caught too.
_ESCALATION_WORDS: frozenset = frozenset({
    "clinically", "clinical", "proven", "guaranteed", "guarantee", "eliminates",
    "eliminate", "instantly", "instant", "permanent", "permanently", "dermatologist",
    "dermatologically", "medical", "cure", "cures", "powerhouse", "best", "1",
})


# ---------------------------------------------------------------------------
# Tokenization + light stemming (pure)
# ---------------------------------------------------------------------------

def _raw_cased(text: Any) -> List[str]:
    """Original-case token stream, hyphens split. Aligned 1:1 with _raw_tokens;
    used to reconstruct the published source span with its casing."""
    if not text:
        return []
    if not isinstance(text, str):
        text = str(text)
    return _TOKEN_RE.findall(text.replace("-", " "))


def _raw_tokens(text: Any) -> List[str]:
    """Lowercased token stream, hyphens split ('alcohol-free' -> alcohol, free),
    stopwords KEPT — the stream polarity + contiguity operate on."""
    return [t.lower() for t in _raw_cased(text)]


def _stem(tok: str) -> str:
    """Light suffix stemmer so brighten/brightens/brightening and
    minimize/minimizes/minimizing collapse to one key."""
    t = tok.lower()
    if t.endswith("'s"):
        t = t[:-2]
    for suf in ("ing", "edly", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            t = t[: -len(suf)]
            break
    if t.endswith("e") and len(t) > 3:
        t = t[:-1]
    return t


# Stem the classification vocabularies through the SAME stemmer the gate uses at
# runtime (requirement #5 — no more minimize->minimiz drift).
_EFFICACY_STEMS: frozenset = frozenset(_stem(w) for w in _EFFICACY_WORDS)
_ATTRIBUTE_GROUPS: Tuple[frozenset, ...] = tuple(
    frozenset(_stem(w) for w in group) for group in _ATTRIBUTE_WORDS
)


def _is_content(tok: str) -> bool:
    return len(tok) >= 3 and tok not in _STOPWORDS


def _content_stems(text: Any) -> Set[str]:
    """Stemmed content tokens (>=3 chars, non-stopword)."""
    return {_stem(t) for t in _raw_tokens(text) if _is_content(t)}


# Back-compat alias.
_content_tokens = _content_stems


def _content_stem_seq(tokens: Sequence[str]) -> List[Tuple[int, str]]:
    """(raw_index, stem) for each content token, in order."""
    return [(i, _stem(t)) for i, t in enumerate(tokens) if _is_content(t)]


def _polarity_map(text: Any) -> Dict[str, int]:
    """stem -> +1 (affirmed) / -1 (negated) / 0 (ambiguous). Parses 'X-free',
    'free of X', 'non X', and 'not/no/without/never ... X'."""
    toks = _raw_tokens(text)
    pol: Dict[str, int] = {}
    last_content: Optional[str] = None
    neg = 0

    def _set(stem: str, p: int, *, override: bool = False) -> None:
        if override:
            pol[stem] = p
            return
        prev = pol.get(stem)
        pol[stem] = p if prev is None else (p if prev == p else 0)

    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok in _NEG_FOLLOW or tok == "non":
            neg = _NEG_WINDOW
            i += 1
            continue
        if tok == "free":
            if i + 1 < len(toks) and toks[i + 1] == "of":
                neg = _NEG_WINDOW
            elif last_content is not None:
                _set(last_content, -1, override=True)
            i += 1
            continue
        if _is_content(tok):
            stem = _stem(tok)
            _set(stem, -1 if neg > 0 else 1)
            last_content = stem
            if neg > 0:
                neg -= 1
        i += 1
    return pol


# ---------------------------------------------------------------------------
# Grounding context (built once per product) + the extractive gate
# ---------------------------------------------------------------------------

def _active_labels(actives: Any) -> List[str]:
    out: List[str] = []
    for a in actives or []:
        if isinstance(a, dict) and a.get("label"):
            out.append(str(a["label"]))
        elif isinstance(a, str) and a.strip():
            out.append(a.strip())
    return out


def build_source_corpus(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    brand: Optional[str] = None,
    category_path: Optional[str] = None,
    tags: Any = None,
    actives: Any = None,
    raw_inci: Optional[str] = None,
) -> str:
    """The ground-truth text a descriptive quote must come from. Owned inputs
    only: brand-authored description, canonical title, INCI + parsed actives,
    taxonomy tags. No web, no priors."""
    parts: List[str] = []
    for v in (title, description, brand, category_path, raw_inci):
        if v and str(v).strip():
            parts.append(str(v))
    if isinstance(tags, (list, tuple)):
        parts.extend(str(t) for t in tags if t)
    elif isinstance(tags, str) and tags.strip():
        parts.append(tags)
    parts.extend(_active_labels(actives))
    return " . ".join(parts)


@dataclass
class GroundingContext:
    """Everything the extractive gate needs about one product."""

    source_stems: Set[str] = field(default_factory=set)
    source_polarity: Dict[str, int] = field(default_factory=dict)
    source_cased: List[str] = field(default_factory=list)          # original-case raw tokens
    source_content_seq: List[Tuple[int, str]] = field(default_factory=list)  # (raw_idx, stem)
    # Each substantiated evidence item: (claim_text, set-of-content-stems).
    substantiated: List[Tuple[str, Set[str]]] = field(default_factory=list)

    @property
    def has_source(self) -> bool:
        return bool(self.source_stems)


def build_context(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    brand: Optional[str] = None,
    category_path: Optional[str] = None,
    tags: Any = None,
    actives: Any = None,
    raw_inci: Optional[str] = None,
    substantiated_claims: Optional[Sequence[str]] = None,
) -> GroundingContext:
    corpus = build_source_corpus(
        title=title,
        description=description,
        brand=brand,
        category_path=category_path,
        tags=tags,
        actives=actives,
        raw_inci=raw_inci,
    )
    cased = _raw_cased(corpus)
    lowered = [t.lower() for t in cased]
    subs: List[Tuple[str, Set[str]]] = []
    for c in substantiated_claims or []:
        text = str(c).strip()
        if text:
            subs.append((text, _content_stems(text)))
    return GroundingContext(
        source_stems=_content_stems(corpus),
        source_polarity=_polarity_map(corpus),
        source_cased=cased,
        source_content_seq=[(i, _stem(t)) for i, t in enumerate(lowered) if _is_content(t)],
        substantiated=subs,
    )


def _polarity_flips(line: str, ctx: GroundingContext) -> bool:
    bpol = _polarity_map(line)
    for stem, bp in bpol.items():
        if bp == 0:
            continue
        sp = ctx.source_polarity.get(stem)
        if sp is not None and sp != 0 and sp != bp:
            return True
    return False


def _attribute_swap(bullet_stems: Set[str], ctx: GroundingContext) -> bool:
    """A skin-type / gentleness value a bullet asserts that the source doesn't."""
    for group in _ATTRIBUTE_GROUPS:
        for member in bullet_stems & group:
            if member not in ctx.source_stems:
                return True
    return False


def _escalation_violation(line: str, published_text: str) -> bool:
    """A strength-escalation token in the model line that is NOT in the published
    source-truth text (grade gate #3). Quantified strength (any digit) counts."""
    pub = set(_raw_tokens(published_text))
    for tok in _raw_tokens(line):
        if tok in _ESCALATION_WORDS or any(c.isdigit() for c in tok):
            if tok not in pub:
                return True
    return False


def _best_evidence_claim(bullet_stems: Set[str], ctx: GroundingContext) -> Optional[str]:
    """The substantiated evidence item's claim_text the bullet best matches, or
    None. Matching is against the GRADED evidence stems; the published text is
    the claim_text verbatim (never the model line)."""
    best: Optional[str] = None
    best_shared = 0
    for claim_text, claim_stems in ctx.substantiated:
        shared = bullet_stems & claim_stems
        if len(shared) >= _EFFICACY_MIN_SHARED and (len(shared) / len(bullet_stems)) >= _EFFICACY_MIN_RATIO:
            if len(shared) > best_shared:
                best, best_shared = claim_text, len(shared)
    return best


def _quote_span(line: str, ctx: GroundingContext) -> Optional[str]:
    """WHOLE-BULLET extractive check: the bullet's content stems must appear as a
    CONTIGUOUS run in the source content sequence (no rider, no stitch). Returns
    the reconstructed source span text (source-truth), or None."""
    b_seq = _content_stem_seq(_raw_tokens(line))
    b_stems = [s for _, s in b_seq]
    if len(b_stems) < _MIN_QUOTE_CONTENT:
        return None
    src = ctx.source_content_seq
    n, m = len(src), len(b_stems)
    for i in range(0, n - m + 1):
        if [s for _, s in src[i : i + m]] == b_stems:
            start_raw = src[i][0]
            end_raw = src[i + m - 1][0]
            return " ".join(ctx.source_cased[start_raw : end_raw + 1])
    return None


def evaluate_bullet(line: str, ctx: GroundingContext) -> Tuple[bool, str, Optional[str]]:
    """The extractive anti-fabrication gate. Returns (kept, reason,
    published_text). published_text is SOURCE-TRUTH — the matched evidence
    claim_text (efficacy) or the quoted source span (descriptive) — never the
    model's paraphrase."""
    bullet_stems = _content_stems(line)
    if not bullet_stems:
        return False, "empty", None

    # (a) polarity guard.
    if _polarity_flips(line, ctx):
        return False, "polarity_flip", None
    # (4) attribute-value swap guard.
    if _attribute_swap(bullet_stems, ctx):
        return False, "attribute_swap", None

    # EFFICACY: a claim-like line publishes ONLY a matched evidence claim_text.
    if bullet_stems & _EFFICACY_STEMS:
        claim_text = _best_evidence_claim(bullet_stems, ctx)
        if claim_text is None:
            return False, "efficacy_unsubstantiated", None
        # (3) grade gate: no escalation beyond what the graded claim itself says.
        if _escalation_violation(line, claim_text):
            return False, "grade_escalation", None
        return True, "efficacy_substantiated", claim_text

    # DESCRIPTIVE: whole-bullet contiguous quote of owned source.
    span = _quote_span(line, ctx)
    if span is None:
        return False, "descriptive_not_quote", None
    if _escalation_violation(line, span):
        return False, "grade_escalation", None
    return True, "descriptive_quote", span


def is_grounded(line: str, ctx: GroundingContext) -> bool:
    """Boolean form of the extractive gate (back-compat name)."""
    return evaluate_bullet(line, ctx)[0]


def _clean_lines(raw: Any, *, cap: int) -> List[str]:
    """Normalize a model list into clean, deduped, length-bounded candidate lines."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        line = re.sub(r"\s+", " ", item).strip().lstrip("-•*").strip()
        if not (_MIN_LINE_CHARS <= len(line) <= _MAX_LINE_CHARS):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= cap * 2:  # room to drop ungrounded before hitting the cap
            break
    return out


@dataclass
class DecisionCopy:
    """The authored dossier copy for one canonical, plus a grounding audit. The
    published bullets/scenarios are SOURCE-TRUTH text, not model paraphrase."""

    bullet_points: List[str] = field(default_factory=list)
    usage_scenarios: List[str] = field(default_factory=list)
    dropped: List[Dict[str, str]] = field(default_factory=list)  # {line, reason}
    generated: bool = False

    def is_publishable(self) -> bool:
        return len(self.bullet_points) >= _MIN_BULLETS


# ---------------------------------------------------------------------------
# LLM generator (dependency-injected — DeepSeek closed-context idiom)
# ---------------------------------------------------------------------------

GenerateFn = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]

_SYSTEM_PROMPT = (
    "You SELECT decision-support copy for a shopping product record — you never "
    "assert anything new. You are given the product's own text and a list of its "
    "SUBSTANTIATED CLAIMS. Rules: "
    "(1) For any benefit/efficacy point, COPY one of the SUBSTANTIATED CLAIMS "
    "exactly; if none are provided, emit no efficacy points. "
    "(2) For descriptive points, QUOTE a contiguous phrase that appears verbatim "
    "in the DESCRIPTION — do not paraphrase, combine, or add words. "
    "(3) Never add strength words (clinically, proven, guaranteed, instantly, "
    "percentages, durations) or invert meaning (no 'with alcohol' for "
    "'alcohol-free'; no skin-type swaps). "
    "The system republishes source-truth and drops anything not verbatim. "
    'Return JSON only: {"bullet_points": ["..."], "usage_scenarios": ["..."]}'
)


def build_prompt(
    *,
    title: Optional[str],
    brand: Optional[str],
    category_path: Optional[str],
    description: Optional[str],
    actives: Any = None,
    raw_inci: Optional[str] = None,
    substantiated_claims: Optional[Sequence[str]] = None,
) -> str:
    active_labels = _active_labels(actives)
    lines = [
        f"BRAND: {brand or ''}",
        f"TITLE: {title or ''}",
        f"CATEGORY: {category_path or ''}",
    ]
    if active_labels:
        lines.append("KEY ACTIVES: " + ", ".join(active_labels))
    elif raw_inci:
        lines.append("INGREDIENTS (INCI): " + str(raw_inci)[:800])
    subs = [str(c).strip() for c in (substantiated_claims or []) if str(c).strip()]
    if subs:
        lines.append("SUBSTANTIATED CLAIMS (copy these verbatim for efficacy):")
        lines.extend(f"  - {c}" for c in subs[:12])
    else:
        lines.append("SUBSTANTIATED CLAIMS: (none — emit NO efficacy points)")
    lines.append("DESCRIPTION (quote verbatim for descriptive points):")
    lines.append((description or "").strip()[:2400] or "(none)")
    return "\n".join(lines)


async def _call_deepseek(prompt: str, *, timeout_s: float = 30.0) -> Optional[Dict[str, Any]]:
    """One DeepSeek closed-context call (repo idiom:
    official_match_judge._call_deepseek_judge). Returns parsed JSON or None; never
    raises."""
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        logger.warning("decision_intelligence.no_api_key")
        return None
    base_url = settings.deepseek_api_base_url.rstrip("/")
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 900,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("decision_intelligence.transport_fail err=%s", exc)
        return None
    if resp.status_code >= 400:
        logger.warning(
            "decision_intelligence.http_%d body=%s", resp.status_code, resp.text[:200]
        )
        return None
    try:
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("decision_intelligence.parse_fail err=%s", exc)
        return None


async def author_decision_copy(
    *,
    title: Optional[str],
    description: Optional[str],
    brand: Optional[str] = None,
    category_path: Optional[str] = None,
    tags: Any = None,
    actives: Any = None,
    raw_inci: Optional[str] = None,
    substantiated_claims: Optional[Sequence[str]] = None,
    generate_fn: Optional[GenerateFn] = None,
) -> DecisionCopy:
    """Author grounded bullet_points + usage_scenarios for one canonical.

    The model SELECTS candidates; the extractive gate republishes SOURCE-TRUTH
    (matched evidence claim_text / quoted source span) and drops anything else.
    Side-effect-free; the caller persists. Returns an empty (non-publishable)
    DecisionCopy when there's no source or nothing usable — never fabricates.
    """
    ctx = build_context(
        title=title,
        description=description,
        brand=brand,
        category_path=category_path,
        tags=tags,
        actives=actives,
        raw_inci=raw_inci,
        substantiated_claims=substantiated_claims,
    )
    if not ctx.has_source:
        return DecisionCopy()

    call = generate_fn or _call_deepseek
    prompt = build_prompt(
        title=title,
        brand=brand,
        category_path=category_path,
        description=description,
        actives=actives,
        raw_inci=raw_inci,
        substantiated_claims=substantiated_claims,
    )
    parsed = await call(prompt)
    if not isinstance(parsed, dict):
        return DecisionCopy(generated=False)

    result = DecisionCopy(generated=True)
    for raw_key, cap, sink in (
        ("bullet_points", _MAX_BULLETS, result.bullet_points),
        ("usage_scenarios", _MAX_SCENARIOS, result.usage_scenarios),
    ):
        seen_pub: Set[str] = set()
        for line in _clean_lines(parsed.get(raw_key), cap=cap):
            if len(sink) >= cap:
                break
            kept, reason, published = evaluate_bullet(line, ctx)
            if kept and published:
                key = published.strip().lower()
                if key in seen_pub:
                    continue  # two model lines mapped to one source-truth item
                seen_pub.add(key)
                sink.append(published)
            else:
                result.dropped.append({"line": line, "reason": reason})
    return result

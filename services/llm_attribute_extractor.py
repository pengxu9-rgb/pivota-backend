"""LLM attribute-extractor fallback (Phase 2 of the multi-vertical architecture).

`sku_sidewalk`'s attribute graph is populated by beauty lexicons. For a niche
category with no lexicon (audio: "bone conduction", "IP68", "open-ear") those
lexicons yield nothing, so the attribute/constraint probe axis — precisely where a
niche brand competes — is dead. This module is the cross-vertical fallback: it
asks an LLM to read the product's own copy and emit attributes into the SAME
generic `ATTRIBUTE_CLASSES`, then GROUNDS each one against the source text before
it may seed a probe.

The load-bearing part is NOT the LLM plumbing — it is the **groundedness guard**
(`ground_extracted_attributes`). An extracted attribute seeds a real audit probe;
a hallucinated "IP68" becomes a confidently-wrong report — the exact failure the
`coverage_unavailable` honesty gates exist to prevent. So every extracted
attribute must carry a verbatim source SPAN, and:
  * the span must be locatable in the product's own source text, and
  * the attribute value must be anchored in that span (its distinctive tokens
    appear there), so the model can't attach a real span to an invented value.
Anything that fails is discarded, never probed. Over-discarding a real attribute
is acceptable; probing a hallucinated one is not.

Lexicon-first: beauty keeps its deterministic lexicon as the fast path — the LLM
runs only when the profile says `llm_extractor` (electronics/generic) or the
lexicon found nothing (the thin-Korean-SKU case from PR #1126). Pure functions
here are unit-tested; the async entry point takes an injectable `synthesize` so
tests never hit a network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from services.llm_fence import PRODUCT_DATA_FENCE
from services.llm_io import parse_llm_object
from services.promo_terms import is_promo_term
from services.sku_sidewalk import ATTRIBUTE_CLASSES, _iter_product_sources

logger = logging.getLogger(__name__)

# Classes the extractor may emit into. Deliberately the EXISTING generic classes —
# only the lexicons filling them were beauty. For hard goods, `ingredient` carries
# a "key spec" sense (battery hours, storage, driver size, waterproof rating).
_EXTRACTABLE_CLASSES = tuple(c for c in ATTRIBUTE_CLASSES if c not in {"offer_variant"})

# Tokens too generic to anchor a value in its span on their own.
_ANCHOR_STOPWORDS = frozenset({
    "for", "with", "the", "and", "a", "an", "of", "to", "in", "on", "or",
    "your", "you", "it", "is", "are", "this", "that", "up", "pro", "plus",
})

_LLM_SOURCE_LABEL = "llm_extracted"


@dataclass(frozen=True)
class GroundedAttribute:
    """An extracted attribute that PASSED the groundedness guard. `span` is the
    verbatim source text that substantiates it — retained for the durable cache
    and honesty auditing, never discarded."""

    class_name: str
    value: str
    span: str


def _normalize(text: Any) -> str:
    """Lowercase; collapse every non-alphanumeric run to a single space. Used for
    both the source text and the span so matching is punctuation/spacing-robust
    ('IP68-rated, waterproof' contains 'ip68 waterproof')."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _tokens(text: Any) -> List[str]:
    return [t for t in _normalize(text).split(" ") if t]


def build_source_text(product: Mapping[str, Any]) -> str:
    """The product's own copy the extractor reads AND the guard checks against —
    the SAME sources the lexicon path uses, so grounding is consistent with it."""
    parts = [text for _source, text in _iter_product_sources(product) if text]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Durable cache (Phase 2b-2): serialize grounded attributes + fingerprint the
# source copy so a product edit invalidates a stale cache (a stale span must
# never seed a probe for a spec the edited page no longer states).
# --------------------------------------------------------------------------- #

def source_fingerprint(source_text: str) -> str:
    """Stable hash of the NORMALIZED source copy — the cache key. Normalized so a
    whitespace/punctuation-only edit doesn't needlessly bust the cache, but any
    real copy change does."""
    return hashlib.sha1(_normalize(source_text).encode("utf-8")).hexdigest()


def serialize_grounded(grounded: Sequence[GroundedAttribute]) -> List[Dict[str, str]]:
    return [
        {"class_name": g.class_name, "value": g.value, "span": g.span}
        for g in grounded
    ]


def deserialize_grounded(items: Any) -> List[GroundedAttribute]:
    """Reconstruct grounded attributes from a cached payload. Light validation
    only — the cache is used ONLY when its source_hash matches the current copy,
    so the spans are still present in the source; no need to re-run the guard."""
    out: List[GroundedAttribute] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        class_name = str(item.get("class_name") or "").strip()
        value = str(item.get("value") or "").strip()
        span = str(item.get("span") or "").strip()
        if class_name in _EXTRACTABLE_CLASSES and value and span:
            out.append(GroundedAttribute(class_name=class_name, value=value, span=span))
    return out


# --------------------------------------------------------------------------- #
# The groundedness guard (the real Phase-2 work) — a pure function.
# --------------------------------------------------------------------------- #

def ground_extracted_attributes(
    items: Sequence[Mapping[str, Any]],
    source_text: str,
) -> List[GroundedAttribute]:
    """Keep ONLY attributes whose claimed source span is real and whose value is
    anchored in that span. Drops (never probes) anything hallucinated.

    Each `item` is {class_name|class, value, span}. An item survives iff:
      1. its class is an extractable ATTRIBUTE_CLASS,
      2. value and span are non-empty and value is not a promo term,
      3. the normalized span is a substring of the normalized source text
         (the model quoted real page copy), and
      4. every distinctive (non-stopword) token of the value appears in the span
         (the value is what that span actually says, not a smuggled invention).
    Order-preserving; de-duplicated on (class, value).
    """
    source_norm = _normalize(source_text)
    out: List[GroundedAttribute] = []
    seen = set()
    if not source_norm:
        return out
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        class_name = str(item.get("class_name") or item.get("class") or "").strip()
        value = str(item.get("value") or "").strip()
        span = str(item.get("span") or "").strip()
        if class_name not in _EXTRACTABLE_CLASSES or not value or not span:
            continue
        if is_promo_term(_normalize(value)):
            continue
        span_norm = _normalize(span)
        if not span_norm or span_norm not in source_norm:
            continue  # hallucinated / unquoted span
        span_tokens = set(span_norm.split(" "))
        value_anchor = [t for t in _tokens(value) if t not in _ANCHOR_STOPWORDS]
        if not value_anchor:
            continue  # value is all stopwords — nothing to anchor
        if not all(t in span_tokens for t in value_anchor):
            continue  # value drifted from its own span
        key = (class_name, value.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(GroundedAttribute(class_name=class_name, value=value, span=span))
    return out


# --------------------------------------------------------------------------- #
# Lexicon-first gating.
# --------------------------------------------------------------------------- #

# Classes whose emptiness means "the lexicon found nothing usable" — the buyer-
# facing attribute axes (not the structural offer/proof classes).
_MEANINGFUL_CLASSES = (
    "category", "format", "ingredient", "certification_constraint",
    "audience", "use_case", "scenario", "exclusion",
)


def _graph_attribute_count(graph: Mapping[str, Any]) -> int:
    classes = graph.get("classes") if isinstance(graph, Mapping) else None
    if not isinstance(classes, Mapping):
        return 0
    return sum(
        len(classes.get(name) or [])
        for name in _MEANINGFUL_CLASSES
    )


def should_run_extractor(profile: Any, graph: Mapping[str, Any]) -> bool:
    """Lexicon-first: run the LLM only when the profile mandates it (electronics /
    generic use `llm_extractor`) OR the lexicon path produced no usable attribute
    (thin-SKU). Beauty with lexicon hits never pays for the LLM."""
    if getattr(profile, "attribute_strategy", "lexicon_first") == "llm_extractor":
        return True
    return _graph_attribute_count(graph) == 0


# --------------------------------------------------------------------------- #
# Merge into an existing attribute graph.
# --------------------------------------------------------------------------- #

def merge_grounded_into_graph(
    graph: Dict[str, Any],
    grounded: Sequence[GroundedAttribute],
) -> Dict[str, Any]:
    """Add grounded attributes into a `build_sku_attribute_graph` result, reusing
    its class buckets + evidence map. Provenance is `llm_extracted` so rendering
    can distinguish LLM-derived lanes from lexicon/structured-data ones. A value
    the lexicon already found is left as-is (its provenance wins)."""
    classes = graph.setdefault("classes", {})
    evidence = graph.setdefault("evidence", {})
    llm_spans = graph.setdefault("llm_extracted_spans", {})
    for attr in grounded:
        cleaned = _clean_value(attr.value)
        if not cleaned or is_promo_term(_normalize(cleaned)):
            continue
        bucket = classes.setdefault(attr.class_name, [])
        if cleaned not in bucket:
            bucket.append(cleaned)
        evidence.setdefault(cleaned, _LLM_SOURCE_LABEL)
        # Retain the substantiating span for the durable cache / honesty audit.
        llm_spans.setdefault(cleaned, attr.span)
    return graph


def _clean_value(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


# --------------------------------------------------------------------------- #
# Prompt + async entry point.
# --------------------------------------------------------------------------- #

_EXTRACTION_SYSTEM_PROMPT = """You extract structured shopping attributes from a product's OWN page copy for a
commerce search-visibility audit. You are NOT a copywriter and you NEVER guess.

Output ONLY attributes that are STATED in the TEXT below. For every attribute you
MUST quote the exact verbatim SPAN of the text it came from — copy the characters,
do not paraphrase. If a fact is not in the text, omit it. Inference, category lore,
and "typical for this kind of product" are forbidden — an attribute with no
verbatim span will be discarded.

Classes (use these names exactly; omit any with no evidence):
- category: the buyer-facing product category ("bone conduction headphones").
- format: the form factor / physical style ("open-ear", "in-ear", "stick").
- ingredient: for hard goods this is a KEY SPEC — battery life, storage, driver,
  wireless range, water rating expressed as a spec ("32GB storage", "15-hour battery").
- certification_constraint: a rating / standard / certification ("IP68", "Bluetooth 5.3").
- audience: who it is explicitly for ("swimmers", "runners").
- use_case: what it is explicitly for ("swimming", "workouts").
- scenario: an occasion / setting stated in the copy ("in the pool", "commuting").
- proof: an evidenced proof stated in the copy ("award-winning", "10,000 reviews").
- exclusion: an explicit exclusion ("no dangling wires", "sweatproof, not for diving").

Return STRICT JSON: {"attributes": [{"class_name": "...", "value": "plain buyer term",
"span": "exact verbatim substring from TEXT"}, ...]}. No prose, no markdown."""


def sanitize_source_text(source_text: str) -> str:
    """The page copy as the model will read it. The groundedness guard must check
    spans against THIS string, not the raw copy: sanitizing removes invisible
    characters and folds lookalike forms, so a span the model quotes faithfully
    from the fenced text can fail to be a substring of the raw text."""
    return PRODUCT_DATA_FENCE.sanitize_text(source_text)


def build_extraction_prompt(source_text: str) -> tuple[str, str]:
    # Sanitizing is idempotent, so a caller that already sanitized (to share the
    # string with the guard) gets the same bytes back. No cap here: the lane has
    # none today and the guard bounds what the output can claim.
    fenced = PRODUCT_DATA_FENCE.fence_text(source_text, max_chars=None)
    system = f"{_EXTRACTION_SYSTEM_PROMPT}\n\n{PRODUCT_DATA_FENCE.notice}"
    return system, f"TEXT:\n{fenced}"


def _parse_attributes(raw_text: str) -> List[Mapping[str, Any]]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    data = parse_llm_object(text, label="llm_attribute_extractor")
    if data is None:
        # Truncated/partial JSON (e.g. the response hit the output-token cap
        # mid-array). Rather than drop EVERYTHING, salvage the complete attribute
        # objects that did arrive. Defense-in-depth behind the raised token cap.
        return _salvage_attribute_objects(text)
    attrs = data.get("attributes")
    return [a for a in attrs if isinstance(a, Mapping)] if isinstance(attrs, list) else []


def _salvage_attribute_objects(text: str) -> List[Mapping[str, Any]]:
    """Best-effort recovery of complete `{...}` attribute objects from a
    truncated JSON array. String-aware brace balancing (so a brace inside a span
    value doesn't confuse the scan); the severed trailing object is dropped."""
    m = re.search(r'"attributes"\s*:\s*\[', text)
    if not m:
        return []
    scan = text[m.end():]
    out: List[Mapping[str, Any]] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(scan):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = json.loads(scan[start : i + 1])
                    except (json.JSONDecodeError, ValueError):
                        obj = None
                    if isinstance(obj, Mapping):
                        out.append(obj)
                    start = -1
        elif ch == "]" and depth == 0:
            break
    return out


async def extract_attributes(
    product: Mapping[str, Any],
    *,
    synthesize: Callable[..., Awaitable[Mapping[str, Any]]],
    provider: str,
    model: str,
    max_tokens: int = 4000,
    source_text: Optional[str] = None,
) -> List[GroundedAttribute]:
    """Run the extractor for one SKU and return ONLY grounded attributes.

    `synthesize` is injected (same shape as services.llm_synthesis.synthesize) so
    this is unit-testable without a network. Returns [] on empty copy, an empty/
    malformed model response, or a transport error (honest under-fill — never a
    fabricated attribute)."""
    text = source_text if source_text is not None else build_source_text(product)
    # One sanitized string feeds both the prompt and the guard below.
    text = sanitize_source_text(text)
    if not text.strip():
        return []
    system, user = build_extraction_prompt(text)
    try:
        result = await synthesize(
            system=system, user=user, provider=provider, model=model, max_tokens=max_tokens
        )
    except Exception as exc:  # transport / provider error -> honest empty
        logger.warning("attribute extractor synthesize failed: %s", exc)
        return []
    raw = result.get("text") if isinstance(result, Mapping) else None
    finish_reason = result.get("finish_reason") if isinstance(result, Mapping) else None
    parsed = _parse_attributes(raw or "")
    grounded = ground_extracted_attributes(parsed, text)
    if (raw or "").strip() and not grounded:
        # A non-empty model response that yielded ZERO grounded attributes. Split
        # by finish_reason so the extractor can never again be a SILENT no-op (the
        # bug that hid the 900-token truncation) without spamming at scale:
        #  - truncated (finish_reason length/MAX_TOKENS) = a real problem, the cap
        #    is too small -> WARNING;
        #  - otherwise = an ordinary guard-discard (the model paraphrased the
        #    spans), expected on thin SKUs -> INFO.
        truncated = str(finish_reason or "").lower() in {"length", "max_tokens"}
        (logger.warning if truncated else logger.info)(
            "attribute extractor: 0 grounded attrs from a %d-char response "
            "(parsed=%d, finish_reason=%s) — %s",
            len(raw or ""), len(parsed), finish_reason,
            "TRUNCATED; raise ATTRIBUTE_EXTRACTOR_MAX_TOKENS" if truncated else "guard-discard",
        )
    return grounded

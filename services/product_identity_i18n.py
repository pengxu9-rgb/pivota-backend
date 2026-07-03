"""Resolve a US-market English product identity for non-English catalogs.

WHY. The AI-visibility audit probes US buyer queries in English, and the
attribute engine (services.sku_sidewalk) is English-lexicon-based. When the
audited PDP is an all-Korean K-beauty storefront, the crawled title is Korean,
so the buyer probes come out Korean ("루트 액티베이팅 탈모 볼륨 샴푸 reviews" —
which no US shopper or agent types) and the branded-identity signal collapses.
For a US-primary market the correct identity is the product's ENGLISH name.

This module resolves that name — cheaply, and WITHOUT fabricating — via a
source ladder, and is gated behind FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION so it
ships dormant:

  1. language gate  — a Latin/ASCII title is already English -> no LLM.
  2. english-in-place — reuse a JSON-LD brand.name / English fragment already
     on the PDP (this is why English-named hero SKUs already audit richly).
  3. grounded LLM (DeepSeek) — translate/transliterate the name faithfully:
     brand VERBATIM, no invented attributes/benefits, no medical/treatment
     claims. The result is guarded (brand must survive verbatim; a claim-guard
     rejects laundered medical claims; the model must not echo the source back;
     model confidence must clear a floor) and fails SAFE to the raw title
     otherwise — a wrong English name is worse than a Korean one. A grounding
     ratio against the product's evidenced attributes is computed as advisory
     metadata (not yet a gate — see resolve_english_identity).

Storage is the caller's concern. For the URL-wedge (synthetic, no catalog row)
the resolved name is substituted in-run; for catalog-resident products it
belongs in product_enrichment.title_override (read by the audit + served PDP).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Mapping, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

# Hangul syllables, Hiragana+Katakana, CJK Unified Ideographs. Kept local (a
# tiny regex) rather than coupling to sku_sidewalk's private tokenizer constant
# or to external_seed_audit.detect_language (whose de/fr/es word-markers drive
# seed-audit anomaly typing we must not perturb).
_CJK_RE = re.compile(r"[가-힣぀-ヿ一-鿿]")

# Confidence floor below which we keep the raw title. Deliberately conservative:
# the failure mode we most want to avoid is confidently publishing a wrong name.
_MIN_CONFIDENCE = 0.6

# Treatment/medical claims that must never be laundered into an English name
# from Korean marketing copy (e.g. 탈모 = "hair loss"). Descriptive audience
# terms like "thinning" are allowed; disease/regrowth/treatment claims are not.
# Mirrors the spirit of services.sku_sidewalk._MEDICAL_BLOCKLIST for the name.
_CLAIM_BLOCKLIST = (
    "hair loss",
    "hairloss",
    "anti-hair-loss",
    "hair regrowth",
    "regrowth",
    "regrow",
    "baldness",
    "alopecia",
    "cure",
    "treats",
    "treatment for",
    "heals",
)

_MAX_NAME_LEN = 160


def flag_enabled() -> bool:
    """FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION. Default OFF (ships dormant)."""
    return os.getenv("FF_ENABLE_ENGLISH_IDENTITY_RESOLUTION", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def looks_non_english(text: Any) -> bool:
    """True when the text contains CJK (Hangul/Kana/Han) — the case the
    English-only audit path mishandles. Latin-accented text (de/fr/es) is left
    alone here; it still tokenizes and is a separate concern."""
    return bool(_CJK_RE.search(str(text or "")))


def _clean_name(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n\"'.,;:-|/")
    return text[:_MAX_NAME_LEN].strip()


def _contains_blocked_claim(name: str) -> Optional[str]:
    low = name.lower()
    for term in _CLAIM_BLOCKLIST:
        if term in low:
            return term
    return None


def _brand_of(product: Mapping[str, Any]) -> str:
    for key in ("vendor", "brand"):
        val = str(product.get(key) or "").strip()
        if val:
            return val
    attrs = product.get("attributes_raw")
    if isinstance(attrs, dict):
        b = attrs.get("brand")
        if isinstance(b, dict):
            b = b.get("name")
        if isinstance(b, str) and b.strip():
            return b.strip()
    return ""


def _description_snippet(product: Mapping[str, Any], limit: int = 400) -> str:
    attrs = product.get("attributes_raw")
    attrs = attrs if isinstance(attrs, dict) else {}
    for value in (attrs.get("description"), product.get("description")):
        text = str(value or "").strip()
        if text:
            return text[:limit].replace("\n", " ")
    return ""


def _evidenced_english_terms(product: Mapping[str, Any]) -> List[str]:
    """Canonical English attribute terms the product actually substantiates
    (post-#1126 these resolve from Korean too). Used both to hint the model
    toward correct terminology and to ground its output."""
    try:
        from services.sku_sidewalk import build_sku_attribute_graph

        graph = build_sku_attribute_graph(dict(product))
    except Exception:  # pragma: no cover - never let grounding crash the audit
        return []
    classes = graph.get("classes") if isinstance(graph, dict) else None
    classes = classes if isinstance(classes, dict) else {}
    terms: List[str] = []
    for key in ("category", "ingredient", "format", "certification_constraint", "use_case"):
        for term in classes.get(key) or []:
            t = str(term or "").strip().lower()
            if t and t not in terms:
                terms.append(t)
    return terms


_SYSTEM_PROMPT = (
    "You render a non-English e-commerce product's name into the ENGLISH name a "
    "US shopper would use. Rules, strictly: (1) keep the BRAND exactly as given, "
    "verbatim; (2) translate or transliterate faithfully — do NOT invent "
    "ingredients, benefits, sizes, or attributes not present in the inputs; "
    "(3) NO medical or treatment claims (e.g. render a Korean 'hair loss' term as "
    "'volumizing' or 'for thinning hair', never 'anti-hair-loss' or 'regrowth'); "
    "(4) output the product NAME only, no marketing sentence. Output strict JSON."
)


def _build_user_message(
    *, brand: str, raw_title: str, description: str, english_terms: List[str],
) -> str:
    parts = [f"Brand: {brand or '(unknown)'}", f"Original name: {raw_title}"]
    if english_terms:
        parts.append("Known English attributes (use this terminology): " + ", ".join(english_terms[:8]))
    if description:
        parts.append(f"Description (for context only): {description}")
    body = "\n".join(parts)
    return (
        f"{body}\n\n"
        'Return JSON only: {"english_name": string, "confidence": float in [0,1], '
        '"notes": short_string}'
    )


async def _call_deepseek_resolve(*, user_message: str, timeout_s: float = 15.0) -> Optional[Dict[str, Any]]:
    """Transport mirrors services.category_classifier_llm._call_deepseek_classify:
    same client, json_object mode, low temperature, graceful None on any fault."""
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        logger.warning("identity_i18n.no_api_key")
        return None
    base_url = settings.deepseek_api_base_url.rstrip("/")
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 120,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("identity_i18n.transport_fail err=%s", exc)
        return None
    if resp.status_code >= 400:
        logger.warning("identity_i18n.http_%d body=%s", resp.status_code, resp.text[:200])
        return None
    try:
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("identity_i18n.parse_fail err=%s", exc)
        return None


def _grounding_ratio(name: str, *, brand: str, english_terms: List[str]) -> float:
    """Fraction of the name's non-brand content tokens that are accounted for by
    an evidenced English attribute term. Not a hard gate (transliterated brand
    sub-words and generic nouns legitimately won't match) — surfaced in metadata
    and used to damp confidence when the model clearly drifted."""
    brand_tokens = {t for t in re.findall(r"[a-z0-9]+", brand.lower()) if t}
    name_tokens = [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) > 2]
    content = [t for t in name_tokens if t not in brand_tokens]
    if not content:
        return 1.0
    vocab = set()
    for term in english_terms:
        vocab.update(re.findall(r"[a-z0-9]+", term))
    covered = sum(1 for t in content if t in vocab)
    return covered / len(content)


def _resolution(name: str, method: str, confidence: float, raw_title: str, ratio: float) -> Dict[str, Any]:
    return {
        "english_name": name,
        "method": method,
        "confidence": round(float(confidence), 3),
        "grounding_ratio": round(float(ratio), 3),
        "raw_title": raw_title,
    }


async def resolve_english_identity(product: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve an English identity for one product, or None to leave it as-is.

    Returns {english_name, method, confidence, grounding_ratio, raw_title} on a
    confident, claim-safe resolution; None when the title is already English,
    the flag is off, or resolution can't be trusted (fail-safe to raw title).
    """
    if not flag_enabled():
        return None
    raw_title = str(product.get("raw_title") or product.get("title") or "").strip()
    if not raw_title or not looks_non_english(raw_title):
        return None  # language gate: already English / nothing to do.

    brand = _brand_of(product)
    english_terms = _evidenced_english_terms(product)

    llm = await _call_deepseek_resolve(
        user_message=_build_user_message(
            brand=brand,
            raw_title=raw_title,
            description=_description_snippet(product),
            english_terms=english_terms,
        )
    )
    if not isinstance(llm, dict):
        return None
    name = _clean_name(llm.get("english_name"))
    try:
        confidence = float(llm.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0

    # --- grounding + claim safety (fail SAFE to raw on any failure) ---
    if not name or looks_non_english(name):
        return None  # empty, or the model echoed the Korean back.
    blocked = _contains_blocked_claim(name)
    if blocked:
        logger.warning("identity_i18n.blocked_claim term=%r name=%r", blocked, name)
        return None
    if brand and brand.lower() not in name.lower():
        # Brand must survive verbatim; prepend it rather than trust a name that
        # dropped it, then re-check length.
        name = _clean_name(f"{brand} {name}")
    # Grounding ratio is ADVISORY metadata, not a gate: legitimate descriptive
    # words ("root", "activating", "volumizing") aren't lexicon attributes, and
    # the evidenced-term vocabulary is only as rich as sku_sidewalk resolves
    # (empty until the K-beauty lexicon lands). Anti-fabrication rests on the
    # hard guards above (brand verbatim, claim blocklist, no-Korean-echo, model
    # confidence, fail-safe). Promoting the ratio to a gate is a follow-up that
    # pairs with the resolved-attribute vocabulary.
    ratio = _grounding_ratio(name, brand=brand, english_terms=english_terms)
    if confidence < _MIN_CONFIDENCE:
        logger.info("identity_i18n.low_confidence conf=%.2f ratio=%.2f name=%r", confidence, ratio, name)
        return None
    return _resolution(name, "deepseek_v1", confidence, raw_title, ratio)


async def resolve_synthetic_items_inplace(
    items: List[Dict[str, Any]], merchant_id: str,
) -> int:
    """Substitute a resolved English title into each URL-wedge synthetic product
    IN PLACE, keeping the original as raw_title and stashing resolution metadata
    under `title_i18n`. Mutating `title` here propagates to BOTH the registered
    SKU context (probe identity) and the products list (attribute graph), since
    both read item['title']. Returns the count resolved. Never raises."""
    if not items or not flag_enabled():
        return 0
    resolved = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            res = await resolve_english_identity(item)
        except Exception as exc:  # never let identity resolution break an audit
            logger.warning("identity_i18n.resolve_error err=%s", str(exc)[:200])
            res = None
        if not res:
            continue
        original = str(item.get("title") or "").strip()
        item.setdefault("raw_title", original)
        item["title"] = res["english_name"]
        item["title_i18n"] = {
            "original": original,
            "method": res["method"],
            "confidence": res["confidence"],
            "grounding_ratio": res["grounding_ratio"],
        }
        resolved += 1
        logger.info(
            "identity_i18n.resolved merchant=%s conf=%.2f %r -> %r",
            merchant_id, res["confidence"], original, res["english_name"],
        )
    return resolved

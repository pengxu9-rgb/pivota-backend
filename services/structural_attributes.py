"""Structural-depth attribute extraction for catalog_products.llm_attributes
(Fix Plan G — T1).

Goal: make the index citable at DEPTH by frontier agents. Post Fix-Plan-B the
catalog has a resolved vertical on every row, but the per-SKU attribute payload a
matching agent needs ("is this for oily skin? does it have niacinamide? what's
the SPF / volume?") is empty — `catalog_products.llm_attributes` is 0 rows.

This module builds that payload for the beauty vertical (98.6% of the live
catalog) under a strict DETERMINISTIC-FIRST discipline:

  1. Deterministic pass (NO LLM, NO network) resolves every field it honestly
     can by RECONCILING existing signal — the already-computed
     `beauty_sku_ingredients.active_ingredients_json`, `beauty_product_profiles
     .concerns_json`, crawled `seed_data` INCI, and the shared regex/lexicon
     extractors (`skincare_attributes`, `haircare_attributes`,
     `beauty_enrichment`). We never regenerate what the catalog already knows.

  2. LLM residual pass fills ONLY the judgment fields the deterministic pass could
     not resolve (skin_type, texture, finish, and concerns when no category
     lexicon applied). The residual set is per-SKU and usually small, which is
     what keeps the full-run LLM cost bounded.

The output is a VERSIONED envelope (``schema_version``) written additively to
``catalog_products.llm_attributes``. The column is otherwise the (flag-gated,
serving-invisible) grounded-span extractor cache; our envelope is distinguished
by ``schema_version`` and only ever written where the column is NULL/'{}' (see
scripts/backfill_llm_attributes.py), so it never clobbers that cache.

Known systemic failure this guards against: an LLM response that overflows the
output-token cap truncates its JSON, the parse silently returns nothing, and the
feature is quietly dead. We (a) cap max_tokens, (b) request ONLY the small
residual field set (never a spec-sheet dump), (c) parse through the single
sanctioned tolerant parser (services.llm_io), and (d) surface a typed
``LLMResidualOutcome`` so the runner can COUNT parse failures and fail loudly
above a threshold.

Pure functions are unit-tested; the async residual entry point takes an
injectable ``synthesize`` so tests never hit the network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from services import haircare_attributes
from services.beauty_enrichment import (
    SOURCE_INCI,
    SOURCE_TEXT,
    extract_key_actives,
    infer_concerns,
)
from services.category_kind import resolve_category_kind
from services.claim_safety import CATEGORY_HAIRCARE, CATEGORY_SKINCARE
from services.llm_io import parse_llm_object
from services.skincare_attributes import (
    detect_fragrance_free,
    extract_format as extract_skincare_format,
    extract_spf_value,
    merge_concentration_into_actives,
)

# The envelope version. BUMP this (never rewrite an old one in place) when the
# field set or extraction semantics change, so a re-run can tell a stale payload
# from a current one and a reader can branch on it.
SCHEMA_VERSION = "structural_depth.beauty.v1"

# The buyer-facing beauty attribute axes (Fix Plan G scope). These are a NEW
# additive schema — they are NOT VerticalProfile fields and MUST NOT be confused
# with the immutable probe-spec ``axis`` key. Order is stable (report + prompt).
CORE_FIELDS: tuple = (
    "skin_type", "concerns", "key_ingredients", "texture", "finish", "spf", "volume",
)
# Extra deterministic flags carried alongside the core 7 (bonus signal, all
# regex/lexicon-derived, never LLM). Additive; a reader ignores unknown keys.
BONUS_FIELDS: tuple = (
    "format", "fragrance_free", "sulfate_free", "silicone_free",
    "vegan_status", "cruelty_free_status",
)

# Fields the LLM may be asked to fill — ONLY the judgment axes. A hard spec (spf,
# volume, ingredients) is NEVER sent to the LLM; determinism owns those.
LLM_RESIDUAL_FIELDS: tuple = ("skin_type", "concerns", "texture", "finish")

# Closed vocabularies for the residual judgment fields. The prompt pins these and
# `_coerce_residual` drops anything off-list, so the LLM cannot invent a value.
SKIN_TYPE_VOCAB: tuple = (
    "dry", "oily", "combination", "normal", "sensitive", "all skin types",
    "acne-prone", "mature",
)
TEXTURE_VOCAB: tuple = (
    "gel", "cream", "lotion", "serum", "oil", "balm", "foam", "mist",
    "lightweight", "rich", "watery", "milky", "powder", "clay", "sheet",
)
FINISH_VOCAB: tuple = (
    "matte", "dewy", "natural", "satin", "glossy", "radiant", "velvet", "sheer",
)
# Concerns the residual pass may emit come from the same cosmetic-FIT vocabulary
# the deterministic `infer_concerns` uses; we reuse its label space rather than
# invent a parallel one.
_CONCERN_VOCAB_LABELS: frozenset = frozenset({
    "dryness", "dullness", "acne-prone", "oiliness", "sensitivity", "aging",
    "pores", "hyperpigmentation", "uneven texture", "damage", "color-treated",
    "scalp", "frizz", "volume", "redness",
})


# --------------------------------------------------------------------------- #
# Volume — a new deterministic extractor (the one core field with no existing
# helper). Matches a size measure in the title/description.
# --------------------------------------------------------------------------- #

# Unit -> canonical spelling. Ordered longest-first in the pattern so "fl oz"
# wins over "oz".
_VOLUME_RE = re.compile(
    r"(?<![\w.])(\d{1,4}(?:\.\d{1,2})?)\s*"
    r"(ml|milliliters?|millilitres?|fl\s*\.?\s*oz|fluid\s+ounces?|oz|ounces?|"
    r"g|grams?|gr|l|liters?|litres?)\b",
    re.IGNORECASE,
)
_VOLUME_UNIT_CANON = {
    "ml": "ml", "milliliter": "ml", "milliliters": "ml", "millilitre": "ml",
    "millilitres": "ml",
    "fl oz": "fl oz", "floz": "fl oz", "fluid ounce": "fl oz", "fluid ounces": "fl oz",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "g": "g", "gram": "g", "grams": "g", "gr": "g",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
}
# Plausibility bounds per canonical unit — a "2024 ml" (a year) or "500 oz"
# (implausible for beauty) is rejected so a stray number never mints a fake size.
_VOLUME_BOUNDS = {
    "ml": (1.0, 5000.0), "l": (0.01, 5.0), "fl oz": (0.1, 64.0),
    "oz": (0.1, 64.0), "g": (1.0, 5000.0),
}


def extract_volume(*texts: Any) -> Optional[str]:
    """First plausible size measure ("50 ml", "1.7 fl oz", "30 g") in the given
    texts, canonicalized, or None. Deterministic; regex only."""
    for text in texts:
        s = str(text or "")
        for m in _VOLUME_RE.finditer(s):
            raw_num, raw_unit = m.group(1), m.group(2)
            unit_key = re.sub(r"\s*\.?\s*", " ", raw_unit.lower()).strip()
            unit_key = re.sub(r"\s+", " ", unit_key)
            canon = _VOLUME_UNIT_CANON.get(unit_key)
            if not canon:
                # collapse "fl  oz" / "fl.oz" variants that lost their space
                canon = _VOLUME_UNIT_CANON.get(unit_key.replace(" ", ""))
            if not canon:
                continue
            try:
                value = float(raw_num)
            except ValueError:
                continue
            lo, hi = _VOLUME_BOUNDS.get(canon, (0.0, 1e9))
            if not (lo <= value <= hi):
                continue
            num_str = str(int(value)) if value.is_integer() else (f"{value:g}")
            return f"{num_str} {canon}"
    return None


# --------------------------------------------------------------------------- #
# Reconcile existing structured signal (do NOT regenerate).
# --------------------------------------------------------------------------- #

def _coerce_jsonish(raw: Any) -> Any:
    """A jsonb column may arrive as a Python object OR a JSON string depending on
    the driver. Return the parsed object; never raise."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def reconcile_key_ingredients(
    *,
    active_ingredients_json: Any = None,
    raw_inci: Optional[str] = None,
    seed_inci: Optional[str] = None,
    concentration_notes: Any = None,
    fallback_text: Optional[str] = None,
) -> tuple:
    """Return (ingredients, provenance) reconciling every existing active-
    ingredient source, in trust order:

      1. an already-computed ``active_ingredients_json`` (authoritative — the
         onboarding/INCI pipeline already parsed it),
      2. else parse the row's own ``raw_inci`` (INCI-verified),
      3. else the crawled ``seed_data`` INCI (INCI-verified),
      4. else a text fallback over title+description (source="text").

    Ingredients are shaped ``{label, source, concentration?}``. Never fabricates:
    an empty result is honest emptiness.
    """
    existing = _coerce_jsonish(active_ingredients_json)
    if isinstance(existing, list):
        cleaned = [a for a in existing if isinstance(a, Mapping) and a.get("label")]
        if cleaned:
            merged = merge_concentration_into_actives(
                [dict(a) for a in cleaned], _coerce_jsonish(concentration_notes)
            )
            return merged, "reconciled:beauty_sku_ingredients"

    for inci, prov in ((raw_inci, "deterministic:raw_inci"),
                       (seed_inci, "deterministic:seed_inci")):
        if inci and str(inci).strip():
            actives = extract_key_actives(
                str(inci), concentration_notes=_coerce_jsonish(concentration_notes)
            )
            if actives:
                return actives, prov

    if fallback_text and str(fallback_text).strip():
        actives = extract_key_actives(None, fallback_text=str(fallback_text))
        if actives:
            return actives, "deterministic:text_fallback"
    return [], None


def reconcile_concerns(
    *,
    concerns_json: Any = None,
    category_kind: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> tuple:
    """Return (concerns, provenance). Prefer an already-authored
    ``concerns_json`` (beauty_product_profiles); else deterministically infer
    from text via the shared category concern vocabulary (needs a known
    skincare/haircare category_kind). Empty (=> residual LLM candidate) when
    neither yields anything."""
    existing = _coerce_jsonish(concerns_json)
    if isinstance(existing, list):
        labels = [str(c).strip() for c in existing if str(c or "").strip()]
        if labels:
            return labels, "reconciled:beauty_product_profiles"
    inferred = infer_concerns(category_kind, title, description)
    if inferred:
        return inferred, "deterministic:concern_lexicon"
    return [], None


def _first_finish_from_shade(shade_json: Any) -> Optional[str]:
    """A makeup SKU's finish is authored on beauty_shades. Pull the first
    non-empty ``finish`` if a shade payload was joined in."""
    data = _coerce_jsonish(shade_json)
    if isinstance(data, Mapping):
        data = [data]
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, Mapping):
                fin = str(entry.get("finish") or "").strip()
                if fin:
                    return fin.lower()
    return None


# --------------------------------------------------------------------------- #
# Deterministic pass.
# --------------------------------------------------------------------------- #

@dataclass
class DeterministicResult:
    attributes: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, str] = field(default_factory=dict)
    residual_fields: List[str] = field(default_factory=list)
    category_kind: Optional[str] = None

    def resolved_field_count(self) -> int:
        return sum(1 for k in CORE_FIELDS if self.attributes.get(k) not in (None, [], ""))


def _text_blob(row: Mapping[str, Any]) -> str:
    return " ".join(
        str(row.get(k) or "")
        for k in ("title", "description", "product_type", "category_path")
    )


def extract_deterministic(row: Mapping[str, Any]) -> DeterministicResult:
    """Resolve every field the deterministic pass honestly can for ONE beauty
    SKU, reconciling existing signal first. Returns the resolved attributes,
    per-field provenance, and the residual judgment fields the LLM must fill.

    ``row`` is a catalog_products row optionally left-joined with beauty signal:
      title, description, product_type, category, category_path, category_kind,
      tags, active_ingredients_json, concentration_notes_json, raw_inci,
      concerns_json, seed_inci, shade_json.
    """
    title = row.get("title")
    description = row.get("description")
    product_type = row.get("product_type")
    category_path = row.get("category_path")
    text = " ".join(s for s in (str(title or ""), str(description or "")) if s)

    category_kind = row.get("category_kind") or resolve_category_kind(
        category_path=category_path, product_type=product_type, title=title,
        tags=row.get("tags"),
    )

    attrs: Dict[str, Any] = {}
    prov: Dict[str, str] = {}

    # --- key_ingredients (reconcile) ---
    ingredients, ing_prov = reconcile_key_ingredients(
        active_ingredients_json=row.get("active_ingredients_json"),
        raw_inci=row.get("raw_inci"),
        seed_inci=row.get("seed_inci"),
        concentration_notes=row.get("concentration_notes_json"),
        fallback_text=text,
    )
    if ingredients:
        attrs["key_ingredients"] = ingredients
        prov["key_ingredients"] = ing_prov

    # --- concerns (reconcile / lexicon) ---
    concerns, con_prov = reconcile_concerns(
        concerns_json=row.get("concerns_json"),
        category_kind=category_kind,
        title=title,
        description=description,
    )
    if concerns:
        attrs["concerns"] = concerns
        prov["concerns"] = con_prov

    # --- spf (deterministic) ---
    spf = extract_spf_value(title, description, product_type)
    if spf is not None:
        attrs["spf"] = spf
        prov["spf"] = "deterministic:spf_regex"

    # --- volume (deterministic) ---
    volume = extract_volume(title, description)
    if volume:
        attrs["volume"] = volume
        prov["volume"] = "deterministic:volume_regex"

    # --- format (deterministic; category-aware) ---
    if category_kind == CATEGORY_HAIRCARE:
        fmt = haircare_attributes.extract_format(title, product_type, category_path)
    else:
        fmt = extract_skincare_format(title, product_type, category_path)
    if fmt:
        attrs["format"] = fmt
        prov["format"] = "deterministic:format_lexicon"

    # --- finish (reconcile from shade only; else residual) ---
    finish = _first_finish_from_shade(row.get("shade_json"))
    if finish:
        attrs["finish"] = finish
        prov["finish"] = "reconciled:beauty_shades"

    # --- deterministic formulation flags (bonus) ---
    if detect_fragrance_free(text, product_type):
        attrs["fragrance_free"] = True
        prov["fragrance_free"] = "deterministic:claim_regex"
    if haircare_attributes.detect_sulfate_free(text, product_type):
        attrs["sulfate_free"] = True
        prov["sulfate_free"] = "deterministic:claim_regex"
    if haircare_attributes.detect_silicone_free(text, product_type):
        attrs["silicone_free"] = True
        prov["silicone_free"] = "deterministic:claim_regex"
    vegan = haircare_attributes.classify_vegan(None, text, product_type)
    if vegan:
        attrs["vegan_status"] = vegan
        prov["vegan_status"] = "deterministic:cert_lexicon"
    cruelty = haircare_attributes.classify_cruelty_free(None, text, product_type)
    if cruelty:
        attrs["cruelty_free_status"] = cruelty
        prov["cruelty_free_status"] = "deterministic:cert_lexicon"

    # --- residual judgment fields: those in LLM_RESIDUAL_FIELDS not yet resolved ---
    residual = [f for f in LLM_RESIDUAL_FIELDS if f not in attrs]

    return DeterministicResult(
        attributes=attrs, provenance=prov, residual_fields=residual,
        category_kind=category_kind,
    )


# --------------------------------------------------------------------------- #
# LLM residual pass — ONLY the unresolved judgment fields, tightly bounded.
# --------------------------------------------------------------------------- #

_RESIDUAL_SYSTEM_PROMPT = """You classify a beauty product into a FEW closed-vocabulary shopping attributes,
reading ONLY the product text provided. You are not a copywriter and you never
guess: if the text does not support a value, omit that field. Do NOT restate the
ingredient list, size, or SPF — those are handled elsewhere.

Return STRICT JSON with ONLY the requested fields, each value drawn from its
allowed list (use the exact strings; omit a field entirely if unsupported):
- skin_type: array of {skin_type_vocab} — who the product is for.
- concerns: array of {concern_vocab} — the cosmetic concerns it addresses.
- texture: one of {texture_vocab} — the product's feel/form.
- finish: one of {finish_vocab} — the look it leaves (mostly makeup).

Output shape: {{"skin_type": [...], "concerns": [...], "texture": "...",
"finish": "..."}}. No prose, no markdown, no extra keys."""


def build_residual_prompt(source_text: str, residual_fields: Sequence[str]) -> tuple:
    """(system, user) for the residual pass, listing only the requested fields'
    vocabularies. Keeping the ask small is what bounds output tokens (the
    truncation-swallow guard)."""
    system = _RESIDUAL_SYSTEM_PROMPT.format(
        skin_type_vocab=list(SKIN_TYPE_VOCAB),
        concern_vocab=sorted(_CONCERN_VOCAB_LABELS),
        texture_vocab=list(TEXTURE_VOCAB),
        finish_vocab=list(FINISH_VOCAB),
    )
    asked = ", ".join(residual_fields)
    user = f"Requested fields: {asked}\n\nPRODUCT TEXT:\n{source_text}"
    return system, user


def _coerce_str_list(raw: Any, vocab: frozenset) -> List[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    for item in raw:
        val = str(item or "").strip().lower()
        if val in vocab and val not in out:
            out.append(val)
    return out


def _coerce_scalar(raw: Any, vocab: frozenset) -> Optional[str]:
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    val = str(raw or "").strip().lower()
    return val if val in vocab else None


def coerce_residual(
    parsed: Optional[Mapping[str, Any]], residual_fields: Sequence[str],
) -> Dict[str, Any]:
    """Validate the LLM object against the closed vocabularies, keeping ONLY the
    requested residual fields with in-vocabulary values. Anything off-list or
    unrequested is dropped — the model cannot smuggle an invented value or
    overwrite a deterministic field."""
    if not isinstance(parsed, Mapping):
        return {}
    requested = set(residual_fields)
    out: Dict[str, Any] = {}
    if "skin_type" in requested:
        vals = _coerce_str_list(parsed.get("skin_type"), frozenset(SKIN_TYPE_VOCAB))
        if vals:
            out["skin_type"] = vals
    if "concerns" in requested:
        vals = _coerce_str_list(parsed.get("concerns"), _CONCERN_VOCAB_LABELS)
        if vals:
            out["concerns"] = vals
    if "texture" in requested:
        val = _coerce_scalar(parsed.get("texture"), frozenset(TEXTURE_VOCAB))
        if val:
            out["texture"] = val
    if "finish" in requested:
        val = _coerce_scalar(parsed.get("finish"), frozenset(FINISH_VOCAB))
        if val:
            out["finish"] = val
    return out


@dataclass
class LLMResidualOutcome:
    """Typed result of ONE residual LLM call so the runner can count failures.

    outcome:
      - "ok"        parsed + at least one in-vocab field returned
      - "empty"     parsed cleanly but no in-vocab field (honest no-op)
      - "truncated" finish_reason said the output hit the token cap (a real
                    problem — raise the cap) — parse yielded nothing usable
      - "parse_fail" a non-empty response that did NOT parse as JSON
      - "error"     transport/provider failure
    """

    attributes: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, str] = field(default_factory=dict)
    outcome: str = "empty"
    usage: Dict[str, Any] = field(default_factory=dict)
    model: Optional[str] = None
    raw_len: int = 0

    @property
    def is_parse_failure(self) -> bool:
        return self.outcome in ("parse_fail", "truncated")


_TRUNCATION_REASONS = frozenset({"length", "max_tokens", "maxtokens"})


async def run_llm_residual(
    row: Mapping[str, Any],
    residual_fields: Sequence[str],
    *,
    synthesize: Callable[..., Awaitable[Mapping[str, Any]]],
    provider: str,
    model: str,
    max_tokens: int = 512,
) -> LLMResidualOutcome:
    """Fill the residual judgment fields for one SKU via the shared LLM client.

    ``synthesize`` is injected (services.llm_synthesis.synthesize shape) so this
    is unit-testable without a network. Bounded output (small field set + capped
    max_tokens), tolerant parse (services.llm_io), strict vocabulary coercion,
    and a typed outcome the runner aggregates into a parse-failure rate."""
    if not residual_fields:
        return LLMResidualOutcome(outcome="empty", model=model)
    source_text = _text_blob(row).strip()
    if not source_text:
        return LLMResidualOutcome(outcome="empty", model=model)
    system, user = build_residual_prompt(source_text, residual_fields)
    try:
        result = await synthesize(
            system=system, user=user, provider=provider, model=model,
            max_tokens=max_tokens,
        )
    except Exception:  # transport/provider error -> honest empty, counted
        return LLMResidualOutcome(outcome="error", model=model)

    raw = str((result or {}).get("text") or "")
    usage = dict((result or {}).get("usage") or {})
    finish = str((result or {}).get("finish_reason") or "").lower().replace("_", "")
    parsed = parse_llm_object(raw, label="structural_depth")
    attrs = coerce_residual(parsed, residual_fields)
    prov = {k: f"llm:{model}" for k in attrs}

    if attrs:
        outcome = "ok"
    elif raw.strip() and parsed is None:
        # A non-empty response that did not parse. Split truncated vs plain
        # parse-fail so the runner can tell "raise the cap" from "model garbage".
        outcome = "truncated" if finish in _TRUNCATION_REASONS else "parse_fail"
    elif finish in _TRUNCATION_REASONS and raw.strip():
        outcome = "truncated"
    else:
        outcome = "empty"

    return LLMResidualOutcome(
        attributes=attrs, provenance=prov, outcome=outcome, usage=usage,
        model=model, raw_len=len(raw),
    )


# --------------------------------------------------------------------------- #
# Envelope.
# --------------------------------------------------------------------------- #

def build_envelope(
    deterministic: DeterministicResult,
    residual: Optional[LLMResidualOutcome] = None,
    *,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the versioned llm_attributes envelope. Deterministic values win;
    the residual pass may only fill fields determinism left empty (coerce_residual
    already dropped anything else). Provenance records how each field was derived
    so a reader can trust deterministic/reconciled fields differently from LLM
    ones."""
    attributes = dict(deterministic.attributes)
    provenance = dict(deterministic.provenance)
    model = None
    if residual is not None:
        model = residual.model
        for key, value in residual.attributes.items():
            if key not in attributes:  # never overwrite a deterministic field
                attributes[key] = value
                provenance[key] = residual.provenance.get(key, f"llm:{model}")

    ts = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "vertical": "beauty",
        "category_kind": deterministic.category_kind,
        "generated_at": ts,
        "model": model,
        "attributes": attributes,
        "provenance": provenance,
    }

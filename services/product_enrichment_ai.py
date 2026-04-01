"""
Lightweight AI-style enrichment helpers for products.

V1 implementation does NOT call an external LLM (to keep the backend
independent of any particular provider), but implements simple,
deterministic heuristics based on StandardProduct fields:

- summary_short: short human‑readable summary
- bullet_points: 3–5 selling points
- usage_scenarios / audience_tags / topic_tags: coarse labels

Later this module can be extended to call a real LLM while keeping the
same interface.
"""

from dataclasses import dataclass, field
import os
from typing import List, Dict, Any, Optional, Tuple
import re

from models.standard_product import StandardProduct


@dataclass
class StandardProductContext:
  """Minimal context used for enrichment generation."""

  merchant_id: str
  platform: str
  platform_product_id: str
  title: str
  description_text: str
  product_type: str
  tags: List[str]
  price_value: float
  currency: str
  main_image_url: str


@dataclass
class TitleSuggestionResult:
  title_health: str = "healthy"
  suggested_title: Optional[str] = None
  suggestion_language: str = "en"
  suggestion_confidence: float = 0.0
  suggestion_rationale: Optional[str] = None
  content_gap_codes: List[str] = field(default_factory=list)
  missing_attribute_labels: List[str] = field(default_factory=list)
  facts_used: Dict[str, Any] = field(default_factory=dict)
  missing_attributes: List[str] = field(default_factory=list)
  skipped_reason: Optional[str] = None


_GENERIC_TITLE_TERMS = {
  "special", "edition", "classic", "premium", "official", "signature", "collection",
  "exclusive", "new", "latest", "arrival", "style", "design", "series",
}
_GENERIC_TITLE_PHRASES = (
  "special edition",
  "limited edition",
  "new arrival",
  "best seller",
  "hot sale",
)
_COLOR_PATTERNS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
  (("black", "jet black", "noir"), "Black"),
  (("white", "ivory"), "White"),
  (("blue", "navy", "sky blue"), "Blue"),
  (("red", "crimson", "burgundy"), "Red"),
  (("green", "olive", "mint"), "Green"),
  (("pink", "rose"), "Pink"),
  (("purple", "violet"), "Purple"),
  (("gray", "grey", "charcoal"), "Gray"),
  (("brown", "tan", "camel"), "Brown"),
  (("beige", "cream"), "Beige"),
  (("yellow", "gold"), "Yellow"),
  (("orange", "coral"), "Orange"),
)
_FEATURE_PATTERNS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
  (("air cushion", "air cushioned", "air-cushion", "max air"), "air-cushion"),
  (("waterproof", "water resistant"), "waterproof"),
  (("breathable", "ventilated"), "breathable"),
  (("lightweight", "ultralight"), "lightweight"),
  (("non-slip", "anti-slip", "slip resistant"), "non-slip"),
  (("wireless", "bluetooth"), "wireless"),
  (("hypoallergenic", "sensitive skin", "sensitive-skin"), "sensitive-skin-friendly"),
  (("fragrance free", "fragrance-free"), "fragrance-free"),
)
_AUDIENCE_PATTERNS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
  (("women", "woman", "female", "ladies"), "Women's"),
  (("men", "man", "male"), "Men's"),
  (("kids", "kid", "boys", "girls", "children", "child"), "Kids'"),
  (("unisex", "all gender", "all-gender"), "Unisex"),
)
_SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]
_LOCALE_LANGUAGE_FALLBACKS = {
  "zh": "zh-Hans",
  "ja": "ja",
  "ko": "ko",
  "fr": "fr",
  "de": "de",
  "es": "es",
  "pt": "pt",
  "it": "it",
}


def _normalize_text(value: Any) -> str:
  return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalized_lower(value: Any) -> str:
  return _normalize_text(value).lower()


def _dedupe_strings(values: List[str]) -> List[str]:
  deduped: List[str] = []
  seen = set()
  for value in values:
    normalized = _normalize_text(value)
    if not normalized:
      continue
    lowered = normalized.lower()
    if lowered in seen:
      continue
    seen.add(lowered)
    deduped.append(normalized)
  return deduped


def _coerce_string_list(value: Any) -> List[str]:
  if isinstance(value, list):
    return [str(item or "").strip() for item in value if str(item or "").strip()]
  if isinstance(value, tuple):
    return [str(item or "").strip() for item in value if str(item or "").strip()]
  if isinstance(value, str):
    normalized = _normalize_text(value)
    return [normalized] if normalized else []
  return []


def _split_compound_values(value: Any) -> List[str]:
  tokens: List[str] = []
  for raw in _coerce_string_list(value):
    parts = re.split(r"\s*(?:/|,|&|\band\b)\s*", raw, flags=re.IGNORECASE)
    cleaned_parts = [part for part in (_normalize_text(part) for part in parts) if part]
    if cleaned_parts:
      tokens.extend(cleaned_parts)
    else:
      tokens.append(raw)
  return _dedupe_strings(tokens)


def _metadata_first_value(metadata: Optional[Dict[str, Any]], *keys: str) -> Any:
  payload = metadata if isinstance(metadata, dict) else {}
  for key in keys:
    if key in payload and payload.get(key) not in (None, "", [], {}):
      return payload.get(key)
  return None


def _resolve_language_code(preferred_language: Optional[str]) -> str:
  normalized = _normalized_lower(preferred_language)
  if not normalized:
    return "en"
  primary = normalized.split("-")[0]
  return _LOCALE_LANGUAGE_FALLBACKS.get(primary, primary or "en")


def title_llm_polish_enabled() -> bool:
  return os.getenv("FF_ENABLE_PRODUCT_TITLE_LLM_POLISH", "false").lower() == "true"


def _collapse_marketing_title_terms(title: str) -> str:
  normalized = _normalize_text(title)
  if not normalized:
    return ""
  cleaned = normalized
  for phrase in _GENERIC_TITLE_PHRASES:
    cleaned = re.sub(re.escape(phrase), " ", cleaned, flags=re.IGNORECASE)
  cleaned_tokens: List[str] = []
  for token in re.split(r"\s+", cleaned):
    bare = re.sub(r"[^a-z0-9]+", "", token.lower())
    if bare and bare not in _GENERIC_TITLE_TERMS:
      cleaned_tokens.append(token)
  if not cleaned_tokens:
    return ""
  cleaned = " ".join(cleaned_tokens)
  return re.sub(r"\s+", " ", cleaned).strip()


def _extract_brand(context: StandardProductContext, product: StandardProduct) -> Optional[str]:
  for candidate in (
    product.vendor,
    _metadata_first_value(product.platform_metadata, "brand", "vendor", "brand_name"),
  ):
    normalized = _normalize_text(candidate)
    if normalized:
      return normalized
  return None


def _extract_category(context: StandardProductContext, product: StandardProduct) -> Optional[str]:
  candidates = [
    product.product_type,
    _metadata_first_value(product.platform_metadata, "product_type", "category", "category_name"),
  ]
  visible_attributes = product.visible_attributes if isinstance(product.visible_attributes, dict) else {}
  for key in ("product_category", "category", "style"):
    for label in visible_attributes.get(key, []) or []:
      candidates.append(label)
  for candidate in candidates:
    normalized = _normalize_text(candidate)
    if normalized:
      return normalized
  return None


def _extract_title_core(context: StandardProductContext, brand: Optional[str], category: Optional[str]) -> Optional[str]:
  title = _normalize_text(context.title)
  if not title:
    return None
  cleaned = title
  if brand:
    cleaned = re.sub(rf"^\s*{re.escape(brand)}\b[:\s-]*", "", cleaned, flags=re.IGNORECASE)
  cleaned = _collapse_marketing_title_terms(cleaned)
  if not cleaned:
    return None
  if category and _normalized_lower(cleaned) == _normalized_lower(category):
    return None
  return cleaned


def _extract_audience(context: StandardProductContext) -> Optional[str]:
  blob = " ".join([context.title, context.product_type, context.description_text, *context.tags]).lower()
  for variants, label in _AUDIENCE_PATTERNS:
    if any(token in blob for token in variants):
      return label
  return None


def _extract_colors(product: StandardProduct, context: StandardProductContext) -> List[str]:
  colors: List[str] = []
  visible_attributes = product.visible_attributes if isinstance(product.visible_attributes, dict) else {}
  for key in ("color", "shade"):
    for label in (visible_attributes.get(key) or []):
      colors.extend(value.title() for value in _split_compound_values(str(label or "").replace("_", " ")))
  for variant in product.variants or []:
    options = variant.options if isinstance(variant.options, dict) else {}
    for name, value in options.items():
      if "color" in _normalized_lower(name) or "colour" in _normalized_lower(name):
        colors.extend(token.title() for token in _split_compound_values(value))
    for label in variant.visible_option_labels or []:
      if str(label or "").startswith("color_"):
        colors.extend(token.title() for token in _split_compound_values(str(label or "")[len("color_"):].replace("_", " ")))
      if str(label or "").startswith("shade_"):
        colors.extend(token.title() for token in _split_compound_values(str(label or "")[len("shade_"):].replace("_", " ")))
  blob = " ".join([context.title, context.description_text, *context.tags]).lower()
  for variants, label in _COLOR_PATTERNS:
    if any(token in blob for token in variants):
      colors.append(label)
  return _dedupe_strings(colors)[:2]


def _extract_feature_labels(context: StandardProductContext, product: StandardProduct) -> List[str]:
  labels: List[str] = []
  blob = " ".join([context.title, context.description_text, " ".join(context.tags)]).lower()
  for variants, label in _FEATURE_PATTERNS:
    if any(token in blob for token in variants):
      labels.append(label)
  visible_attributes = product.visible_attributes if isinstance(product.visible_attributes, dict) else {}
  for key, values in visible_attributes.items():
    if key in {"color", "shade", "product_category"}:
      continue
    for value in values or []:
      normalized = _normalize_text(value).replace("_", " ")
      if normalized and len(normalized) <= 32:
        labels.append(normalized)
  return _dedupe_strings(labels)[:2]


def _extract_size_values(product: StandardProduct) -> List[str]:
  values: List[str] = []
  for variant in product.variants or []:
    options = variant.options if isinstance(variant.options, dict) else {}
    for name, value in options.items():
      if "size" in _normalized_lower(name):
        values.extend(_coerce_string_list(value))
    for label in variant.visible_option_labels or []:
      normalized = _normalize_text(label)
      if normalized.upper() in _SIZE_ORDER:
        values.append(normalized.upper())
      elif normalized.lower().startswith("size_"):
        values.append(normalized.split("_", 1)[1].upper())
    title = _normalize_text(variant.title)
    if title and title.lower() not in {"default", "default title"}:
      for match in re.findall(r"\b(?:XXS|XS|S|M|L|XL|XXL|XXXL|\d{2,3}(?:\.\d+)?)\b", title, flags=re.IGNORECASE):
        values.append(match.upper())
  return _dedupe_strings(values)


def _format_size_range(values: List[str]) -> Optional[str]:
  normalized = _dedupe_strings(values)
  if not normalized:
    return None
  numeric_values: List[float] = []
  for value in normalized:
    try:
      numeric_values.append(float(value))
    except Exception:
      numeric_values = []
      break
  if numeric_values:
    if len(numeric_values) == 1:
      return str(int(numeric_values[0])) if float(int(numeric_values[0])) == numeric_values[0] else str(numeric_values[0])
    minimum = min(numeric_values)
    maximum = max(numeric_values)
    if minimum == maximum:
      return str(int(minimum)) if float(int(minimum)) == minimum else str(minimum)
    def _fmt(value: float) -> str:
      return str(int(value)) if float(int(value)) == value else str(value)
    return f"{_fmt(minimum)}-{_fmt(maximum)}"
  ranked = [value.upper() for value in normalized if value.upper() in _SIZE_ORDER]
  if ranked:
    indices = sorted(_SIZE_ORDER.index(value) for value in ranked)
    return f"{_SIZE_ORDER[indices[0]]}-{_SIZE_ORDER[indices[-1]]}"
  if len(normalized) == 1:
    return normalized[0]
  return " / ".join(normalized[:3])


def _extract_material_or_ingredients(product: StandardProduct, context: StandardProductContext) -> Optional[str]:
  metadata = product.platform_metadata if isinstance(product.platform_metadata, dict) else {}
  value = _metadata_first_value(
    metadata,
    "material",
    "materials",
    "composition",
    "fabric",
    "ingredient_ids",
    "reviewed_ingredient_ids",
    "canonical_ingredient_ids",
    "active_ingredients",
  )
  values = _coerce_string_list(value)
  if values:
    if len(values) == 1:
      return values[0].replace("_", " ")
    return ", ".join(value.replace("_", " ") for value in values[:2])
  if product.ingredient_ids:
    return ", ".join(str(item or "").replace("_", " ") for item in product.ingredient_ids[:2])
  blob = " ".join([context.description_text, context.title]).lower()
  for token in ("cotton", "leather", "polyester", "nylon", "wool", "silk", "spandex", "vitamin c", "retinol", "hyaluronic acid"):
    if token in blob:
      return token.title()
  return None


def _extract_usage_signal(product: StandardProduct, context: StandardProductContext) -> Optional[str]:
  metadata = product.platform_metadata if isinstance(product.platform_metadata, dict) else {}
  value = _metadata_first_value(
    metadata,
    "usage",
    "usage_scenarios",
    "use_cases",
    "how_to_use",
    "ideal_for",
    "fit_notes",
    "size_guide",
  )
  values = _coerce_string_list(value)
  if values:
    return values[0]
  description_blob = " ".join([context.title, context.description_text]).lower()
  if any(
    token in description_blob
    for token in ("ideal for", "best for", "suitable for", "designed for", "perfect for", "for daily", "for commuting", "for training")
  ):
    return _normalize_text(context.description_text or context.title)[:80]
  return None


def _title_slot_coverage(title: str, slots: Dict[str, Any]) -> int:
  normalized_title = _normalized_lower(title)
  coverage = 0
  for key in ("brand", "model", "category", "audience", "size_range"):
    value = slots.get(key)
    if value and _normalized_lower(value) in normalized_title:
      coverage += 1
  for value in slots.get("colors", []) or []:
    if _normalized_lower(value) in normalized_title:
      coverage += 1
      break
  for value in slots.get("features", []) or []:
    if _normalized_lower(value) in normalized_title:
      coverage += 1
      break
  return coverage


def _compose_deterministic_title(slots: Dict[str, Any]) -> Optional[str]:
  category = slots.get("category")
  extra_slot_count = sum(
    1 for key in ("audience", "size_range", "material_or_ingredient", "usage")
    if slots.get(key)
  ) + len(slots.get("colors", []) or []) + len(slots.get("features", []) or [])
  if not category or extra_slot_count < 2:
    return None

  parts: List[str] = []
  for key in ("brand", "model", "category", "audience"):
    value = slots.get(key)
    if value:
      parts.append(value)
  if slots.get("colors"):
    parts.append("/".join(slots["colors"]))
  if slots.get("features"):
    parts.append(", ".join(slots["features"]))
  if slots.get("size_range"):
    parts.append(f"Sizes {slots['size_range']}")

  composed = _normalize_text(" ".join(parts))
  return composed or None


def _bounded_title_polish(title: str, language: str) -> str:
  normalized = _normalize_text(title)
  if not normalized:
    return normalized
  if not title_llm_polish_enabled():
    return normalized
  # Provider-backed LLM polish is intentionally feature-gated. Until a
  # provider integration is wired in, keep a deterministic fallback that
  # only normalizes separators and spacing.
  normalized = normalized.replace(" ,", ",").replace(" / ", "/")
  normalized = re.sub(r"\s+", " ", normalized)
  return normalized.strip()


def generate_title_suggestion(
  product: StandardProduct,
  *,
  preferred_language: Optional[str] = None,
) -> TitleSuggestionResult:
  context = build_context_from_standard_product(product)
  suggestion_language = _resolve_language_code(preferred_language)
  brand = _extract_brand(context, product)
  category = _extract_category(context, product)
  model = _extract_title_core(context, brand=brand, category=category)
  audience = _extract_audience(context)
  colors = _extract_colors(product, context)
  features = _extract_feature_labels(context, product)
  size_range = _format_size_range(_extract_size_values(product))
  material_or_ingredient = _extract_material_or_ingredients(product, context)
  usage = _extract_usage_signal(product, context)

  slots = {
    "brand": brand,
    "model": model,
    "category": category,
    "audience": audience,
    "colors": colors,
    "features": features,
    "size_range": size_range,
    "material_or_ingredient": material_or_ingredient,
    "usage": usage,
  }
  deterministic_title = _compose_deterministic_title(slots)
  title_coverage = _title_slot_coverage(context.title, slots)
  low_information = bool(
    not context.title
    or len(_collapse_marketing_title_terms(context.title).split()) <= 2
    or title_coverage < 2
  )

  missing_labels: List[str] = []
  content_gap_codes: List[str] = []
  if not category:
    missing_labels.append("Style / product type")
    content_gap_codes.append("missing_category_signal")
  if not audience:
    missing_labels.append("Audience / gender")
    content_gap_codes.append("missing_audience_signal")
  if not colors:
    missing_labels.append("Color")
    content_gap_codes.append("missing_color_signal")
  if not features:
    missing_labels.append("Key feature")
    content_gap_codes.append("missing_feature_signal")
  if not size_range:
    missing_labels.append("Size guidance")
    content_gap_codes.append("missing_size_guidance")
  if not material_or_ingredient:
    missing_labels.append("Material / ingredient info")
    content_gap_codes.append("missing_material_or_ingredient_info")
  if not usage:
    missing_labels.append("Usage scenario")
    content_gap_codes.append("missing_usage_scenario")
  if low_information:
    content_gap_codes.append("generic_low_information_title")

  missing_labels = _dedupe_strings(missing_labels)
  content_gap_codes = _dedupe_strings(content_gap_codes)

  if (
    low_information
    and deterministic_title
    and deterministic_title.lower() != _normalize_text(context.title).lower()
  ):
    polished = _bounded_title_polish(deterministic_title, suggestion_language)
    trusted_slot_count = sum(
      1
      for key in ("category", "audience", "size_range", "material_or_ingredient", "usage")
      if slots.get(key)
    ) + len(colors) + len(features)
    if category and trusted_slot_count >= 2:
      confidence = min(0.95, 0.45 + (trusted_slot_count * 0.08))
      return TitleSuggestionResult(
        title_health="rewrite_candidate",
        suggested_title=polished,
        suggestion_language=suggestion_language,
        suggestion_confidence=round(confidence, 2),
        suggestion_rationale="Suggested title uses verified product facts and keeps missing facts out of the copy.",
        content_gap_codes=content_gap_codes,
        missing_attribute_labels=missing_labels,
        facts_used={
          "brand": brand,
          "model": model,
          "category": category,
          "audience": audience,
          "colors": colors,
          "features": features,
          "size_range": size_range,
        },
        missing_attributes=missing_labels,
      )

  if low_information and missing_labels:
    return TitleSuggestionResult(
      title_health="needs_more_facts",
      suggested_title=None,
      suggestion_language=suggestion_language,
      suggestion_confidence=0.0,
      suggestion_rationale="Add the missing catalog facts before generating a fuller market-facing title.",
      content_gap_codes=content_gap_codes,
      missing_attribute_labels=missing_labels,
      facts_used={
        "brand": brand,
        "model": model,
        "category": category,
        "audience": audience,
        "colors": colors,
        "features": features,
        "size_range": size_range,
      },
      missing_attributes=missing_labels,
      skipped_reason="insufficient_trusted_title_facts",
    )

  if low_information:
    return TitleSuggestionResult(
      title_health="generic_low_information",
      suggested_title=None,
      suggestion_language=suggestion_language,
      suggestion_confidence=0.0,
      suggestion_rationale="The current title is thin on attributes and should be expanded once richer facts are available.",
      content_gap_codes=content_gap_codes,
      missing_attribute_labels=missing_labels,
      facts_used={
        "brand": brand,
        "model": model,
        "category": category,
      },
      missing_attributes=missing_labels,
      skipped_reason="title_needs_more_specific_attributes",
    )

  return TitleSuggestionResult(
    title_health="healthy",
    suggested_title=None,
    suggestion_language=suggestion_language,
    suggestion_confidence=0.0,
    suggestion_rationale="The current title already carries enough merchant-safe product detail for the workspace.",
    content_gap_codes=content_gap_codes,
    missing_attribute_labels=missing_labels,
    facts_used={
      "brand": brand,
      "model": model,
      "category": category,
      "audience": audience,
      "colors": colors,
      "features": features,
      "size_range": size_range,
    },
    missing_attributes=missing_labels,
  )


def generate_enrichment_draft(
  product: StandardProduct,
  *,
  preferred_language: Optional[str] = None,
) -> Tuple[Dict[str, Any], TitleSuggestionResult]:
  context = build_context_from_standard_product(product)
  title_suggestion = generate_title_suggestion(
    product,
    preferred_language=preferred_language,
  )
  summary = generate_summary(context)
  bullets = generate_bullets(context)
  usage_scenarios = classify_usage_scenarios(context)
  audience_tags = classify_audience_tags(context)
  topic_tags = classify_topic_tags(context)
  auto_confidence = compute_auto_confidence(summary, bullets, context)

  return (
    {
      "title_override": (
        title_suggestion.suggested_title
        if title_suggestion.title_health == "rewrite_candidate"
        else None
      ),
      "summary_short": summary,
      "bullet_points": bullets,
      "usage_scenarios": usage_scenarios,
      "audience_tags": audience_tags,
      "topic_tags": topic_tags,
      "llm_readability_score": max(
        float(auto_confidence or 0.0),
        float(title_suggestion.suggestion_confidence or 0.0),
      ),
    },
    title_suggestion,
  )


def _strip_html(text: str) -> str:
  if not text:
    return ""
  # Very lightweight HTML removal
  text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
  text = re.sub(r"<[^>]+>", " ", text)
  # Collapse whitespace
  text = re.sub(r"\s+", " ", text)
  return text.strip()


def build_context_from_standard_product(product: StandardProduct) -> StandardProductContext:
  return StandardProductContext(
    merchant_id=product.merchant_id,
    platform=product.platform,
    platform_product_id=product.product_id or product.id,
    title=product.title or "",
    description_text=_strip_html(product.description or "") or "",
    product_type=(product.product_type or "").strip(),
    tags=product.tags or [],
    price_value=float(product.price or 0.0),
    currency=product.currency or "USD",
    main_image_url=product.image_url or (product.images[0] if product.images else ""),
  )


def generate_summary(context: StandardProductContext) -> str:
  """
  Generate a short 1–2 sentence summary.

  Heuristic:
  - Prefer using description text if available.
  - Otherwise fall back to title.
  """
  if context.description_text:
    base = context.description_text
  else:
    base = context.title

  if not base:
    return ""

  # Take the first ~120 characters, cut at sentence boundary if possible.
  max_len = 120
  if len(base) <= max_len:
    return base

  snippet = base[: max_len + 40]  # small buffer to find a period
  # Try to end at a full stop / punctuation.
  m = re.search(r"[。.!?]", snippet)
  if m:
    return snippet[: m.end()].strip()

  return base[:max_len].strip()


def generate_bullets(context: StandardProductContext) -> List[str]:
  """
  Generate 3–5 bullets from description/title and basic attributes.
  """
  bullets: List[str] = []

  title = context.title
  if title:
    bullets.append(f"商品名称：{title}")

  if context.product_type:
    bullets.append(f"适用品类：{context.product_type}")

  if context.price_value > 0:
    bullets.append(f"当前价格约为 {context.price_value:.2f} {context.currency}。")

  desc = context.description_text
  if desc:
    # Try to split description into short sentences.
    parts = re.split(r"[。.!?]", desc)
    for part in parts:
      part = part.strip()
      if not part:
        continue
      # Avoid repeating title-like sentence
      if title and part.startswith(title):
        continue
      bullets.append(part)
      if len(bullets) >= 5:
        break

  # Ensure at least 3 bullets with generic fallbacks.
  while len(bullets) < 3:
    if len(bullets) == 0:
      bullets.append("该商品适合日常使用，满足基础需求。")
    elif len(bullets) == 1:
      bullets.append("适合作为礼物或自用，使用场景灵活。")
    else:
      bullets.append("简洁易用，上手门槛低。")

  return bullets[:8]


def classify_usage_scenarios(context: StandardProductContext) -> List[str]:
  """
  Very coarse usage scenario classification, using product_type/title.
  Values are free‑form strings but can later be restricted to enums.
  """
  text = (context.product_type + " " + context.title).lower()
  scenarios: List[str] = []

  if any(k in text for k in ["shoe", "跑鞋", "运动鞋"]):
    scenarios.extend(["日常通勤", "城市慢跑"])
  elif any(k in text for k in ["monitor", "display", "显示器"]):
    scenarios.extend(["办公", "游戏娱乐"])
  elif any(k in text for k in ["headphone", "耳机"]):
    scenarios.extend(["通勤听音", "居家娱乐"])
  else:
    scenarios.append("日常使用")

  # Deduplicate
  seen = set()
  result: List[str] = []
  for s in scenarios:
    if s not in seen:
      seen.add(s)
      result.append(s)
  return result


def classify_audience_tags(context: StandardProductContext) -> List[str]:
  text = (context.title + " " + context.product_type + " " + " ".join(context.tags)).lower()
  tags: List[str] = []
  if any(k in text for k in ["kid", "儿童", "小朋友"]):
    tags.append("儿童")
  if any(k in text for k in ["women", "女", "女士"]):
    tags.append("女性")
  if any(k in text for k in ["men", "男", "先生"]):
    tags.append("男性")
  if not tags:
    tags.append("通用用户")
  return tags


def classify_topic_tags(context: StandardProductContext) -> List[str]:
  tags: List[str] = []
  if context.price_value and context.price_value < 50:
    tags.append("高性价比")
  if context.price_value and context.price_value > 300:
    tags.append("高端定位")
  # Fallback
  if not tags:
    tags.append("标准款")
  return tags


def compute_auto_confidence(
  summary: str,
  bullets: List[str],
  context: StandardProductContext,
) -> float:
  """
  Heuristic 0–1 confidence score for auto‑generated enrichment.
  """
  score = 0.0

  # Summary length: prefer [20, 160]
  if summary:
    length = len(summary)
    if 20 <= length <= 160:
      score += 0.3
    elif length > 0:
      score += 0.15

  # Bullet count
  count = len(bullets)
  if 3 <= count <= 8:
    score += 0.3
  elif count > 0:
    score += 0.15

  # Title presence
  if context.title:
    score += 0.2

  # Basic price sanity
  if context.price_value and context.price_value > 0:
    score += 0.2

  return min(1.0, score)

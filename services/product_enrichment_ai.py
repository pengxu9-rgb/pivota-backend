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

from dataclasses import dataclass
from typing import List, Dict, Any
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


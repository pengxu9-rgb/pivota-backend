"""
Standard Product Format
Pivota 的核心价值：将多平台产品数据转换为统一标准格式
供 AI Agent 调用，无需关心底层平台差异
"""

from datetime import datetime
from enum import Enum
import re
from typing import Optional, List, Dict, Any, Tuple
import unicodedata

from pydantic import BaseModel, Field, validator, field_validator, model_validator
from utils.rich_text import rich_text_to_plain_text


_VISIBLE_BEAUTY_ATTRIBUTE_RULES: Dict[str, List[Dict[str, Any]]] = {
    "product_category": [
        {"label": "serum", "terms": ["serum", "serums"]},
        {"label": "moisturizer", "terms": ["moisturizer", "moisturizers", "moisturiser", "moisturisers"]},
        {"label": "cleanser", "terms": ["cleanser", "cleansers"]},
        {"label": "toner", "terms": ["toner", "toners"]},
    ],
    "skin_concern": [
        {"label": "sensitive_skin", "terms": ["sensitive skin", "sensitive-skin"]},
        {"label": "brightening", "terms": ["brightening", "brighten"]},
        {"label": "hydrating", "terms": ["hydrating", "hydrate", "hydration"]},
    ],
    "formula_constraint": [
        {
            "label": "fragrance_free",
            "terms": [
                "fragrance free",
                "fragrance-free",
                "free fragrance",
                "without fragrance",
                "no fragrance",
                "sin fragancia",
            ],
        },
    ],
}

_SIZE_OPTION_PATTERNS: List[Tuple[str, List[str]]] = [
    ("size_xs", ["xs", "x-small", "extra small"]),
    ("size_s", ["s", "small"]),
    ("size_m", ["m", "medium"]),
    ("size_l", ["l", "large"]),
    ("size_xl", ["xl", "x-large", "extra large"]),
    ("size_xxl", ["xxl", "2xl", "xx-large", "extra extra large"]),
]

_COLOR_OPTION_PATTERNS: List[Tuple[str, List[str]]] = [
    ("color_red", ["red"]),
    ("color_black", ["black"]),
    ("color_blue", ["blue"]),
    ("color_pink", ["pink"]),
    ("color_white", ["white"]),
    ("color_gray", ["gray", "grey"]),
]

_INGREDIENT_CANONICAL_ALIASES: Dict[str, str] = {
    "ascorbic acid": "ascorbic_acid",
    "azelaic acid": "azelaic_acid",
    "benzoyl peroxide": "benzoyl_peroxide",
    "benzoyl": "benzoyl_peroxide",
    "bpo": "benzoyl_peroxide",
    "ceramide": "ceramide_np",
    "ceramides": "ceramide_np",
    "ceramide np": "ceramide_np",
    "glycerin": "glycerin",
    "glycerine": "glycerin",
    "hyaluronic acid": "hyaluronic_acid",
    "hyaluronic": "hyaluronic_acid",
    "hyaluron": "hyaluronic_acid",
    "sodium hyaluronate": "hyaluronic_acid",
    "niacinamide": "niacinamide",
    "panthenol": "panthenol",
    "vitamin b5": "panthenol",
    "provitamin b5": "panthenol",
    "b5": "panthenol",
    "peptide": "peptides",
    "peptides": "peptides",
    "multi peptide": "peptides",
    "multi-peptide": "peptides",
    "copper peptide": "peptides",
    "copper peptides": "peptides",
    "tripeptide": "peptides",
    "tetrapeptide": "peptides",
    "hexapeptide": "peptides",
    "retinol": "retinol",
    "retinoid": "retinol",
    "vitamin a": "retinol",
    "salicylic acid": "salicylic_acid",
    "bha": "salicylic_acid",
    "vitamin c": "ascorbic_acid",
    "zinc pca": "zinc_pca",
    "zinc": "zinc_pca",
}

_SHADE_FIELD_NAMES = {
    "shade",
    "shade_name",
    "shade_name_text",
    "shadename",
    "shade_code",
    "shade_hex",
    "shade_label",
}

_COSMETIC_SHADE_CATEGORY_RULES: List[Tuple[str, List[str]]] = [
    ("foundation", ["foundation", "foundations"]),
    ("lipstick", ["lipstick", "lipsticks"]),
    ("blush", ["blush", "blushes"]),
    ("gloss", ["gloss", "glosses", "lip gloss", "lip glosses"]),
]


def _normalize_visible_attribute_text(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def _normalized_visible_term_matches(text: str, term: str) -> bool:
    source = _normalize_visible_attribute_text(text)
    target = _normalize_visible_attribute_text(term)
    if not source or not target:
        return False
    parts = [re.escape(part) for part in re.split(r"\s+", target) if part]
    if not parts:
        return False
    pattern = r"\s+".join(parts)
    return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", source) is not None


def _normalize_visible_attribute_labels(raw: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    if not isinstance(raw, dict):
        return {}

    normalized: Dict[str, List[str]] = {}
    for bucket, values in raw.items():
        bucket_name = str(bucket or "").strip()
        if not bucket_name:
            continue
        if isinstance(values, str):
            items = [values]
        elif isinstance(values, list):
            items = values
        else:
            continue
        deduped: List[str] = []
        for value in items:
            label = str(value or "").strip()
            if label and label not in deduped:
                deduped.append(label)
        if deduped:
            normalized[bucket_name] = deduped
    return normalized


def _coerce_string_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[;,|]", raw)
        return [str(part or "").strip() for part in parts if str(part or "").strip()]
    if isinstance(raw, (list, tuple, set)):
        values: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                for key in ("id", "ingredient_id", "canonical_id", "name", "label", "value"):
                    candidate = str(item.get(key) or "").strip()
                    if candidate:
                        values.append(candidate)
                        break
            else:
                candidate = str(item or "").strip()
                if candidate:
                    values.append(candidate)
        return values
    if isinstance(raw, dict):
        values: List[str] = []
        for key in (
            "ingredient_ids",
            "ingredientIds",
            "canonical_ingredient_ids",
            "canonicalIngredientIds",
            "canonical_ingredients",
            "canonicalIngredients",
            "reviewed_ingredient_ids",
            "reviewedIngredientIds",
        ):
            if key in raw:
                values.extend(_coerce_string_list(raw.get(key)))
        return values
    return []


def _normalize_ingredient_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized_text = _normalize_visible_attribute_text(raw)
    if not normalized_text:
        return ""
    alias_match = _INGREDIENT_CANONICAL_ALIASES.get(normalized_text)
    if alias_match:
        return alias_match
    return normalized_text.replace(" ", "_")


def _normalize_ingredient_ids(raw: Any) -> List[str]:
    deduped: List[str] = []
    for value in _coerce_string_list(raw):
        normalized = _normalize_ingredient_id(value)
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped


def _normalize_visible_option_labels(raw: Any) -> List[str]:
    deduped: List[str] = []
    for value in _coerce_string_list(raw):
        label = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        label = re.sub(r"[^a-z0-9_]+", "_", label)
        label = re.sub(r"_+", "_", label).strip("_")
        if label and label not in deduped:
            deduped.append(label)
    return deduped


def _extract_metadata_value(raw: Any, key_candidates: List[str]) -> Any:
    if not isinstance(raw, dict):
        return None
    for key in key_candidates:
        if key in raw and raw.get(key) is not None:
            return raw.get(key)
    for nested_key in ("beauty_meta", "beautyMeta", "catalog_meta", "catalogMeta"):
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            for key in key_candidates:
                if key in nested and nested.get(key) is not None:
                    return nested.get(key)
    return None


def _infer_size_option_label(value: Any) -> str:
    normalized = _normalize_visible_attribute_text(value)
    if not normalized:
        return ""
    for label, terms in _SIZE_OPTION_PATTERNS:
        if any(_normalized_visible_term_matches(normalized, term) for term in terms):
            return label
    numeric_match = re.search(r"(?<![a-z0-9])(\d{2,3})(?![a-z0-9])", normalized)
    if numeric_match:
        return f"size_{numeric_match.group(1)}"
    return ""


def _infer_color_option_labels(value: Any) -> List[str]:
    normalized = _normalize_visible_attribute_text(value)
    if not normalized:
        return []
    labels: List[str] = []
    for label, terms in _COLOR_OPTION_PATTERNS:
        if any(_normalized_visible_term_matches(normalized, term) for term in terms):
            if label not in labels:
                labels.append(label)
    return labels


def _normalize_shade_label(value: Any) -> str:
    normalized = _normalize_visible_attribute_text(value)
    if not normalized:
        return ""
    tokens = [token for token in re.split(r"\s+", normalized) if token]
    if not tokens:
        return ""
    return f"shade_{'_'.join(tokens)}"


def _extract_explicit_shade_labels(
    *,
    title: Optional[str],
    options: Optional[Dict[str, str]],
    platform_metadata: Optional[Dict[str, Any]],
) -> List[str]:
    labels: List[str] = []

    if isinstance(options, dict):
        for name, value in options.items():
            option_name = _normalize_visible_attribute_text(name)
            option_value = str(value or "").strip()
            if not option_value:
                continue
            if option_name and any(hint in option_name for hint in ("shade", "tone")):
                label = _normalize_shade_label(option_value)
                if label and label not in labels:
                    labels.append(label)

    metadata_value = _extract_metadata_value(
        platform_metadata,
        list(_SHADE_FIELD_NAMES),
    )
    for value in _coerce_string_list(metadata_value):
        label = _normalize_shade_label(value)
        if label and label not in labels:
            labels.append(label)

    title_text = _normalize_visible_attribute_text(title)
    if title_text:
        for match in re.finditer(r"(?<![a-z0-9])shade\s+([a-z0-9][a-z0-9 ]{0,30})(?![a-z0-9])", title_text):
            label = _normalize_shade_label(match.group(1))
            if label and label not in labels:
                labels.append(label)

    return labels


def _derive_variant_visible_option_labels(
    *,
    title: Optional[str],
    options: Optional[Dict[str, str]],
    platform_metadata: Optional[Dict[str, Any]],
) -> List[str]:
    labels: List[str] = []

    if isinstance(options, dict):
        for name, value in options.items():
            option_name = _normalize_visible_attribute_text(name)
            option_value = str(value or "").strip()
            if not option_value:
                continue
            if any(hint in option_name for hint in ("size",)):
                size_label = _infer_size_option_label(option_value)
                if size_label and size_label not in labels:
                    labels.append(size_label)
            if any(hint in option_name for hint in ("color", "colour")):
                for color_label in _infer_color_option_labels(option_value):
                    if color_label not in labels:
                        labels.append(color_label)

    title_text = _normalize_visible_attribute_text(title)
    for label, terms in _SIZE_OPTION_PATTERNS:
        if any(_normalized_visible_term_matches(title_text, term) for term in terms):
            if label not in labels:
                labels.append(label)
    numeric_match = re.search(r"(?<![a-z0-9])(\d{2,3})(?![a-z0-9])", title_text)
    if numeric_match:
        numeric_label = f"size_{numeric_match.group(1)}"
        if numeric_label not in labels:
            labels.append(numeric_label)
    for color_label in _infer_color_option_labels(title_text):
        if color_label not in labels:
            labels.append(color_label)
    for shade_label in _extract_explicit_shade_labels(
        title=title,
        options=options,
        platform_metadata=platform_metadata,
    ):
        if shade_label not in labels:
            labels.append(shade_label)

    return labels


def _derive_cosmetic_shade_categories(
    *,
    title: Optional[str],
    product_type: Optional[str],
    tags: Optional[List[str]],
) -> List[str]:
    sources: List[str] = []
    for candidate in [title, product_type, *(tags or [])]:
        text = str(candidate or "").strip()
        if text:
            sources.append(text)
    if not sources:
        return []
    matched: List[str] = []
    for label, terms in _COSMETIC_SHADE_CATEGORY_RULES:
        if any(_normalized_visible_term_matches(source, term) for source in sources for term in terms):
            if label not in matched:
                matched.append(label)
    return matched


def _derive_beauty_visible_attributes(
    *,
    title: Optional[str],
    product_type: Optional[str],
    tags: Optional[List[str]],
) -> Dict[str, List[str]]:
    sources: List[str] = []
    for candidate in [title, product_type, *(tags or [])]:
        text = str(candidate or "").strip()
        if text:
            sources.append(text)

    if not sources:
        return {}

    visible_attributes: Dict[str, List[str]] = {}
    for bucket, rules in _VISIBLE_BEAUTY_ATTRIBUTE_RULES.items():
        matched_labels: List[str] = []
        for rule in rules:
            label = str(rule.get("label") or "").strip()
            terms = [str(term or "").strip() for term in (rule.get("terms") or []) if str(term or "").strip()]
            if not label or not terms:
                continue
            if any(_normalized_visible_term_matches(source, term) for source in sources for term in terms):
                if label not in matched_labels:
                    matched_labels.append(label)
        if matched_labels:
            visible_attributes[bucket] = matched_labels
    return visible_attributes


def _merge_visible_attribute_maps(
    primary: Dict[str, List[str]],
    secondary: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    merged: Dict[str, List[str]] = {}
    for bucket in list(primary.keys()) + [bucket for bucket in secondary.keys() if bucket not in primary]:
        deduped: List[str] = []
        for label in [*(primary.get(bucket) or []), *(secondary.get(bucket) or [])]:
            if label and label not in deduped:
                deduped.append(label)
        if deduped:
            merged[bucket] = deduped
    return merged


class ProductStatus(str, Enum):
    """产品状态（标准化）"""
    ACTIVE = "active"
    DRAFT = "draft"
    ARCHIVED = "archived"


class StandardProductVariant(BaseModel):
    """产品变体（标准格式）"""
    id: str = Field(..., description="变体ID (deprecated, 请使用 variant_id)")  # 平台的变体 ID
    title: str  # 变体名称（例如："Small / Red"）
    
    # 标准字段名别名 (推荐使用)
    variant_id: Optional[str] = None
    sku: Optional[str] = None
    barcode: Optional[str] = None
    price: float  # 价格
    compare_at_price: Optional[float] = None  # 划线价
    inventory_quantity: int = 0  # 库存
    weight: Optional[float] = None
    weight_unit: Optional[str] = None  # kg, lb, g, oz
    options: Optional[Dict[str, str]] = None  # {"Size": "Small", "Color": "Red"}
    image_url: Optional[str] = None  # 变体专属图片
    visible_option_labels: List[str] = Field(default_factory=list)
    platform_metadata: Optional[Dict[str, Any]] = None
    
    @validator('variant_id', always=True)
    def set_variant_id(cls, v, values):
        """Ensure variant_id always equals id for backward compatibility"""
        return values.get('id', v)

    @field_validator("visible_option_labels", mode="before")
    @classmethod
    def normalize_visible_option_labels(cls, v):
        return _normalize_visible_option_labels(v)

    @model_validator(mode="after")
    def derive_visible_option_labels(self):
        explicit_labels = _normalize_visible_option_labels(self.visible_option_labels)
        derived_labels = _derive_variant_visible_option_labels(
            title=self.title,
            options=self.options,
            platform_metadata=self.platform_metadata,
        )
        self.visible_option_labels = [*explicit_labels]
        for label in derived_labels:
            if label not in self.visible_option_labels:
                self.visible_option_labels.append(label)
        return self


class StandardProduct(BaseModel):
    """
    Pivota 统一产品标准格式
    所有平台（Shopify/Wix/WooCommerce）的产品都转换为此格式
    """
    # 基本信息
    id: str = Field(..., description="产品ID (deprecated, 请使用 product_id)")  # 平台的产品 ID
    platform: str  # shopify, wix, woocommerce
    merchant_id: str  # 商户 ID
    
    # 标准字段名别名 (推荐使用)
    product_id: Optional[str] = None
    
    # 产品详情
    title: str
    description: Optional[str] = None
    description_text: Optional[str] = None
    vendor: Optional[str] = None  # 品牌/供应商
    product_type: Optional[str] = None  # 产品类型（例如："T-Shirts"）
    tags: List[str] = []  # 标签
    visible_attributes: Dict[str, List[str]] = Field(default_factory=dict)
    ingredient_ids: List[str] = Field(default_factory=list)
    
    # 价格和库存（默认变体）
    price: float
    compare_at_price: Optional[float] = None
    currency: str = "USD"
    inventory_quantity: Optional[int] = 0
    in_stock: Optional[bool] = None  # 简化库存判断 (inventory_quantity > 0 && orderable)
    
    # SKU 和条形码
    sku: Optional[str] = None
    barcode: Optional[str] = None
    
    # 图片
    image_url: Optional[str] = None  # 主图
    images: List[str] = []  # 所有图片
    
    # 变体（如果有多个 SKU）
    variants: List[StandardProductVariant] = []
    
    # 状态和时间
    status: ProductStatus = ProductStatus.ACTIVE
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    # 简单数据完整度评分（EPIC-4，用于 MDQS 预览）
    data_completeness_score: Optional[float] = None
    
    # 元数据（保留原始平台特定数据）
    platform_metadata: Optional[Dict[str, Any]] = None
    # 是否可下单及校验结果（EPIC-7 准备）
    orderable: Optional[bool] = None
    orderable_validation: Optional[Dict[str, Any]] = None
    
    @validator('product_id', always=True)
    def set_product_id(cls, v, values):
        """Ensure product_id always equals id for backward compatibility"""
        return values.get('id', v)

    @field_validator("ingredient_ids", mode="before")
    @classmethod
    def normalize_ingredient_ids(cls, v):
        return _normalize_ingredient_ids(v)
    
    @field_validator("inventory_quantity", mode="before")
    @classmethod
    def normalize_inventory_quantity(cls, v):
        """Treat None inventory as 0 for backward compatibility."""
        if v is None:
            return 0
        try:
            return int(v)
        except Exception:
            return 0

    @field_validator("orderable", mode="before")
    @classmethod
    def normalize_orderable(cls, v):
        if v is None:
            return None
        return bool(v)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, v):
        if v is None:
            return ProductStatus.ACTIVE
        if isinstance(v, str) and not v.strip():
            return ProductStatus.ACTIVE
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @model_validator(mode="after")
    def calculate_in_stock(self):
        """
        Calculate in_stock based on inventory_quantity and orderable.

        Run as a model-level validator so `orderable` is available.
        """
        self.description_text = rich_text_to_plain_text(
            self.description_text or self.description or ""
        ) or None
        normalized_visible_attributes = _normalize_visible_attribute_labels(self.visible_attributes)
        normalized_ingredient_ids = _normalize_ingredient_ids(self.ingredient_ids)
        metadata_ingredient_ids = _normalize_ingredient_ids(
            _extract_metadata_value(
                self.platform_metadata,
                [
                    "ingredient_ids",
                    "ingredientIds",
                    "reviewed_ingredient_ids",
                    "reviewedIngredientIds",
                    "canonical_ingredient_ids",
                    "canonicalIngredientIds",
                    "canonical_ingredients",
                    "canonicalIngredients",
                ],
            )
        )
        derived_visible_attributes = _derive_beauty_visible_attributes(
            title=self.title,
            product_type=self.product_type,
            tags=self.tags,
        )
        self.visible_attributes = _merge_visible_attribute_maps(
            normalized_visible_attributes,
            derived_visible_attributes,
        )
        self.ingredient_ids = [*normalized_ingredient_ids]
        for ingredient_id in metadata_ingredient_ids:
            if ingredient_id not in self.ingredient_ids:
                self.ingredient_ids.append(ingredient_id)

        cosmetic_shade_categories = _derive_cosmetic_shade_categories(
            title=self.title,
            product_type=self.product_type,
            tags=self.tags,
        )
        if cosmetic_shade_categories:
            for variant in self.variants:
                for option_label in list(variant.visible_option_labels or []):
                    if not option_label.startswith("color_"):
                        continue
                    shade_label = f"shade_{option_label[len('color_'):]}"
                    if shade_label not in variant.visible_option_labels:
                        variant.visible_option_labels.append(shade_label)
                has_explicit_shade_label = any(
                    str(option_label or "").startswith("shade_")
                    for option_label in (variant.visible_option_labels or [])
                )
                variant_title_text = _normalize_visible_attribute_text(variant.title)
                if not has_explicit_shade_label and variant_title_text not in {"", "default", "default title"}:
                    shade_label = _normalize_shade_label(variant.title)
                    if shade_label and shade_label not in variant.visible_option_labels:
                        variant.visible_option_labels.append(shade_label)
        if self.in_stock is None:
            inv = self.inventory_quantity or 0
            self.in_stock = bool(inv > 0 and self.orderable is True)
        return self
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


def validate_orderable(product: StandardProduct) -> Tuple[bool, Dict[str, Any]]:
    required_fields_present = {
        "id": bool(product.id),
        "platform": bool(product.platform),
        "price": product.price is not None and product.price > 0,
        "currency": bool(product.currency),
        "variant_id": bool(product.variants and product.variants[0].id),
    }
    errors: List[str] = []
    for field_name, present in required_fields_present.items():
        if not present:
            errors.append(f"missing_or_invalid_{field_name}")
    # Structural validity: required fields are present and well-formed.
    structural_ok = len(errors) == 0

    # Respect explicit platform-level orderable flags when provided.
    # Examples:
    # - Shopify/Wix adapters set product.orderable based on publish/visibility.
    # - Amazon/Temu imports do NOT set orderable and rely purely on structure.
    explicit_fields = getattr(product, "model_fields_set", None)
    if not isinstance(explicit_fields, set):
        explicit_fields = getattr(product, "__fields_set__", set())
    has_explicit_orderable = "orderable" in explicit_fields

    if has_explicit_orderable:
        # Platform authoritative: do not flip the platform-provided flag.
        # Structural issues are still surfaced in `orderable_validation` so
        # order-creation endpoints can block unsafe purchases.
        orderable = bool(getattr(product, "orderable", False))
    else:
        # Legacy/aggregated products without an explicit orderable flag keep
        # the previous semantics: structural validity alone decides.
        orderable = structural_ok

    validation = {
        "orderable": orderable,
        "structural_ok": structural_ok,
        "has_explicit_orderable": has_explicit_orderable,
        "required_fields_present": required_fields_present,
        "errors": errors,
    }
    return orderable, validation


class ProductListResponse(BaseModel):
    """产品列表响应（用于 Agent 调用）"""
    status: str = "success"
    merchant_id: str
    platform: str
    total: int
    products: List[StandardProduct]
    next_page_token: Optional[str] = None  # 分页游标（如果支持）
    fetched_at: datetime  # 数据获取时间（实时）
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }

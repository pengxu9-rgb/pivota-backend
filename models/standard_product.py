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
    
    @validator('variant_id', always=True)
    def set_variant_id(cls, v, values):
        """Ensure variant_id always equals id for backward compatibility"""
        return values.get('id', v)


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
        derived_visible_attributes = _derive_beauty_visible_attributes(
            title=self.title,
            product_type=self.product_type,
            tags=self.tags,
        )
        self.visible_attributes = _merge_visible_attribute_maps(
            normalized_visible_attributes,
            derived_visible_attributes,
        )
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

"""
Standard Product Format
Pivota 的核心价值：将多平台产品数据转换为统一标准格式
供 AI Agent 调用，无需关心底层平台差异
"""

from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class ProductStatus(str, Enum):
    """产品状态（标准化）"""
    ACTIVE = "active"
    DRAFT = "draft"
    ARCHIVED = "archived"


class StandardProductVariant(BaseModel):
    """产品变体（标准格式）"""
    id: str  # 平台的变体 ID
    title: str  # 变体名称（例如："Small / Red"）
    sku: Optional[str] = None
    barcode: Optional[str] = None
    price: float  # 价格
    compare_at_price: Optional[float] = None  # 划线价
    inventory_quantity: int = 0  # 库存
    weight: Optional[float] = None
    weight_unit: Optional[str] = None  # kg, lb, g, oz
    options: Optional[Dict[str, str]] = None  # {"Size": "Small", "Color": "Red"}
    image_url: Optional[str] = None  # 变体专属图片


class StandardProduct(BaseModel):
    """
    Pivota 统一产品标准格式
    所有平台（Shopify/Wix/WooCommerce）的产品都转换为此格式
    """
    # 基本信息
    id: str  # 平台的产品 ID
    platform: str  # shopify, wix, woocommerce
    merchant_id: str  # 商户 ID
    
    # 产品详情
    title: str
    description: Optional[str] = None
    vendor: Optional[str] = None  # 品牌/供应商
    product_type: Optional[str] = None  # 产品类型（例如："T-Shirts"）
    tags: List[str] = []  # 标签
    
    # 价格和库存（默认变体）
    price: float
    compare_at_price: Optional[float] = None
    currency: str = "USD"
    inventory_quantity: int = 0
    
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
    
    # 元数据（保留原始平台特定数据）
    platform_metadata: Optional[Dict[str, Any]] = None
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


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


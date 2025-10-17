"""
Products Database - Shopify/Wix/WooCommerce Product Management
Stores synced products from merchant commerce platforms
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, Boolean, Text, JSON, Float, BigInteger
from sqlalchemy.sql import func
from db.database import metadata, database
from typing import Dict, List, Any, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# Products table
products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),  # 所属商户
    Column("platform", String(50), nullable=False, index=True),  # shopify, wix, woocommerce
    Column("platform_product_id", String(100), nullable=False, index=True),  # 平台的产品 ID
    Column("title", String(500), nullable=False),  # 产品标题
    Column("description", Text, nullable=True),  # 产品描述
    Column("vendor", String(255), nullable=True),  # 供应商/品牌
    Column("product_type", String(255), nullable=True),  # 产品类型
    Column("tags", JSON, nullable=True),  # 标签数组
    Column("price", Float, nullable=True),  # 价格
    Column("compare_at_price", Float, nullable=True),  # 对比价格（划线价）
    Column("currency", String(10), default="USD"),  # 货币
    Column("inventory_quantity", Integer, default=0),  # 库存数量
    Column("sku", String(255), nullable=True),  # SKU
    Column("barcode", String(255), nullable=True),  # 条形码
    Column("weight", Float, nullable=True),  # 重量
    Column("weight_unit", String(10), nullable=True),  # 重量单位（kg, lb）
    Column("image_url", Text, nullable=True),  # 主图 URL
    Column("images", JSON, nullable=True),  # 所有图片 URLs
    Column("variants", JSON, nullable=True),  # 变体信息（颜色/尺寸等）
    Column("options", JSON, nullable=True),  # 选项配置
    Column("status", String(50), default="active"),  # active, draft, archived
    Column("published_at", DateTime, nullable=True),  # 发布时间
    Column("platform_created_at", DateTime, nullable=True),  # 平台创建时间
    Column("platform_updated_at", DateTime, nullable=True),  # 平台更新时间
    Column("synced_at", DateTime, server_default=func.now()),  # 同步时间
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)

# Product Sync History (追踪同步记录)
product_sync_history = Table(
    "product_sync_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("platform", String(50), nullable=False),
    Column("sync_type", String(50), default="manual"),  # manual, scheduled, incremental
    Column("status", String(50), default="pending"),  # pending, running, success, failed
    Column("total_products", Integer, default=0),  # 总产品数
    Column("synced_products", Integer, default=0),  # 已同步数量
    Column("new_products", Integer, default=0),  # 新增产品
    Column("updated_products", Integer, default=0),  # 更新产品
    Column("error_message", Text, nullable=True),  # 错误信息
    Column("started_at", DateTime, nullable=True),
    Column("completed_at", DateTime, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
)

# ============================================================================
# PRODUCT OPERATIONS
# ============================================================================

async def upsert_product(product_data: Dict[str, Any]) -> Optional[int]:
    """
    插入或更新产品（基于 merchant_id + platform + platform_product_id）
    返回产品 ID
    """
    try:
        # 检查是否存在
        query = products.select().where(
            (products.c.merchant_id == product_data["merchant_id"]) &
            (products.c.platform == product_data["platform"]) &
            (products.c.platform_product_id == product_data["platform_product_id"])
        )
        existing = await database.fetch_one(query)
        
        if existing:
            # 更新
            update_query = products.update().where(
                products.c.id == existing["id"]
            ).values(**product_data)
            await database.execute(update_query)
            logger.info(f"✅ Updated product: {product_data['title']} (ID: {existing['id']})")
            return existing["id"]
        else:
            # 插入
            insert_query = products.insert().values(**product_data)
            product_id = await database.execute(insert_query)
            logger.info(f"✅ Inserted product: {product_data['title']} (ID: {product_id})")
            return product_id
            
    except Exception as e:
        logger.error(f"❌ Failed to upsert product: {e}")
        return None


async def get_products(
    merchant_id: Optional[str] = None,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """
    获取产品列表（支持筛选、搜索、分页）
    """
    try:
        query = products.select()
        
        # 筛选条件
        if merchant_id:
            query = query.where(products.c.merchant_id == merchant_id)
        if platform:
            query = query.where(products.c.platform == platform)
        if status:
            query = query.where(products.c.status == status)
        
        # 搜索（标题、描述、SKU）
        if search:
            from sqlalchemy import or_, func as sqlfunc
            search_term = f"%{search}%"
            query = query.where(
                or_(
                    products.c.title.ilike(search_term),
                    products.c.description.ilike(search_term),
                    products.c.sku.ilike(search_term)
                )
            )
        
        # 排序和分页
        query = query.order_by(products.c.synced_at.desc()).limit(limit).offset(offset)
        
        results = await database.fetch_all(query)
        return [dict(r) for r in results]
        
    except Exception as e:
        logger.error(f"❌ Failed to get products: {e}")
        return []


async def get_product_count(
    merchant_id: Optional[str] = None,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None
) -> int:
    """获取产品总数（用于分页）"""
    try:
        from sqlalchemy import select, func as sqlfunc, or_
        query = select(sqlfunc.count()).select_from(products)
        
        conditions = []
        if merchant_id:
            conditions.append(products.c.merchant_id == merchant_id)
        if platform:
            conditions.append(products.c.platform == platform)
        if status:
            conditions.append(products.c.status == status)
        if search:
            search_term = f"%{search}%"
            conditions.append(
                or_(
                    products.c.title.ilike(search_term),
                    products.c.description.ilike(search_term),
                    products.c.sku.ilike(search_term)
                )
            )
        
        if conditions:
            query = query.where(*conditions)
        
        result = await database.fetch_val(query)
        return result or 0
        
    except Exception as e:
        logger.error(f"❌ Failed to get product count: {e}")
        return 0


async def create_sync_record(merchant_id: str, platform: str, sync_type: str = "manual") -> int:
    """创建同步记录"""
    try:
        query = product_sync_history.insert().values(
            merchant_id=merchant_id,
            platform=platform,
            sync_type=sync_type,
            status="pending",
            started_at=datetime.now()
        )
        sync_id = await database.execute(query)
        return sync_id
    except Exception as e:
        logger.error(f"❌ Failed to create sync record: {e}")
        return 0


async def update_sync_record(
    sync_id: int,
    status: str,
    total_products: int = 0,
    synced_products: int = 0,
    new_products: int = 0,
    updated_products: int = 0,
    error_message: Optional[str] = None
):
    """更新同步记录"""
    try:
        update_data = {
            "status": status,
            "total_products": total_products,
            "synced_products": synced_products,
            "new_products": new_products,
            "updated_products": updated_products,
        }
        
        if error_message:
            update_data["error_message"] = error_message
        
        if status in ["success", "failed"]:
            update_data["completed_at"] = datetime.now()
        
        query = product_sync_history.update().where(
            product_sync_history.c.id == sync_id
        ).values(**update_data)
        
        await database.execute(query)
    except Exception as e:
        logger.error(f"❌ Failed to update sync record: {e}")


async def get_sync_history(merchant_id: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """获取同步历史记录"""
    try:
        query = product_sync_history.select().order_by(product_sync_history.c.created_at.desc()).limit(limit)
        
        if merchant_id:
            query = query.where(product_sync_history.c.merchant_id == merchant_id)
        
        results = await database.fetch_all(query)
        return [dict(r) for r in results]
    except Exception as e:
        logger.error(f"❌ Failed to get sync history: {e}")
        return []


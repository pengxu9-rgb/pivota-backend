"""
订单处理 API 路由
Pivota 核心业务流程：Agent 下单 → 支付 → 履约
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Optional, List, Dict, Any, Tuple
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
import time
import hashlib
import httpx
import os
import json

from models.order import (
    CreateOrderRequest, OrderResponse, PaymentConfirmRequest, 
    OrderListResponse, OrderItem, OrderStatus
)
from db.orders import (
    create_order, get_order, get_orders_by_merchant, get_orders_by_customer,
    update_order_status, update_payment_info, mark_order_paid, 
    update_fulfillment_info, mark_order_shipped, get_order_stats
)
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from db.database import database
from utils.auth import require_admin, get_current_user
from config.settings import settings
from adapters.psp_adapter import get_psp_adapter
from adapters.multi_psp_orchestrator import create_payment_with_failover
from utils.logger import logger
from services.payment_routing_service import PaymentRoutingService
from services.promotions_service import list_promotions, PromotionStatus
from services.quote_service import (
    QuoteError,
    QuoteService,
    compute_request_fingerprint,
    normalize_discount_codes,
    normalize_items_for_fingerprint,
    normalize_shipping_for_fingerprint,
    parse_decimal_money,
)

router = APIRouter(prefix="/orders", tags=["orders"])


# ============================================================================
# 促销折扣应用（多件折扣）
# ============================================================================

async def compute_order_discount_from_promotions(
    merchant_id: str,
    items: List[OrderItem],
    channel: str = "creator_agents",
) -> Tuple[Decimal, List[Dict[str, Any]]]:
    """
    根据当前订单和促销配置计算订单级折扣金额。

    当前 v0 仅支持：
    - type = MULTI_BUY_DISCOUNT
    - scope.global = true 或 scope.productIds 精确匹配 product_id
    - channel 包含 creator_agents
    """
    discount_total = Decimal("0")
    applied: List[Dict[str, Any]] = []

    try:
        promotions, _ = await list_promotions(
            merchant_id=merchant_id,
            status=PromotionStatus.ACTIVE,
            channel=channel,
        )
    except Exception as e:
        logger.warning(
            f"[OrderRoutes] Failed to load promotions for merchant {merchant_id}: {e}"
        )
        return discount_total, applied

    if not promotions:
        return discount_total, applied

    for promo in promotions:
        try:
            if promo.type != "MULTI_BUY_DISCOUNT":
                continue

            scope = promo.scope or {}
            cfg = promo.config or {}

            threshold = int(
                cfg.get("thresholdQuantity")
                or cfg.get("threshold_quantity")
                or 0
            )
            discount_percent_raw = (
                cfg.get("discountPercent") or cfg.get("discount_percent") or 0
            )
            discount_percent = Decimal(str(discount_percent_raw))

            if threshold <= 0 or discount_percent <= 0:
                continue

            # 收集满足 scope 的每一件商品的单价（按件展开）
            unit_prices: List[Decimal] = []
            for item in items:
                eligible = False
                product_id = item.product_id
                if scope.get("global"):
                    eligible = True
                else:
                    product_ids = scope.get("productIds") or scope.get("product_ids") or []
                    if product_id in product_ids:
                        eligible = True

                if not eligible:
                    continue

                # 将每件商品按数量展开成单价列表
                for _ in range(item.quantity):
                    unit_prices.append(Decimal(item.unit_price))

            total_qty = len(unit_prices)
            if total_qty < threshold:
                continue

            # 优先对价格较高的商品进行折扣
            unit_prices.sort(reverse=True)
            discountable_qty = (total_qty // threshold) * threshold
            discount_base = sum(unit_prices[:discountable_qty])

            if discount_base <= 0:
                continue

            promo_discount = (
                discount_base * discount_percent / Decimal("100")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if promo_discount <= 0:
                continue

            discount_total += promo_discount
            applied.append(
                {
                    "id": promo.id,
                    "label": promo.humanReadableRule,
                    "type": promo.type,
                    "thresholdQuantity": threshold,
                    "discountPercent": float(discount_percent),
                    "discountAmount": float(promo_discount),
                }
            )
        except Exception as promo_err:
            logger.warning(
                f"[OrderRoutes] Failed to apply promotion {getattr(promo, 'id', None)}: {promo_err}"
            )
            continue

    return discount_total, applied


# ============================================================================
# 库存检查
# ============================================================================

async def check_inventory_availability(
    merchant_id: str,
    items: List[OrderItem]
) -> Tuple[bool, Dict[str, Any]]:
    """
    检查 Shopify 库存是否充足
    
    返回: (是否有库存, 库存详情)
    """
    try:
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant or not True:
            # 如果未连接 MCP，默认允许订单
            return True, {"message": "MCP not connected, skipping inventory check"}
        
        # 获取主店铺信息（Shopify/Wix/...），用于后续判断
        store_info = await get_primary_store(merchant_id)
        if not store_info:
            return True, {"message": "No store connected, skipping inventory check"}

        if store_info.get("platform") != "shopify":
            # 非 Shopify 平台，暂不检查库存
            return True, {"message": f"Platform {merchant.get('mcp_platform')} inventory check not implemented"}
        
        shop_domain = store_info.get("domain")
        access_token = store_info.get("api_key")
        
        if not shop_domain or not access_token:
            return True, {"message": "Shop credentials missing, skipping inventory check"}
        
        # 获取所有产品和变体
        url = f"https://{shop_domain}/admin/api/2024-01/products.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            
            if response.status_code != 200:
                return True, {"message": "Failed to fetch products, allowing order"}
            
            products = response.json().get("products", [])
            
            # 建立 variant_id -> inventory 的映射
            inventory_map = {}
            for product in products:
                for variant in product.get("variants", []):
                    variant_id = str(variant["id"])
                    inventory_map[variant_id] = {
                        "available": variant.get("inventory_quantity", 0),
                        "tracked": variant.get("inventory_management") == "shopify",
                        "sku": variant.get("sku"),
                        "title": f"{product['title']} - {variant.get('title', '')}"
                    }
            
            # 检查每个订单项的库存
            insufficient_items = []
            inventory_details = {}
            
            for item in items:
                if not item.variant_id:
                    # 如果没有 variant_id，跳过检查
                    continue
                
                variant_id = str(item.variant_id)
                if variant_id in inventory_map:
                    inv = inventory_map[variant_id]
                    inventory_details[variant_id] = inv
                    
                    if inv["tracked"] and inv["available"] < item.quantity:
                        insufficient_items.append({
                            "product": item.product_title,
                            "requested": item.quantity,
                            "available": inv["available"]
                        })
            
            if insufficient_items:
                return False, {
                    "message": "Insufficient inventory",
                    "items": insufficient_items
                }
            
            return True, {
                "message": "Inventory check passed",
                "details": inventory_details
            }
            
    except Exception as e:
        # 库存检查失败时，默认允许订单（fail-open）
        logger.error(f"Inventory check failed: {e}")
        return True, {"message": f"Inventory check error: {str(e)}, allowing order"}


def _extract_delivery_option_identifier(selected_delivery_option: Any) -> Optional[str]:
    """
    Best-effort extraction of a stable delivery option identifier for drift diagnostics.

    Do not include full delivery option payload in responses/events (may contain extra data),
    only a stable identifier-like string.
    """
    if not selected_delivery_option:
        return None

    if isinstance(selected_delivery_option, str):
        value = selected_delivery_option.strip()
        return value or None

    if not isinstance(selected_delivery_option, dict):
        return None

    for key in (
        "id",
        "identifier",
        "handle",
        "code",
        "shipping_rate_id",
        "rate_id",
        "title",
        "name",
    ):
        raw = selected_delivery_option.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            return value
    return None


def _build_quote_drift_normalized_request(
    *,
    items: List[Dict[str, Any]],
    discount_codes: List[str],
    shipping_address: Optional[Dict[str, Any]],
    selected_delivery_option: Any,
) -> Dict[str, Any]:
    return {
        "items": normalize_items_for_fingerprint(items),
        "discount_codes": normalize_discount_codes(discount_codes),
        "shipping_geo": normalize_shipping_for_fingerprint(shipping_address),
        "selected_delivery_option": _extract_delivery_option_identifier(selected_delivery_option),
    }


# ============================================================================
# 订单创建（Agent 调用）
# ============================================================================

@router.post("/create", response_model=OrderResponse)
async def create_new_order(
    order_request: CreateOrderRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin)  # Agent 需要管理员权限
):
    """
    **创建新订单（Agent → Pivota）**
    
    流程：
    1. 验证商户存在且已连接 PSP
    2. 计算订单总价
    3. 创建订单记录
    4. 创建 Stripe Payment Intent
    5. 返回订单详情和支付密钥
    
    防御性设计：
    - 订单创建后立即记录事件日志
    - 金额使用 Decimal 精确计算
    - 支付信息与订单解耦，失败不影响订单创建
    """
    try:
        # 1. 验证商户
        merchant = await get_merchant_onboarding(order_request.merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        if not merchant.get("psp_connected"):
            # 从 merchant_psps 回退推断 PSP 连接
            try:
                psp_row = await database.fetch_one(
                    """
                    SELECT provider FROM merchant_psps
                    WHERE merchant_id = :merchant_id
                    ORDER BY connected_at DESC
                    LIMIT 1
                    """,
                {"merchant_id": order_request.merchant_id}
                )
            except Exception:
                psp_row = None
            if psp_row:
                merchant["psp_connected"] = True
                merchant["psp_type"] = psp_row["provider"]
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Merchant has not connected PSP. Cannot process payments."
                )

        # Quote-first enforcement (PCS v0.2-a): dual guard to prevent bypass.
        from services.quote_first_enforcement import should_require_quote_for_order_create

        require_quote, require_ctx = await should_require_quote_for_order_create(merchant_id=order_request.merchant_id)
        if require_quote and not order_request.quote_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "QUOTE_REQUIRED",
                    "message": "quote_id is required",
                    "context": require_ctx,
                },
            )

        # 2. 检查库存（如果商户连接了 Shopify）
        has_inventory, inventory_info = await check_inventory_availability(
            order_request.merchant_id,
            order_request.items
        )
        if not has_inventory:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Insufficient inventory",
                    "items": inventory_info.get("items", [])
                }
            )

        # 3. 计算订单金额
        # Quote-first path: if quote_id is provided, amounts come from quote snapshot.
        pricing_quote_meta: Optional[Dict[str, Any]] = None
        if order_request.quote_id:
            quote_service = QuoteService()
            try:
                quote = await quote_service.load_active_quote_or_raise(
                    quote_id=order_request.quote_id
                )

                order_items_for_fingerprint = [
                    {
                        "product_id": it.product_id,
                        "variant_id": it.variant_id or "",
                        "quantity": it.quantity,
                    }
                    for it in (order_request.items or [])
                ]
                order_discount_codes = normalize_discount_codes(order_request.discount_codes)
                order_shipping_geo = (
                    {
                        "country": order_request.shipping_address.country,
                        "postal_code": order_request.shipping_address.postal_code,
                        "city": order_request.shipping_address.city,
                        "state": order_request.shipping_address.state,
                    }
                    if order_request.shipping_address
                    else None
                )

                order_request_fingerprint = compute_request_fingerprint(
                    merchant_id=order_request.merchant_id,
                    items=order_items_for_fingerprint,
                    discount_codes=order_discount_codes,
                    shipping_address=order_shipping_geo,
                    selected_delivery_option=order_request.selected_delivery_option,
                )

                order_request_normalized = _build_quote_drift_normalized_request(
                    items=order_items_for_fingerprint,
                    discount_codes=order_discount_codes,
                    shipping_address=order_shipping_geo,
                    selected_delivery_option=order_request.selected_delivery_option,
                )

                quote_request_json = quote.request_json if isinstance(quote.request_json, dict) else {}
                quote_request_normalized = _build_quote_drift_normalized_request(
                    items=quote_request_json.get("items") or [],
                    discount_codes=quote_request_json.get("discount_codes") or [],
                    shipping_address=quote_request_json.get("shipping_address"),
                    selected_delivery_option=quote_request_json.get("selected_delivery_option"),
                )

                drift_fields: List[str] = []
                if quote.merchant_id != order_request.merchant_id:
                    drift_fields.append("merchant_id")
                if quote_request_normalized.get("items") != order_request_normalized.get("items"):
                    drift_fields.append("items")
                if quote_request_normalized.get("discount_codes") != order_request_normalized.get(
                    "discount_codes"
                ):
                    drift_fields.append("discount_codes")
                if quote_request_normalized.get("shipping_geo") != order_request_normalized.get("shipping_geo"):
                    drift_fields.append("shipping_geo")
                if quote_request_normalized.get("selected_delivery_option") != order_request_normalized.get(
                    "selected_delivery_option"
                ):
                    drift_fields.append("selected_delivery_option")

                drift_details = {
                    "quote_id": quote.quote_id,
                    "quote_expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
                    "quote_hash_sha256": quote.quote_hash_sha256,
                    "quote_request_fingerprint": quote.request_fingerprint,
                    "order_request_fingerprint": order_request_fingerprint,
                    "drift_fields": (
                        drift_fields
                        if drift_fields
                        else ["selected_delivery_option"]
                        if order_request_fingerprint != quote.request_fingerprint
                        else []
                    ),
                    "quote_request_normalized": quote_request_normalized,
                    "order_request_normalized": order_request_normalized,
                }

                if quote.merchant_id != order_request.merchant_id:
                    raise QuoteError(
                        "QUOTE_MISMATCH",
                        "quote merchant_id mismatch",
                        debug_id=quote.debug_id,
                        details=drift_details,
                    )

                if order_request_fingerprint != quote.request_fingerprint:
                    raise QuoteError(
                        "QUOTE_MISMATCH",
                        "order request does not match quote snapshot",
                        debug_id=quote.debug_id,
                        details=drift_details,
                    )

                snap = quote.snapshot_json or {}
                pricing = (snap.get("pricing") or {}) if isinstance(snap, dict) else {}
                quote_currency = None
                try:
                    quote_currency = str(snap.get("currency") or "").strip().upper() if isinstance(snap, dict) else None
                except Exception:
                    quote_currency = None
                if quote_currency:
                    # Quote-first: currency is locked by the quote snapshot, not by the request payload.
                    # This prevents mismatches where amounts are from EUR but currency is defaulted to USD.
                    order_request.currency = quote_currency

                subtotal = parse_decimal_money(pricing.get("subtotal"))
                discount_total = parse_decimal_money(pricing.get("discount_total"))
                shipping_fee = parse_decimal_money(pricing.get("shipping_fee"))
                tax = parse_decimal_money(pricing.get("tax"))
                total = parse_decimal_money(pricing.get("total"))

                pricing_quote_meta = {
                    "quote_id": quote.quote_id,
                    "expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
                    "engine": quote.engine,
                    "engine_ref": quote.engine_ref,
                    "request_fingerprint": quote.request_fingerprint,
                    "quote_hash_sha256": quote.quote_hash_sha256,
                    "pricing": pricing,
                    "promotion_lines": snap.get("promotion_lines") or [],
                    "line_items": snap.get("line_items") or [],
                }
            except QuoteError as e:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": e.code,
                        "message": e.message,
                        "debug_id": e.debug_id,
                        **({"details": e.details} if getattr(e, "details", None) else {}),
                    },
                )

        else:
            subtotal = sum(item.subtotal for item in order_request.items)

            # Legacy promotions (multi-buy) for non-quote orders.
            discount_total = Decimal("0")
            applied_promos: List[Dict[str, Any]] = []
            try:
                discount_total, applied_promos = await compute_order_discount_from_promotions(
                    merchant_id=order_request.merchant_id,
                    items=order_request.items,
                    channel="creator_agents",
                )
            except Exception as promo_err:
                logger.warning(
                    f"[OrderRoutes] Failed to compute promotions for order: {promo_err}"
                )
                discount_total = Decimal("0")
                applied_promos = []

            if discount_total > 0:
                logger.info(
                    f"[OrderRoutes] Applied promotions for merchant {order_request.merchant_id}: "
                    f"discount_total={discount_total}"
                )
                subtotal = max(Decimal("0"), subtotal - discount_total)

            shipping_fee = Decimal("0")
            tax = Decimal("0")
            total = subtotal + shipping_fee + tax

        # 4. 创建订单
        # Extract agent_id from metadata if present
        agent_id = None
        if order_request.metadata:
            agent_id = order_request.metadata.get("agent_id")

        # Determine PSP using PaymentRoutingService (merchant routing UI),
        # falling back to legacy hints only if routing config is missing.
        routing_service = PaymentRoutingService(database)
        selected_psp = None
        route_config: Dict[str, Any] = {}
        try:
            selected_psp, route_config = await routing_service.select_psp(
                agent_id=agent_id or "",
                merchant_id=order_request.merchant_id,
                amount=float(total),
                currency=order_request.currency or "USD",
            )
            logger.info(
                f"[OrderRoutes] Routing selected PSP '{selected_psp}' for order "
                f"{order_request.merchant_id} via payment_routes config"
            )
        except Exception as e:
            logger.error(f"[OrderRoutes] Routing selection failed, falling back to legacy PSP: {e}")
            selected_psp = None

        # Source of truth is routing config; merchant_onboarding.psp_type and
        # preferred_psp are legacy hints.
        psp_type = selected_psp or (order_request.preferred_psp or merchant.get("psp_type")) or None

        # Always get psp_id for PSP metrics tracking (even if psp_type is known)
        psp_id_value = None
        try:
            psp_row = None
            if psp_type:
                # Try to get a matching active PSP for the requested type
                psp_row = await database.fetch_one(
                    """
                    SELECT provider, psp_id FROM merchant_psps
                    WHERE merchant_id = :merchant_id AND provider = :provider AND status = 'active'
                    ORDER BY connected_at DESC
                    LIMIT 1
                    """,
                    {
                        "merchant_id": order_request.merchant_id,
                        "provider": psp_type,
                    },
                )

            # If no explicit type or no active PSP for that type, fall back to first active PSP
            if not psp_row:
                psp_row = await database.fetch_one(
                    """
                    SELECT provider, psp_id FROM merchant_psps
                    WHERE merchant_id = :merchant_id AND status = 'active'
                    ORDER BY connected_at DESC
                    LIMIT 1
                    """,
                    {"merchant_id": order_request.merchant_id},
                )

            if psp_row:
                psp_type = psp_row["provider"]
                psp_id_value = psp_row["psp_id"]
            else:
                logger.error(
                    f"No active PSP found for merchant {order_request.merchant_id}"
                )
                raise HTTPException(
                    status_code=400,
                    detail="No active PSP configuration found for this merchant",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get PSP configuration: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to determine PSP: {str(e)}"
            )
        
        # Ensure psp_type is lowercase for consistency
        if psp_type:
            psp_type = psp_type.lower()
        
        # Validate PSP fields are set
        if not psp_type or not psp_id_value:
            logger.error(f"PSP fields incomplete: psp_type={psp_type}, psp_id={psp_id_value}")
            raise HTTPException(
                status_code=500,
                detail="Failed to determine complete PSP configuration"
            )
        
        logger.info(f"✅ PSP determined: {psp_type} (ID: {psp_id_value})")
        
        # 合并订单元数据并记录促销信息（如果有）
        order_metadata: Dict[str, Any] = dict(order_request.metadata or {})
        if pricing_quote_meta:
            order_metadata["pricing_quote"] = pricing_quote_meta
        elif discount_total > 0:
            promo_meta = {
                "discount_total": float(discount_total),
                "applied_promotions": applied_promos,
            }
            existing_promos = order_metadata.get("promotions") or {}
            # 促销信息统一挂在 metadata.promotions 下
            order_metadata["promotions"] = {**existing_promos, **promo_meta}

        order_data = {
            "merchant_id": order_request.merchant_id,
            "customer_email": order_request.customer_email,
            "items": [json.loads(item.json()) for item in order_request.items],
            "shipping_address": json.loads(order_request.shipping_address.json()),
            "subtotal": float(subtotal),
            "shipping_fee": float(shipping_fee),
            "tax": float(tax),
            "total": float(total),
            # "amount" field removed - use "total" instead
            "currency": order_request.currency,
            "agent_id": agent_id,  # Extract from metadata
            "agent_session_id": order_request.agent_session_id,
            "metadata": order_metadata,
            "psp_used": psp_type,  # Record which PSP provider is used (lowercase)
            # Legacy fields (optional, can be null)
            "store_id": None,
            "psp_id": psp_id_value,  # Include actual PSP ID for metrics tracking
            "payment_method": None
        }
        order_id = await create_order(order_data)

        # Consume quote best-effort after order creation succeeds.
        if order_request.quote_id:
            try:
                quote_service = QuoteService()
                await quote_service.consume_quote_best_effort(order_request.quote_id, order_id=str(order_id))
            except Exception:
                pass

        # 5. 同步创建 Payment Intent（立即返回结果）
        payment_intent_id = None
        client_secret = None
        # For future monitoring: track a single payment_attempt row per order
        # without changing routing or PSP behavior.
        payment_attempt_id = None
        route_id_for_attempt = route_config.get("route_id") if isinstance(route_config, dict) else None
        # Unified payment action for frontends (optional, best-effort)
        payment_action: Dict[str, Any] = {}
        
        try:
            # PSP type already determined above when creating order_data
            if not psp_type:
                psp_type = "stripe"  # Final fallback

            # PSP 密钥查找：优先从 merchant_psps 表
            psp_key = None
            psp_account_id = None
            psp_secret = None  # For PayPal client_secret
            
            # 1. 首先尝试从 merchant_psps 表获取对应 PSP 的 key 和 account_id
            # 数据库配置优先于环境变量！
            try:
                psp_row = await database.fetch_one(
                    """
                    SELECT api_key, account_id, secret_key FROM merchant_psps
                    WHERE merchant_id = :merchant_id AND provider = :provider AND status = 'active'
                    ORDER BY connected_at DESC
                    LIMIT 1
                    """,
                    {"merchant_id": order_request.merchant_id, "provider": psp_type}
                )
                if psp_row and psp_row["api_key"]:
                    psp_key = psp_row["api_key"]
                    try:
                        psp_account_id = psp_row["account_id"]
                        psp_secret = psp_row.get("secret_key") if hasattr(psp_row, 'get') else psp_row["secret_key"]
                    except Exception:
                        psp_account_id = None
                        psp_secret = None
                    logger.info(f"✅ Found {psp_type} key in DB for merchant {order_request.merchant_id}")
                    logger.info(f"   API Key length: {len(psp_key)}, Account ID: {psp_account_id}, Has secret: {bool(psp_secret)}")
                else:
                    logger.info(f"⚠️  No {psp_type} config in DB for merchant {order_request.merchant_id}")
            except Exception as e:
                logger.warning(f"DB PSP key lookup failed: {e}")
            
            # 2. 如果数据库没有，且是 Stripe，尝试从 merchant 表获取（兼容旧数据）
            if not psp_key and psp_type == "stripe":
                psp_key = merchant.get("psp_sandbox_key") or merchant.get("psp_key")
                if psp_key:
                    logger.info(f"Using legacy Stripe key from merchant table")
            
            # 3. 最后回退到环境变量（仅作为开发/测试的备选）
            # 注意：数据库配置优先！环境变量只用于没有数据库配置的情况
            if not psp_key:
                if psp_type == "stripe":
                    env_key = getattr(settings, "stripe_secret_key", None)
                    if env_key and len(env_key) > 10:  # Validate key is not empty
                        psp_key = env_key
                        logger.info(f"Using Stripe key from environment (fallback)")
                # 移除 Adyen 环境变量回退 - 强制使用数据库配置
                # Checkout 和 PayPal 已经只使用数据库配置
                
            # 4. If still no key, fail with clear error message
            if not psp_key:
                logger.error(f"❌ No {psp_type} API key found for merchant {merchant['merchant_id']}")
                # Skip payment intent creation but continue with order
                # This allows the order to be created, but merchant needs to configure PSP
                logger.warning(f"⚠️  Order will be created without payment intent. Merchant must configure {psp_type} to accept payments.")
            else:
                logger.info(f"✅ Using PSP key from database/environment for {psp_type}")
            
            # Build preferred PSP ordering from routing config (if available)
            preferred_psps: Optional[List[str]] = None
            try:
                if isinstance(route_config, dict):
                    raw_priority = route_config.get("psp_priority") or []
                    if isinstance(raw_priority, str):
                        try:
                            raw_priority = json.loads(raw_priority)
                        except Exception:
                            raw_priority = []
                    if isinstance(raw_priority, list) and raw_priority:
                        preferred_psps = [
                            str(entry.get("psp", "")).lower()
                            for entry in sorted(
                                raw_priority, key=lambda e: e.get("priority", 999)
                            )
                            if entry.get("psp")
                        ]
            except Exception as pref_err:
                logger.warning(
                    f"[OrderRoutes] Failed to build preferred_psps list from route_config: {pref_err}"
                )
                preferred_psps = None

            # Attempt-level logging is handled inside MultiPSPOrchestrator (best-effort),
            # so we don't create a single aggregated payment_attempt row here.
            payment_attempt_id = None

            # 使用 MultiPSPOrchestrator，按路由配置的优先级（preferred_psps）
            # 自动在 adyen → stripe → checkout 之间切换。
            start_ts = time.monotonic()
            # Agent / 对话场景下，如果前端传了 preferred_psp = "stripe_checkout"，
            # 则通过 metadata.psp_mode 告诉 Stripe 适配器走 Checkout Session 流程，
            # 但 PSP provider 仍然是 "stripe"（由 routing 决定）。
            psp_mode = None
            if (order_request.preferred_psp or "").lower() == "stripe_checkout":
                psp_mode = "stripe_checkout"

            success, payment_intent, error, psp_used = await create_payment_with_failover(
                merchant_id=order_request.merchant_id,
                amount=total,
                currency=order_request.currency,
                metadata={
                    "order_id": order_id,
                    "merchant_id": order_request.merchant_id,
                    "customer_email": order_request.customer_email,
                    "route_id": route_id_for_attempt,
                    "agent_id": agent_id,
                    **({"psp_mode": psp_mode} if psp_mode else {}),
                },
                preferred_psps=preferred_psps,
            )
            response_ms = int((time.monotonic() - start_ts) * 1000)

            # 最终实际使用的 PSP（如果 orchestrator 没返回，则回退到 initial_psp_name）
            final_psp = (psp_used or initial_psp_name or psp_type or "stripe").lower()
            logger.info(
                f"[OrderRoutes] Payment intent result via MultiPSPOrchestrator: "
                f"success={success}, psp_used={final_psp}, has_intent={payment_intent is not None}, error={error}"
            )

            if success and payment_intent:
                payment_intent_id = payment_intent.id
                client_secret = getattr(payment_intent, "client_secret", None)
                psp_type = final_psp
                logger.info(f"✅ Payment intent created via {psp_type}: {payment_intent_id}")

                # Build unified payment_action for frontend / Agent
                try:
                    redirect_url = getattr(payment_intent, "redirect_url", None)
                    raw = getattr(payment_intent, "raw_response", None)

                    if redirect_url:
                        payment_action = {
                            "type": "redirect_url",
                            "url": redirect_url,
                            "raw": raw,
                        }
                    elif psp_type == "stripe" and client_secret:
                        payment_action = {
                            "type": "stripe_client_secret",
                            "client_secret": client_secret,
                            "raw": raw,
                        }
                    elif psp_type == "adyen" and client_secret:
                        payment_action = {
                            "type": "adyen_session",
                            "client_secret": client_secret,
                            "raw": raw,
                        }
                    elif psp_type in ["checkout", "paypal"] and client_secret and str(
                        client_secret
                    ).startswith("http"):
                        payment_action = {
                            "type": "redirect_url",
                            "url": client_secret,
                            "raw": raw,
                        }
                    else:
                        # Fallback: expose minimal info; frontends can仍然使用 legacy 字段
                        payment_action = {
                            "type": None,
                            "client_secret": client_secret,
                        }
                except Exception as pa_err:
                    logger.warning(
                        f"⚠️ Failed to build payment_action for order {order_id}: {pa_err}"
                    )

                # MultiPSPOrchestrator logs each PSP attempt; no aggregated update here.

                # Log redirect URL when available（Checkout / PayPal / Stripe Checkout）
                redirect_url = getattr(payment_intent, "redirect_url", None)
                if (
                    not redirect_url
                    and psp_type in ["checkout", "paypal"]
                    and client_secret
                    and isinstance(client_secret, str)
                    and client_secret.startswith("http")
                ):
                    redirect_url = client_secret
                if redirect_url:
                    logger.info(f"🔗 {psp_type.capitalize()} redirect URL: {redirect_url}")

                await update_payment_info(
                    order_id=order_id,
                    payment_intent_id=payment_intent_id,
                    client_secret=client_secret or "",
                    payment_status="awaiting_payment",
                    psp_used=final_psp,
                )
                await log_order_event(
                    event_type="order_created",
                    order_id=order_id,
                    merchant_id=order_request.merchant_id,
                    metadata={
                        "total": float(total),
                        "currency": order_request.currency,
                        "items_count": len(order_request.items),
                        "payment_intent_id": payment_intent_id,
                        "psp_type": psp_type,
                    },
                )
            else:
                logger.error(f"Payment intent creation failed via MultiPSP: {error}")
                # MultiPSPOrchestrator logs each PSP attempt; no aggregated update here.
                await log_order_event(
                    event_type="payment_intent_failed",
                    order_id=order_id,
                    merchant_id=order_request.merchant_id,
                    metadata={"error": error, "psp_type": final_psp},
                )
        except Exception as e:
            logger.error(f"Payment intent creation error: {e}")
            await log_order_event(
                event_type="payment_intent_error",
                order_id=order_id,
                merchant_id=order_request.merchant_id,
                metadata={"error": str(e)},
            )

        # 6. 返回订单信息（支付已同步创建）
        return OrderResponse(
            order_id=order_id,
            merchant_id=order_request.merchant_id,
            customer_email=order_request.customer_email,
            items=order_request.items,
            shipping_address=order_request.shipping_address,
            subtotal=float(subtotal),
            shipping_fee=float(shipping_fee),
            tax=float(tax),
            total=float(total),
            currency=order_request.currency,
            status="pending",
            payment_status="awaiting_payment" if payment_intent_id else "pending",
            payment_intent_id=payment_intent_id,
            client_secret=client_secret,
             psp=psp_type,
             payment_action=payment_action or None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Order creation internal error: {e}")
        raise HTTPException(status_code=500, detail=f"Order creation internal error: {str(e)}")


# ============================================================================
# 支付处理
# ============================================================================

@router.post("/payment/confirm")
async def confirm_payment(
    payment_request: PaymentConfirmRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin)
):
    """
    **确认支付（Agent 调用）**
    
    流程：
    1. 验证订单存在
    2. 确认 Stripe Payment Intent
    3. 更新订单状态为已支付
    4. 触发履约流程（创建 Shopify 订单）
    """
    
    order = await get_order(payment_request.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["payment_status"] == "paid":
        return {"status": "success", "message": "Order already paid"}
    
    # 获取商户信息
    merchant = await get_merchant_onboarding(order["merchant_id"])
    # 如果标志未更新，尝试从 merchant_psps 推断
    if not merchant.get("psp_connected") or not merchant.get("psp_type"):
        try:
            psp_row = await database.fetch_one(
                """
                SELECT provider FROM merchant_psps
                WHERE merchant_id = :merchant_id
                ORDER BY connected_at DESC
                LIMIT 1
                """,
                {"merchant_id": order["merchant_id"]}
            )
            if psp_row:
                merchant["psp_connected"] = True
                merchant["psp_type"] = merchant.get("psp_type") or psp_row["provider"]
        except Exception:
            pass
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    try:
        # 获取商户的 PSP 类型和密钥（带 fallback）
        psp_type = merchant.get("psp_type")
        if not psp_type:
            try:
                psp_row = await database.fetch_one(
                    """
                    SELECT provider FROM merchant_psps
                    WHERE merchant_id = :merchant_id
                    ORDER BY connected_at DESC
                    LIMIT 1
                    """,
                    {"merchant_id": order["merchant_id"]}
                )
                if psp_row:
                    psp_type = psp_row["provider"]
            except Exception:
                psp_type = None
        if not psp_type:
            psp_type = "stripe"
        # 尝试获取 psp_sandbox_key 或 psp_key (same logic as create_new_order)
        psp_key = merchant.get("psp_sandbox_key") or merchant.get("psp_key")
        
        # 如果商户没有配置密钥，使用系统默认（开发环境）
        if not psp_key:
            if psp_type == "stripe":
                psp_key = getattr(settings, "stripe_secret_key", None)
            else:
                psp_key = getattr(settings, "adyen_api_key", None)
        
        if not psp_key:
            raise ValueError(f"No PSP key found for merchant {merchant['merchant_id']}")
        
        # 创建 PSP 适配器
        psp_adapter = get_psp_adapter(psp_type, psp_key)
        
        # 确认支付
        success, status, error = await psp_adapter.confirm_payment(
            payment_intent_id=order["payment_intent_id"],
            payment_method_id=payment_request.payment_method_id
        )
        
        if not success:
            raise HTTPException(status_code=400, detail=f"Payment confirmation failed: {error}")
        
        if status == "succeeded":
            # 标记订单已支付
            await mark_order_paid(payment_request.order_id)
            
            # 记录支付成功事件
            await log_order_event(
                event_type="payment_succeeded",
                order_id=payment_request.order_id,
                merchant_id=order["merchant_id"],
                metadata={
                    "payment_intent_id": order["payment_intent_id"],
                    "amount": float(order["total"]),
                    "currency": order["currency"],
                    "psp_type": psp_type
                }
            )
            
            # 后台任务：创建 Shopify 订单
            async def create_shopify_order_task():
                """创建 Shopify 订单通知商户发货"""
                try:
                    if True and store_info.get("platform") == "shopify":
                        logger.info(f"Creating Shopify order for {payment_request.order_id}")
                        success = await create_shopify_order(payment_request.order_id)
                        if success:
                            logger.info(f"Shopify order created successfully for {payment_request.order_id}")
                        else:
                            logger.error(f"Failed to create Shopify order for {payment_request.order_id}")
                except Exception as e:
                    logger.error(f"Error in Shopify order creation task: {e}")
            
            background_tasks.add_task(create_shopify_order_task)
            
            # 后台任务：计算订单佣金（Phase 6 - Commission Automation）
            async def calculate_commission_task():
                """自动计算订单佣金并记录"""
                try:
                    from services.order_commission_service import process_order_commission
                    logger.info(f"Calculating commission for order {payment_request.order_id}")
                    result = await process_order_commission(payment_request.order_id, database)
                    if result.get("status") == "success":
                        logger.info(
                            f"Commission calculated: ${result.get('commission_amount', 0):.2f} "
                            f"at {result.get('commission_rate', 0) * 100}%"
                        )
                    elif result.get("status") == "skipped":
                        logger.info(f"Commission skipped: {result.get('reason')}")
                    else:
                        logger.error(f"Commission calculation failed: {result.get('message')}")
                except Exception as e:
                    logger.error(f"Error in commission calculation task: {e}")
            
            background_tasks.add_task(calculate_commission_task)
            
            return {
                "status": "success",
                "message": "Payment confirmed successfully",
                "order_id": payment_request.order_id,
                "payment_intent_id": order["payment_intent_id"],
                "psp_type": psp_type
            }
        else:
            return {
                "status": "pending",
                "message": f"Payment status: {status}",
                "payment_intent_id": order["payment_intent_id"]
            }
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payment failed: {str(e)}")


# ============================================================================
# 订单查询
# ============================================================================

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order_details(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """获取订单详情"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    return OrderResponse(
        order_id=order["order_id"],
        merchant_id=order["merchant_id"],
        customer_email=order["customer_email"],
        items=[OrderItem(**item) for item in order["items"]],
        shipping_address=order["shipping_address"],
        subtotal=order["subtotal"],
        shipping_fee=order["shipping_fee"],
        tax=order["tax"],
        total=order["total"],
        currency=order["currency"],
        status=order["status"],
        payment_status=order["payment_status"],
        fulfillment_status=order.get("fulfillment_status"),
        payment_intent_id=order.get("payment_intent_id"),
        shopify_order_id=order.get("shopify_order_id"),
        tracking_number=order.get("tracking_number"),
        created_at=order["created_at"],
        updated_at=order["updated_at"],
        paid_at=order.get("paid_at"),
        shipped_at=order.get("shipped_at"),
        agent_session_id=order.get("agent_session_id"),
        metadata=order.get("metadata")
    )


@router.get("/merchant/{merchant_id}", response_model=OrderListResponse)
async def get_merchant_orders(
    merchant_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(get_current_user)  # Allow authenticated users
):
    """获取商户的订单列表"""
    orders_list = await get_orders_by_merchant(merchant_id, status, limit, offset)
    
    return OrderListResponse(
        status="success",
        total=len(orders_list),
        orders=[
            OrderResponse(
                order_id=o["order_id"],
                merchant_id=o["merchant_id"],
                customer_email=o["customer_email"],
                items=[OrderItem(**item) for item in o["items"]],
                shipping_address=o["shipping_address"],
                subtotal=o["subtotal"],
                shipping_fee=o["shipping_fee"],
                tax=o["tax"],
                total=o["total"],
                currency=o["currency"],
                status=o["status"],
                payment_status=o["payment_status"],
                fulfillment_status=o.get("fulfillment_status"),
                payment_intent_id=o.get("payment_intent_id"),
                shopify_order_id=o.get("shopify_order_id"),
                tracking_number=o.get("tracking_number"),
                created_at=o["created_at"],
                updated_at=o["updated_at"],
                paid_at=o.get("paid_at"),
                shipped_at=o.get("shipped_at"),
                agent_session_id=o.get("agent_session_id"),
                metadata=o.get("metadata")
            ) for o in orders_list
        ]
    )


@router.get("/merchant/{merchant_id}/stats")
async def get_merchant_order_stats(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """获取商户订单统计"""
    stats = await get_order_stats(merchant_id)
    return stats


# ============================================================================
# Shopify 订单创建（履约集成）
# ============================================================================

async def create_shopify_order(order_id: str) -> bool:
    """
    在 Shopify 中创建订单（通知商户发货）
    
    防御性设计：
    - 失败不影响 Pivota 订单状态
    - 记录事件日志用于后续重试
    """
    try:
        logger.info(f"[Shopify] Starting order creation for {order_id}")
        
        order = await get_order(order_id)
        if not order:
            logger.error(f"[Shopify] Order {order_id} not found")
            return False
        
        logger.info(f"[Shopify] Order data: merchant_id={order.get('merchant_id')}, customer_email={order.get('customer_email')}, items_count={len(order.get('items', []))}")
        
        merchant = await get_merchant_onboarding(order["merchant_id"])
        if not merchant:
            logger.error(f"[Shopify] Merchant {order['merchant_id']} not found")
            return False
        
        # 获取主店铺（需要包含 domain 与 api_key）
        store_info = await get_primary_store(order["merchant_id"])
        if not store_info:
            logger.error(f"[Shopify] Primary store not found for merchant {order['merchant_id']}")
            return False
            
        if store_info.get("platform") != "shopify":
            logger.error(f"[Shopify] Primary store is {store_info.get('platform')}, not Shopify for merchant {order['merchant_id']}")
            return False

        shop_domain = store_info.get("domain")
        access_token = store_info.get("api_key")
        
        logger.info(f"[Shopify] Store credentials: domain={shop_domain}, has_token={bool(access_token)}, token_length={len(access_token) if access_token else 0}")
        
        if not shop_domain or not access_token:
            logger.error(f"[Shopify] Missing credentials for merchant {order['merchant_id']}: domain={bool(shop_domain)}, token={bool(access_token)}")
            return False
        
        logger.info(f"[Shopify] Using store: {shop_domain}")
        
        # 构造 Shopify 订单数据
        # Priority: Use variant_id if available (from real Shopify products)
        # Fallback: Use title-based custom line items (for testing/manual orders)
        line_items = []
        for item in order["items"]:
            # Check if item has a real Shopify variant_id
            has_variant = False
            if item.get("variant_id"):
                try:
                    variant_id = int(item["variant_id"])
                    # Real Shopify variant IDs are typically > 10000000000
                    # Use variant_id for proper inventory management
                    line_item = {
                        "variant_id": variant_id,
                        "quantity": item["quantity"]
                    }
                    line_items.append(line_item)
                    has_variant = True
                    logger.info(f"Using variant_id {variant_id} for {item.get('product_title')}")
                except (ValueError, TypeError):
                    logger.warning(f"Invalid variant_id: {item.get('variant_id')}")
            
            # Fallback to custom line item if no variant_id
            if not has_variant:
                line_item = {
                    "title": item.get("product_title", "Product"),
                    "quantity": item["quantity"],
                    "price": str(item["unit_price"]),
                    "taxable": False  # Custom items, tax already calculated
                }
                line_items.append(line_item)
                logger.info(f"Using custom line item for {item.get('product_title')}")
        
        # 转换地址格式：Pivota → Shopify
        shipping_addr = order["shipping_address"]
        name_parts = shipping_addr.get("name", "Customer").split(" ", 1)
        shopify_shipping = {
            "first_name": name_parts[0] if len(name_parts) > 0 else "Customer",
            "last_name": name_parts[1] if len(name_parts) > 1 else "",
            "address1": shipping_addr.get("address_line1", ""),
            "address2": shipping_addr.get("address_line2"),
            "city": shipping_addr.get("city", ""),
            "province": shipping_addr.get("state", ""),
            "zip": shipping_addr.get("postal_code", ""),
            "country": shipping_addr.get("country", "US"),
            "phone": shipping_addr.get("phone")
        }
        
        logger.info(f"[Shopify] Converted address: {shopify_shipping}")
        
        shopify_order_data = {
            "order": {
                "email": order["customer_email"],
                "financial_status": "paid",
                "send_receipt": True,
                "send_fulfillment_receipt": True,
                "line_items": line_items,
                "shipping_address": shopify_shipping,
                "note": f"Pivota Order ID: {order_id}",
                "tags": "pivota,agent-order"
            }
        }
        
        # NOTE: Shopify REST Admin API is on a legacy track; keep as-is for v0.1,
        # but plan migration to GraphQL Admin Orders API if you intend to ship as a public app.
        # 调用 Shopify API
        url = f"https://{shop_domain}/admin/api/2024-01/orders.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        logger.info(f"[Shopify] Calling API: {url}")
        logger.info(f"[Shopify] Order data payload: line_items_count={len(shopify_order_data['order']['line_items'])}, email={shopify_order_data['order'].get('email')}")
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=shopify_order_data, headers=headers, timeout=10.0)
            
            logger.info(f"[Shopify] API response: {response.status_code}")
            
            if response.status_code == 201:
                shopify_order = response.json()["order"]
                shopify_order_id = str(shopify_order["id"])
                
                logger.info(f"[Shopify] ✅ Order created: {shopify_order_id}")
                
                # 更新 Pivota 订单的 Shopify 订单 ID
                await update_fulfillment_info(
                    order_id=order_id,
                    shopify_order_id=shopify_order_id,
                    fulfillment_status="processing"
                )
                
                logger.info(f"[Shopify] Updated Pivota order {order_id} with shopify_order_id={shopify_order_id}")
                
                # 记录事件
                await log_order_event(
                    event_type="shopify_order_created",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={"shopify_order_id": shopify_order_id}
                )
                
                logger.info(f"[Shopify] ✅ Successfully created Shopify order {shopify_order_id} for Pivota order {order_id}")
                return True
            else:
                error_msg = response.text[:500]
                logger.error(f"[Shopify] ❌ API error: {response.status_code} - {error_msg}")
                
                # 记录失败事件
                await log_order_event(
                    event_type="shopify_order_failed",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={
                        "status_code": response.status_code,
                        "error": error_msg
                    }
                )
                return False
                
    except Exception as e:
        logger.error(f"[Shopify] ❌ Exception in create_shopify_order: {type(e).__name__}: {e}", exc_info=True)
        
        # 记录异常
        try:
            await log_order_event(
                event_type="shopify_order_error",
                order_id=order_id,
                merchant_id=order.get("merchant_id", "unknown") if order else "unknown",
                metadata={"error": str(e), "error_type": type(e).__name__}
            )
        except Exception as log_error:
            logger.error(f"[Shopify] Failed to log order event: {log_error}")
            
        return False


# ============================================================================
# 订单状态更新（Admin/Webhook 调用）
# ============================================================================

@router.get("/{order_id}/debug")
async def debug_order_data(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """调试端点：查看订单的原始数据结构和Shopify credentials"""
    try:
        order = await get_order(order_id)
        if not order:
            return {"error": "Order not found"}
        
        # 获取 Shopify credentials
        from services.merchant_store_service import get_primary_store
        store_info = await get_primary_store(order["merchant_id"])
        
        credentials_info = {}
        if store_info:
            credentials_info = {
                "platform": store_info.get("platform"),
                "domain": store_info.get("domain"),
                "has_api_key": bool(store_info.get("api_key")),
                "api_key_length": len(store_info.get("api_key", "")),
                "api_key_prefix": store_info.get("api_key", "")[:15] if store_info.get("api_key") else None,
                "api_key_suffix": store_info.get("api_key", "")[-10:] if store_info.get("api_key") else None,
                "status": store_info.get("status"),
                "store_id": store_info.get("store_id")
            }
        
        # 检查数据类型
        return {
            "order_id": order_id,
            "merchant_id": order["merchant_id"],
            "shopify_credentials": credentials_info,
            "data_types": {
                "items": str(type(order.get("items"))),
                "items_count": len(order.get("items", [])),
                "shipping_address": str(type(order.get("shipping_address"))),
                "customer_email": order.get("customer_email"),
            }
        }
    except Exception as e:
        logger.error(f"Debug error: {type(e).__name__}: {e}", exc_info=True)
        return {"error": str(e), "error_type": type(e).__name__}


@router.post("/{order_id}/create-shopify")
async def trigger_shopify_order(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """Manually trigger Shopify order creation for debugging"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.get("shopify_order_id"):
        return {"status": "already_exists", "shopify_order_id": order["shopify_order_id"]}
    
    if order.get("payment_status") != "paid":
        return {"status": "not_paid", "payment_status": order.get("payment_status")}
    
    try:
        success = await create_shopify_order(order_id)
        if success:
            updated_order = await get_order(order_id)
            return {
                "status": "success",
                "shopify_order_id": updated_order.get("shopify_order_id"),
                "message": "Shopify order created"
            }
        else:
            # 查询最近的order事件日志来获取错误
            event_query = """
                SELECT event_type, metadata, created_at
                FROM order_events
                WHERE order_id = :order_id
                ORDER BY created_at DESC
                LIMIT 5
            """
            events = await database.fetch_all(event_query, {"order_id": order_id})
            
            error_details = []
            for event in events:
                if event["event_type"] in ["shopify_order_failed", "shopify_order_error"]:
                    error_details.append({
                        "event": event["event_type"],
                        "metadata": event["metadata"],
                        "time": str(event["created_at"])
                    })
            
            return {
                "status": "failed",
                "message": "Shopify order creation failed",
                "error_details": error_details if error_details else "No error events found - check Railway logs for [Shopify] entries"
            }
    except Exception as e:
        logger.error(f"Exception in trigger_shopify_order: {type(e).__name__}: {e}", exc_info=True)
        return {"status": "error", "error": str(e), "error_type": type(e).__name__}

@router.post("/{order_id}/ship")
async def mark_order_as_shipped(
    order_id: str,
    tracking_number: str,
    carrier: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """标记订单已发货"""
    success = await mark_order_shipped(order_id, tracking_number, carrier)
    
    if not success:
        raise HTTPException(status_code=404, detail="Order not found")
    
    order = await get_order(order_id)
    
    # 记录发货事件
    await log_order_event(
        event_type="order_shipped",
        order_id=order_id,
        merchant_id=order["merchant_id"],
        metadata={
            "tracking_number": tracking_number,
            "carrier": carrier
        }
    )
    
    return {
        "status": "success",
        "message": "Order marked as shipped",
        "order_id": order_id,
        "tracking_number": tracking_number
    }


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    reason: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """取消订单"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["payment_status"] == "paid":
        raise HTTPException(
            status_code=400, 
            detail="Cannot cancel paid order. Please process refund first."
        )
    
    success = await update_order_status(
        order_id=order_id,
        status="cancelled",
        cancelled_at=datetime.now(),
        metadata={**(order.get("metadata") or {}), "cancellation_reason": reason}
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to cancel order")
    
    # 记录取消事件
    await log_order_event(
        event_type="order_cancelled",
        order_id=order_id,
        merchant_id=order["merchant_id"],
        metadata={"reason": reason}
    )
    
    return {
        "status": "success",
        "message": "Order cancelled",
        "order_id": order_id
    }

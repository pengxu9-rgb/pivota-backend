"""
Agent 管理 API
用于创建、管理和监控 AI Agents
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from pydantic import BaseModel, EmailStr

from db.agents import (
    create_agent,
    get_agent,
    get_agent_analytics,
    agents
)
from db.database import database
from utils.auth import AGENT_OR_EMPLOYEE_STAFF_ROLES, EMPLOYEE_STAFF_ROLES, can_access_agent, get_current_employee, get_current_user, require_admin, verify_jwt_token
from utils.logger import logger


router = APIRouter(prefix="/agents", tags=["agent-management"])


# ============================================================================
# 请求模型
# ============================================================================

class CreateAgentRequest(BaseModel):
    """创建 Agent 请求"""
    agent_name: str
    agent_type: str  # chatbot, voice_assistant, custom
    description: Optional[str] = None
    owner_email: Optional[EmailStr] = None
    rate_limit: int = 100  # 每分钟请求数
    daily_quota: int = 10000  # 每日配额
    allowed_merchants: Optional[List[str]] = None  # null = 所有商户
    webhook_url: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class UpdateAgentRequest(BaseModel):
    """更新 Agent 请求"""
    agent_name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    rate_limit: Optional[int] = None
    daily_quota: Optional[int] = None
    allowed_merchants: Optional[List[str]] = None
    webhook_url: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# ============================================================================
# Agent CRUD
# ============================================================================

@router.post("/create")
async def create_new_agent(
    request: CreateAgentRequest,
    admin_user: dict = Depends(get_current_employee)
):
    """
    创建新的 AI Agent
    
    返回 API Key（仅显示一次）
    """
    try:
        result = await create_agent(
            agent_name=request.agent_name,
            agent_type=request.agent_type,
            description=request.description,
            owner_email=request.owner_email,
            rate_limit=request.rate_limit,
            daily_quota=request.daily_quota,
            allowed_merchants=request.allowed_merchants,
            webhook_url=request.webhook_url,
            metadata=request.metadata
        )
        
        logger.info(f"Agent created: {result['agent_id']} by admin {admin_user['user_id']}")
        
        return {
            "status": "success",
            "agent": result,
            "warning": "Please save the API key securely. It won't be shown again."
        }
        
    except Exception as e:
        logger.error(f"Failed to create agent: {e}")
        raise HTTPException(status_code=500, detail="Failed to create agent")


def _is_own_agent_record(current_user: dict, agent_id: str, agent: dict) -> bool:
    """Does this token identify the agent whose record was just loaded?

    utils.auth.can_access_agent reads only the `agent_id` claim, but not every
    agent token carries it -- which is why the guards in this module have
    always accepted more. They do not agree on what: update_agent takes
    `user_id`, while the analytics, usage, funnel, query-analytics and
    merchants reads take `email`. A token carrying only one spelling is
    therefore itself on some of its own sub-routes and a stranger on others.

    The `email` spelling in those five is `email == agent_id`, which can only
    ever match because agent ids happen to be shaped `agent_<hex>` and an
    address is not -- a coincidence of format, not an ownership relation, and
    one that stops holding the day an id is minted differently. Here the email
    claim is matched against the RECORD's own `owner_email` instead: that is
    the actual relation, `users.email` is UNIQUE NOT NULL so it names exactly
    one account, and it does not care how ids are shaped.
    """
    target = str(agent_id)
    if any(
        str(current_user.get(claim) or "") == target
        for claim in ("agent_id", "user_id")
    ):
        return True
    email = str(current_user.get("email") or "").strip().lower()
    owner_email = str((agent or {}).get("owner_email") or "").strip().lower()
    return bool(email) and email == owner_email


@router.get("/{agent_id}")
async def get_agent_details(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """获取 Agent 详情（不含 API Key）- v331ebf4e+"""
    # Allow agent to access their own details, or admin/employee to access any
    current_role = current_user.get("role")
    current_agent_id = current_user.get("agent_id") or current_user.get("user_id")
    current_email = current_user.get("email")
    
    logger.info(
        f"[GET /agents/{{id}}] Auth check: role={current_role}, "
        f"email={current_email}, agent_id={current_agent_id}, "
        f"requested={agent_id}"
    )
    
    if current_role not in AGENT_OR_EMPLOYEE_STAFF_ROLES:
        logger.error(f"[GET /agents/{{id}}] Role '{current_role}' not allowed")
        raise HTTPException(status_code=403, detail=f"Access denied - invalid role: {current_role}")
    
    # Get the requested agent details first
    agent = await get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # OWNERSHIP. This used to read: "For agent role: allow access (API key
    # already removed by get_agent) / No additional restrictions needed since
    # sensitive data is filtered". Stripping api_key/api_key_hash is not
    # filtering: what get_agent still returns is owner_email, webhook_url,
    # allowed_merchants, metadata, rate_limit and daily_quota -- another
    # tenant's contact address, their callback endpoint, the list of merchants
    # they are cleared for, and their commercial limits. AGENT_OR_EMPLOYEE_
    # STAFF_ROLES decides who may attempt the route; it never decided WHOSE
    # record. Staff keep cross-agent reads; an `agent` gets their own.
    #
    # Ownership must be at least as wide as this route's own sub-routes, or an
    # agent is refused the record whose /analytics, /usage and /funnel it can
    # read -- see _is_own_agent_record for why the three claim spellings exist
    # and why the email one is matched against owner_email rather than the id.
    if not can_access_agent(current_user, agent_id) and not _is_own_agent_record(
        current_user, agent_id, agent
    ):
        logger.error(
            f"[GET /agents/{{id}}] {current_role} {current_email} denied: "
            f"{current_agent_id} is not agent {agent_id}"
        )
        raise HTTPException(status_code=403, detail="Access denied - not your agent")

    logger.info(f"[Agent Access OK] {current_role} {current_email} → agent {agent_id}")
    
    return {
        "status": "success",
        "agent": agent
    }


@router.put("/{agent_id}")
async def update_agent(
    agent_id: str,
    request: UpdateAgentRequest,
    current_user: dict = Depends(get_current_user)
):
    """更新 Agent 配置"""
    try:
        # 检查 Agent 是否存在
        agent = await get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        current_role = current_user.get("role")
        current_agent_id = current_user.get("agent_id") or current_user.get("user_id")
        if current_role not in EMPLOYEE_STAFF_ROLES and str(current_agent_id or "") != str(agent_id):
            raise HTTPException(status_code=403, detail="Not authorized")
        
        # 构建更新数据
        update_data = {}
        if request.agent_name is not None:
            update_data["agent_name"] = request.agent_name
        if request.description is not None:
            update_data["description"] = request.description
        if request.is_active is not None:
            update_data["is_active"] = request.is_active
        if request.rate_limit is not None:
            update_data["rate_limit"] = request.rate_limit
        if request.daily_quota is not None:
            update_data["daily_quota"] = request.daily_quota
        if request.allowed_merchants is not None:
            update_data["allowed_merchants"] = request.allowed_merchants
        if request.webhook_url is not None:
            update_data["webhook_url"] = request.webhook_url
        if request.metadata is not None:
            update_data["metadata"] = request.metadata
        
        update_data["updated_at"] = datetime.utcnow()
        
        # 执行更新
        query = agents.update().where(agents.c.agent_id == agent_id).values(**update_data)
        await database.execute(query)
        
        logger.info(f"Agent {agent_id} updated by {current_user.get('email') or current_user.get('user_id')}")
        
        return {
            "status": "success",
            "message": "Agent updated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update agent: {e}")
        raise HTTPException(status_code=500, detail="Failed to update agent")


@router.patch("/{agent_id}/tier")
async def update_agent_tier(
    agent_id: str,
    tier_data: Dict[str, Any],
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 6.2] Update agent tier (basic/premium)
    
    Only employees/admins can change agent tiers.
    Valid values: 'basic', 'premium'
    """
    if current_user["role"] not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Validate tier
        new_tier = tier_data.get("agent_type", "").lower().strip()
        if new_tier not in ["basic", "premium"]:
            raise HTTPException(
                status_code=400,
                detail="agent_type must be 'basic' or 'premium'"
            )
        
        # Check agent exists
        agent = await get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        old_tier = agent.get("agent_type", "basic")
        
        # Update tier
        await database.execute(
            """UPDATE agents 
               SET agent_type = :tier
               WHERE agent_id = :agent_id""",
            {"tier": new_tier, "agent_id": agent_id}
        )
        
        logger.info(
            f"[Phase 6.2] Agent {agent_id} tier changed: {old_tier} → {new_tier} "
            f"by {current_user.get('email')}"
        )
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "agent_type": new_tier,
            "previous_tier": old_tier,
            "message": f"Agent tier updated to {new_tier}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update agent tier: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update tier: {str(e)}")


@router.delete("/{agent_id}")
async def deactivate_agent(
    agent_id: str,
    admin_user: dict = Depends(get_current_employee)
):
    """停用 Agent（软删除）"""
    try:
        # 检查 Agent 是否存在
        agent = await get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # 停用 Agent
        query = agents.update().where(agents.c.agent_id == agent_id).values(
            is_active=False,
            updated_at=datetime.utcnow()
        )
        await database.execute(query)
        
        logger.info(f"Agent {agent_id} deactivated by admin {admin_user['user_id']}")
        
        return {
            "status": "success",
            "message": "Agent deactivated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to deactivate agent: {e}")
        raise HTTPException(status_code=500, detail="Failed to deactivate agent")


# ============================================================================
# Agent 列表和搜索
# ============================================================================

@router.get("/")
async def list_agents(
    is_active: Optional[bool] = None,
    agent_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    admin_user: dict = Depends(get_current_user)  # Allow authenticated users
):
    """
    列出所有 Agents
    
    支持过滤和搜索
    """
    try:
        # 构建查询
        query = agents.select()
        
        if is_active is not None:
            query = query.where(agents.c.is_active == is_active)
        
        if agent_type:
            query = query.where(agents.c.agent_type == agent_type)
        
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                (agents.c.agent_name.ilike(search_pattern)) |
                (agents.c.description.ilike(search_pattern)) |
                (agents.c.owner_email.ilike(search_pattern))
            )
        
        # 排序和分页
        query = query.order_by(agents.c.created_at.desc()).limit(limit).offset(offset)
        
        # 执行查询
        results = await database.fetch_all(query)
        
        # 移除敏感信息
        agent_list = []
        for agent in results:
            agent_dict = dict(agent)
            agent_dict.pop("api_key", None)
            agent_dict.pop("api_key_hash", None)
            agent_list.append(agent_dict)
        
        # 获取总数
        count_query = "SELECT COUNT(*) as count FROM agents"
        if is_active is not None:
            count_query += f" WHERE is_active = {is_active}"
        
        count_result = await database.fetch_one(count_query)
        total = count_result["count"] if count_result else 0
        
        return {
            "status": "success",
            "total": total,
            "limit": limit,
            "offset": offset,
            "agents": agent_list
        }
        
    except Exception as e:
        logger.error(f"Failed to list agents: {e}")
        raise HTTPException(status_code=500, detail="Failed to list agents")


# ============================================================================
# Agent 分析和监控
# ============================================================================

@router.get("/{agent_id}/analytics")
async def get_agent_analytics_endpoint(
    agent_id: str,
    days: int = Query(default=30, le=365),
    current_user: dict = Depends(get_current_user)
):
    """
    获取 Agent 分析数据
    
    包括：
    - 请求量趋势
    - 订单转化率
    - 错误率
    - 热门端点
    - GMV
    """
    try:
        # 检查 Agent 是否存在
        agent = await get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        if current_user.get("role") not in ["admin", "employee", "super_admin"]:
            user_agent_id = current_user.get("agent_id") or current_user.get("email")
            if str(user_agent_id or "") != str(agent_id):
                raise HTTPException(status_code=403, detail="Not authorized")
        
        # 获取分析数据
        start_date = datetime.utcnow() - timedelta(days=days)
        analytics = await get_agent_analytics(agent_id, start_date=start_date)
        
        return {
            "status": "success",
            "agent": {
                "agent_id": agent["agent_id"],
                "agent_name": agent["agent_name"],
                "agent_type": agent["agent_type"]
            },
            "analytics": analytics
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get agent analytics: {e}")
        raise HTTPException(status_code=500, detail="Failed to get analytics")


@router.get("/{agent_id}/usage")
async def get_agent_usage(
    agent_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = Query(default=100, le=1000),
    current_user: dict = Depends(get_current_user)
):
    """
    获取 Agent 使用日志
    
    详细的 API 调用记录
    """
    try:
        if current_user.get("role") not in ["admin", "employee", "super_admin"]:
            user_agent_id = current_user.get("agent_id") or current_user.get("email")
            if str(user_agent_id or "") != str(agent_id):
                raise HTTPException(status_code=403, detail="Not authorized")

        # 默认时间范围
        if not end_date:
            end_date = datetime.utcnow()
        if not start_date:
            start_date = end_date - timedelta(days=1)
        
        # 查询使用日志
        query = f"""
            SELECT * FROM agent_usage_logs
            WHERE agent_id = :agent_id
            AND timestamp BETWEEN :start_date AND :end_date
            ORDER BY timestamp DESC
            LIMIT :limit
        """
        
        logs = await database.fetch_all(
            query,
            {
                "agent_id": agent_id,
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit
            }
        )
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat()
            },
            "total": len(logs),
            "logs": [dict(log) for log in logs]
        }
        
    except Exception as e:
        logger.error(f"Failed to get agent usage: {e}")
        raise HTTPException(status_code=500, detail="Failed to get usage logs")


@router.get("/{agent_id}/funnel")
async def get_agent_conversion_funnel(
    agent_id: str,
    days: int = Query(7, ge=1, le=90),
    current_user: dict = Depends(get_current_user)
):
    """
    Get order conversion funnel for an agent
    Shows: orders_initiated → payment_attempted → orders_completed
    """
    try:
        # Verify access (allow agent via agent_id/email fallback)
        if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
            user_agent_id = current_user.get("agent_id") or current_user.get("email")
            if user_agent_id != agent_id:
                raise HTTPException(status_code=403, detail="Not authorized")
        
        since = datetime.now() - timedelta(days=days)
        
        # Count orders initiated (any order creation attempt)
        orders_initiated = await database.fetch_val(
            """
            SELECT COUNT(DISTINCT order_id) 
            FROM orders 
            WHERE agent_id = :agent_id 
            AND created_at >= :since
            """,
            {"agent_id": agent_id, "since": since}
        ) or 0
        
        # Payment attempted: count all non-cancelled orders (safe query)
        payment_attempted = await database.fetch_val(
            """
            SELECT COUNT(DISTINCT order_id) 
            FROM orders 
            WHERE agent_id = :agent_id 
            AND created_at >= :since
            AND status != 'cancelled'
            """,
            {"agent_id": agent_id, "since": since}
        ) or 0
        
        # Completed: orders with successful payment status
        orders_completed = await database.fetch_val(
            """
            SELECT COUNT(DISTINCT order_id) 
            FROM orders 
            WHERE agent_id = :agent_id 
            AND created_at >= :since
            AND (payment_status IN ('succeeded', 'completed', 'paid') OR status = 'completed')
            """,
            {"agent_id": agent_id, "since": since}
        ) or 0
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "period_days": days,
            "orders_initiated": orders_initiated,
            "payment_attempted": payment_attempted,
            "orders_completed": orders_completed,
            "conversion_rate": round((orders_completed / orders_initiated * 100) if orders_initiated > 0 else 0, 2)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get funnel data: {e}")
        import traceback
        traceback.print_exc()
        # Return zeros if error (no mock data)
        return {
            "status": "error",
            "agent_id": agent_id,
            "period_days": days,
            "orders_initiated": 0,
            "payment_attempted": 0,
            "orders_completed": 0,
            "conversion_rate": 0,
            "error": str(e)
        }


@router.get("/{agent_id}/query-analytics")
async def get_agent_query_analytics(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get MCP query analytics for an agent
    Tracks: product searches, inventory checks, price queries
    """
    try:
        # Verify access
        if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
            user_agent_id = current_user.get("agent_id") or current_user.get("email")
            if user_agent_id != agent_id:
                raise HTTPException(status_code=403, detail="Not authorized")
        
        # Get query counts from agent_usage_logs
        last_24h = datetime.now() - timedelta(hours=24)
        last_48h = datetime.now() - timedelta(hours=48)
        
        # Product searches
        product_searches = await database.fetch_val(
            """
            SELECT COUNT(*) FROM agent_usage_logs
            WHERE agent_id = :agent_id 
            AND timestamp >= :since
            AND (endpoint LIKE '%/products/search%' OR endpoint LIKE '%/catalog/search%' OR endpoint LIKE '%/products%')
            """,
            {"agent_id": agent_id, "since": last_24h}
        ) or 0
        
        product_searches_prev = await database.fetch_val(
            """
            SELECT COUNT(*) FROM agent_usage_logs
            WHERE agent_id = :agent_id 
            AND timestamp >= :since_prev AND timestamp < :since
            AND (endpoint LIKE '%/products/search%' OR endpoint LIKE '%/catalog/search%' OR endpoint LIKE '%/products%')
            """,
            {"agent_id": agent_id, "since": last_24h, "since_prev": last_48h}
        ) or 0
        
        # Inventory checks
        inventory_checks = await database.fetch_val(
            """
            SELECT COUNT(*) FROM agent_usage_logs
            WHERE agent_id = :agent_id 
            AND timestamp >= :since
            AND endpoint LIKE '%/inventory%'
            """,
            {"agent_id": agent_id, "since": last_24h}
        ) or 0
        
        inventory_checks_prev = await database.fetch_val(
            """
            SELECT COUNT(*) FROM agent_usage_logs
            WHERE agent_id = :agent_id 
            AND timestamp >= :since_prev AND timestamp < :since
            AND endpoint LIKE '%/inventory%'
            """,
            {"agent_id": agent_id, "since": last_24h, "since_prev": last_48h}
        ) or 0
        
        # Price queries
        price_queries = await database.fetch_val(
            """
            SELECT COUNT(*) FROM agent_usage_logs
            WHERE agent_id = :agent_id 
            AND timestamp >= :since
            AND endpoint LIKE '%/pricing%'
            """,
            {"agent_id": agent_id, "since": last_24h}
        ) or 0
        
        price_queries_prev = await database.fetch_val(
            """
            SELECT COUNT(*) FROM agent_usage_logs
            WHERE agent_id = :agent_id 
            AND timestamp >= :since_prev AND timestamp < :since
            AND endpoint LIKE '%/pricing%'
            """,
            {"agent_id": agent_id, "since": last_24h, "since_prev": last_48h}
        ) or 0
        
        # Calculate trends
        def get_trend(current, previous):
            if previous == 0:
                return ("stable", 0) if current == 0 else ("up", 100)
            change = ((current - previous) / previous * 100)
            if abs(change) < 5:
                return ("stable", round(change, 1))
            elif change > 0:
                return ("up", round(change, 1))
            else:
                return ("down", round(abs(change), 1))
        
        ps_trend, ps_change = get_trend(product_searches, product_searches_prev)
        ic_trend, ic_change = get_trend(inventory_checks, inventory_checks_prev)
        pq_trend, pq_change = get_trend(price_queries, price_queries_prev)
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "product_searches": product_searches,
            "product_searches_trend": ps_trend,
            "product_searches_change": ps_change,
            "inventory_checks": inventory_checks,
            "inventory_checks_trend": ic_trend,
            "inventory_checks_change": ic_change,
            "price_queries": price_queries,
            "price_queries_trend": pq_trend,
            "price_queries_change": pq_change
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get query analytics: {e}")
        # Return an honest unavailable state instead of fabricated analytics.
        return {
            "status": "error",
            "agent_id": agent_id,
            "product_searches": 0,
            "product_searches_trend": "stable",
            "product_searches_change": 0,
            "inventory_checks": 0,
            "inventory_checks_trend": "stable",
            "inventory_checks_change": 0,
            "price_queries": 0,
            "price_queries_trend": "stable",
            "price_queries_change": 0,
            "error": str(e),
        }


@router.get("/{agent_id}/merchants")
async def get_agent_merchant_authorizations(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get list of merchants this agent is authorized to access
    """
    try:
        # Verify access
        if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
            user_agent_id = current_user.get("agent_id") or current_user.get("email")
            if user_agent_id != agent_id:
                raise HTTPException(status_code=403, detail="Not authorized")
        
        # Get agent's allowed merchants
        agent = await get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        allowed_merchants = agent.get("allowed_merchants")
        
        # If null, agent has access to all merchants
        if allowed_merchants is None:
            merchants = await database.fetch_all(
                """
                SELECT 
                    m.merchant_id, 
                    m.business_name, 
                    m.status,
                    m.contact_email,
                    m.store_url,
                    m.region,
                    COUNT(DISTINCT o.order_id) as total_orders,
                    COALESCE(SUM(o.total), 0) as total_gmv
                FROM merchant_onboarding m
                LEFT JOIN orders o ON m.merchant_id = o.merchant_id 
                    AND o.agent_id = :agent_id
                    AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)
                WHERE m.status = 'approved'
                GROUP BY m.merchant_id, m.business_name, m.status, m.contact_email, m.store_url, m.region
                LIMIT 100
                """,
                {"agent_id": agent_id}
            )
        else:
            if len(allowed_merchants) == 0:
                merchants = []
            else:
                merchants = await database.fetch_all(
                    """
                    SELECT 
                        m.merchant_id, 
                        m.business_name, 
                        m.status,
                        m.contact_email,
                        m.store_url,
                        m.region,
                        COUNT(DISTINCT o.order_id) as total_orders,
                        COALESCE(SUM(o.total), 0) as total_gmv
                    FROM merchant_onboarding m
                    LEFT JOIN orders o ON m.merchant_id = o.merchant_id 
                        AND o.agent_id = :agent_id
                        AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)
                    WHERE m.merchant_id = ANY(:merchant_ids)
                    GROUP BY m.merchant_id, m.business_name, m.status, m.contact_email, m.store_url, m.region
                    """,
                    {"merchant_ids": allowed_merchants, "agent_id": agent_id}
                )
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "access_type": "all" if allowed_merchants is None else "restricted",
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "status": m["status"],
                    "contact_email": m["contact_email"],
                    "store_url": m["store_url"],
                    "region": m["region"],
                    "total_orders": m["total_orders"],
                    "total_gmv": float(m["total_gmv"])
                }
                for m in merchants
            ],
            "count": len(merchants)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get merchant authorizations: {e}")
        raise HTTPException(status_code=500, detail="Failed to get merchant authorizations")

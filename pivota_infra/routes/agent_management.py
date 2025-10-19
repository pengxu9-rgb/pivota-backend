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
from utils.auth import require_admin, get_current_employee, get_current_user, verify_jwt_token
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


@router.get("/{agent_id}")
async def get_agent_details(
    agent_id: str,
    admin_user: dict = Depends(get_current_user)  # Allow authenticated users
):
    """获取 Agent 详情（不含 API Key）"""
    agent = await get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    return {
        "status": "success",
        "agent": agent
    }


@router.put("/{agent_id}")
async def update_agent(
    agent_id: str,
    request: UpdateAgentRequest,
    admin_user: dict = Depends(get_current_employee)
):
    """更新 Agent 配置"""
    try:
        # 检查 Agent 是否存在
        agent = await get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
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
        
        logger.info(f"Agent {agent_id} updated by admin {admin_user['user_id']}")
        
        return {
            "status": "success",
            "message": "Agent updated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update agent: {e}")
        raise HTTPException(status_code=500, detail="Failed to update agent")


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
    admin_user: dict = Depends(get_current_user)  # Allow authenticated users
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
    admin_user: dict = Depends(get_current_employee)
):
    """
    获取 Agent 使用日志
    
    详细的 API 调用记录
    """
    try:
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


@router.post("/{agent_id}/reset-api-key")
async def reset_agent_api_key(
    agent_id: str,
    admin_user: dict = Depends(get_current_employee)
):
    """
    重置 Agent API Key
    
    生成新的 API Key，旧的将失效
    """
    try:
        import secrets
        import hashlib
        
        # 检查 Agent 是否存在
        agent = await get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # 生成新的 API Key
        new_api_key = f"ak_{secrets.token_hex(32)}"
        new_api_key_hash = hashlib.sha256(new_api_key.encode()).hexdigest()
        
        # 更新数据库
        query = agents.update().where(agents.c.agent_id == agent_id).values(
            api_key=new_api_key,
            api_key_hash=new_api_key_hash,
            updated_at=datetime.utcnow()
        )
        await database.execute(query)
        
        logger.info(f"API Key reset for agent {agent_id} by admin {admin_user['user_id']}")
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "new_api_key": new_api_key,
            "warning": "Please save the new API key securely. The old key is now invalid."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reset API key: {e}")
        raise HTTPException(status_code=500, detail="Failed to reset API key")

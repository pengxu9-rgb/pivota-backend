from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from routes.agent_auth import AgentContext, get_agent_context
from db.agent_product_events import log_product_events
from utils.logger import logger


router = APIRouter(prefix="/agent/v1", tags=["agent-events"])


class ProductClickEvent(BaseModel):
    """
    Lightweight click tracking payload for Agent / UI gateway.
    """

    merchant_id: str
    platform: Optional[str] = None
    platform_product_id: str
    query: Optional[str] = None
    position: Optional[int] = None
    ranking_score: Optional[float] = None
    quality_content_score: Optional[float] = None
    quality_model_readiness: Optional[float] = None


@router.post("/events/product-click")
async def track_product_click(
    event: ProductClickEvent,
    context: AgentContext = Depends(get_agent_context),
):
    """
    Track a product click explicitly from Agent Gateway / UI.

    用途：
    - 当对话 UI 中用户点击某个搜索结果卡片时，由 Gateway 调用此接口；
    - 不依赖后端是否调用商品详情接口，便于覆盖更多点击场景。
    """
    try:
        # 权限校验：Agent 必须能访问该商家
        if not context.can_access_merchant(event.merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")

        await log_product_events(
            [
                {
                    "agent_id": getattr(context, "agent_id", None),
                    "session_id": getattr(context, "session_id", None),
                    "event_type": "click",
                    "endpoint": "/agent/v1/events/product-click",
                    "query": event.query,
                    "merchant_id": event.merchant_id,
                    "platform": event.platform,
                    "platform_product_id": event.platform_product_id,
                    "ranking_score": event.ranking_score,
                    "position": event.position,
                    "quality_content_score": event.quality_content_score,
                    "quality_model_readiness": event.quality_model_readiness,
                }
            ]
        )

        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to track product click event: {e}")
        raise HTTPException(status_code=500, detail="Failed to track product click")


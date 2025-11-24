"""
Standardized Order Response Model
统一的订单响应模型，解决 amount/total 混乱问题
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Any
from datetime import datetime
from decimal import Decimal


class StandardOrderResponse(BaseModel):
    """标准化的订单响应格式"""
    
    # 核心字段（前端应该使用这些）
    order_id: str = Field(..., description="订单ID")
    total_amount: float = Field(..., description="订单总金额（前端应该使用这个）")
    status: str = Field(..., description="订单状态")
    payment_status: str = Field(..., description="支付状态")
    customer_email: str = Field(..., description="客户邮箱")
    created_at: Optional[datetime] = Field(None, description="创建时间")
    
    # 详细信息
    currency: str = Field(default="USD", description="货币")
    items: Optional[List[Any]] = Field(default=[], description="订单项目")
    shipping_address: Optional[dict] = Field(default={}, description="配送地址")
    
    # 兼容性字段（为了向后兼容，但不推荐使用）
    amount: Optional[float] = Field(None, description="[已废弃] 请使用 total_amount")
    total: Optional[float] = Field(None, description="[已废弃] 请使用 total_amount")
    
    # PSP 相关
    psp_used: Optional[str] = Field(None, description="使用的支付服务商")
    payment_method: Optional[str] = Field(None, description="支付方式")
    
    class Config:
        schema_extra = {
            "example": {
                "order_id": "ORD_123456",
                "total_amount": 99.99,
                "status": "completed",
                "payment_status": "paid",
                "customer_email": "customer@example.com",
                "currency": "USD",
                "psp_used": "stripe",
                "created_at": "2024-10-27T10:00:00Z"
            }
        }


def format_order_for_response(order_data: dict) -> dict:
    """
    将数据库订单数据格式化为标准响应
    只使用 total 字段
    """
    # 只使用 total 字段
    total = order_data.get('total', 0)
    
    return {
        "order_id": order_data.get('order_id'),
        "total": float(total) if total else 0,
        "total_amount": float(total) if total else 0,  # 前端兼容性
        "status": order_data.get('status', 'pending'),
        "payment_status": order_data.get('payment_status', 'unpaid'),
        "customer_email": order_data.get('customer_email'),
        "created_at": order_data.get('created_at'),
        "currency": order_data.get('currency', 'USD'),
        "items": order_data.get('items', []),
        "shipping_address": order_data.get('shipping_address', {}),
        "psp_used": order_data.get('psp_used'),
        "psp_id": order_data.get('psp_id'),  # Include psp_id for PSP metrics
        "payment_method": order_data.get('payment_method'),
    }

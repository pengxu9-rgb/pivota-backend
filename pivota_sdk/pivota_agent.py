"""
Pivota Agent SDK
简化 AI Agent 与 Pivota API 的集成
"""

import requests
import json
from typing import Optional, List, Dict, Any
from datetime import datetime
from urllib.parse import urljoin
import logging


class PivotaAgent:
    """
    Pivota Agent SDK 主类
    
    使用示例:
    ```python
    from pivota_sdk import PivotaAgent
    
    # 初始化
    agent = PivotaAgent(
        api_key="ak_your_api_key_here",
        base_url="https://api.pivota.com"  # 或使用默认值
    )
    
    # 搜索产品
    products = agent.search_products(
        merchant_id="merch_123",
        query="coffee",
        min_price=10,
        max_price=50
    )
    
    # 创建订单
    order = agent.create_order(
        merchant_id="merch_123",
        items=[...],
        customer_email="customer@example.com",
        shipping_address={...}
    )
    ```
    """
    
    DEFAULT_BASE_URL = "https://web-production-fedb.up.railway.app"
    
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        timeout: int = 30,
        max_retries: int = 3,
        debug: bool = False
    ):
        """
        初始化 Pivota Agent SDK
        
        Args:
            api_key: Agent API Key (格式: ak_xxx)
            base_url: Pivota API 基础 URL
            timeout: 请求超时时间（秒）
            max_retries: 最大重试次数
            debug: 是否开启调试日志
        """
        self.api_key = api_key
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self.timeout = timeout
        self.max_retries = max_retries
        
        # 设置日志
        self.logger = logging.getLogger("PivotaAgent")
        if debug:
            self.logger.setLevel(logging.DEBUG)
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            )
            self.logger.addHandler(handler)
        
        # HTTP Session
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "PivotaAgent-SDK/1.0"
        })
    
    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_count: int = 0
    ) -> Dict[str, Any]:
        """
        发送 HTTP 请求
        
        包含自动重试和错误处理
        """
        url = urljoin(self.base_url, endpoint)
        
        self.logger.debug(f"{method} {url}")
        if data:
            self.logger.debug(f"Data: {json.dumps(data, indent=2)}")
        
        try:
            response = self.session.request(
                method=method,
                url=url,
                json=data,
                params=params,
                timeout=self.timeout
            )
            
            # 处理响应
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                # 速率限制
                retry_after = response.headers.get("Retry-After", 60)
                raise RateLimitError(
                    f"Rate limit exceeded. Retry after {retry_after} seconds",
                    retry_after=int(retry_after)
                )
            elif response.status_code >= 500 and retry_count < self.max_retries:
                # 服务器错误，重试
                self.logger.warning(f"Server error {response.status_code}, retrying...")
                return self._request(method, endpoint, data, params, retry_count + 1)
            else:
                # 其他错误
                error_message = response.text
                try:
                    error_data = response.json()
                    error_message = error_data.get("detail", error_message)
                except:
                    pass
                
                raise PivotaAPIError(
                    f"API request failed: {response.status_code} - {error_message}",
                    status_code=response.status_code,
                    response=response
                )
                
        except requests.exceptions.Timeout:
            raise PivotaAPIError(f"Request timeout after {self.timeout} seconds")
        except requests.exceptions.ConnectionError:
            raise PivotaAPIError("Connection error. Please check your network")
        except Exception as e:
            if isinstance(e, (RateLimitError, PivotaAPIError)):
                raise
            raise PivotaAPIError(f"Unexpected error: {str(e)}")
    
    # ========================================================================
    # 产品管理
    # ========================================================================
    
    def search_products(
        self,
        merchant_id: str,
        query: Optional[str] = None,
        category: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        in_stock_only: bool = True,
        limit: int = 20,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        搜索产品
        
        Args:
            merchant_id: 商户 ID
            query: 搜索关键词
            category: 产品类别
            min_price: 最低价格
            max_price: 最高价格
            in_stock_only: 仅显示有库存产品
            limit: 返回数量限制
            offset: 偏移量（用于分页）
        
        Returns:
            包含产品列表的字典
        """
        params = {
            "merchant_id": merchant_id,
            "limit": limit,
            "offset": offset,
            "in_stock_only": in_stock_only
        }
        
        if query:
            params["query"] = query
        if category:
            params["category"] = category
        if min_price is not None:
            params["min_price"] = min_price
        if max_price is not None:
            params["max_price"] = max_price
        
        return self._request("GET", "/agent/v1/products/search", params=params)
    
    def get_product(self, merchant_id: str, product_id: str) -> Dict[str, Any]:
        """
        获取单个产品详情
        
        Args:
            merchant_id: 商户 ID
            product_id: 产品 ID
        
        Returns:
            产品详情
        """
        return self._request("GET", f"/agent/v1/products/{merchant_id}/{product_id}")
    
    # ========================================================================
    # 购物车管理
    # ========================================================================
    
    def validate_cart(
        self,
        merchant_id: str,
        items: List[Dict[str, Any]],
        shipping_country: str = "US"
    ) -> Dict[str, Any]:
        """
        验证购物车并计算价格
        
        Args:
            merchant_id: 商户 ID
            items: 购物车商品列表
                [{"product_id": "123", "quantity": 2}, ...]
            shipping_country: 配送国家代码
        
        Returns:
            验证结果和价格信息
        """
        data = {
            "merchant_id": merchant_id,
            "items": items,
            "shipping_country": shipping_country
        }
        
        return self._request("POST", "/agent/v1/cart/validate", data=data)
    
    # ========================================================================
    # 订单管理
    # ========================================================================
    
    def create_order(
        self,
        merchant_id: str,
        customer_email: str,
        items: List[Dict[str, Any]],
        shipping_address: Dict[str, Any],
        currency: str = "USD",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        创建订单
        
        Args:
            merchant_id: 商户 ID
            customer_email: 客户邮箱
            items: 订单商品列表
                [{
                    "product_id": "123",
                    "product_title": "Product Name",
                    "quantity": 1,
                    "unit_price": "99.99",
                    "subtotal": "99.99"
                }, ...]
            shipping_address: 收货地址
                {
                    "name": "Customer Name",
                    "address_line1": "123 Main St",
                    "city": "New York",
                    "postal_code": "10001",
                    "country": "US"
                }
            currency: 货币代码
            metadata: 额外元数据
        
        Returns:
            订单信息（包含 payment intent）
        """
        data = {
            "merchant_id": merchant_id,
            "customer_email": customer_email,
            "items": items,
            "shipping_address": shipping_address,
            "currency": currency
        }
        
        if metadata:
            data["metadata"] = metadata
        
        return self._request("POST", "/agent/v1/orders/create", data=data)
    
    def get_order(self, order_id: str) -> Dict[str, Any]:
        """
        获取订单状态
        
        Args:
            order_id: 订单 ID
        
        Returns:
            订单详情
        """
        return self._request("GET", f"/agent/v1/orders/{order_id}")
    
    def list_orders(
        self,
        merchant_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        列出订单
        
        Args:
            merchant_id: 商户 ID（可选，用于过滤）
            status: 订单状态（可选，用于过滤）
            limit: 返回数量限制
            offset: 偏移量（用于分页）
        
        Returns:
            订单列表
        """
        params = {
            "limit": limit,
            "offset": offset
        }
        
        if merchant_id:
            params["merchant_id"] = merchant_id
        if status:
            params["status"] = status
        
        return self._request("GET", "/agent/v1/orders", params=params)
    
    # ========================================================================
    # 分析
    # ========================================================================
    
    def get_analytics(self, days: int = 30) -> Dict[str, Any]:
        """
        获取 Agent 分析数据
        
        Args:
            days: 统计天数
        
        Returns:
            分析数据
        """
        return self._request("GET", "/agent/v1/analytics/summary", params={"days": days})
    
    # ========================================================================
    # 工具方法
    # ========================================================================
    
    def health_check(self) -> bool:
        """
        检查 API 连接状态
        
        Returns:
            True if API is accessible
        """
        try:
            response = self.session.get(
                urljoin(self.base_url, "/health"),
                timeout=5
            )
            return response.status_code == 200
        except:
            return False
    
    def close(self):
        """关闭 HTTP Session"""
        self.session.close()
    
    def __enter__(self):
        """Context manager 支持"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager 清理"""
        self.close()


# ============================================================================
# 异常类
# ============================================================================

class PivotaAPIError(Exception):
    """Pivota API 错误基类"""
    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[requests.Response] = None):
        self.message = message
        self.status_code = status_code
        self.response = response
        super().__init__(self.message)


class RateLimitError(PivotaAPIError):
    """速率限制错误"""
    def __init__(self, message: str, retry_after: int):
        self.retry_after = retry_after
        super().__init__(message, status_code=429)


# ============================================================================
# 便捷函数
# ============================================================================

def quick_order(
    api_key: str,
    merchant_id: str,
    product_ids: List[str],
    customer_email: str,
    shipping_address: Dict[str, Any]
) -> Dict[str, Any]:
    """
    快速下单函数
    
    简化的下单流程，自动处理产品查询和价格计算
    
    Example:
    ```python
    order = quick_order(
        api_key="ak_xxx",
        merchant_id="merch_123",
        product_ids=["prod_1", "prod_2"],
        customer_email="customer@example.com",
        shipping_address={
            "name": "John Doe",
            "address_line1": "123 Main St",
            "city": "New York",
            "postal_code": "10001",
            "country": "US"
        }
    )
    print(f"Order created: {order['order_id']}")
    print(f"Payment URL: {order['payment']['client_secret']}")
    ```
    """
    with PivotaAgent(api_key) as agent:
        # 1. 获取产品信息
        items = []
        for product_id in product_ids:
            try:
                product = agent.get_product(merchant_id, product_id)
                items.append({
                    "product_id": product_id,
                    "product_title": product["product"]["title"],
                    "quantity": 1,
                    "unit_price": product["product"]["price"],
                    "subtotal": product["product"]["price"]
                })
            except:
                # 如果获取失败，使用默认值
                items.append({
                    "product_id": product_id,
                    "product_title": f"Product {product_id}",
                    "quantity": 1,
                    "unit_price": "0",
                    "subtotal": "0"
                })
        
        # 2. 创建订单
        return agent.create_order(
            merchant_id=merchant_id,
            customer_email=customer_email,
            items=items,
            shipping_address=shipping_address
        )


# ============================================================================
# 导出
# ============================================================================

__all__ = [
    "PivotaAgent",
    "PivotaAPIError",
    "RateLimitError",
    "quick_order"
]

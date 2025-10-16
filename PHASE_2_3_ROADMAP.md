# 🗺️ Phase 2 & Phase 3 路线图

## ✅ Phase 2: 商户入驻 & PSP连接 - 进行中

### 已完成
- ✅ 商户注册 API
- ✅ KYC 自动审批（5秒模拟）
- ✅ PSP 连接端点
- ✅ PSP 验证逻辑（Stripe/Adyen）
- ✅ API 密钥生成
- ✅ 支付路由注册
- ✅ 前端入驻界面
- ✅ 管理员审批界面

### 当前问题
- 🐛 数据库事务错误（修复中）
- 🐛 PSP验证500错误（已修复async/sync问题）
- ⏳ 等待Render部署完成

### Phase 2 最终测试清单
- [ ] 商户注册成功
- [ ] KYC 5秒后自动批准
- [ ] 假Stripe密钥被拒绝（400错误）
- [ ] 真实Stripe测试密钥成功连接
- [ ] 生成API密钥
- [ ] 管理员查看商户列表
- [ ] 管理员手动批准/拒绝

---

## 🎯 Phase 3: 店铺集成 & 产品同步

### 目标
将商户的电商店铺连接到Pivota系统，实现产品、库存、订单的自动同步。

### 架构设计

```
商户(已入驻) 
   ↓
选择平台: Shopify / WooCommerce / Wix / Custom
   ↓
提供店铺信息: Store URL + API Token
   ↓
Pivota通过MCP连接店铺
   ↓
同步: 产品目录、库存、价格
   ↓
配置Webhook: 订单创建 → 触发支付
```

---

## 📋 Phase 3 详细任务

### 3.1 店铺连接 API

#### 新增端点
```python
POST /merchant/{merchant_id}/store/connect
- 输入: platform, store_url, api_token
- 验证店铺连接
- 保存店铺配置
- 返回: store_id, connection_status

GET /merchant/{merchant_id}/stores
- 列出商户所有连接的店铺

POST /merchant/{merchant_id}/store/{store_id}/sync
- 手动触发同步

DELETE /merchant/{merchant_id}/store/{store_id}
- 断开店铺连接
```

#### 数据库表
```sql
CREATE TABLE merchant_stores (
    id SERIAL PRIMARY KEY,
    merchant_id VARCHAR(50) REFERENCES merchant_onboarding(merchant_id),
    platform VARCHAR(50),  -- shopify, woocommerce, wix
    store_url VARCHAR(500),
    api_token TEXT,  -- encrypted
    store_name VARCHAR(255),
    connection_status VARCHAR(50),  -- connected, error, syncing
    last_sync_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

### 3.2 产品同步服务

#### MCP 集成
```python
# services/store_sync_service.py

async def sync_products(store_id: int):
    """从店铺同步产品到Pivota"""
    store = await get_store(store_id)
    
    if store.platform == "shopify":
        products = await shopify_mcp_client.get_products(store.store_url, store.api_token)
    elif store.platform == "woocommerce":
        products = await woocommerce_mcp_client.get_products(store.api_token)
    # ...
    
    # 保存到products表
    await save_products(products, store_id)
```

#### 产品表
```sql
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    store_id INTEGER REFERENCES merchant_stores(id),
    external_product_id VARCHAR(255),  -- 店铺平台的产品ID
    name VARCHAR(500),
    description TEXT,
    price DECIMAL(10, 2),
    currency VARCHAR(3),
    stock_quantity INTEGER,
    image_url VARCHAR(500),
    sku VARCHAR(100),
    synced_at TIMESTAMP
);
```

---

### 3.3 Webhook 配置

#### 自动配置
```python
POST /merchant/{merchant_id}/store/{store_id}/webhooks/setup
- 在店铺平台创建webhook
- 订单创建 → POST https://pivota.com/webhooks/order
- 自动触发支付流程
```

#### Webhook处理
```python
# routes/webhook_routes.py

@router.post("/webhooks/order")
async def handle_order_webhook(request: Request):
    """接收来自店铺的订单webhook"""
    payload = await request.json()
    
    # 验证webhook签名
    if not verify_webhook_signature(payload):
        raise HTTPException(403)
    
    # 提取订单信息
    order_id = payload["order_id"]
    amount = payload["total"]
    merchant_id = payload["merchant_id"]
    
    # 自动执行支付
    payment_result = await execute_payment(
        merchant_id=merchant_id,
        order_id=order_id,
        amount=amount
    )
    
    return {"status": "success", "payment_id": payment_result.id}
```

---

### 3.4 前端界面

#### 店铺管理页面
```
/merchant/stores
- 显示已连接店铺列表
- 添加新店铺按钮
- 同步状态指示器
- 产品数量统计
```

#### 产品目录页面
```
/merchant/products
- 显示所有同步的产品
- 搜索和筛选
- 库存预警
- 价格编辑（可选）
```

---

## 🔄 完整支付流程（Phase 3完成后）

```
1. 商户入驻（Phase 2）
   └─> 获得API密钥

2. 连接店铺（Phase 3）
   └─> 同步产品
   └─> 配置webhook

3. 客户在店铺下单
   └─> Webhook触发

4. Pivota自动处理
   └─> 验证商户API密钥
   └─> 查询产品信息
   └─> 执行支付（/payment/execute）
   └─> 路由到正确的PSP
   └─> 返回支付结果给店铺

5. 店铺更新订单状态
```

---

## 📊 数据流图

```
┌─────────────┐
│   Shopify   │
│   Store     │
└──────┬──────┘
       │ Webhook (Order Created)
       ↓
┌─────────────────────────────────┐
│  Pivota Webhook Handler         │
│  - Verify signature             │
│  - Extract order data           │
│  - Lookup merchant              │
└──────┬──────────────────────────┘
       │
       ↓
┌─────────────────────────────────┐
│  Payment Router                 │
│  - Use merchant API key         │
│  - Get PSP config               │
│  - Route to Stripe/Adyen        │
└──────┬──────────────────────────┘
       │
       ↓
┌─────────────────────────────────┐
│  PSP (Stripe/Adyen)             │
│  - Process payment              │
│  - Return result                │
└──────┬──────────────────────────┘
       │
       ↓
┌─────────────────────────────────┐
│  Shopify Webhook Response       │
│  - Update order status          │
│  - Mark as paid                 │
└─────────────────────────────────┘
```

---

## 🎯 Phase 3 优先级

### 高优先级（核心功能）
1. ✅ Shopify 集成（最常见）
2. ✅ 产品同步API
3. ✅ Webhook配置
4. ✅ 订单→支付自动流程

### 中优先级（增强功能）
5. WooCommerce 集成
6. 产品目录前端
7. 库存管理
8. 手动支付触发

### 低优先级（未来功能）
9. Wix 集成
10. 多店铺管理
11. 产品分析
12. 定价规则

---

## 📝 当前状态

**Phase 2 状态**: 🟡 90% 完成
- 核心功能已实现
- 正在修复数据库和PSP验证bug
- 需要最终测试

**Phase 3 状态**: 🔴 未开始
- 等待 Phase 2 完成
- 架构设计已完成
- 准备好开始实施

---

## 🚀 下一步行动

### 立即（Phase 2 收尾）
1. ⏳ 等待Render部署（3分钟）
2. 🧪 测试商户注册
3. 🧪 测试PSP验证
4. 📝 记录所有工作的endpoint

### 然后（Phase 3 启动）
1. 创建 merchant_stores 表
2. 实现 Shopify 连接 API
3. 集成 MCP Server
4. 测试产品同步
5. 配置 webhook

---

## ⏱️ 预计时间

- Phase 2 收尾: **1小时**
- Phase 3.1 (店铺连接): **2小时**
- Phase 3.2 (产品同步): **3小时**
- Phase 3.3 (Webhook): **2小时**
- Phase 3.4 (前端): **3小时**
- **总计**: 11小时

---

**当前重点**: 修复Phase 2的数据库事务错误，完成商户入驻测试。
**你想现在等待部署完成测试，还是先开始规划Phase 3的代码？**


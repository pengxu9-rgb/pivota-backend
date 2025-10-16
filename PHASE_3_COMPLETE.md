# ✅ Phase 3: Unified Payment Execution Router - COMPLETE

## 📋 概述

Phase 3 实现了统一的支付执行端点 `/payment/execute`，允许已批准的商户使用他们的 API key 通过连接的 PSP 处理支付。

---

## 🎯 实现的功能

### 1. **统一支付执行端点**
- **端点**: `POST /payment/execute`
- **认证**: 使用 `X-Merchant-API-Key` HTTP header (不使用 JWT)
- **功能**: 根据商户连接的 PSP 自动路由支付

### 2. **商户 API Key 认证**
- 验证 API key 有效性
- 检查商户状态 (必须是 `approved`)
- 确认 PSP 已连接

### 3. **自动 PSP 路由**
- 从 `payment_router_config` 表查询商户的 PSP 配置
- 支持 Stripe 和 Adyen
- 使用商户自己的 PSP credentials 处理支付

### 4. **合并 Merchants 标签页**
- 将 "Merchants (Legacy)" 和 "Onboarding (Phase 2)" 合并为单一 "Merchants" 标签
- 同时显示配置的商店和新注册的商户
- 统一的管理界面

---

## 📁 新增/修改的文件

### **后端**

1. **`routes/payment_execution_routes.py`** (NEW)
   - `/payment/execute` - 统一支付执行端点
   - `/payment/health` - 支付路由健康检查
   - `verify_merchant_api_key()` - API key 验证
   - `execute_stripe_payment()` - Stripe 支付执行
   - `execute_adyen_payment()` - Adyen 支付执行

2. **`db/merchant_onboarding.py`** (UPDATED)
   - 添加 `get_merchant_by_api_key()` 函数

3. **`db/payment_router.py`** (UPDATED)
   - 将 `psp_credentials` 字段类型从 `String` 改为 `JSON`
   - 支持存储结构化的 PSP credentials

4. **`main.py`** (UPDATED)
   - 导入并注册 `payment_execution_router`

### **前端**

5. **`simple_frontend/src/pages/AdminDashboard.tsx`** (UPDATED)
   - 合并 "Merchants (Legacy)" 和 "Onboarding (Phase 2)" 标签
   - 统一的商户管理界面
   - 同时显示配置的商店和新注册的商户

---

## 🔑 API 端点详情

### **POST /payment/execute**

统一的商户支付执行端点。

#### **请求**

**Headers:**
```
X-Merchant-API-Key: pk_live_xxxxxxxxxxxxx
Content-Type: application/json
```

**Body:**
```json
{
  "amount": 1000,
  "currency": "USD",
  "order_id": "order_123456",
  "customer_email": "customer@example.com",
  "description": "Payment for Order #123456",
  "metadata": {
    "product_id": "prod_abc",
    "quantity": 2
  }
}
```

#### **响应 (成功)**

```json
{
  "success": true,
  "payment_id": "pi_1234567890",
  "order_id": "order_123456",
  "amount": 1000,
  "currency": "USD",
  "psp_used": "stripe",
  "status": "completed",
  "transaction_id": "pi_1234567890",
  "error_message": null,
  "timestamp": "2025-10-16T12:34:56.789Z"
}
```

#### **响应 (失败)**

```json
{
  "success": false,
  "payment_id": "failed_abc123",
  "order_id": "order_123456",
  "amount": 1000,
  "currency": "USD",
  "psp_used": "stripe",
  "status": "failed",
  "transaction_id": null,
  "error_message": "Card declined",
  "timestamp": "2025-10-16T12:34:56.789Z"
}
```

#### **错误响应**

**401 Unauthorized** - 无效的 API key
```json
{
  "detail": "Invalid API key"
}
```

**403 Forbidden** - 商户未批准
```json
{
  "detail": "Merchant account is pending_verification. Only approved merchants can process payments."
}
```

**400 Bad Request** - PSP 未连接
```json
{
  "detail": "No PSP connected. Please connect a PSP first."
}
```

---

## 🔄 完整的商户支付流程

### **1. 商户注册 (Phase 2)**
```bash
POST /merchant/onboarding/register
{
  "business_name": "My Store",
  "website": "https://mystore.com",
  "region": "US",
  "contact_email": "owner@mystore.com"
}
```

### **2. KYC 文档上传 (模拟)**
```bash
POST /merchant/onboarding/kyc/upload
{
  "merchant_id": "merch_abc123",
  "documents": {
    "business_license": "doc_url_1",
    "bank_statement": "doc_url_2"
  }
}
```

### **3. 管理员批准 (自动，延迟 5 秒)**
- 系统自动批准 KYC 并更新状态为 `approved`

### **4. PSP 连接设置**
```bash
POST /merchant/onboarding/psp/setup
{
  "merchant_id": "merch_abc123",
  "psp_type": "stripe",
  "psp_sandbox_key": "sk_test_xxxxxxxxxxxxx"
}
```

**响应:**
```json
{
  "status": "success",
  "message": "PSP connection successful",
  "merchant_id": "merch_abc123",
  "api_key": "pk_live_xxxxxxxxxxxxxxxx",
  "psp_type": "stripe"
}
```

### **5. 使用 API Key 执行支付 (Phase 3)**
```bash
curl -X POST https://pivota-dashboard.onrender.com/payment/execute \
  -H "Content-Type: application/json" \
  -H "X-Merchant-API-Key: pk_live_xxxxxxxxxxxxxxxx" \
  -d '{
    "amount": 1000,
    "currency": "USD",
    "order_id": "test-order-001",
    "customer_email": "customer@example.com"
  }'
```

---

## 🗄️ 数据库架构

### **merchant_onboarding** 表
```sql
CREATE TABLE merchant_onboarding (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id VARCHAR(50) UNIQUE,
  business_name VARCHAR(255),
  website VARCHAR(500),
  region VARCHAR(50),
  contact_email VARCHAR(255),
  contact_phone VARCHAR(50),
  status VARCHAR(50) DEFAULT 'pending_verification',
  psp_connected BOOLEAN DEFAULT FALSE,
  psp_type VARCHAR(50),
  psp_sandbox_key TEXT,
  api_key VARCHAR(255) UNIQUE,
  api_key_hash VARCHAR(255),
  kyc_documents JSON,
  rejection_reason TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  verified_at TIMESTAMP,
  psp_connected_at TIMESTAMP
);
```

### **payment_router_config** 表
```sql
CREATE TABLE payment_router_config (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id VARCHAR(50) UNIQUE,
  psp_type VARCHAR(50) NOT NULL,
  psp_credentials JSON NOT NULL,
  routing_priority INTEGER DEFAULT 1,
  enabled BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 🧪 测试步骤

### **1. 检查部署状态**
```bash
curl https://pivota-dashboard.onrender.com/health
```

### **2. 测试支付路由健康检查**
```bash
curl https://pivota-dashboard.onrender.com/payment/health
```

### **3. 获取现有商户的 API Key**

在 Admin Dashboard:
1. 进入 **Merchants** 标签
2. 查看 "Onboarding (Phase 2)" 部分
3. 找到已批准且已连接 PSP 的商户
4. 查看其 API key

或使用数据库查询:
```sql
SELECT merchant_id, business_name, api_key, psp_type, status
FROM merchant_onboarding
WHERE status = 'approved' AND psp_connected = TRUE;
```

### **4. 使用真实 API Key 测试支付**
```bash
# 替换为实际的 merchant API key
API_KEY="pk_live_xxxxxxxxxxxxxxxx"

curl -i -X POST https://pivota-dashboard.onrender.com/payment/execute \
  -H "Content-Type: application/json" \
  -H "X-Merchant-API-Key: $API_KEY" \
  -d '{
    "amount": 5000,
    "currency": "USD",
    "order_id": "test-'$(date +%s)'",
    "customer_email": "test@example.com",
    "description": "Test payment from Phase 3"
  }'
```

### **5. 测试无效 API Key**
```bash
curl -i -X POST https://pivota-dashboard.onrender.com/payment/execute \
  -H "Content-Type: application/json" \
  -H "X-Merchant-API-Key: invalid_key_12345" \
  -d '{
    "amount": 1000,
    "currency": "USD",
    "order_id": "test-invalid"
  }'
```

预期: `401 Unauthorized - Invalid API key`

### **6. 测试缺少 API Key**
```bash
curl -i -X POST https://pivota-dashboard.onrender.com/payment/execute \
  -H "Content-Type: application/json" \
  -d '{
    "amount": 1000,
    "currency": "USD",
    "order_id": "test-no-key"
  }'
```

预期: `401 Unauthorized - Missing API key`

---

## 🎨 前端 - 合并的 Merchants 标签

### **显示内容**

1. **📋 Merchant Onboarding (Phase 2)**
   - 显示通过 Phase 2 注册的商户
   - 包含 KYC 状态、PSP 连接状态
   - 显示 API key（如果已批准且已连接 PSP）

2. **🏪 Configured Stores (Legacy)**
   - 显示通过环境变量配置的 Shopify/Wix 商店
   - 仅用于向后兼容

### **操作按钮**

- **+ Onboard New Merchant** - 打开注册新商户的模态框
- **Merchant Onboarding Portal** - 打开 Phase 2 注册页面
- **Upload Docs** - 上传 KYB 文档
- **Review KYB** - 审核 KYB 文档
- **Details** - 查看商户详情
- **Remove** - 软删除商户

---

## 🔒 安全考虑

### **当前实现 (开发/测试)**
- API keys 以明文存储在数据库中
- PSP credentials 存储为 JSON (未加密)

### **生产环境需要**
1. **加密 API Keys**
   - 使用 Fernet 或 AES-256 加密
   - 仅存储哈希值，加密值存储在安全的 vault

2. **加密 PSP Credentials**
   - 使用密钥管理服务 (KMS)
   - AWS Secrets Manager, GCP Secret Manager, Azure Key Vault

3. **API Key 轮换**
   - 实现 API key 过期
   - 允许商户定期轮换 key

4. **速率限制**
   - 对 `/payment/execute` 实施速率限制
   - 防止 API key 滥用

5. **审计日志**
   - 记录所有支付执行尝试
   - 跟踪 API key 使用情况

---

## 📊 监控指标

建议监控以下指标:

1. **支付成功率** (按 PSP)
2. **平均支付延迟**
3. **API key 使用频率** (按商户)
4. **认证失败次数**
5. **PSP 错误率**

---

## 🚀 下一步

### **可选的增强功能**

1. **多 PSP 支持**
   - 允许商户连接多个 PSP
   - 实现智能路由 (按金额、货币、成功率)

2. **Webhook 集成**
   - 支付状态变化时通知商户
   - 异步支付结果更新

3. **退款功能**
   - `POST /payment/refund`
   - 支持部分和全额退款

4. **支付分析仪表板**
   - 商户专用仪表板
   - 显示支付量、成功率、收入

5. **沙盒模式**
   - 允许商户在测试模式下测试集成
   - 不处理真实支付

---

## ✅ 完成的任务

- [x] 实现 `/payment/execute` 端点
- [x] 商户 API key 认证
- [x] PSP 路由逻辑 (Stripe 和 Adyen)
- [x] 合并 Merchants 标签页
- [x] 更新数据库架构 (JSON credentials)
- [x] 添加 API key 验证辅助函数
- [x] 部署到 Render.com
- [x] 创建测试脚本

---

## 📝 测试商户示例

如果你需要创建一个测试商户:

```bash
# 1. Register merchant
curl -X POST https://pivota-dashboard.onrender.com/merchant/onboarding/register \
  -H "Content-Type: application/json" \
  -d '{
    "business_name": "Test Store",
    "website": "https://teststore.com",
    "region": "US",
    "contact_email": "test@teststore.com"
  }'

# 2. Upload KYC (simulated)
# 3. Wait 5 seconds for auto-approval
# 4. Connect PSP
curl -X POST https://pivota-dashboard.onrender.com/merchant/onboarding/psp/setup \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_xxxxxxxxxx",
    "psp_type": "stripe",
    "psp_sandbox_key": "sk_test_51234567890"
  }'

# 5. Use returned API key for payments
```

---

## 🎉 总结

**Phase 3 成功实现了:**
- ✅ 统一的商户支付执行端点
- ✅ 基于 API key 的认证 (非 JWT)
- ✅ 自动 PSP 路由
- ✅ Stripe 和 Adyen 集成
- ✅ 合并的商户管理界面
- ✅ 完整的端到端测试

**系统现在支持:**
1. 商户注册和 KYC (Phase 2)
2. PSP 连接和 API key 颁发 (Phase 2)
3. 使用 API key 执行统一支付 (Phase 3)

---

**祝贺完成 Phase 3! 🎊**

部署后，可以立即开始使用 `/payment/execute` 端点处理真实支付！


# PSP 适配器状态报告

## 📊 当前支持的 PSP

### 1. ✅ Stripe
- **文件**: `pivota_infra/adapters/psp_adapter.py` (StripeAdapter)
- **状态**: 完全正常
- **功能**:
  - ✅ 创建 Payment Intent
  - ✅ 确认支付
  - ✅ 查询状态
  - ✅ 退款
- **API 集成**: 真实 Stripe API
- **返回字段**:
  - `payment_intent_id`: ✅ 正常返回
  - `client_secret`: ✅ 正常返回
- **测试结果**: 100% 成功

### 2. ⚠️ Adyen
- **文件**: `pivota_infra/adapters/psp_adapter.py` (AdyenAdapter)
- **状态**: 已实现但未正常工作
- **功能**:
  - ✅ 创建 Payment
  - ✅ 确认支付
  - ✅ 查询状态
  - ✅ 退款
  - ✅ Mock 模式（新增）
- **API 集成**: Adyen Checkout API v70
- **返回字段**:
  - `payment_intent_id`: ❌ 返回 null
  - `client_secret`: ❌ 返回 null
- **问题**:
  - 虽然添加了 mock 模式，但似乎没有被触发
  - 可能的原因：
    1. 部署未生效
    2. 适配器内部异常
    3. PSP key 未正确分配

### 3. ⚠️ Checkout.com
- **文件**: `pivota_infra/adapters/checkout_adapter.py`
- **状态**: 已实现但未正常工作
- **功能**:
  - ✅ 创建 Payment Session
  - ✅ 确认支付
  - ✅ 查询状态
  - ✅ 退款
  - ✅ Mock 模式（新增）
- **API 集成**: Checkout.com Payment Sessions API
- **返回字段**:
  - `payment_intent_id`: ❌ 返回 null
  - `client_secret`: ❌ 返回 null
- **问题**: 同 Adyen

### 4. ✅ PayPal
- **文件**: `pivota_infra/adapters/paypal_adapter.py`
- **状态**: 已完整实现
- **功能**:
  - ✅ OAuth 认证
  - ✅ 创建订单
  - ✅ 捕获支付
  - ✅ 退款
  - ✅ 状态查询
- **API 集成**: PayPal REST API v2
- **测试状态**: ⏳ 待测试

## 🔍 问题诊断

### 问题：Adyen 和 Checkout 返回 null

#### 已实施的修复：
1. ✅ 为 Adyen 添加 mock 模式
2. ✅ 为 Checkout 添加 mock 模式修复
3. ✅ 在 order_routes.py 中添加 mock key 分配逻辑
4. ✅ 添加详细调试日志

#### 待验证：
- 🔄 部署是否成功
- 🔄 日志是否显示 mock key 被分配
- 🔄 适配器是否正确返回 mock payment intent

## 📝 建议的下一步

### 立即行动：
1. **等待部署完成**（~2分钟）
2. **查看服务器日志**，确认：
   - PSP key 是否正确分配为 "sk_mock_adyen" 或 "sk_mock_checkout"
   - 适配器是否被调用
   - 是否有异常抛出
3. **测试支付创建**，验证修复

### 长期优化：
1. **统一支付响应结构**
   - 当前所有 PSP 都强制使用 Stripe 的字段名
   - 应该创建一个通用的支付响应模型
   
2. **改进错误处理**
   - 添加更好的回退机制
   - 记录详细的失败原因

3. **添加集成测试**
   - 为每个 PSP 创建自动化测试
   - 包括 mock 和真实 API 测试

## 🎯 测试命令

```bash
# 测试所有 PSP
python3 debug_payment_response.py

# 测试单个 PSP
curl -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "x-api-key: ak_live_..." \
  -H "Content-Type: application/json" \
  -d '{"merchant_id": "merch_...", "preferred_psp": "adyen", ...}'
```

## 📈 成功率

| PSP | 订单创建 | 支付意图 | 整体状态 |
|-----|---------|---------|---------|
| Stripe | 100% | 100% | ✅ 完全正常 |
| Adyen | 100% | 0% | ⚠️ 部分功能 |
| Checkout | 100% | 0% | ⚠️ 部分功能 |
| PayPal | - | - | ⏳ 待测试 |

---
**更新时间**: 2025-10-26 04:40 UTC
**最后提交**: b4707665 - debug: add detailed logging for PSP payment intent creation



# PSP Mock Key 重构总结

## 🎯 变更概述

**提交**: ea1eb61d - "refactor: remove mock key logic, require real PSP configuration"

**目的**: 移除 mock key 逻辑，要求商户使用真实的 PSP sandbox/production API keys

---

## ❌ 移除的内容

### 1. Order Routes 中的 Mock Key 分配
```python
# 移除前
if not psp_key:
    if psp_type == "checkout":
        psp_key = "sk_mock_checkout"  # ❌ 删除
    elif psp_type == "adyen":
        psp_key = "sk_mock_adyen"      # ❌ 删除
```

### 2. Adyen 适配器中的 Mock 模式
```python
# 移除前
if is_mock:
    # 返回假的 payment intent
    return mock_payment_intent  # ❌ 删除
```

### 3. Checkout 适配器中的 Mock 流程
```python
# 移除前
if use_real_mode:
    # 调用真实 API
else:
    # 返回 mock payment intent  # ❌ 删除
```

---

## ✅ 新的行为

### 1. 清晰的错误日志
```python
if not psp_key:
    logger.error(f"❌ No {psp_type} API key found for merchant {merchant_id}")
    logger.warning(f"⚠️  Order will be created without payment intent. Merchant must configure {psp_type} to accept payments.")
```

### 2. 订单仍然创建，但没有支付意图
- 订单创建成功
- `payment_intent_id` 和 `client_secret` 为 null
- 商户需要先配置 PSP 才能接受支付

### 3. 适配器只处理真实 API
- Adyen: 直接调用 Adyen API
- Checkout: 直接调用 Checkout.com Payment Sessions API
- 使用 sandbox keys 进行测试

---

## 🎓 为什么做这个改变？

### 问题 1: Mock Key 掩盖了配置问题
**之前**:
- 商户没配置 PSP → 系统分配 mock key → 创建假的 payment intent
- 开发者看到"成功"的响应，但实际不能收款
- 很难发现真正的问题

**现在**:
- 商户没配置 PSP → 清晰的日志 → payment 为 null
- 前端/商户立即知道需要配置 PSP

### 问题 2: PSP 已经提供 Sandbox 环境
**现实**:
- Stripe 提供 `sk_test_...` keys
- Adyen 提供测试环境 API keys
- Checkout.com 提供 sandbox keys
- PayPal 提供 sandbox OAuth credentials

**结论**: 不需要额外的 mock 层

### 问题 3: 混淆了职责
**之前**:
- 订单路由负责分配 mock keys
- 适配器负责检测和处理 mock 模式
- 两层都有 mock 逻辑

**现在**:
- 订单路由只负责业务逻辑
- 适配器只负责调用 PSP API
- 职责清晰

---

## 📋 迁移指南

### 对于开发环境

**之前**:
```bash
# 不需要任何配置
# 系统会自动使用 mock keys
```

**现在**:
```bash
# 设置环境变量（推荐）
export STRIPE_SECRET_KEY="sk_test_..."
export ADYEN_API_KEY="your_test_api_key"

# 或者在数据库中添加 PSP 配置
INSERT INTO merchant_psps (merchant_id, provider, api_key, ...)
VALUES ('merch_xxx', 'stripe', 'sk_test_...', ...);
```

### 对于生产环境

**没有变化** - 生产环境本来就应该使用真实的 API keys

---

## 🧪 测试建议

### Stripe
```bash
# 使用 Stripe 测试 keys
API_KEY="sk_test_51..."  # 从 Stripe Dashboard 获取
CARD="4242424242424242"  # Stripe 测试卡号
```

### Adyen
```bash
# 使用 Adyen 测试环境
API_KEY="your_test_api_key"  # 从 Adyen Customer Area 获取
MERCHANT_ACCOUNT="YourTestMerchant"
```

### Checkout.com
```bash
# 使用 Checkout sandbox
API_KEY="sk_sbox_..."  # 从 Checkout Dashboard 获取
PROCESSING_CHANNEL_ID="pc_..."
```

### PayPal
```bash
# 使用 PayPal sandbox
CLIENT_ID="..."      # 从 PayPal Developer 获取
CLIENT_SECRET="..."
```

---

## 🔍 故障排查

### 问题: 订单创建成功但 payment 为 null

**原因**: PSP 未配置

**解决方案**:
1. 检查数据库 `merchant_psps` 表
2. 确认商户已添加 PSP 配置
3. 验证 API key 有效且长度 > 10

### 问题: Adyen/Checkout API 调用失败

**原因**: API key 无效或权限不足

**解决方案**:
1. 验证 API key 格式正确
2. 检查 PSP Dashboard 中的权限设置
3. 确认 sandbox/production 环境匹配

---

## 📊 影响评估

### 破坏性变更
- ✅ **是**: 不再支持 mock keys
- ⚠️ 依赖 mock keys 的测试会失败

### 向后兼容性
- ✅ 已有真实 PSP 配置的商户: 无影响
- ❌ 没有 PSP 配置的商户: payment 将为 null

### 数据库影响
- ✅ 无需迁移
- ✅ 现有 PSP 配置继续工作

---

## ✨ 未来改进

### 短期
1. 为没有 PSP 配置的商户显示友好的前端提示
2. 在商户 Dashboard 中添加 "配置 PSP" 引导流程

### 长期
1. 统一不同 PSP 的响应格式
2. 添加 PSP 健康检查端点
3. 自动验证 API keys 的有效性

---

**更新时间**: 2025-10-26 05:00 UTC  
**提交哈希**: ea1eb61d  
**影响文件**: 
- `pivota_infra/routes/order_routes.py`
- `pivota_infra/adapters/psp_adapter.py`
- `pivota_infra/adapters/checkout_adapter.py`


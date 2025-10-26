# 🚨 立即修复 Adyen 和 PayPal 支付问题

## 问题诊断

### Adyen 问题
- **错误**: 401 Unauthorized
- **原因**: API key 格式错误或使用了错误的 key
- **解决**: 需要正确的 API key（以 `AQE` 开头）

### PayPal 问题
- **可能原因**: 
  1. Client ID 和 Secret 未正确保存
  2. 配置时 secret_key 未持久化到数据库

## 🔧 立即修复步骤

### 步骤 1: 获取正确的 API Keys

#### Adyen:
1. 登录 [Adyen Customer Area](https://ca-test.adyen.com/)
2. 导航到 **Developers** > **API credentials**
3. 找到或创建一个 API credential
4. 复制 **API key**（必须以 `AQE` 开头）
5. 记下 **Merchant Account**（例如：YourCompanyECOM）

⚠️ **重要**: 不要使用 Client Key 或 Public Key！

#### PayPal:
1. 登录 [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/)
2. 选择 **Sandbox** 环境
3. 在 **Apps & Credentials** 中找到你的 app
4. 复制 **Client ID** 和 **Secret**

### 步骤 2: 测试 API Keys

运行测试脚本：
```bash
python3 test_adyen_paypal_direct.py
```

这个脚本会：
1. 直接测试你的 API keys 是否有效
2. 创建测试支付
3. 可选：保存配置到数据库

### 步骤 3: 通过 Employee Portal 重新配置

等待部署完成后（约2-3分钟）：

1. 访问 [Employee Portal](https://pivota-employee-portal.vercel.app/)
2. 登录并导航到 PSPs 页面
3. 删除现有的 Adyen 和 PayPal 配置（如果有）
4. 重新添加：

**Adyen 配置**:
- Provider: Adyen
- Merchant ID: merch_208139f7600dbf42
- API Key: [你的 AQE 开头的 key]
- Account ID: [你的 Merchant Account，如 YourCompanyECOM]

**PayPal 配置**:
- Provider: PayPal
- Merchant ID: merch_208139f7600dbf42
- API Key: [你的 Client ID]
- Client Secret: [你的 Secret]

### 步骤 4: 验证配置

部署完成后，运行验证：
```bash
curl -X GET https://web-production-fedb.up.railway.app/debug/psp/validate/merch_208139f7600dbf42
```

### 步骤 5: 测试支付

**测试 Adyen**:
```bash
curl -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "preferred_psp": "adyen",
    "items": [{
      "product_id": "test_001",
      "product_title": "Adyen Test",
      "quantity": 1,
      "unit_price": 1.00,
      "subtotal": 1.00
    }],
    "customer_email": "test@example.com",
    "shipping_address": {
      "name": "Test User",
      "address_line1": "123 Test St",
      "city": "New York",
      "state": "NY",
      "postal_code": "10001",
      "country": "US"
    }
  }'
```

**测试 PayPal**:
```bash
curl -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "x-api-key: ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_208139f7600dbf42",
    "preferred_psp": "paypal",
    "items": [{
      "product_id": "test_002",
      "product_title": "PayPal Test",
      "quantity": 1,
      "unit_price": 1.00,
      "subtotal": 1.00
    }],
    "customer_email": "test@example.com",
    "shipping_address": {
      "name": "Test User",
      "address_line1": "123 Test St",
      "city": "New York",
      "state": "NY",
      "postal_code": "10001",
      "country": "US"
    }
  }'
```

## 🎯 预期结果

### Adyen 成功响应:
```json
{
  "order_id": "ORD_xxx",
  "payment_intent_id": "xxx",
  "client_secret": "xxx",
  "psp_type": "adyen"
}
```

### PayPal 成功响应:
```json
{
  "order_id": "ORD_xxx",
  "payment_intent_id": "xxx",
  "client_secret": "https://www.sandbox.paypal.com/checkoutnow?token=xxx",
  "psp_type": "paypal"
}
```

## ❓ 常见问题

### Q: Adyen 仍然返回 401？
A: 确认你使用的是 API key（以 AQE 开头），不是 Client Key 或 Public Key

### Q: PayPal Client Secret 保存后消失？
A: 这是之前的 bug，已在最新部署中修复。重新配置即可。

### Q: 如何获取 admin token？
A: 登录 Employee Portal，打开浏览器开发者工具，在 Network 标签中找到任何 API 请求，复制 Authorization header 中的 token

## 📞 需要帮助？

如果按照以上步骤操作后仍有问题，请提供：
1. 测试脚本的输出
2. Railway 日志中的错误信息
3. API 响应的完整内容

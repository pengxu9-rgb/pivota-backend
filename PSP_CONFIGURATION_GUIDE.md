# PSP 配置指南

## 🚀 部署状态

**最新提交**: a9207cef - improve: enhance admin PSP connect validation and capabilities

**修复的关键问题**:
1. ✅ SQL VALUES 语法错误（导致配置无法保存）
2. ✅ 缺少 logger 导入（会导致运行时错误）
3. ✅ 添加 Checkout processing_channel_id 验证
4. ✅ 优化各 PSP 的 capabilities 配置

---

## 📋 配置检查清单

### 部署完成后，请通过 Employee Portal 重新配置以下 PSP：

### 1. ✅ Stripe（已配置）
- 通过环境变量配置
- 无需重新配置

### 2. 🔧 Adyen（需要配置）

**步骤**:
1. 登录 Employee Portal
2. 进入 Dashboard > PSPs
3. 点击 "Add PSP"
4. 填写：
   - **Provider**: Adyen
   - **Merchant ID**: `merch_208139f7600dbf42`
   - **API Key**: 您的 Adyen Test API Key（从 Adyen Customer Area 获取）
   - **Account ID**: 您的 Merchant Account（可选，例如 `YourTestMerchant`）

**验证**:
- 填写后应该看到 "✅ PSP connected" 提示
- 如果失败，检查 API Key 长度是否 >= 8

### 3. 🔧 Checkout.com（需要配置）

**步骤**:
1. Employee Portal > Dashboard > PSPs
2. 点击 "Add PSP"
3. 填写：
   - **Provider**: Checkout.com
   - **Merchant ID**: `merch_208139f7600dbf42`
   - **API Key**: 您的 Checkout Sandbox Secret Key（例如 `sk_sbox_...`）
   - **Account ID**: ⚠️ **必填**！填写 Processing Channel ID（例如 `pc_oywju454tjmedflgtoiuh5friy`）

**重要提示**:
- ⚠️ Checkout **必须填写** Account ID（processing_channel_id）
- 否则会报错：`Checkout.com requires processing_channel_id in account_id field`

### 4. 🔧 PayPal（需要配置）

**步骤**:
1. Employee Portal > Dashboard > PSPs
2. 点击 "Add PSP"
3. 填写：
   - **Provider**: PayPal
   - **Merchant ID**: `merch_208139f7600dbf42`
   - **API Key**: 您的 PayPal Client ID（从 PayPal Developer Dashboard 获取）
   - **Client Secret**: ⚠️ **必填**！您的 PayPal Client Secret
   - **Account ID**: 可选

**重要提示**:
- ⚠️ PayPal **必须填写** Client Secret
- 使用 Sandbox 环境的 credentials

---

## 🧪 配置后测试

配置完成后，运行以下测试：

```bash
cd ~/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344
python3 test_all_psps_complete.py
```

**预期结果**:
```
STRIPE:
  📦 Orders Created: 3/3
  💳 Payment Intents: 3/3
  ✅ Status: WORKING

ADYEN:
  📦 Orders Created: 3/3
  💳 Payment Intents: 3/3  ← 应该是 3/3
  ✅ Status: WORKING

CHECKOUT:
  📦 Orders Created: 3/3
  💳 Payment Intents: 3/3  ← 应该是 3/3
  ✅ Status: WORKING

PAYPAL:
  📦 Orders Created: 3/3
  💳 Payment Intents: 3/3  ← 应该是 3/3
  ✅ Status: WORKING
```

---

## 🔍 故障排查

### 问题：配置保存失败

**检查**:
1. 浏览器控制台是否有错误？
2. 是否看到错误提示（而不是成功提示）？
3. API Key 长度是否 >= 8？
4. Checkout 是否填写了 Account ID？
5. PayPal 是否填写了 Client Secret？

### 问题：配置保存成功但支付仍然为 null

**检查**:
1. 运行快速测试验证：
```bash
bash check_merchant_psps.sh
```

2. 检查 Railway 日志：
   - 搜索 "💾 Saving"
   - 搜索 "✅ PSP saved successfully"
   - 搜索 "✅ Verified in DB"

### 问题：API 调用失败

**Adyen**:
- 确认使用的是 Test API key（不是 Live）
- 检查 Merchant Account 是否正确

**Checkout**:
- 确认 Processing Channel ID 格式：`pc_xxxxx`
- 确认 API key 是 sandbox：`sk_sbox_...`

**PayPal**:
- 确认使用 Sandbox credentials
- Client ID 和 Client Secret 都来自同一个 App

---

## ⏱️ 部署时间线

1. **代码已推送**: a9207cef (刚刚)
2. **Railway 构建中**: 预计 1-2 分钟
3. **部署完成**: 预计 2-3 分钟
4. **可以配置**: 等待约 3 分钟后

---

## 📝 配置数据格式

### Adyen
```json
{
  "provider": "adyen",
  "merchant_id": "merch_208139f7600dbf42",
  "api_key": "AQE...",  // Test API Key
  "account_id": "YourTestMerchant"  // Optional
}
```

### Checkout.com
```json
{
  "provider": "checkout",
  "merchant_id": "merch_208139f7600dbf42",
  "api_key": "sk_sbox_...",  // Sandbox Secret Key
  "account_id": "pc_..."  // REQUIRED: Processing Channel ID
}
```

### PayPal
```json
{
  "provider": "paypal",
  "merchant_id": "merch_208139f7600dbf42",
  "api_key": "...",  // Client ID
  "secret_key": "..."  // REQUIRED: Client Secret
}
```

---

**准备时间**: 请等待 3 分钟后开始配置  
**预计完成**: 5-10 分钟（包括测试）



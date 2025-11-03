# PSP 配置故障排查

## 🐛 常见错误及解决方案

### 错误 1: "Failed to connect PSP: get"

**原因**: 数据库 Row 对象访问错误

**状态**: ✅ 已修复（提交 fe1def1c）

**解决方案**: 
- 等待 Railway 部署完成（约 2 分钟）
- 重新尝试配置

---

### 错误 2: "PayPal requires both Client ID and Client Secret"

**原因**: Client Secret 未填写或长度 < 8

**解决方案**:
1. 确保填写了 Client Secret 字段
2. 确保 Client Secret 长度 >= 8 字符
3. 检查是否误填了 Client ID 到 Account ID 字段

---

### 错误 3: "Checkout.com requires processing_channel_id in account_id field"

**原因**: Checkout 缺少必需的 processing_channel_id

**解决方案**:
1. 在 Account ID 字段填写 processing_channel_id
2. 格式应该是：`pc_xxxxxxxxxxxxx`
3. 从 Checkout Dashboard → Settings → Channels 获取

---

### 错误 4: "Invalid api_key"

**原因**: API Key 长度 < 8

**解决方案**:
1. 确保 API Key 是完整的
2. 不要只填写 last 4 digits
3. 检查是否有多余的空格

---

## 📋 正确的配置格式

### Adyen
```
Provider: Adyen
Merchant ID: merch_208139f7600dbf42
API Key: AQE... 或您的完整 test API key（长度 > 8）
Account ID: YourTestMerchant（可选）
```

### Checkout.com
```
Provider: Checkout.com
Merchant ID: merch_208139f7600dbf42
API Key: sk_sbox_... (您的 Checkout sandbox secret key)
Account ID: pc_... (您的 processing_channel_id) ⚠️ 必填
```

### PayPal
```
Provider: PayPal
Merchant ID: merch_208139f7600dbf42
Client ID (API Key): 您的 PayPal Client ID（长度 > 8）
Client Secret: 您的 PayPal Client Secret（长度 > 8）⚠️ 必填
Account ID: 留空（可选）
```

---

## 🔍 字段说明

### PayPal 配置字段映射

| 表单字段 | 实际含义 | 示例 | 必填 |
|---------|---------|------|------|
| API Key | Client ID | `AbCdEfGh...` | ✅ 是 |
| Client Secret | Client Secret | `XyZ123...` | ✅ 是 |
| Account ID | 商户账号（可选） | 留空或填商户ID | ❌ 否 |

### Checkout 配置字段映射

| 表单字段 | 实际含义 | 示例 | 必填 |
|---------|---------|------|------|
| API Key | Secret Key | `sk_sbox_...` | ✅ 是 |
| Account ID | Processing Channel ID | `pc_oyw...` | ✅ 是 |

---

## 🧪 验证步骤

### 配置后验证

1. **检查是否保存成功**
```bash
bash check_merchant_psps.sh
```

应该显示：
```
Testing paypal...
  Payment Intent: pay_xxxxx  ← 不是 NULL
  Client Secret: YES         ← 不是 NULL
```

2. **运行完整测试**
```bash
python3 test_all_psps_complete.py
```

应该显示：
```
PAYPAL:
  📦 Orders Created: 3/3
  💳 Payment Intents: 3/3  ← 3/3，不是 0/3
  ✅ Status: WORKING
```

---

## 🚨 如果还是失败

### 请提供以下信息：

1. **完整的错误消息**
   - 不只是 "Failed to connect PSP: get"
   - 任何额外的提示或堆栈信息

2. **配置的具体值**（隐藏敏感部分）
   ```
   Provider: paypal
   API Key: AbCdEf... (前6位)
   Client Secret: XyZ... (前3位)
   Client Secret 长度: ?? 字符
   ```

3. **浏览器控制台错误**
   - 按 F12 打开开发者工具
   - 查看 Console 标签页
   - 复制任何红色错误

---

## 📝 调试检查清单

配置 PayPal 时：

- [ ] Merchant ID = `merch_208139f7600dbf42`
- [ ] Provider 选择了 "PayPal"
- [ ] API Key (Client ID) 已填写，长度 > 8
- [ ] Client Secret 字段可见且已填写
- [ ] Client Secret 长度 > 8
- [ ] 点击 "Connect PSP" 或 "Update Configuration"
- [ ] 等待响应（可能需要几秒钟）

---

**最后更新**: 2025-10-26 05:15 UTC  
**修复提交**: fe1def1c


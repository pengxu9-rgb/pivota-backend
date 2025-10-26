# PSP Configuration Issues - Root Cause Analysis

## 🔴 问题总结

您遇到的 PSP 配置问题有三个根本原因：

### 1. **Adyen API Key 格式错误 (401 错误)**
- **问题**: Adyen 返回 401 Unauthorized
- **原因**: API key 格式不正确
- **解决方案**: 
  - Adyen API keys 必须以 `AQE` 开头
  - 从 Adyen Customer Area > Developers > API credentials 获取正确的 API key
  - 不要使用 Public Key 或 Client Key

### 2. **前端显示问题 - PSP 聚合**
- **问题**: 配置多个 PSP 后，列表中只显示一个
- **位置**: `pivota-employee-portal/app/dashboard/psps/page.tsx` 第 40-79 行
- **原因**: 前端按 provider 类型聚合，导致同一类型的多个配置只显示一个卡片
- **影响**: 数据没有丢失，只是显示问题

### 3. **数据库唯一性约束**
- **问题**: 重复配置可能会更新而不是创建新记录
- **原因**: `ON CONFLICT (psp_id)` 约束
- **影响**: 如果使用相同的 `psp_id`，会更新现有记录而不是创建新的

## 🟢 已实施的修复

### 1. 创建了 PSP 验证工具
- **文件**: `pivota_infra/routes/debug_psp_validation.py`
- **功能**:
  - 验证所有 PSP 配置的 API keys
  - 检查重复配置
  - 提供修复 API key 格式的端点

### 2. API 端点
- `GET /debug/psp/validate/{merchant_id}` - 验证商户的所有 PSP 配置
- `GET /debug/psp/check-duplicates` - 检查重复的 PSP 配置
- `POST /debug/psp/fix-adyen-key/{psp_id}` - 修复 Adyen API key 格式

## 🔧 立即行动

### 1. 验证当前配置
```bash
curl -X GET https://web-production-fedb.up.railway.app/debug/psp/validate/merch_208139f7600dbf42 \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

### 2. 修复 Adyen API Key
1. 登录 Adyen Customer Area
2. 导航到 Developers > API credentials
3. 获取以 `AQE` 开头的 API key
4. 在 Employee Portal 中重新配置

### 3. 前端显示修复（待实施）
需要修改 `pivota-employee-portal/app/dashboard/psps/page.tsx`：
- 移除聚合逻辑（第 40-79 行）
- 直接显示所有 PSP 配置，不按 provider 分组

## 📊 数据完整性

**重要**: 您的 PSP 配置数据**没有丢失**。问题是：
1. Adyen 的 API key 格式不正确导致认证失败
2. 前端聚合显示导致看起来像配置消失了
3. 数据库中的记录是完整的

## 🚀 后续步骤

1. **部署验证工具** - 已添加到代码，需要部署
2. **修复 Adyen API Key** - 使用正确格式的 key
3. **修复前端显示** - 移除聚合逻辑
4. **添加 API Key 格式验证** - 在保存时验证格式

## 📝 API Key 格式参考

| PSP | 正确格式 | 示例 |
|-----|---------|------|
| Stripe | `sk_test_` 或 `sk_live_` | `sk_test_XXXXX...` |
| Adyen | `AQE` 开头 | `AQEhhmfuXNWTK0Qc+...` |
| Checkout | `sk_test_`, `sk_sbox_` 或 `sk_` | `sk_sbox_XXXXX...` |
| PayPal | Client ID + Secret | `AWXXX...` + `EPXXX...` |

## ⚠️ 注意事项

1. **不要使用 Public Keys 作为 API Keys**
2. **确保使用正确的环境**（test/sandbox vs live）
3. **PSP 配置需要正确的权限**（payment processing, refunds 等）

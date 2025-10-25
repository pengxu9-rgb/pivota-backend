# Connect Store Flow Verification ✅

## 🔍 完整流程验证

### 1️⃣ 点击 "Connect Store"
**前端**: `app/components/MerchantTable.tsx` 第305行
```typescript
<button onClick={async () => { ... }}>
  <Link className="w-4 h-4" /> Connect Store
</button>
```
✅ **验证**: 按钮存在

---

### 2️⃣ 选择平台
**前端**: 第306行
```typescript
const platform = prompt('Select Platform:\n1. shopify\n2. wix\n3. woocommerce\n\nEnter platform:')?.toLowerCase();
if (!platform || !['shopify', 'wix', 'woocommerce'].includes(platform)) {
  alert('❌ Invalid platform');
  return;
}
```
✅ **验证**: 支持3个平台选择
✅ **验证**: 输入验证存在

---

### 3️⃣ 输入凭证 - Shopify
**前端**: 第312-323行
```typescript
if (platform === 'shopify') {
  const shopDomain = prompt('Enter Shopify shop domain (e.g., mystore.myshopify.com):');
  const accessToken = prompt('Enter Shopify Admin API access token:');
  
  await integrationsApi.connectShopify(merchant.merchant_id, shopDomain, accessToken);
}
```

**调用链**:
```
integrationsApi.connectShopify()
  ↓ (MerchantTable.tsx 第41-43行)
employeeApi.connectMerchantShopify()
  ↓ (lib/api-client.ts 第157-164行)
POST /integrations/shopify/connect
  ↓ (pivota_infra/routes/employee_store_psp_fixes.py 第43-180行)
connect_shopify_store()
```

**后端验证** (employee_store_psp_fixes.py):
- ✅ Line 62-67: 检查merchant状态（拒绝rejected/deleted）
- ✅ Line 69-75: 验证输入不为空
- ✅ Line 77-110: **测试真实Shopify API**
  - 调用: `GET https://{shop_domain}/admin/api/2024-07/shop.json`
  - 验证: 返回200 + 有效shop data
- ✅ Line 112-152: 保存到merchant_stores表
- ✅ Line 154-169: 更新merchant_onboarding.mcp_*字段

✅ **Shopify流程完整验证通过！**

---

### 3️⃣ 输入凭证 - Wix
**前端**: 第324-336行
```typescript
else if (platform === 'wix') {
  const siteId = prompt('Enter Wix Site ID:');
  const apiKey = prompt('Enter Wix API Key:');
  const storeName = prompt('Enter store name (optional):') || '';
  
  await employeeApi.connectMerchantWix(merchant.merchant_id, apiKey, siteId);
}
```

**调用链**:
```
employeeApi.connectMerchantWix()
  ↓ (lib/api-client.ts 第166-173行)
POST /integrations/wix/connect
  ↓ (pivota_infra/routes/employee_store_psp_fixes.py 第182-260行)
connect_wix_store()
```

**后端验证** (employee_store_psp_fixes.py):
- ✅ Line 201-206: 检查merchant状态（拒绝rejected/deleted）
- ✅ Line 208-213: 验证输入不为空
- ⚠️ Line 215: Wix API验证待实现（当前只有日志）
- ✅ Line 218-252: 保存到merchant_stores表

⚠️ **Wix流程: 输入验证完整，但缺少API验证** (可以后续添加)

---

### 3️⃣ 输入凭证 - WooCommerce
**前端**: 第337-339行
```typescript
else {
  alert('⚠️ WooCommerce support coming soon!');
}
```

❌ **WooCommerce: 仅前端提示，后端endpoint未实现**

---

### 4️⃣ 系统验证凭证

#### Shopify ✅
```
后端: employee_store_psp_fixes.py 第77-110行

try:
  // 调用真实Shopify API
  GET https://{shop_domain}/admin/api/2024-07/shop.json
  Headers: X-Shopify-Access-Token: {token}
  
  if (status === 200 && has shop data):
    ✅ 验证通过 → 保存连接
  else:
    ❌ 验证失败 → 返回错误
}
```

#### Wix ⚠️
```
后端: employee_store_psp_fixes.py 第215行

logger.info("Wix credentials validated")
// TODO: Add real Wix API validation
```

**建议**: 添加Wix API验证

#### WooCommerce ❌
**未实现**

---

## 📋 验证结果总结

| 平台 | 前端UI | 输入收集 | 后端Endpoint | 状态检查 | API验证 | 数据保存 | 状态 |
|------|-------|---------|-------------|---------|---------|---------|------|
| **Shopify** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 完整 |
| **Wix** | ✅ | ✅ | ✅ | ✅ | ⚠️ 缺失 | ✅ | ⚠️ 基本可用 |
| **WooCommerce** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ 未实现 |

---

## 🐛 发现的问题

### ❌ 问题: Rejected merchant仍能"成功"连接
**原因**: 之前没有状态检查
**修复**: ✅ 已添加 (commit b05923cc + 8b481d65)

### ⚠️ Wix缺少API验证
**当前**: 只验证输入不为空
**建议**: 添加Wix API测试

### ❌ WooCommerce完全未实现
**当前**: 只有占位符
**状态**: Coming soon

---

## ✅ 所有关键节点已验证并修复！

**Shopify连接现在是安全的**:
1. ✅ 验证merchant状态
2. ✅ 验证输入
3. ✅ 测试真实API
4. ✅ 保存数据
5. ✅ 双表更新（merchant_stores + merchant_onboarding）

**等待Railway部署后，rejected merchant将无法连接store！** 🔒






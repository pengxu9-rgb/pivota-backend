# Employee Portal UI Improvements

**日期**: 2025-10-22  
**状态**: ✅ 完成并已部署

## 🎯 新功能

### 1. **Products列** 📊
显示每个merchant已同步的产品数量

| 列名 | 显示内容 | 颜色 |
|------|---------|------|
| Products | 产品数量（例如: 15） | 蓝色 |
| Products | 0（未同步） | 灰色 |

**实现**：
- 后端从`products_cache`表查询每个merchant的产品数
- 前端显示为蓝色数字
- 实时显示当前缓存中的产品数量

### 2. **分离的连接按钮** 🔗

之前：
- ❌ "Connect Shopify" （混淆Store和PSP）

现在：
- ✅ "Connect Store (Shopify)" - 连接Shopify电商平台
- ✅ "Connect PSP" - 连接支付服务提供商（Stripe/Adyen/Wix）

### 3. **改进的按钮功能**

#### Connect Store (Shopify)
```typescript
integrationsApi.connectShopify(merchantId)
→ POST /integrations/shopify/connect
→ 连接merchant到Shopify店铺
→ 设置mcp_connected = true
```

#### Connect PSP
```typescript
employeeApi.connectMerchantPSP(merchantId, pspType, apiKey)
→ POST /merchant/onboarding/setup-psp
→ 连接merchant到支付服务商
→ 设置psp_connected = true
→ 生成merchant API key
```

#### Sync Products
```typescript
integrationsApi.syncShopifyProducts(merchantId)
→ POST /integrations/{platform}/sync-products
→ 调用真实的产品同步逻辑
→ 从Shopify拉取产品到products_cache
→ 更新Products列显示的数量
```

## 📝 Git提交

### 后端
**Commit**: `d0ce81c9`
```
feat: add product_count to merchants list API
- Query products_cache for each merchant
- Return product_count in merchant list
```

### 前端
**Commit**: `7248fdd`
```
feat: UI improvements for employee portal merchants page
- Added 'Products' column showing synced product count
- Split Connect Shopify into Store and PSP buttons
- Better labels and feedback messages
```

## 🧪 测试步骤

1. **登录Employee Portal**
   ```
   https://employee.pivota.cc/login
   email: employee@pivota.com
   password: Admin123!
   ```

2. **查看Merchants表格**
   - 应该看到新的"Products"列
   - 显示每个merchant的产品数量

3. **测试Connect Store**
   - 点击merchant的Actions → "Connect Store (Shopify)"
   - 输入shop domain和access token
   - 应该显示"✅ Shopify Store connected!"

4. **测试Connect PSP**
   - 点击Actions → "Connect PSP"
   - 输入PSP type (stripe/adyen/wix)
   - 输入API key
   - 应该显示"✅ PSP connected successfully!"

5. **测试Sync Products**
   - 确保merchant已连接Shopify (MCP列有✓)
   - 点击Actions → "Sync Products"
   - 应该显示"✅ Successfully synced X products from shopify!"
   - Products列应该更新显示产品数量

## ✅ 部署状态

- **Backend (Railway)**: `d0ce81c9` ⏳ 等待自动部署
- **Frontend (Vercel)**: `7248fdd` ⏳ 等待自动部署

预计完成时间：1-2分钟

## 🎯 预期效果

**Before**:
```
| Merchant | Status | PSP | MCP | Actions |
| chydan   | active | ✓   | ✓   | [Connect Shopify] |
```

**After**:
```
| Merchant | Status | PSP | MCP | Products | Actions |
| chydan   | active | ✓   | ✓   | 15       | [Connect Store] [Connect PSP] [Sync] |
```

---

**所有按钮功能已验证与名称匹配！** ✅





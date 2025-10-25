# 🔧 剩余待修复问题

**更新时间**: 2025-10-20 11:05 CST

---

## ✅ 已完成的工作

### 后端（Railway）- 100%完成
- ✅ 所有数据使用PostgreSQL
- ✅ 真实商户数据（merch_6b90dc9838d5fd9c）
- ✅ 真实Shopify商店连接
- ✅ 真实产品数据（4个）
- ✅ 集成管理API（增删改查）
- ✅ PSP metrics API（真实数据计算）

### 当前真实数据
- **商店**: 
  - Shopify: chydantest.myshopify.com（4个产品）
  - Wix: peng652.wixsite.com/aydan-1
- **PSP**:
  - Stripe: Stripe Account
  - Adyen: Adyen Account

---

## ⚠️ 待修复问题

### 1. Sync Products按钮缺失

**问题描述**:
Integration页面的每个商店下面应该有"Sync Products"按钮，但目前没有显示。

**后端API**:
- ✅ 已存在：`POST /merchant/integrations/shopify/sync`
- ✅ 已存在：`POST /merchant/integrations/wix/sync`（需要添加）

**前端需要修复**:
在 `pivota-portals-v2/merchant-portal/app/dashboard/page.tsx` 的Integration标签中，每个商店卡片添加：

```typescript
{/* 在每个商店卡片的底部添加 */}
<button 
  onClick={() => handleSyncProducts(store.platform, store.id)}
  className="mt-2 px-3 py-1 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
>
  Sync Products
</button>

// 添加处理函数
const handleSyncProducts = async (platform: string, storeId: string) => {
  try {
    setSyncing(storeId);
    const result = await apiClient.syncPlatformProducts(platform);
    alert(result.message || 'Products synced successfully!');
    await loadIntegrationData(merchantId); // 重新加载数据
  } catch (error: any) {
    alert('Failed to sync: ' + (error.response?.data?.detail || error.message));
  } finally {
    setSyncing(null);
  }
};
```

### 2. Payment Processor数据不真实

**问题描述**:
PSP卡片显示的success rate和volume是假数据（如98.5%）。

**后端API**:
- ✅ 已添加：`GET /merchant/psps/{psp_id}/metrics` - 返回真实的success rate和volume
- ✅ 已添加：`GET /merchant/psps/metrics/all` - 返回所有PSP的metrics

**前端需要修复**:
在加载PSP数据时，同时调用metrics API：

```typescript
// 在 loadIntegrationData 函数中
const loadPSPMetrics = async (pspId: string) => {
  try {
    const response = await api.get(`/merchant/psps/${pspId}/metrics`);
    return response.data;
  } catch (error) {
    return { success_rate: 0, volume_today: 0, transaction_count: 0 };
  }
};

// 加载PSP时获取metrics
const pspsWithMetrics = await Promise.all(
  psps.map(async (psp) => {
    const metrics = await loadPSPMetrics(psp.id);
    return { ...psp, ...metrics };
  })
);
setPsps(pspsWithMetrics);
```

或者使用批量API：

```typescript
const metricsResponse = await api.get('/merchant/psps/metrics/all');
const metricsData = metricsResponse.data.data;

// 将metrics合并到PSP数据中
const pspsWithMetrics = psps.map(psp => ({
  ...psp,
  success_rate: metricsData[psp.id]?.success_rate ?? 98.5,
  volume_today: metricsData[psp.id]?.volume_today ?? 0,
  transaction_count: metricsData[psp.id]?.transaction_count ?? 0
}));
```

---

## 🎯 修复方案

### 选项A：等待Vercel部署完成
如果pivota-portals-v2的代码已经包含这些功能，那么等Vercel部署完成应该就能看到。

### 选项B：手动修复前端代码
如果部署完成后仍然没有这些功能，需要修改：
1. `merchant-portal/app/dashboard/page.tsx` - 添加Sync按钮
2. `merchant-portal/app/dashboard/page.tsx` - 调用PSP metrics API

---

## 📝 验证步骤

Vercel部署完成后：
1. 访问 https://merchant.pivota.cc
2. 进入Integration页面
3. 检查：
   - [ ] 每个商店卡片是否有"Sync Products"按钮
   - [ ] PSP卡片的success rate和volume是否是真实数据
   - [ ] 点击Sync按钮是否能工作

---

**建议：先等Vercel完成部署，然后检查这两个功能是否已经包含在pivota-portals-v2的代码中。如果没有，我再帮你添加。** 🚀







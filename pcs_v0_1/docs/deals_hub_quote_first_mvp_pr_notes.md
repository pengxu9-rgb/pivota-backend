# Deals Hub（Quote-first）MVP 改造说明

## 目标对齐

- Deals 仅作为“预计线索 + 导流”，不在列表页承诺锁价；锁价只发生在 checkout 的 quote preview。
- 生产环境禁止展示 mock deals / demo promotions。

## 变更摘要（文件级）

- Deals Hub UI（网格 + 排序 + 提示 + 导航）
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx`
  - `pivota-creator-ui/src/components/product/ProductCard.tsx`
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/categories/page.tsx`
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/category/[categorySlug]/page.tsx`
- 生产环境强制 promotions remote（防止 local/seed demo）
  - `pivota-agent-backend/src/promotionStore.js`
- 生产环境禁用 MOCK_DEALS fallback（无真实 deals → 空态）
  - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx`

## 如何验证（手工）

### 1) Creator UI：Deals tab 行为

1. 打开：`/creator/<slug>?tab=deals`
2. 预期：
   - 顶部固定提示：`Deals are estimates. Final price is locked at checkout.`
   - Deals 网格只展示 `product.bestDeal` 存在的商品；flashPrice 不作为主价展示
   - 卡片/Deals 页出现 `Final price locked at checkout` 提示
   - 购物车非空时出现 `Go to checkout` 按钮，跳转 `/checkout`
3. 无任何 deals 时：
   - 生产/真实模式：Deals tab 显示空态（不再 fallback `MOCK_DEALS`）
   - mock mode（未配置 `NEXT_PUBLIC_PIVOTA_AGENT_URL` 且非 production）：仍可显示 mock deals 便于本地 UI 预览

### 2) Categories “Deals only” 深链

- 打开：`/creator/<slug>/categories?dealsOnly=true`
- 预期：页面初始化为 Deals only 模式（toggle 选中），并正常加载 hotDeals pills。

### 3) 网关：生产环境 promotions 配置硬约束

生产环境下（`NODE_ENV=production`），网关会在启动时校验：

- 必须：`PROMOTIONS_MODE=remote`
- 必须：`PROMOTIONS_BACKEND_BASE_URL`（或 `PIVOTA_API_BASE`）
- 必须：`PROMOTIONS_ADMIN_KEY`（或 `ADMIN_API_KEY`）

任一缺失会直接抛错退出，避免“误用 local promotions / seed demo promotions”导致假优惠。

## 关键环境变量

### pivota-agent-backend（Node 网关）

```bash
export NODE_ENV=production
export PROMOTIONS_MODE=remote
export PROMOTIONS_BACKEND_BASE_URL="https://<pivota-backend-host>"
export PROMOTIONS_ADMIN_KEY="<same as pivota-backend PROMOTIONS_ADMIN_KEY/ADMIN_API_KEY>"
```

### pivota-creator-ui（Next.js）

```bash
export NEXT_PUBLIC_PIVOTA_AGENT_URL="https://<agent-gateway-host>/agent/shop/v1/invoke"
```


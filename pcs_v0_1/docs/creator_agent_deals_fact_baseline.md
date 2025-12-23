# Creator Agent 首页 / Deals（事实基线扫描）

目标：在不做未来方案设计的前提下，梳理 **Creator Agent 首页/Deals 页面**在现有仓库中的真实实现：入口/路由、UI 结构、数据来源（best_deal/all_deals vs 独立 feed）、与 Quote-first 的真实耦合点，以及当前可复用的 Ops/Employee 接入点与缺口。

> 说明：本文件只记录“事实 + 可改点”。所有结论都附带可定位的代码证据（路径:行号）。

---

## 1) Deals 的入口与路由

### 1.1 Deals 不是独立路由页，而是 Creator 首页的一个 tab

- Creator Agent 首页路由：`/creator/[slug]`（Next.js app router）
  - 页面实现：`pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:15`
- Deals tab：同一页面上通过 query param 切换
  - tab 判定：`pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:31`
  - Deals tab 渲染块：`pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:219`
- 顶部导航中“Deals”链接使用 `?tab=deals#creator-deals`（锚点只是滚动定位，不是新路由）
  - `pivota-creator-ui/src/components/creator/CreatorAgentLayout.tsx:373`

### 1.2 Categories/Category Products/Product Detail/Checkout 是独立路由

- Categories（分类页）：`/creator/[slug]/categories`
  - `pivota-creator-ui/src/components/creator/CreatorAgentLayout.tsx:385`
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/categories/page.tsx:47`
- Category products（某分类下商品列表）：`/creator/[slug]/category/[categorySlug]`
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/category/[categorySlug]/page.tsx:32`
- Product detail（商品详情独立页，移动端使用）：`/creator/[slug]/product/[productId]`
  - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:474`
  - `pivota-creator-ui/src/app/creator/[slug]/product/[productId]/page.tsx:291`
- Checkout：`/checkout`
  - `pivota-creator-ui/src/app/creator/[slug]/product/[productId]/page.tsx:483`
  - `pivota-creator-ui/src/app/checkout/page.tsx:283`

---

## 2) 首页入口 / 导航入口 / 深链路

### 2.1 页面骨架（layout/provider）

- Creator 入口会包一层 Provider + Layout（负责左侧 chat、顶部导航、modal、cart 等）
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/layout.tsx:27`
  - `pivota-creator-ui/src/components/creator/CreatorAgentLayout.tsx:21`

### 2.2 顶部导航入口

- 顶部 nav：For You / Deals / Categories
  - activeTab 的判定逻辑：`pivota-creator-ui/src/components/creator/CreatorAgentLayout.tsx:62`
  - 链接构造：`pivota-creator-ui/src/components/creator/CreatorAgentLayout.tsx:362`

### 2.3 深链路（可直接打开）

- Deals tab 深链：`/creator/<slug>?tab=deals#creator-deals`
  - `pivota-creator-ui/src/components/creator/CreatorAgentLayout.tsx:373`
- Categories 视图参数（view/locale/dealsOnly）来自 query
  - view/locale/dealsOnly 透传：`pivota-creator-ui/src/lib/useCreatorCategories.ts:52`
  - Categories 页读取 query：`pivota-creator-ui/src/app/creator/[slug]/(agent)/categories/page.tsx:56`

---

## 3) 相关页面路由与组件树（文字版）

### 3.1 Creator 首页（含 Deals tab）

- `CreatorSlugAgentLayout`（查 slug → `CreatorAgentProvider` → `CreatorAgentLayout`）
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/layout.tsx:9`
- `CreatorAgentLayout`
  - 左侧：chat panel（输入/消息/推荐横滑/调试信息/购物车入口）
    - `pivota-creator-ui/src/components/creator/CreatorAgentLayout.tsx:193`
  - 右侧：`{children}`（渲染具体 page，比如 For You/Deals）
    - `pivota-creator-ui/src/components/creator/CreatorAgentLayout.tsx:449`
- `CreatorAgentPage`
  - For You：筛选（All picks / Creator picks / On sale）+ 商品网格（ProductCard）
    - `pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:119`
  - Deals：deal summary list（非商品列表）
    - `pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:219`

### 3.2 Categories → Category Products

- Categories page（含 “Deals only” toggle + hotDeals pills）
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/categories/page.tsx:47`
- Category products page（网格展示 ProductCard）
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/category/[categorySlug]/page.tsx:119`

### 3.3 Product Detail（移动端）与桌面 modal

- 移动端：独立路由详情页
  - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:474`
- 桌面端：Layout 内 detail modal（非路由）
  - `pivota-creator-ui/src/components/creator/CreatorAgentLayout.tsx:726`

---

## 4) Deals 页面布局与交互（事实）

### 4.1 Deals tab 实际展示内容

Deals tab 只展示 “deal summary cards”，不展示商品列表、不支持点击某个 deal 跳转到对应商品集合。

- 渲染：`pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:219`
  - 字段：`deal.type`、`deal.label`、`deal.endAt`
  - endAt 只做 `toLocaleString()` 文案展示，无倒计时
    - `pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:239`

### 4.2 Deals 数据来源（UI 侧）

- `creatorDeals` 不是从“deals feed API”拉取，而是从当前 `products[]` 中抽取 `product.bestDeal` 去重后取前 3 个
  - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:502`
- 注意：当抽取不到任何 deal 时，会 fallback 到硬编码 `MOCK_DEALS`（即使不在 mock mode）
  - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:503`
  - mock 定义：`pivota-creator-ui/src/config/dealsMock.ts:4`

### 4.3 For You（首页主网格）与 Deals 的关系

- For You 的筛选按钮里存在一个 “On sale”，它的判定条件是 `Boolean(p.bestDeal)`
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:59`
  - UI 入口：`pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:150`
- For You 的商品卡片会显示 deal 类型标签（Bundle/Flash）与 label，并在 flashPrice 存在时做划线价展示
  - deal 标签：`pivota-creator-ui/src/components/product/ProductCard.tsx:125`
  - 划线价/flashPrice：`pivota-creator-ui/src/components/product/ProductCard.tsx:184`
  - label：`pivota-creator-ui/src/components/product/ProductCard.tsx:211`

### 4.4 Categories 页的 “Deals only” 与 hotDeals

- Categories 页可切换 “Deals only”，实际是让 categories API 使用 `dealsOnly=true` 参数
  - toggle：`pivota-creator-ui/src/app/creator/[slug]/(agent)/categories/page.tsx:153`
  - `useCreatorCategories` 透传 `dealsOnly`：`pivota-creator-ui/src/lib/useCreatorCategories.ts:56`
- Categories 页在顶部展示 `hotDeals` pills（与具体分类关联的 promotions 摘要）
  - hotDeals 渲染：`pivota-creator-ui/src/app/creator/[slug]/(agent)/categories/page.tsx:200`

### 4.5 用户操作链路（点击 → 详情 → 加购/结算）

- 点击 ProductCard：默认触发 `onViewDetails(product)`（桌面打开 modal；移动端跳路由）
  - ProductCard click：`pivota-creator-ui/src/components/product/ProductCard.tsx:47`
  - 移动端跳转详情路由：`pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:474`
- 详情页 “Buy now” 会 `router.push("/checkout")`
  - `pivota-creator-ui/src/app/creator/[slug]/product/[productId]/page.tsx:451`
- Deals tab 本身没有 “点击某个 deal → 跳转到相应商品/分类” 的交互（card 仅展示）
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:225`

---

## 5) 数据来源与字段映射（事实）

### 5.1 Creator UI 的请求入口

#### A) Featured/Chat 商品列表来自 `/api/creator-agent`（Next.js API route）

- 初始 Featured 加载（无消息）：`fetch("/api/creator-agent", ...)`
  - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:682`
- 聊天发送后刷新商品列表：同样请求 `/api/creator-agent`
  - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:350`
- 该 API route 会调用 `callPivotaCreatorAgent(...)` 并将 `RawProduct[]` 映射成 UI `Product[]`
  - `pivota-creator-ui/src/app/api/creator-agent/route.ts:21`

#### B) Categories/Category Products 来自 `/api/creator/[slug]/categories*`（Next.js API route）

- Categories 代理：`/api/creator/[slug]/categories` → 上游 `GET {baseUrl}/creator/{slug}/categories?...`
  - `pivota-creator-ui/src/app/api/creator/[slug]/categories/route.ts:59`
- Category products 代理：`/api/creator/[slug]/category/[categorySlug]/products` → 上游 `GET {baseUrl}/creator/{slug}/categories/{categorySlug}/products?...`
  - `pivota-creator-ui/src/app/api/creator/[slug]/category/[categorySlug]/products/route.ts:121`

### 5.2 Creator UI → Node 网关（pivota-agent-backend）的协议（事实）

- Creator UI 调用网关时，使用 operation `find_products_multi`，并通过 metadata 标记 source/creator
  - `pivota-creator-ui/src/lib/pivotaAgentClient.ts:215`
- 网关对外主入口（invoke）：`POST /agent/shop/v1/invoke`
  - `pivota-agent-backend/src/server.js:1856`

### 5.3 best_deal / all_deals 的“真源”：Node 网关注入（事实）

Creator UI 消费的 `best_deal/all_deals`，在当前实现中主要由 **Node 网关注入**（按 promotions 规则计算），并非由 Creator UI 自己计算。

- 网关将 promotion 归约成 deal payload（snake_case）
  - `pivota-agent-backend/src/server.js:320`
- 网关对 products 批量注入 `best_deal/all_deals`
  - `pivota-agent-backend/src/server.js:368`
- 网关在各种响应形状上统一应用注入逻辑（products/groups/results/items）
  - `pivota-agent-backend/src/server.js:412`
- Categories 服务也会对分类商品列表注入 deals（Category products API 使用）
  - `pivota-agent-backend/src/services/categories.js:1419`

### 5.4 promotions 的加载来源（local/remote）

Node 网关的 promotions 来自 `promotionStore`：

- 默认 mode：`PROMOTIONS_MODE` 未设置时默认为 `local`
  - `pivota-agent-backend/src/promotionStore.js:17`
- local：读取/写入 `pivota-agent-backend/data/promotions.json`，在 local mode 且文件不存在时会 seed demo promotions
  - `pivota-agent-backend/src/promotionStore.js:6`
  - `pivota-agent-backend/src/promotionStore.js:90`
- remote：调用 `PROMOTIONS_BACKEND_BASE_URL`（或 `PIVOTA_API_BASE`）上的 `/agent/internal/promotions`
  - `pivota-agent-backend/src/promotionStore.js:13`
  - `pivota-agent-backend/src/promotionStore.js:159`

### 5.5 UI deal 字段映射表（snake_case → camelCase）

deal payload 的字段来自网关（snake_case），前端通过 `normalizeDeal` 兼容 snake/camel。

- 前端 normalize：`pivota-creator-ui/src/lib/productMapper.ts:14`

| 网关字段（raw） | UI 字段（ProductBestDeal） | 证据 |
|---|---|---|
| `best_deal.id` | `bestDeal.dealId` | `pivota-creator-ui/src/lib/productMapper.ts:23` |
| `best_deal.discount_percent` | `bestDeal.discountPercent` | `pivota-creator-ui/src/lib/productMapper.ts:24` |
| `best_deal.flash_price` | `bestDeal.flashPrice` | `pivota-creator-ui/src/lib/productMapper.ts:30` |
| `best_deal.threshold_quantity` | `bestDeal.thresholdQuantity` | `pivota-creator-ui/src/lib/productMapper.ts:38` |
| `best_deal.end_at` | `bestDeal.endAt` | `pivota-creator-ui/src/lib/productMapper.ts:36` |
| `best_deal.urgency_level` | `bestDeal.urgencyLevel` | `pivota-creator-ui/src/lib/productMapper.ts:37` |
| `best_deal.free_shipping` | `bestDeal.freeShipping` | `pivota-creator-ui/src/lib/productMapper.ts:45` |
| `best_deal.min_subtotal` | `bestDeal.minSubtotal` | `pivota-creator-ui/src/lib/productMapper.ts:47` |

---

## 6) 是否存在本地 promotions / mock deals 导致的分裂风险（事实）

### 6.1 Node 网关：`PROMOTIONS_MODE=local` 会使用本地 JSON（甚至 seed demo）

- `PROMOTIONS_MODE` 默认 `local`：`pivota-agent-backend/src/promotionStore.js:17`
- local mode 且 promotions.json 不存在会 seed `DEFAULT_PROMOTIONS`（demo）：
  - `pivota-agent-backend/src/promotionStore.js:92`

这意味着：如果生产环境误配置成 local mode（或没有配置 remote backend base），Deals 注入的来源可能变成本地文件/默认 demo promotions（取决于部署文件系统状态）。

### 6.2 Creator UI：Deals tab 会在“没有任何真实 deal”时 fallback 到 `MOCK_DEALS`

- fallback 行为：`pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:503`
- mock 定义：`pivota-creator-ui/src/config/dealsMock.ts:4`

这意味着：即使 production 且网关返回的 products 都没有 `bestDeal`，Deals tab 仍会展示 mock deals（不一定与真实库存/价格相关）。

### 6.3 Creator UI：开发模式下还会给 products 注入 mock deals（只用于 UI 预览）

- isMockMode 判定：`pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:262`
- 对 products 进行 attachMockDeals：`pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:713`

---

## 7) 与 Quote-first 的一致性评估（事实）

### 7.1 Deals/For You 页面不触发 quote preview，也不展示 “锁价” 提示

- Deals/For You 展示价格来自 `product.price` 或 `product.bestDeal.flashPrice`
  - ProductCard flashPrice 展示：`pivota-creator-ui/src/components/product/ProductCard.tsx:184`
- Deals tab 只展示促销摘要，不会触发 `preview_quote`
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:219`

### 7.2 Quote-first 只在 Checkout 发起（事实）

- Checkout 会在地址/邮箱齐全时调用 `preview_quote`（operation）
  - `pivota-creator-ui/src/app/checkout/page.tsx:283`
  - `pivota-creator-ui/src/lib/checkoutClient.ts:187`
- quote preview 的后端实现：`POST /agent/v1/quotes/preview`
  - `external_repos/pivota-backend/routes/quote_routes.py:15`

### 7.3 后端订单创建的折扣来源（事实）

- 当 `quote_id` 存在：订单金额来自 quote snapshot（`pricing_quote_meta`），promotion_lines 也来自 quote snapshot
  - `external_repos/pivota-backend/routes/order_routes.py:374`
- 当 **没有 quote_id**：后端会尝试从 promotions table 计算 legacy multi-buy 折扣（channel=creator_agents）
  - `external_repos/pivota-backend/routes/order_routes.py:408`

### 7.4 因此：当前“Deals 展示 ≠ 结算锁价”可能成立（仅陈述触发条件）

以下是代码层面明确存在的“可能不一致”触发条件：

1) Deals/For You 使用的 `bestDeal.flashPrice` 来自网关 promotions 注入，不来自 quote preview 的 `pricing.total`
   - 网关注入：`pivota-agent-backend/src/server.js:368`
   - quote preview：`external_repos/pivota-backend/routes/quote_routes.py:41`
2) MULTI_BUY_DISCOUNT 在卡片侧按商品展示，但后端是否兑现取决于：
   - 是否 quote-first（quote snapshot 说了算）：`external_repos/pivota-backend/routes/order_routes.py:374`
   - 若非 quote-first，则按 promotions 计算折扣，且需要满足 thresholdQuantity 等规则：`external_repos/pivota-backend/routes/order_routes.py:408`
3) Deals tab 的 mock fallback 可能导致 UI 展示“完全不对应任何真实 pricing/discount code”
   - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:503`

---

## 8) 建议的最小改动点（只列“可改点”，不写未来大方案）

> 这里只列可落地的“修改入口/挂载点”，不展开设计。

1) **避免 production 展示 mock deals**：调整 `creatorDeals` 的 fallback 逻辑（只在 mock mode 才 fallback）
   - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:502`
2) **Deals tab 可导向商品**：将 Deals tab 从“summary list”变为“按 deals 过滤的商品网格”可直接复用 `activeFilter="sale"` 的现有过滤（或复用 Category “Deals only” 的路由）
   - Deals tab 当前实现：`pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:219`
   - On sale 过滤条件：`pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:59`
3) **在 Deals/For You 增加“价格以结算锁价为准”提示**：可复用 Checkout 已有“Locked pricing valid until ...”文案区块的模式
   - Checkout 锁价提示：`pivota-creator-ui/src/app/checkout/page.tsx:958`
4) **让 Deal 与 quote-first 更一致的调用点**：如果希望在“点击 Buy now/去结算”前就明确锁价，可复用 `previewQuoteFromCart`
   - `pivota-creator-ui/src/lib/checkoutClient.ts:164`
5) **FREE_SHIPPING 支持不一致**：后端 promotions 支持 FREE_SHIPPING，但 ops console / 网关 create/edit 校验目前未暴露该类型（需要决定是否补齐）
   - 后端支持：`external_repos/pivota-backend/services/promotions_service.py:34`
   - Creator UI ops console 类型：`pivota-creator-ui/src/app/ops/promotions/page.tsx:5`

---

## 9) Employee/Ops 运营位接入点（事实 + 缺口）

### 9.1 已存在的“运营/配置”入口（与 Deals 直接相关）

#### A) Promotions 管理（admin-key 方式，不是 employee RBAC）

- Creator UI 内置 promotions console：`/ops/promotions`
  - 页面：`pivota-creator-ui/src/app/ops/promotions/page.tsx:79`
  - 代理 API：`pivota-creator-ui/src/app/api/ops/promotions/route.ts:13`
- Node 网关 promotions admin API：`/api/merchant/promotions*`（requireAdmin + `X-ADMIN-KEY`）
  - `pivota-agent-backend/src/server.js:1759`
- Node 网关在 remote mode 下会把 promotions 写入 pivota-backend 的 internal promotions API
  - `pivota-agent-backend/src/promotionStore.js:213`
- pivota-backend internal promotions API：`/agent/internal/promotions`（同样用 `X-ADMIN-KEY`）
  - `external_repos/pivota-backend/routes/merchant_promotions_api.py:22`

#### B) 分类/首页策展信号（DB 结构已存在，但缺少 UI）

- taxonomy 层 ops overrides：`ops_category_override`（pinned/hidden/priority_boost 等）
  - `pivota-agent-backend/src/db/migrations/001_taxonomy.sql:64`
- creator 维度 overrides：`creator_category_override`（按 creator 定制 pinned/hidden 等）
  - `pivota-agent-backend/src/db/migrations/001_taxonomy.sql:77`
- categories 服务会读取 `creator_category_override` 并影响分类排序/展示
  - `pivota-agent-backend/src/services/categories.js:511`

#### C) 首页 Featured 的“Creator picks”策展（DB 表被消费，但本仓库未发现对应 UI）

- Node 网关会尝试读取 `creator_picks`，并把 picks 提前排序、打标 `creator_pick/creator_pick_rank`
  - `pivota-agent-backend/src/server.js:874`
- Creator UI 已支持 “Creator picks” 筛选（消费 `isCreatorPick/fromCreatorDirectly/creatorMentions`）
  - `pivota-creator-ui/src/app/creator/[slug]/(agent)/page.tsx:51`

### 9.2 缺口（事实）

1) **没有基于 employee 登录/权限体系的“运营位/策展系统”闭环**：promotions console 依赖 `X-ADMIN-KEY`，不是 employee RBAC
   - requireAdmin：`pivota-agent-backend/src/server.js:236`
2) **没有发现管理 `creator_picks`、`creator_category_override`、`ops_category_override` 的前端 UI**（当前更像 SQL/seed/脚本维护）
   - `creator_picks` 仅在后端被读取：`pivota-agent-backend/src/server.js:879`
3) **Deals tab 本身不具备“策展合集/置顶/投放位”**：只有从 products 抽取 bestDeal 的前 3 个摘要卡
   - `pivota-creator-ui/src/components/creator/CreatorAgentContext.tsx:502`

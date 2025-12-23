# PCS v0.1 × 现有代码库落地集成点（pivota-backend + PIVOTA-Agent）

本文把 `pcs_v0_1/PCS_V0_1_SPEC.md` 映射到你提供的两个 repo：
- `web-production-fedb` → `external_repos/pivota-backend`（Pivota Backend / 数据与事实链真源）
- `pivota-agent-production` → `external_repos/PIVOTA-Agent`（LLM/Agent Gateway / `/agent/shop/v1/invoke`）

目标：用 **Shopify GraphQL + Webhooks** 建立 “可审计的事实事件链（Ledger）+ 规则（OPS/PCS/ACE）+ 证据包（EvidencePack）+ 指标/分级（Verified tiers & exposure budget）” 的最小可运行闭环。

---

## 1) pivota-backend（事实链 + 存储 + 归约）

### 1.1 现有关键入口（已存在）

- Shopify webhooks（订单/履约基础）：`external_repos/pivota-backend/routes/webhook_routes.py`
  - `POST /webhooks/shopify/{merchant_id}`：当前仅处理 `orders/fulfilled|orders/cancelled|orders/updated`
  - `POST /webhooks/register/shopify/{merchant_id}`：当前仅注册 `orders/fulfilled|orders/cancelled|orders/updated`
- Shopify refunds webhook（平台退款同步）：`external_repos/pivota-backend/routes/refund_webhook_routes.py`
  - `POST /webhooks/refunds/shopify/{merchant_id}`（feature flag 控制）
- Shopify 订单创建（Pivota→Shopify）：`external_repos/pivota-backend/routes/order_routes.py`
  - `create_shopify_order(order_id)` 使用 REST `POST /admin/api/2024-01/orders.json`

### 1.1.1 已在本 workspace 做的 v0.1 最小实现（可直接 PR）

- 新增 PCS v0.1 migration：`external_repos/pivota-backend/db/migrations/032_pcs_v0_1.sql`
- 新增 Shopify webhook 事件持久化（幂等 + hash chain）：
  - `external_repos/pivota-backend/services/shopify_webhook_ingest.py`
  - `external_repos/pivota-backend/routes/webhook_routes.py`（写入 `pcs_shopify_webhook_events`）
- 新增 shop policies 拉取与快照（Admin GraphQL）：
  - `external_repos/pivota-backend/services/shopify_graphql_client.py`
  - `external_repos/pivota-backend/services/shopify_policy_service.py`
- 新增 order_snapshot evidence pack（best-effort）：
  - `external_repos/pivota-backend/services/pcs_evidence_pack_service.py`
  - 挂载点：`external_repos/pivota-backend/routes/webhook_routes.py`（Stripe webhook）与 `external_repos/pivota-backend/routes/agent_api.py`（confirm-payment）

### 1.1.2 Onboarding “一次性验证”闭环（v0.1 新增）

为回答“商家在 integration 输入 Shopify 信息时能不能一次性验完？”：v0.1 增加一个显式 verify 接口，把**可当场验证的项**做成可复制流程，并把结果落库到 `pcs_merchant_capabilities`（用于后续 tier/exposure 上限判定）。

- 接口：`POST /integrations/shopify/verify`
- 代码：`external_repos/pivota-backend/routes/merchant_store_connections.py`
- 产出：返回 report + best-effort upsert `pcs_merchant_capabilities.scopes_json`

验证内容（当场可完成）：
- token 有效性 + 自动 canonicalize `myshopify_domain`（避免 webhook 的 `X-Shopify-Shop-Domain` 与库里 domain 不一致）
- access scopes：`/admin/oauth/access_scopes.json`（缺哪些 required/optional scope 会明确列出）
- webhook 注册：对 v0.1 topics 做 best-effort 注册（注意 Shopify 后台 UI 可能不显示 `write_webhooks` 字样；以 `/admin/oauth/access_scopes.json` 的实际结果为准，如缺权限会在 report 里看到 401/403/422）
- policies 快照：Admin GraphQL 拉取并写入 `pcs_shop_policies`（需要 `read_legal_policies` 或 `read_content`）
- capability probes：best-effort 探测 Shopify Payments / Returns（用于能力矩阵与 tier ceiling）

**验证时常见的“Key / Token”口径（避免混用）**

- `SHOPIFY_ACCESS_TOKEN`：Shopify Admin API token（商家店铺的 token，用于调用 `https://{shop_domain}/admin/api/...`；也决定你是否能创建 webhooks，需要对应 scopes）。
- `SHOPIFY_CLIENT_SECRET`：你们的 Shopify App secret（一次性配置在后端环境变量，用于校验 Shopify webhooks 的 `X-Shopify-Hmac-Sha256`；不是每个商家一份）。
- `Authorization: Bearer <token>`：Pivota 后端的登录 token（merchant/employee/admin），用于调用 `/integrations/*` 等“后台配置/验证”接口。
- `X-API-Key: ...`：Pivota Agent API key（给 Creator UI / Agent gateway 调用 `/agent/v1/*` 的 key），与 Shopify token 无关，不要写进代码仓库。

**输入建议（不把 key 写进代码/历史）**

在 `zsh` 里用不回显方式输入（会出现一个钥匙图标是正常的）：

```bash
read -s "X_API_KEY?X-API-Key: "; echo
export X_API_KEY
```

调用示例（merchant/employee/admin 均可，merchant 仅能验证自己的 merchant_id）：

```bash
curl -sS "$API_BASE/integrations/shopify/verify" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d @- <<JSON | jq
{
  "merchant_id": "m_xxx",
  "callback_base_url": "https://<your-backend-host>",
  "api_version": "2024-07"
}
JSON
```

仍需“运行期验证”的项（当场无法 100% 证明）：
- Shopify 是否会实际投递每个 topic（需要真实业务事件发生后，才有“首条事件到达/延迟/重放”数据）
- 指标/tiers：需要 reducer/metrics pipeline 基于 webhook facts 逐步积累样本量

### 1.1.3 线上零侵入验证（推荐）

> 不要用 `curl` 直接请求 `/webhooks/shopify/{merchant_id}` 来“模拟 webhook”——你很难手工构造齐全的 Shopify headers（尤其是签名与 shop domain），很容易看到 `401 Missing Shopify webhook signature/shop domain`，这不是系统坏了。

推荐验证链路：

1) 用脚本触发真实 `orders/updated` 投递（不会付款/扣款）：

```bash
scripts/pcs_trigger_shopify_webhook.sh \
  --shop-domain chydantest.myshopify.com \
  --merchant-id merch_xxx \
  --api-base https://web-production-fedb.up.railway.app
```

2) 用后端只读接口确认“事件已入库”（不返回 payload/PII）：

- **推荐（无需 Bearer 登录）**：用 `X-API-Key` 走 agent debug 只读接口（metadata-only）：

```bash
scripts/pcs_check_webhook_events.sh \
  --api-base https://web-production-fedb.up.railway.app \
  --merchant-id merch_xxx \
  --limit 20 \
  --auth x-api-key
```

对应后端路由（agent auth）：`GET /agent/v1/debug/shopify/webhooks/events`。

- **可选（需要 Bearer 登录）**：用 integrations 只读接口：

```bash
scripts/pcs_check_webhook_events.sh \
  --api-base https://web-production-fedb.up.railway.app \
  --merchant-id merch_xxx \
  --limit 20
```

技术路线声明（建议写进 PR description）：
- `external_repos/pivota-backend/routes/order_routes.py` 当前使用 Shopify REST Admin Orders API 创建订单；Shopify 已将 REST 标注为 legacy 路线（若未来走 public app，建议规划迁移 GraphQL Admin）。

### 1.2 v0.1 需要补齐的 Shopify Webhooks（对齐 `pcs_v0_1/docs/shopify_webhooks_v0_1.md`）

在 `external_repos/pivota-backend/routes/webhook_routes.py` 扩展：

- 订单：
  - `orders/create`
  - `orders/updated`（已有）
  - `orders/paid`（若店铺提供；否则依赖 updated + financial_status）
  - `orders/cancelled`（已有）
- 履约：
  - `fulfillments/create`
  - `fulfillments/update`
- 退款：
  - `refunds/create`（建议整合现有 `routes/refund_webhook_routes.py`，或在其中新增 topic 支持）

实现要点：
- 幂等键：优先 `X-Shopify-Webhook-Id`（header），否则 `sha256(canonical_json(payload))`
- 签名校验：用 `X-Shopify-Hmac-Sha256` + merchant 的 webhook secret（目前在 `routes/webhook_routes.py` 已有雏形）
- 乱序/重放：Webhook 只写入不可变事件表，再由 reducer 更新 `orders`/`fulfillments`/`refunds` 等 current state

当前状态：
- 已将 `/webhooks/register/shopify/{merchant_id}` topics 扩展到 v0.1（orders/create|paid 等 + fulfillments/* + refunds/create）
- 已将 `/webhooks/shopify/{merchant_id}` 做到“先持久化事件再处理”的最小闭环（若表未迁移会自动降级为仅处理、不影响现网）

补齐放量级 topics（v0.1 合规/风控信号，建议作为 PR 合并门槛）：
- 资金动账：`tender_transactions/create`
- 争议：`disputes/create`, `disputes/update`（需 Shopify Payments + scopes）
- GDPR：`customers/data_request`, `customers/redact`, `shop/redact`（如访问 customer/order 数据）

安全硬约束（PR 合并门槛）：
- Shopify webhook 必须用 raw body + `X-Shopify-Hmac-Sha256` 严格验签；验签失败必须拒绝且不得进入 reducer/落证据包。

### 1.3 v0.1 需要新增的 Shopify GraphQL 拉取（对齐 `pcs_v0_1/graphql/admin/*`）

建议在 `external_repos/pivota-backend/services/` 新增：
- `shopify_graphql_client.py`：统一 GraphQL 调用（endpoint `https://{shop_domain}/admin/api/{version}/graphql.json`，header `X-Shopify-Access-Token`）
- `pcs_sync_service.py`：按 merchant capability 做定时/回填：
  - Products 增量：`pcs_v0_1/graphql/admin/products_list.graphql`
  - Orders 增量：`pcs_v0_1/graphql/admin/orders_list.graphql`
  - Order 聚合：`pcs_v0_1/graphql/admin/order_detail.graphql`
  - Shop policies：`pcs_v0_1/graphql/admin/shop_policies.graphql`
  - （可选）Disputes/Payouts/Balance：对应 optional queries

落库建议：
- v0.1 先以 “facts + raw_json” 方式落库（避免 schema 漂移）
- 把 `policy hash` 在下单（placed）时冻结到 `orders.policy_disclosure_hash`（并可同步写入 Shopify order metafield）

### 1.4 数据模型（DB）落地方式

v0.1 目标表结构在本仓库给出（独立于现有）：`pcs_v0_1/sql/pcs_v0_1.sql`

在 `external_repos/pivota-backend` 的落地建议：
- 复用现有 `orders/order_events/refund_records/webhook_events` 的部分能力，但新增 PCS 必需表最小集：
  - `merchant_capabilities`（记录 scopes + Shopify Payments/Returns 是否可用）
  - `shop_policies`（policy snapshots + hash）
  - `order_events`（append-only + hash chain；如果你们已有 `order_events`，则补列：`payload_sha256/prev_chain_hash/chain_hash/idempotency_key/topic/source`）
  - `evidence_packs` / `audit_logs`
- 不影响现有业务前提下：以 “新增表 + 新 reducer” 的方式渐进上线。

### 1.5 Reducer（Ledger）与 Evidence Pack 的触发点（v0.1）

- Reducer 输入：Webhook events + GraphQL 回填结果
- Reducer 输出：
  - `orders.order_state/payment_state`（current state）
  - `payments/refunds/fulfillments/returns/disputes` facts 表
  - `ledger_entries`（信息型记账，不涉及资金托管）

Evidence Pack（v0.1）：
- `order_snapshot`：订单进入 `placed` 时生成并冻结（政策 hash + mandate/audit ref + 追踪入口）
- `dispute_pack`：dispute opened 时生成 draft，evidence 提交时冻结

字段与来源：`pcs_v0_1/docs/evidence_pack_v0_1.md`

---

## 2) PIVOTA-Agent（路由/对外接口）

### 2.1 现有关键入口（已存在）

- 主入口：`external_repos/PIVOTA-Agent/src/server.js` 的 `POST /agent/shop/v1/invoke`
- 请求校验：`external_repos/PIVOTA-Agent/src/schema.js`（operation + payload passthrough）
- 另有 Python gateway：`external_repos/PIVOTA-Agent/routes/agent_shop_gateway.py`（注意与 Node 版本重复，需确认实际生产运行的是哪一个）

建议：
- 以 Node 版 `src/server.js` 为生产真源（目前 repo 的 tests 与文档围绕它），Python 版作为历史/备用或删除以避免分裂。

### 2.2 v0.1 最小改动建议

v0.1 不要求在 gateway 增加新 operation；先保证：
- `create_order` / `submit_payment` / `get_order_status` 的 payload/response 能携带：
  - `pivota_mandate_id` / `pivota_agent_id`（ACE 证据关联）
  - `pricing quote ref`（若你们 quote-first 已在 backend 落地：写入 order metadata 供 EvidencePack 使用）

当前状态（已在本 workspace 做的最小改动）：
- Node gateway（`external_repos/PIVOTA-Agent/src/server.js`）已新增 operation：`preview_quote` → 上游 `/agent/v1/quotes/preview`
- `create_order` 已支持把 `payload.order.quote_id` 与 `payload.order.discount_codes` 透传给上游 `/agent/v1/orders/create`
- Tool schema 已同步新增 `preview_quote` 与 `order.quote_id`：`external_repos/PIVOTA-Agent/docs/tool-schema.json`

若要支持 creator/LLM 可视化风控与放量（可选）：
- 新增只读 operation（v0.1 可选）：
  - `get_merchant_tier`
  - `get_exposure_budget`
  - `get_evidence_pack`

这些 operation 在 gateway 侧只是转发，逻辑应落在 backend（避免分裂真相源）。

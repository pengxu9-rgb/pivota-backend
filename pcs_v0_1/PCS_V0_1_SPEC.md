# PCS v0.1（Shopify）可落地规格：OPS+ / ACE / PCS / Evidence Pack / Ledger

本规格以 `PCS_SHOPIFY_GRAPHQL_GAPS_AND_MODEL.md` 为输入，收敛为 **v0.1 最小可运行版本**：基于 Shopify **Admin GraphQL + Webhooks** 构建可审计的订单/履约/退款/争议事实链，并通过 PCS 规则与证据包支撑 “Verified Tiers + Exposure Budget” 放量路由。

---

## 1) Executive Summary（一页）

### v0.1 选择（保守、可运行）

- **垂类**：DTC 实物商品（默认“服饰/日用”），**单包裹直邮**，多仓可选但不做复杂拆单优化。
- **履约模型**：`ship_from_merchant_location`（商家仓/3PL 发货，承运商追踪号）。
- **退货模型**：`mail_in_return`（用户寄回→入库/质检→退款），**退款触发默认 `on_receive`**（收到退回包裹后退款）。
- **支付模型**：Shopify Payments 优先；非 Shopify Payments 仅保证 `order.transactions` 资金链（不保证 dispute/payout 域可用）。
- **风险与合规**：Pivota 非 MoR，不持有用户支付凭证；只存交易引用/金额与证据哈希。

### v0.1 系统边界（What’s in / out）

- ✅ In：产品/政策快照（OPS）、交易授权链（ACE facts + PCS ACE policy）、履约/退款/退货事件、争议/结算（若可用）、证据包与审计哈希链、Verified tiers + exposure budget。
- ❌ Out：承运商 API 适配细节、WMS 深度事件、客服系统全量 ETL（v0.1 只定义接口与证据哈希策略）。

### 关键设计原则

- **事实优先**：所有可从 Shopify 得到的数据都作为 `source=shopify` 事实落库，并保留 `raw_payload` 以便审计。
- **规则版本化**：所有 PCS 规则（退货窗口、SLA、Incoterms、reserve policy、ACE step-up triggers）都有 `schema_version/effective_from` 并可追溯。
- **事件不可变**：Webhook/拉取写入 `*_events` 表（append-only），以 hash chain 防篡改；实体 current state 由事件归约得到。
- **可降级**：Disputes/Payouts/Returns 不可用时，明确 fallback（外部系统 + metafields/metaobjects + Pivota 事件流）。

---

## 2) Shopify 可用性矩阵（Availability Matrix）

**基线 API 版本（v0.1）**：Admin API `2024-07`（可上调到你们当前统一版本；v0.1 以 capability 探测为准）。

> 说明：下表的 “GraphQL 字段/对象” 与 “Webhook topic” 以常见命名为准；安装时必须做 schema/capability 探测并记录在 `merchant_capabilities`，缺失即走 fallback。

| 模块 | 数据项（PCS v0.1） | Shopify GraphQL（Admin） | Webhook topic | API 版本 | Scopes | 店铺能力 | 不可用 fallback |
|---|---|---|---|---|---|---|---|
| OPS+ | Product/Variant 基础字段（SKU/价格/库存） | `Product`, `ProductVariant`, `InventoryItem`, `InventoryLevel` | `products/create`, `products/update`, `inventory_levels/update` | 2024-07+ | `read_products`, `read_inventory` | 无 | 定时全量拉取（poll） |
| OPS+ | Shop policies（refund/shipping/privacy/terms） | `Shop.refundPolicy/shippingPolicy/privacyPolicy/termsOfService` | 无（policy 变更无统一 webhook） | 2024-07+ | `read_content`（如需）/`read_shopify_payments_*` 不相关 | 无 | 定时拉取 + 下单时 hash snapshot |
| OPS+ | 关税/原产地/HS code | `InventoryItem.countryCodeOfOrigin/harmonizedSystemCode` | `inventory_items/update`（若启用） | 2024-07+ | `read_inventory` | 无 | Variant metafields `pcs.customs_*` |
| PCS | 发货时效承诺（ship_within_hours） |（通常无结构化字段） | 无 | n/a | n/a | 无 | Shop metafield `pcs.ship_within_hours` / Metaobject 规则 |
| ACE | 订单资金链（授权/捕获/退款/撤销） | `Order.transactions`（`OrderTransaction`） | `orders/paid`, `orders/updated`, `refunds/create` | 2024-07+ | `read_orders` | 无 | poll `orders` + `transactions` |
| ACE | “谁授权/授权范围/触发条件”审计 |（不稳定/缺失） | 无 | n/a | n/a | 无 | Pivota 事件流 + `order.metafields[pcs.authorization_audit_ref]` |
| 履约 | 发货/追踪号 | `Order.fulfillments.trackingInfo` | `fulfillments/create`, `fulfillments/update` | 2024-07+ | `read_fulfillments`, `read_orders` | 无 | poll fulfillments |
| 履约 | Tracking timeline（节点事件） |（多数店铺缺失） |（无统一） | n/a | n/a | 无 | AfterShip/承运商 API + `pcs_tracking_event` |
| 履约 | POD（签收证明） |（通常无） | 无 | n/a | n/a | 无 | 承运商 API 拉取 POD，存对象存储 + 哈希 |
| Returns/RMA | Refund 事实（金额、行项、回库策略） | `Order.refunds` | `refunds/create` | 2024-07+ | `read_orders` | 无 | poll refunds |
| Returns/RMA | Return/RMA 对象（申请、状态、退回物流） | `Return`/`order.returns`（若店铺支持） | `returns/create`, `returns/update`（若支持） | 2024-07+（视店铺） | `read_returns` | 启用 Returns | Pivota RMA 主表 + 退货 label/扫码外部系统 |
| Dispute | Shopify Payments disputes | `shopifyPaymentsDisputes` | `disputes/create`, `disputes/update`（若可用） | 2024-07+（视店铺） | `read_shopify_payments_disputes` | Shopify Payments | 外部 PSP/银行争议系统 + 手工上传证据 |
| Settlement | Payouts / Balance tx | `shopifyPaymentsPayouts`, `shopifyPaymentsBalanceTransactions` | `payouts/create`, `payouts/update`（若可用） | 2024-07+（视店铺） | `read_shopify_payments_payouts`, `read_shopify_payments_balance` | Shopify Payments | 用 `Order.transactions` 做弱对账 + Pivota Ledger |

**结论摘要**
- v0.1 “一定可做”：Products/Variants/Inventory、Orders/Transactions、Fulfillments、Refunds（基于 `read_products/read_inventory/read_orders/read_fulfillments`）。
- v0.1 “条件可做”：Returns/Disputes/Payouts（依赖店铺能力 + scopes）；必须 capability 探测并降级。
- v0.1 “Shopify 不提供/不稳定”：配送承诺时效、tracking timeline、POD、客服对话审计、授权操作者/触发条件解释；统一用 PCS 规则 + 外部集成补齐。

---

## 3) Schemas + Sample Payloads（五大对象 v0.1）

> JSON Schema 使用 draft 2020-12。文件可直接用于后端校验、版本管理与迁移。

- OPS@0.1：`pcs_v0_1/schemas/ops@0.1.schema.json`
- PCS@0.1：`pcs_v0_1/schemas/pcs@0.1.schema.json`
- ACE@0.1：`pcs_v0_1/schemas/ace@0.1.schema.json`
- MerchantMetrics@0.1：`pcs_v0_1/schemas/merchant_metrics@0.1.schema.json`
- Ledger@0.1：`pcs_v0_1/schemas/ledger@0.1.schema.json`

示例 payload：
- `pcs_v0_1/samples/ops@0.1.sample.json`
- `pcs_v0_1/samples/pcs@0.1.sample.json`
- `pcs_v0_1/samples/ace@0.1.sample.json`
- `pcs_v0_1/samples/merchant_metrics@0.1.sample.json`
- `pcs_v0_1/samples/ledger@0.1.sample.json`

---

## 4) Shopify GraphQL 查询 + Webhooks + 字段映射

### 4.1 GraphQL Queries（可运行示例，含分页）

Admin GraphQL queries（v0.1）：

- Products（增量/全量）：`pcs_v0_1/graphql/admin/products_list.graphql`
- Product by id（排障/回填）：`pcs_v0_1/graphql/admin/product_by_id.graphql`
- Shop policies：`pcs_v0_1/graphql/admin/shop_policies.graphql`
- Orders（按更新时间增量）：`pcs_v0_1/graphql/admin/orders_list.graphql`
- Order detail（交易/履约/退款聚合）：`pcs_v0_1/graphql/admin/order_detail.graphql`
- Disputes（如可用）：`pcs_v0_1/graphql/admin/shopify_payments_disputes_list.graphql`
- Payouts（如可用）：`pcs_v0_1/graphql/admin/shopify_payments_payouts_list.graphql`
- Balance tx（如可用）：`pcs_v0_1/graphql/admin/shopify_payments_balance_transactions_list.graphql`

### 4.2 Webhooks（按业务域分组）

v0.1 推荐启用 topics 与处理要求：`pcs_v0_1/docs/shopify_webhooks_v0_1.md`

### 4.2.1 字段映射表（可直接给开发做 ETL）

- `pcs_v0_1/docs/field_mapping_tables_v0_1.md`

### 4.3 事件摄取要求（v0.1 规范）

**idempotency_key（必需）**
- Webhook：优先用 `X-Shopify-Webhook-Id`（若存在）拼接 `shop_domain + topic`；否则用 `sha256(topic + shop_domain + payload_json_canonical)`。
- GraphQL poll：`sha256(shop_domain + object_gid + updated_at)`。

**乱序/重放（必需）**
- 所有事件写入 `*_events`（append-only），以 `occurred_at`（或 Shopify `updatedAt/processedAt`）为主排序归约。
- 允许重放：若 `idempotency_key` 已存在则丢弃；若同一对象出现“更旧事件”则仍写入 events，但归约逻辑需可重算。
- 每日/每小时回填：按 `updated_at` query 拉增量，修复 webhook 丢失。

**数据保留（v0.1 默认）**
- `raw webhook payload`：400 天（便于 90d 指标 + 争议周期排查）。
- `audit hash chain`：7 年（仅存 hash/元数据，payload 可分级存储）。
- `evidence packs`：争议关闭后 400 天（可配置）。

---

## 5) Metafields / Metaobjects 补齐（缺口字段可一键 bootstrap）

Bootstrap mutations（definitions + metaobjects）：
- `pcs_v0_1/graphql/bootstrap/pcs_metafields_metaobjects_bootstrap.graphql`
补齐字段清单（逐项对齐 PCS 路径 + 默认值）：
- `pcs_v0_1/docs/metafields_metaobjects_v0_1.md`

默认值（v0.1 建议，保守）：
- `return_window_days=30`
- `warranty_days=90`（无则不宣传）
- `ship_within_hours=48`
- `refund_sla_days=5`（收到退回后 5 个自然日内完成退款）
- `support_sla_hours=24`
- `reserve_policy`: `holdback_rate=0.05`, `holdback_days=14`（仅用于 exposure 风险预算，不代表资金托管）

---

## 6) Ledger 状态机 + Postgres 数据模型（DDL）

### 6.1 状态机（v0.1 规范）

Order states：
- `proposed → authorized → placed → shipped → delivered → settled`
- `canceled`：从 `proposed/authorized/placed` 进入（后续仅允许退款事件）

Payment states：
- `authorized → captured → refunded`（可插入 `voided`）

Return states：
- `rma_created → label_issued → in_transit → received → refunded`

Dispute states：
- `opened → evidence_submitted → won|lost → closed`

### 6.2 Postgres DDL

完整 DDL：`pcs_v0_1/sql/pcs_v0_1.sql`

---

## 7) Evidence Pack v0.1

Schema：`pcs_v0_1/schemas/evidence_pack@0.1.schema.json`  
Sample：`pcs_v0_1/samples/evidence_pack@0.1.sample.json`
字段清单与来源映射：
- `pcs_v0_1/docs/evidence_pack_v0_1.md`

**生成时机（v0.1）**
- `order_snapshot`：订单 `placed` 时冻结（policy hash、授权引用、地址摘要、产品快照引用）。
- `dispute_pack`：dispute `opened` 时生成/更新；`evidence_submitted` 时冻结。

**冻结规则**
- 冻结后只能追加 “补充材料” 作为新版本（`pack_version+1`），旧版本不可修改。

**签名/哈希链（v0.1 默认）**
- manifest json canonical → `sha256(manifest)`，写入 `evidence_packs.manifest_hash`
- assets 均记录 `sha256` 与对象存储 key
- 可选：`HMAC-SHA256`（KMS secret）对 manifest_hash 签名（v0.2 可升级 Ed25519）

---

## 8) Verified Tiers + Exposure Budget（v0.1）

### 8.1 Tier 定义（L0-L3）

| Tier | 门槛（必须全部满足） | 默认曝光倍数 |
|---|---|---|
| L0 | 完成安装 + 最小 scopes + webhooks（订单/履约/退款）上线 | 0.2× |
| L1 | OPS/PCS/ACE 规则已配置（非空）+ 28d 订单 ≥ 20 + evidence completeness ≥ 0.70 | 1.0× |
| L2 | 90d 订单 ≥ 100 + late shipment ≤ 5% + return ≤ 15% + chargeback ≤ 0.65% + mttr ≤ 48h + evidence ≥ 0.85 | 2.0× |
| L3 | 90d 订单 ≥ 300 + late shipment ≤ 2% + return ≤ 10% + chargeback ≤ 0.30% + mttr ≤ 24h + evidence ≥ 0.95 | 4.0× |

### 8.2 Promotion / Demotion（v0.1）

- Promotion：连续 14 天满足目标 tier 的核心指标（且样本量达标）→ 升级。
- Demotion：任一 hard-fail 触发立即降级 1 档（至少保持 7 天）：
  - 7d chargeback rate ≥ 2× 当前 tier 阈值
  - 7d late shipment rate ≥ 2× 当前 tier 阈值
  - evidence completeness 7d < 0.60

### 8.3 Exposure Budget 算法（v0.1）

定义：
- `base = max(5, avg_orders_per_day_28d)`（最低保障 5 单/天上限为 baseline）
- `tier_multiplier`：见上表
- `risk_penalty ∈ (0,1]`：由实时异常触发

伪代码（v0.1）：

```text
budget_today = base * tier_multiplier * risk_penalty

risk_penalty = 1.0
if chargeback_spike_7d: risk_penalty *= 0.3
if late_shipment_spike_7d: risk_penalty *= 0.5
if refund_spike_7d: risk_penalty *= 0.7

recovery:
  if 3 consecutive days no spikes:
    risk_penalty = min(1.0, risk_penalty + 0.2)
```

完整细则（含 spike 定义与路由执行）：
- `pcs_v0_1/docs/verified_tiers_and_exposure_budget_v0_1.md`

---

## 9) Merchant Onboarding Checklist（“如何拿到更多 LLM 曝光”）

Checklist 版本：v0.1（必须项不满足＝Tier 降级＝曝光减少）

详表：`pcs_v0_1/docs/merchant_onboarding_checklist_v0_1.md`

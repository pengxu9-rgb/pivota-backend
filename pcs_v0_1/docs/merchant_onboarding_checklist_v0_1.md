# Merchant Onboarding Checklist（PCS v0.1）

目标：把 “商家必须做什么才能拿到更多 LLM 曝光（更高 Verified Tier）” 写成可执行清单；不达标＝tier 降级＝曝光减少。

---

## 0) 安装与权限（必需）

- 安装 Pivota PCS Shopify App（或完成自建 OAuth 连接）。
- 授权 scopes（最低）：`read_products`, `read_inventory`, `read_orders`, `read_fulfillments`
- 推荐额外 scopes（可选但能提升 tier 上限）：`read_returns`, `read_shopify_payments_disputes`, `read_shopify_payments_payouts`, `read_shopify_payments_balance`
- 结果：Pivota 能拉取 Products/Orders/Transactions/Fulfillments/Refunds 的事实数据。

不达标后果：
- 无法进入 L1（仅 L0），曝光倍数 0.2×。

---

## 1) Webhooks（必需）

必须启用 topics（v0.1）：
- 订单：`orders/create`, `orders/updated`, `orders/paid`, `orders/cancelled`
- 履约：`fulfillments/create`, `fulfillments/update`
- 退款：`refunds/create`

可选启用（影响 L2/L3 上限）：
- Returns：`returns/create`, `returns/update`（仅当店铺支持 Returns）
- Disputes：`disputes/create`, `disputes/update`（仅 Shopify Payments）
- Payouts：`payouts/create`, `payouts/update`（仅 Shopify Payments）

验收标准：
- Pivota 能在 10 分钟内看到一次 “测试订单→发货→退款” 的事件全链路（事件可重放、无重复扣减）。

不达标后果：
- 无法进入 L1；或在 L1 期间 evidence completeness 持续偏低触发降权。

---

## 2) OPS（商品与政策，必需）

### 2.1 商品字段（必须可同步）

必须同步（Shopify facts）：
- Product：`title/status/vendor/productType/tags/updatedAt`
- Variant：`sku/price/compareAtPrice/availableForSale/requiresShipping`
- Inventory：`tracked + available by location`

验收标准：
- Pivota 能在 `pcs_v0_1/graphql/admin/products_list.graphql` 拉到至少 1 个可售 SKU，且库存为非负数。

### 2.2 政策披露（必须）

必须提供（Shopify policies 或外部 URL）：
- Refund policy
- Shipping policy
- Privacy policy
- Terms of service

验收标准：
- Pivota 能生成并保存 policies 的 `hash_sha256`（下单时 snapshot）。

不达标后果：
- Evidence pack 缺失 policy snapshot → L1 无法稳定保持，且 dispute 风险无法放量。

---

## 3) PCS 参数（必需：配置后才进入 L1）

> v0.1 推荐直接跑 `pcs_v0_1/graphql/bootstrap/pcs_metafields_metaobjects_bootstrap.graphql` 创建 definitions。

必须配置（默认值建议）：
- `pcs.ship_within_hours`（默认 48）
- `pcs.return_window_days`（默认 30）
- `pcs.refund_sla_days`（默认 5）
- `pcs.support_sla_hours`（默认 24）
- `pcs.incoterms_default`（默认 DDP）
- `pcs.duty_responsibility`（默认 merchant）

可选配置（提升转化/降低争议）：
- 产品 warranty：`pcs.warranty_terms` 或 `pcs.warranty_days`
- 变体禁运：`pcs.restricted_regions`（国家/地区列表）

不达标后果：
- PCS 规则缺失 → 无法验证履约与退货承诺 → tier 限制在 L0。

---

## 4) ACE 风控策略（必需：用于放量）

商家必须接受并配合（v0.1）：
- Pivota 在每笔订单写入：
  - `pcs.pivota_mandate_id`
  - `pcs.pivota_agent_id`
  - `pcs.authorization_audit_ref`
- Pivota 侧保留 append-only 授权审计事件流（不含支付凭证）。

商家可选择（影响可放量上限）：
- 最大单笔金额、单日订单上限
- step-up triggers（高金额/高退款率/地址不一致/库存不一致）对应的处置：人工复核/重新报价/拦截

不达标后果：
- 无法证明 “谁授权/为何授权” → dispute 证据不足 → 曝光降权或不能升到 L2/L3。

---

## 5) Ledger 与冲正规则（必需：非 MoR 约束下的对账真相）

商家必须接受：
- Pivota 仅记录交易引用/金额/事件哈希，不持有用户支付凭证，不代收代付。
- 所有订单状态以 Shopify facts + webhook 事件链为准；任何修正都以新事件追加（不可篡改）。

验收标准：
- Pivota 能对任一订单生成 `Ledger@0.1`（含 events + current state）。

不达标后果：
- 无法稳定计算指标（late shipment/returns/chargebacks）→ tier 无法提升。

---

## 6) 证据包（建议：提升 L2/L3）

商家可提升证据完整度的方法：
- 提供可追踪的承运商（tracking number + URL）
- 能提供 POD（签名/图片/投递证明）的，授权 Pivota 从承运商/AfterShip 拉取并存证
- 使用客服系统（Gorgias/Zendesk/等）并允许 Pivota 拉取对话摘要哈希（不必导出全部内容）

不达标后果：
- disputes 时 evidence 不足 → 风险惩罚触发（exposure budget 降低）。


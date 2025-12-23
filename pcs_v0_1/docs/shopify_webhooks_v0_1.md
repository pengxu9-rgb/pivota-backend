# Shopify Webhooks（PCS v0.1）

目标：用最少 topics 覆盖 “订单/履约/退款/（可选）退货/争议/结算” 的事实链；所有 webhook 统一走 append-only 事件表与幂等摄取。

---

## 1) 必须启用（v0.1）

### 订单

- `orders/create`
- `orders/updated`
- `orders/paid`（用于更清晰的付款边界；若店铺不发该 topic，仍可用 `orders/updated` + financialStatus 推断）
- `orders/cancelled`

### 履约

- `fulfillments/create`
- `fulfillments/update`

### 退款

- `refunds/create`

---

## 2) 条件启用（v0.1）

### Returns（店铺启用 Returns API 才启用）

- `returns/create`
- `returns/update`

若不可用：Pivota 以 `returns` 主表作为 RMA 真源，Shopify 仅存 `order.metafields[pcs.rma_ref]`（可选）。

### Shopify Payments Disputes（仅 Shopify Payments + scope）

- `disputes/create`
- `disputes/update`

若不可用：外部 PSP/银行争议系统导入（manual/external），证据包仍按 v0.1 生成。

### Shopify Payments Payouts / Balance（仅 Shopify Payments + scope）

- `payouts/create`
- `payouts/update`

若不可用：结算侧只做弱对账（订单交易链），并标注 `settlement_source=unavailable`。

---

## 3) 事件摄取要求（v0.1）

### 3.1 验签与信任边界

- 必须校验 Shopify webhook signature（HMAC）。
- 校验通过后仍按 “不可信输入” 处理：所有写入都必须幂等；对外部 URL/HTML 做长度与格式限制。

### 3.2 幂等键（idempotency_key）

优先级：
1) `"{shop_domain}:{topic}:{X-Shopify-Webhook-Id}"`
2) `"{shop_domain}:{topic}:{sha256(canonical_json(payload))}"`

**唯一约束**：`(merchant_id, idempotency_key)`。

### 3.3 乱序与重放

- 所有 webhook 入 `shopify_webhook_events`（不可变），不直接更新业务表。
- 归约器（reducer）对同一实体按 `occurred_at` 排序重放生成 current state。
- 任何时刻允许全量重算（事件表 + 原始 facts）。

### 3.4 回填（必需）

- 每日/每小时按 `updated_at` 做 GraphQL 增量拉取（Products/Orders/Disputes/Payouts），用于修复 webhook 丢失。

### 3.5 保留周期（默认）

- webhook 原始 payload：400 天
- 归约后的 current state：永久（或按业务需要）
- audit hash chain：7 年


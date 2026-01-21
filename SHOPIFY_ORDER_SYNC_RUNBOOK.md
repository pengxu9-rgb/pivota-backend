# Shopify Order Sync Runbook (Paid → Shopify)

## 目标

当 Pivota 侧订单已支付（`payment_status=paid`）但 Shopify 后台查不到订单时，提供可重复、可观测、可批量修复的手段，避免商户履约漏单。

本 Runbook 适用于：
- Checkout/Stripe 等支付成功后，`orders.shopify_order_id` 为空
- Shopify token 发生 401/403（连接重建后旧 token 仍被引用）
- 订单绑定的 `orders.store_id` 过期/缺失，需要自动“自愈”到最新有效 store row

---

## 关键机制（已加固）

- **幂等标签**：创建 Shopify order 时会写入 tag `pivota_order_id:<ORDER_ID>`（同时保留 `pivota,agent-order`）。
  - 目的：即使 DB 写入失败/重试，也可以通过 tag 在 Shopify 侧反查并“复用已存在订单”。
- **多 token 兜底**：优先使用订单绑定的 `store_id`，若遇到 401/403，会尝试同域名的其他 active store row（再尝试全量）。
- **自愈 store 绑定**：成功创建/复用 Shopify order 后，会 best-effort 更新 `orders.store_id` 为实际使用的 store row。
- **批量对账入口**：新增对账 API 扫描“已支付但缺少 shopify_order_id”的订单并补齐。

---

## 1) 快速诊断（debug）

接口：`GET /orders/{order_id}/debug`
- 鉴权：`X-ADMIN-KEY`（`ADMIN_API_KEY` 或 `PROMOTIONS_ADMIN_KEY`）或 admin JWT
- 返回：
  - `order_store_id`：订单行绑定的 store_id（可能为空/过期）
  - `bound_store`：按 `order_store_id` 查到的 store（若存在）
  - `primary_store`：当前 merchant 最新 active store
  - `api_key_fp`：token 指纹（不泄露 token）

示例：

```bash
export API_BASE="https://web-production-fedb.up.railway.app"
export ADMIN_API_KEY="***"
export ORDER_ID="ORD_XXXX"

curl -sS "${API_BASE}/orders/${ORDER_ID}/debug" \
  -H "X-ADMIN-KEY: ${ADMIN_API_KEY}" | jq
```

排查要点：
- `primary_store.has_api_key=false` → 商户 Shopify 未正确连接/缺 token
- `shopify_order_failed` 事件里大量 `401/403` → token 已失效（需重连）
- `bound_store` 为空但 `primary_store` 存在 → 订单绑定的 store_id 已过期，可通过对账/手动补单自愈

---

## 2) 单笔修复（手动触发 Shopify 创建/复用）

接口：`POST /orders/{order_id}/create-shopify`
- 鉴权同上
- 行为：
  - 若已存在 `shopify_order_id` → 直接返回 `already_exists`
  - 若未支付 → 返回 `not_paid`
  - 否则执行创建/复用逻辑（含多 token 兜底 + 自愈 store 绑定）

示例：

```bash
curl -sS -X POST "${API_BASE}/orders/${ORDER_ID}/create-shopify" \
  -H "X-ADMIN-KEY: ${ADMIN_API_KEY}" | jq
```

---

## 3) 批量对账（建议做成 Cron）

接口：`POST /orders/reconcile-missing-shopify`
- 鉴权同上
- 参数：
  - `merchant_id`（可选）：只处理某商户
  - `limit`：每次最多处理多少单（默认 50）
  - `min_age_seconds`：只处理“支付至少 N 秒之前”的订单（默认 120）
  - `dry_run=true`：只返回候选 order_id 列表，不执行

Dry run：

```bash
curl -sS -X POST "${API_BASE}/orders/reconcile-missing-shopify?merchant_id=merch_xxx&dry_run=true" \
  -H "X-ADMIN-KEY: ${ADMIN_API_KEY}" | jq
```

执行：

```bash
curl -sS -X POST "${API_BASE}/orders/reconcile-missing-shopify?merchant_id=merch_xxx&limit=50&min_age_seconds=120" \
  -H "X-ADMIN-KEY: ${ADMIN_API_KEY}" | jq
```

建议：
- 以 1–5 分钟频率跑（每次限量），配合告警指标：`paid && shopify_order_id IS NULL` 超过 N 分钟。
- 若发现大量 401/403，优先修复 Shopify 连接（否则对账会一直失败）。

---

## 4) Shopify 侧定位（运营/支持）

在 Shopify Admin 后台搜索订单 tag：
- `pivota_order_id:ORD_XXXX`

用途：
- 判断 Shopify 订单是否其实已创建，只是 Pivota 未写入 `shopify_order_id`（DB transient / 重试中断）。
- 若存在，则对账/手动触发会走“复用”路径并补齐 `shopify_order_id`。


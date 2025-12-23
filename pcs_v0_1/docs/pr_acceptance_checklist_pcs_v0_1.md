# PR 验收清单（PCS v0.1 × Shopify）

用于合并 `external_repos/pivota-backend` 与 `external_repos/PIVOTA-Agent` 的 PCS v0.1 相关 PR。

---

## A) 安全（必过）

- [ ] Shopify webhook：`APP_ENV=production` 时必须严格验签（raw body + `X-Shopify-Hmac-Sha256`），失败返回 `401`：`external_repos/pivota-backend/routes/webhook_routes.py:1`
- [ ] Shopify webhook：`APP_ENV=production` 时必须要求 `X-Shopify-Shop-Domain` 且与 merchant primary store domain 匹配，否则 `403`：`external_repos/pivota-backend/routes/webhook_routes.py:1`
- [ ] 事件落库失败：`APP_ENV=production` 必须返回 `500` 触发 Shopify retry（避免丢审计链）：`external_repos/pivota-backend/routes/webhook_routes.py:1`
- [ ] Refund webhook（如启用 `enable_platform_webhook_refund`）：生产必须验签：`external_repos/pivota-backend/routes/refund_webhook_routes.py:1`

---

## B) 幂等与重放（必过）

- [ ] 同一 webhook 重放 2 次：`pcs_shopify_webhook_events` 仅 1 条（unique `(merchant_id, idempotency_key)`）：`external_repos/pivota-backend/db/migrations/032_pcs_v0_1.sql:1`
- [ ] 并发 webhook 不产生 hash chain 分叉（merchant 级事务锁）：`external_repos/pivota-backend/services/shopify_webhook_ingest.py:1`

---

## C) Webhooks topics 覆盖（放量级，建议合并门槛）

- [ ] Orders：`orders/create`, `orders/updated`, `orders/paid`, `orders/cancelled`, `orders/fulfilled`
- [ ] Fulfillments：`fulfillments/create`, `fulfillments/update`
- [ ] Refunds：`refunds/create`（注意：与资金动账无关，只作“退款意图”事实）
- [ ] Money movement：`tender_transactions/create`
- [ ] Disputes：`disputes/create`, `disputes/update`
- [ ] GDPR：`customers/data_request`, `customers/redact`, `shop/redact`

注册入口：`external_repos/pivota-backend/routes/webhook_routes.py:1`

---

## D) Evidence pack（必过）

- [ ] `order_snapshot` 在支付确认节点 best-effort 生成并冻结：`external_repos/pivota-backend/services/pcs_evidence_pack_service.py:1`
- [ ] `manifest_sha256` 不自引用（hash 不包含 `manifest_sha256/manifest_signature` 字段）：`external_repos/pivota-backend/services/pcs_evidence_pack_service.py:1`

---

## E) quote-first（放量友好，建议合并门槛）

- [ ] Gateway 支持 `preview_quote`（转发 `/agent/v1/quotes/preview`）：`external_repos/PIVOTA-Agent/src/server.js:1`
- [ ] Gateway 支持透传 `order.quote_id` 与 `order.discount_codes` 到 `/agent/v1/orders/create`：`external_repos/PIVOTA-Agent/src/server.js:1`
- [ ] Tool schema 同步：`external_repos/PIVOTA-Agent/docs/tool-schema.json:1`

---

## F) 技术路线声明（必过：PR 描述需明确）

- [ ] Shopify 下单当前走 REST Admin Orders API（legacy track），如未来走 public app 需规划迁移 GraphQL Admin：`external_repos/pivota-backend/routes/order_routes.py:1`


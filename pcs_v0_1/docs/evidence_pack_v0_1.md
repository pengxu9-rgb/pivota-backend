# Evidence Pack v0.1 规格（字段清单 + 来源映射）

Schema：`pcs_v0_1/schemas/evidence_pack@0.1.schema.json`  
Sample：`pcs_v0_1/samples/evidence_pack@0.1.sample.json`

---

## 1) 生成时机与冻结规则

### 1.1 order_snapshot（下单冻结）

触发：
- `Ledger.order.order_state` 进入 `placed`（或收到 `orders/paid` 且订单非取消）

产物：
- EvidencePack（`pack_type=order_snapshot, status=frozen`）

冻结内容（必须）：
- `policy_snapshot`（所有 policies 的 url + hash + updated_at）
- `mandate_evidence`（pivota_mandate_id/pivota_agent_id/audit_ref）
- `order_ref`（总金额/币种/时间）

### 1.2 dispute_pack（争议冻结）

触发：
- Dispute `opened`：生成 `draft` 版本
- Evidence `submitted`：冻结为 `frozen`

规则：
- 冻结后不可修改；若需要补充材料，创建 `pack_version+1` 新版本（旧版本保持可审计）。

---

## 2) 字段来源映射（v0.1）

| EvidencePack 字段 | Source | Shopify / External / Pivota 来源 | 备注 |
|---|---|---|---|
| `order_ref.order_gid/order_name/placed_at/currency/order_total` | shopify | `Order.{id,name,processedAt,currencyCode,totalPriceSet}` | 由 `order_detail.graphql` 回填 |
| `mandate_evidence.*` | pivota | Pivota append-only `authorization_event`（外部） + Shopify order metafields | Shopify 仅存引用键，不存支付凭证 |
| `policy_snapshot.policies[]` | shopify | `Shop.{refundPolicy,shippingPolicy,privacyPolicy,termsOfService}` | 生成 `hash_sha256`（canonical html） |
| `policy_snapshot.policy_disclosure_hash` | pivota_derived | `sha256(order_id + policy_hashes + placed_at)` | 同时写入 `order.metafields[pcs.policy_disclosure_hash]`（可选） |
| `fulfillment_proof.tracking[]` | shopify | `Fulfillment.trackingInfo` | 追踪入口 |
| `fulfillment_proof.delivered_evidence` | shopify/external | `Fulfillment.deliveredAt`（若有）或承运商/AfterShip | Shopify 不保证提供 deliveredAt |
| `fulfillment_proof.pod_assets[]` | external | 承运商 POD API / AfterShip 附件 | 需对象存储 + sha256 |
| `support_timeline.timeline_sha256` | external | Helpdesk/Email/Chat 的摘要导出后 hash | v0.1 只存 hash + ticket ids |
| `assets[]` | pivota/external | 对象存储 pointers（S3/GCS） | 必须带 sha256 与 obtained_at |
| `manifest_sha256/signature` | pivota_derived | canonical manifest → sha256；签名（HMAC 或 Ed25519） | v0.1 默认 HMAC |

---

## 3) 规范化与安全（v0.1）

- **PII 最小化**：EvidencePack 不存完整地址/邮箱正文；只存必要摘要（hash）与外部工单引用。
- **对象存储**：所有附件（PDF/图片/HTML）以对象存储 key 引用；下载需短期签名 URL。
- **可审计**：manifest_sha256 + audit hash chain（DB）共同提供不可抵赖证据。


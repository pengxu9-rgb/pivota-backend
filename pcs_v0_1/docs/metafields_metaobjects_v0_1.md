# Metafields / Metaobjects 缺口补齐清单（PCS v0.1）

目标：把 Shopify 原生缺口字段变成 Shopify 侧可发现 schema（definitions）+ Pivota 侧可版本化规则（metaobjects/内部配置）。

Bootstrap mutations：
- `pcs_v0_1/graphql/bootstrap/pcs_metafields_metaobjects_bootstrap.graphql`

---

## 1) Metafields（namespace=pcs）

| 缺口字段 | OwnerType | Metafield key | Shopify type | PCS 路径 | 默认值（v0.1） | 说明 |
|---|---|---|---|---|---|---|
| ship_within_hours | SHOP | `pcs.ship_within_hours` | `number_integer` | `PCS.fulfillment.ship_within_hours` | 48 | Shopify 无结构化 SLA |
| return_window_days | SHOP | `pcs.return_window_days` | `number_integer` | `PCS.returns.return_window_days` | 30 | 从富文本政策中剥离为结构化规则 |
| refund_sla_days | SHOP | `pcs.refund_sla_days` | `number_integer` | `PCS.returns.refund_sla_days` | 5 | 收到退回后完成退款的 SLA |
| support_sla_hours | SHOP | `pcs.support_sla_hours` | `number_integer` | `PCS.support.support_sla_hours` | 24 | 客服首响 SLA |
| incoterms_default | SHOP | `pcs.incoterms_default` | `single_line_text_field` | `PCS.duties.incoterms_default` | DDP | Shopify 不表达责任归属 |
| duty_responsibility | SHOP | `pcs.duty_responsibility` | `single_line_text_field` | `PCS.duties.duty_responsibility` | merchant | `merchant/customer` |
| warranty_terms | PRODUCT | `pcs.warranty_terms` | `multi_line_text_field` | `OPS.products[].pcs_metafields.warranty_terms` | 空（不宣传） | 若有保修承诺必须结构化 |
| warranty_days | PRODUCT | `pcs.warranty_days` | `number_integer` | `PCS.returns.warranty_days` | 90 | 可被 warranty_terms 覆盖 |
| restricted_regions | PRODUCTVARIANT | `pcs.restricted_regions` | `list.single_line_text_field` | `PCS.returns.restricted_regions`/`OPS.products[].variants[].pcs_metafields.restricted_regions` | `[]` | 禁运/不支持退货区域 |
| policy_disclosure_hash | ORDER | `pcs.policy_disclosure_hash` | `single_line_text_field` | `EvidencePack.policy_snapshot.policy_disclosure_hash` | 生成 | 下单时冻结“披露版本” |
| pivota_mandate_id | ORDER | `pcs.pivota_mandate_id` | `single_line_text_field` | `EvidencePack.mandate_evidence.pivota_mandate_id` | 由 Pivota 写入 | ACE 关联键 |
| pivota_agent_id | ORDER | `pcs.pivota_agent_id` | `single_line_text_field` | `EvidencePack.mandate_evidence.pivota_agent_id` | 由 Pivota 写入 | ACE 关联键 |
| authorization_audit_ref | ORDER | `pcs.authorization_audit_ref` | `single_line_text_field` | `EvidencePack.mandate_evidence.authorization_audit_ref` | 由 Pivota 写入 | 指向 Pivota append-only 事件流 |
| ledger_ref | ORDER | `pcs.ledger_ref` | `single_line_text_field` | `Ledger.order.*`（引用） | 由 Pivota 写入 | 便于排障/溯源 |

---

## 2) Metaobjects（用于版本化规则集）

> v0.1 允许 “metafield 快速配置” 与 “metaobject 版本化配置” 并存；优先以 metaobject 作为规则主源，metafield 作为可读摘要。

| 规则集 | Metaobject type | 关键 fields | 对应 PCS 对象 | 默认（v0.1） |
|---|---|---|---|---|
| Return policy bundle | `pcs_return_policy_bundle` | `window_days/refund_sla_days/refund_trigger/effective_from` | `PCS.returns.*` | 30/5/on_receive |
| Delivery promise | `pcs_delivery_promise_policy` | `ship_within_hours/late_shipment_grace_hours/allowed_carriers_json` | `PCS.fulfillment.*` | 48/12/["UPS","USPS"] |
| Reserve policy | `pcs_reserve_policy` | `holdback_rate/holdback_days/triggers_json` | `PCS.reserve_policy.*` | 0.05/14 |
| ACE policy | `pcs_ace_policy` | `max_amount_json/max_orders_per_day/step_up_triggers_json` | `ACE@0.1` | 见 `ace@0.1.sample.json` |


# Verified Tiers + Exposure Budget（PCS v0.1）

目标：将商家在 OPS/PCS/ACE/Ledger/Evidence 的合规与历史表现转化为可执行的“放量/限流”规则，供 LLM/agent 路由在生产环境安全放量。

---

## 1) 输入（v0.1）

### 1.1 合规输入（hard requirements）

- OPS：`OPS@0.1` 可用（products + policies）且 policies 有 `hash_sha256`
- PCS：`PCS@0.1` 可用且关键字段非空
- ACE：`ACE@0.1` 可用且 `pcs.pivota_mandate_id / pcs.pivota_agent_id` 写入订单（或在 Pivota 侧可关联）
- Webhooks：最小 topics 已启用（订单/履约/退款）
- Evidence：`EvidencePack@0.1` 能生成 `order_snapshot`

### 1.2 指标输入（rolling windows）

来自 `MerchantMetrics@0.1`：
- 28d：用于即时风险判断与 L1 升级
- 90d：用于 L2/L3 稳定性判断

核心指标定义（v0.1）：
- `late_shipment_rate` = `late_shipments_count / orders_count`
  - `late_shipment`：`placed_at + ship_within_hours + grace_hours < first_fulfillment_created_at`
- `return_rate` = `returns_count / orders_count`（若 Returns 不可用则用 “退款+回库信号” 近似，需标注 `approx=true`）
- `refund_rate` = `refunds_count / orders_count`
- `chargeback_rate` = `chargebacks_count / orders_count`（仅 Shopify Payments 可直接观测）
- `evidence_completeness`：对“需要的证据字段集合”做覆盖率（0~1）

---

## 2) Tier 定义（L0-L3）

| Tier | 合规门槛 | 指标门槛（28d/90d） | 默认曝光倍数 |
|---|---|---|---|
| L0 | 安装完成 + 最小 scopes + 基础 webhooks | 不要求 | 0.2× |
| L1 | OPS/PCS/ACE 可用 + order_snapshot 可生成 | 28d orders ≥ 20；evidence ≥ 0.70 | 1.0× |
| L2 | L1 +（可用时）Shopify Payments disputes/payouts 可拉取 | 90d orders ≥ 100；late ≤ 5%；return ≤ 15%；chargeback ≤ 0.65%；mttr ≤ 48h；evidence ≥ 0.85 | 2.0× |
| L3 | L2 + 证据链闭环（POD 或等价） | 90d orders ≥ 300；late ≤ 2%；return ≤ 10%；chargeback ≤ 0.30%；mttr ≤ 24h；evidence ≥ 0.95 | 4.0× |

> 说明：若 disputes/balance 域不可用，chargeback 指标标记为 `unknown`，则最高只能到 L1（v0.1 保守策略）。

---

## 3) Promotion / Demotion（v0.1）

### 3.1 Promotion（升级）

- 每日计算目标 tier（L1/L2/L3），若连续 `N=14` 天满足该 tier 的合规与指标门槛且样本量达标 → 升级。

### 3.2 Demotion（降级）

即时 hard-fail（触发立即降级 1 档，冷却 `cooldown_days=7`）：
- `evidence_completeness_7d < 0.60`
- `chargeback_rate_7d >= 2 * tier_chargeback_threshold`（仅可观测时）
- `late_shipment_rate_7d >= 2 * tier_late_threshold`

---

## 4) Exposure Budget（v0.1 可运行算法）

### 4.1 基础预算（baseline）

- `base = max(5, round(avg_orders_per_day_28d))`
- `tier_multiplier`：L0=0.2, L1=1.0, L2=2.0, L3=4.0

### 4.2 Spike 定义（7d）

对比 28d 的 baseline（或 tier 阈值），定义 “异常放大”：
- `refund_spike_7d`：`refund_rate_7d >= max(0.10, 2 * refund_rate_28d)`
- `late_shipment_spike_7d`：`late_rate_7d >= max(0.08, 2 * late_rate_28d)`
- `chargeback_spike_7d`：`chargeback_rate_7d >= max(0.01, 2 * chargeback_rate_90d)`（若不可观测则跳过）

### 4.3 风险惩罚与恢复

```text
risk_penalty = 1.0
if chargeback_spike_7d: risk_penalty *= 0.3
if late_shipment_spike_7d: risk_penalty *= 0.5
if refund_spike_7d: risk_penalty *= 0.7

budget_today = base * tier_multiplier * risk_penalty
budget_today = clamp(budget_today, min=1, max=base * tier_multiplier)

recovery:
  if no spikes for 3 consecutive days:
    risk_penalty = min(1.0, risk_penalty + 0.2)
```

### 4.4 路由执行（LLM/agent）

- 每次路由前检查 `budget_remaining_today > 0`；若不足则降级到更低 tier 的商家集合或走手工/白名单。
- 每次成功下单（placed）扣减 1（或按 GMV 扣减，v0.2 再引入）。


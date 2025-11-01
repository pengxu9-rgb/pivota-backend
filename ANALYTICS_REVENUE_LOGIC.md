# Analytics Revenue & Order Classification Logic

## 📊 核心设计原则

### 1. Total Revenue = Confirmed Revenue Only
**口径**: 只计算 `payment_status = 'paid'` 的订单  
**原因**: 实际到账的收入，财务可审计

### 2. Pending Orders 基于时间分类
**原理**: 正常支付应在 5 分钟内完成，超过则可能存在问题

---

## 🎯 订单状态分类

### Paid（已支付）✅
- **条件**: `payment_status = 'paid'`
- **含义**: 支付成功，资金已确认
- **计入**: Total Revenue, Total Transactions
- **展示**: 绿色，主要指标

### Pending - Recent（处理中）🟡
- **条件**: `payment_status = 'pending'` AND `created_at >= NOW() - 5 minutes`
- **含义**: 正常的支付处理时间窗口
- **计入**: Pending Recent Revenue
- **展示**: 黄色，"处理中"
- **操作**: 无需干预，等待即可

### Pending - Stale（需要检查）🟠
- **条件**: `payment_status = 'pending'` AND `created_at BETWEEN (NOW() - 30 min, NOW() - 5 min)`
- **含义**: 处理时间异常长，可能卡住
- **计入**: Pending Stale Revenue
- **展示**: 橙色，"需要检查"
- **操作**: 
  - 检查 PSP 连接
  - 查看支付网关日志
  - 可能需要手动处理

### Pending - Abandoned（已放弃）⚪
- **条件**: `payment_status = 'pending'` AND `created_at < NOW() - 30 minutes`
- **含义**: 大概率失败或用户放弃
- **计入**: Pending Abandoned Revenue
- **展示**: 灰色，"已失败/放弃"
- **操作**: 
  - 可以标记为 failed
  - 从活跃待处理中移除
  - 统计为转化漏斗流失

### Failed（明确失败）❌
- **条件**: `payment_status = 'failed'`
- **含义**: 支付明确失败
- **计入**: Failed Revenue
- **展示**: 红色，"支付失败"
- **操作**: 
  - 分析失败原因
  - 通知用户重试
  - 优化支付流程

---

## 📈 Analytics Dashboard 响应结构

### 主要指标（Top 4 Cards）
```json
{
  "total_transactions": 1,        // 只计 paid orders
  "total_revenue": 24.99,         // 只计 paid orders
  "success_rate": 100.0,          // paid / total_orders * 100
  "avg_transaction_value": 24.99  // AVG(paid orders)
}
```

### 详细分解（Revenue Breakdown）
```json
{
  "revenue_breakdown": {
    "confirmed": 24.99,           // payment_status = 'paid'
    "pending_recent": 15.50,      // pending < 5 mins
    "pending_stale": 8.00,        // pending 5-30 mins
    "pending_abandoned": 12.00,   // pending > 30 mins
    "failed": 5.99                // payment_status = 'failed'
  }
}
```

### 订单数量分解（Order Breakdown）
```json
{
  "order_breakdown": {
    "total": 5,                   // 所有订单
    "paid": 1,                    // 已支付
    "pending_recent": 1,          // < 5 mins
    "pending_stale": 1,           // 5-30 mins
    "pending_abandoned": 1,       // > 30 mins
    "failed": 1                   // 明确失败
  }
}
```

---

## 🎨 前端展示建议

### 主要指标卡片（简洁版）
```typescript
<Card>
  <h3>Total Revenue</h3>
  <p className="text-3xl font-bold">$24.99</p>
  <p className="text-sm text-gray-500">Confirmed payments only</p>
</Card>
```

### 详细分解（可展开/悬停）
```typescript
<Card>
  <h3>Revenue Breakdown</h3>
  
  {/* Confirmed */}
  <div className="flex justify-between">
    <span className="text-green-600">✅ Confirmed</span>
    <span className="font-bold">$24.99</span>
  </div>
  
  {/* Pending Recent (if > 0) */}
  {data.pending_recent_count > 0 && (
    <div className="flex justify-between">
      <span className="text-yellow-600">🟡 Processing ({data.pending_recent_count})</span>
      <span>$15.50</span>
    </div>
  )}
  
  {/* Pending Stale (if > 0, show warning) */}
  {data.pending_stale_count > 0 && (
    <div className="flex justify-between items-center">
      <span className="text-orange-600">⚠️ Needs Attention ({data.pending_stale_count})</span>
      <span>$8.00</span>
      <button onClick={handleInvestigate}>Investigate</button>
    </div>
  )}
  
  {/* Pending Abandoned (show as grayed out) */}
  {data.pending_abandoned_count > 0 && (
    <div className="flex justify-between opacity-50">
      <span className="text-gray-500">⚪ Abandoned ({data.pending_abandoned_count})</span>
      <span>$12.00</span>
    </div>
  )}
  
  {/* Failed */}
  {data.failed_orders > 0 && (
    <div className="flex justify-between">
      <span className="text-red-600">❌ Failed ({data.failed_orders})</span>
      <span>$5.99</span>
    </div>
  )}
</Card>
```

### 告警规则
```typescript
// 如果有 Stale Pending，显示橙色徽章
{data.pending_stale_count > 0 && (
  <Badge variant="warning">
    {data.pending_stale_count} orders need attention
  </Badge>
)}

// 如果 Failed 订单超过 10%，显示红色警告
{(data.failed_orders / data.total_orders) > 0.1 && (
  <Alert variant="error">
    High failure rate: {((data.failed_orders / data.total_orders) * 100).toFixed(1)}%
  </Alert>
)}
```

---

## 🔧 SQL 实现

### 完整查询
```sql
SELECT 
    COUNT(*) as total_orders,
    
    -- Confirmed Revenue (only paid orders)
    COUNT(CASE WHEN payment_status = 'paid' THEN 1 END) as paid_orders,
    COALESCE(SUM(CASE WHEN payment_status = 'paid' THEN total ELSE 0 END), 0) as confirmed_revenue,
    
    -- Pending Revenue (time-based classification)
    COUNT(CASE WHEN payment_status = 'pending' 
        AND created_at >= NOW() - INTERVAL '5 minutes' THEN 1 END) as pending_recent_count,
    COALESCE(SUM(CASE WHEN payment_status = 'pending' 
        AND created_at >= NOW() - INTERVAL '5 minutes' THEN total ELSE 0 END), 0) as pending_recent_revenue,
    
    COUNT(CASE WHEN payment_status = 'pending' 
        AND created_at < NOW() - INTERVAL '5 minutes'
        AND created_at >= NOW() - INTERVAL '30 minutes' THEN 1 END) as pending_stale_count,
    COALESCE(SUM(CASE WHEN payment_status = 'pending' 
        AND created_at < NOW() - INTERVAL '5 minutes'
        AND created_at >= NOW() - INTERVAL '30 minutes' THEN total ELSE 0 END), 0) as pending_stale_revenue,
    
    COUNT(CASE WHEN payment_status = 'pending' 
        AND created_at < NOW() - INTERVAL '30 minutes' THEN 1 END) as pending_abandoned_count,
    COALESCE(SUM(CASE WHEN payment_status = 'pending' 
        AND created_at < NOW() - INTERVAL '30 minutes' THEN total ELSE 0 END), 0) as pending_abandoned_revenue,
    
    -- Failed Revenue
    COUNT(CASE WHEN payment_status = 'failed' THEN 1 END) as failed_orders,
    COALESCE(SUM(CASE WHEN payment_status = 'failed' THEN total ELSE 0 END), 0) as failed_revenue,
    
    -- Average Transaction (only paid)
    AVG(CASE WHEN payment_status = 'paid' THEN total ELSE NULL END) as avg_transaction_value
FROM orders
WHERE created_at >= :start_date
```

---

## 📊 使用场景

### 1. 日常监控
- **关注**: Confirmed Revenue, Success Rate
- **如果**: Pending Recent > 0 → 正常，等待即可

### 2. 异常检测
- **如果**: Pending Stale > 0 → 🚨 立即检查 PSP 连接
- **如果**: Pending Abandoned > 5 → 🚨 支付流程可能有问题

### 3. 财务对账
- **使用**: Confirmed Revenue
- **忽略**: 所有 Pending（尚未确认到账）

### 4. 转化率分析
- **Success Rate** = Paid / Total Orders
- **Abandonment Rate** = Abandoned / Total Orders
- **Failure Rate** = Failed / Total Orders

---

## ⚙️ 可配置参数（未来扩展）

```python
# 可在环境变量或配置文件中调整
PENDING_RECENT_THRESHOLD_MINUTES = 5    # 当前: 5 分钟
PENDING_STALE_THRESHOLD_MINUTES = 30    # 当前: 30 分钟

# 不同 PSP 可能需要不同的阈值
PSP_THRESHOLDS = {
    "stripe": {"recent": 2, "stale": 10},      # Stripe 很快
    "paypal": {"recent": 10, "stale": 60},     # PayPal 较慢
    "checkout": {"recent": 5, "stale": 30}     # 默认
}
```

---

## 🎯 总结

| 状态 | 时间窗口 | 含义 | 操作 | 计入收入 |
|------|----------|------|------|----------|
| **Paid** | N/A | 支付成功 | ✅ 无 | ✅ 是 |
| **Pending Recent** | < 5 min | 处理中 | ⏳ 等待 | ❌ 否 |
| **Pending Stale** | 5-30 min | 需要检查 | ⚠️ 调查 | ❌ 否 |
| **Pending Abandoned** | > 30 min | 已放弃 | 🗑️ 标记失败 | ❌ 否 |
| **Failed** | N/A | 明确失败 | 📊 分析原因 | ❌ 否 |

**核心理念**: 
- **Conservative Revenue Recognition**: 只计算确认收入
- **Proactive Monitoring**: 基于时间的自动分类
- **Clear Action Items**: 不同状态有明确的处理方式


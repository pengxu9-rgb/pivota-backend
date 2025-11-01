# 🚨 Demo Data Fallback 问题修复

## 问题描述
Merchant 相关数据显示错误，怀疑：
1. 数据被除以 1000
2. Fallback 到 demo data

## 🔍 调查发现

### 1. 除以 1000 问题 ❌
检查后发现前端的 `/1000` 都是格式化显示用的（例如 $1.2k），这是正常的。

### 2. Demo Data Fallback 问题 ✅
**这是真正的问题！** 在 `merchant_dashboard_routes.py` 中发现多处 fallback 逻辑：

```python
# 第 166-171 行
except Exception as e:
    print(f"Error fetching merchant profile: {e}")
    # Fallback to demo data  <-- 问题在这里！
    merchant_data = DEMO_MERCHANT_DATA.get(merchant_id)
    if merchant_data:
        return {"status": "success", "data": merchant_data["profile"]}
```

### Demo Data 的特征值
```python
DEMO_MERCHANT_DATA = {
    "business_name": "ChydanTest Store",
    "total_transactions": 1250,
    "total_volume": 125000,
    # ... 其他假数据
}
```

## 修复内容

### 1. 创建新的路由文件
`merchant_dashboard_routes_fixed.py` - 删除所有 demo data fallback

### 2. 主要更改
- ❌ 删除 `DEMO_MERCHANT_DATA` 常量
- ❌ 删除所有 fallback 逻辑
- ✅ 数据库查询失败时返回真实错误
- ✅ 所有数据都从数据库获取

### 3. 影响的端点
- `/merchant/profile` - 现在只返回真实数据
- `/merchant/dashboard/stats` - 不再 fallback 到 demo
- `/merchant/{id}/orders` - 真实订单数据
- `/merchant/{id}/integrations` - 真实集成数据
- `/merchant/{id}/psps` - 真实 PSP 数据

## 测试方法

### 1. 等待部署（2-3分钟）
Railway 会自动部署新代码

### 2. 运行测试脚本
```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344

# 使用 merchant token 测试
./test_merchant_real_data.sh YOUR_MERCHANT_TOKEN
```

### 3. 验证标志
**Demo Data 标志**（如果看到这些值说明还在用 demo）：
- business_name: "ChydanTest Store"
- total_orders: 1250
- total_volume: 125000

**真实数据标志**：
- 实际的订单数和金额
- 可能是 0 或其他真实值
- 数据库错误时会返回 500 错误而不是 demo data

## 相关问题

### Agent 和 Merchant 数据不一致
这次修复揭示了几个系统性问题：

1. **多个数据源**
   - orders 表（真实订单）
   - agent_usage_logs 表（API 日志）
   - Demo data（硬编码假数据）

2. **计算口径不同**
   - Merchant: 从 orders 表计算
   - Agent: 之前从 usage_logs 计算（已修复）

3. **Fallback 逻辑**
   - 多处 fallback 到 demo data
   - 掩盖了真实的数据问题

## 长期建议

### 1. 删除所有 Demo Data
- 开发环境使用种子数据
- 生产环境必须用真实数据
- 错误时返回错误，不要 fallback

### 2. 统一数据源
- 所有业务统计从 orders 表计算
- API 日志只用于技术监控
- 避免多个"真相源"

### 3. 添加数据验证
```sql
-- 定期检查数据一致性
SELECT 'Orders' as source, COUNT(*), SUM(total) FROM orders
UNION ALL
SELECT 'Agent Stats', total_orders, total_gmv FROM agents;
```

## 状态
- ✅ **后端修复已部署**
- ⏳ **等待 Railway 自动部署**
- 📊 **准备验证真实数据**

---

**重要**: 这个修复确保了系统显示真实数据，不再有假数据混入。如果看到数据为 0 或报错，那是真实情况，需要检查数据库而不是被 demo data 掩盖。

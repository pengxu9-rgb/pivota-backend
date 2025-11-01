# 重构安全检查清单 - 避免挖坑指南

## 🎯 核心原则

1. **数据流不能断** - 任何改动都不能破坏现有的数据关系
2. **向后兼容** - 新版本必须兼容旧数据
3. **测试先行** - 先写测试，再改代码
4. **分步实施** - 大改动拆成小步骤

## 📋 重构前必查项

### 1. 数据库表关系检查

```sql
-- 检查关键外键关系
SELECT 
    tc.table_name, 
    kcu.column_name, 
    ccu.table_name AS foreign_table_name,
    ccu.column_name AS foreign_column_name 
FROM information_schema.table_constraints AS tc 
JOIN information_schema.key_column_usage AS kcu
    ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage AS ccu
    ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY';
```

### 2. API依赖关系图

```
Agent Portal 依赖的API:
├── /agent/v1/metrics/summary          # 仪表板数据
├── /agent/v1/metrics/recent           # 最近活动
├── /agents/{id}/merchants             # 商家列表
├── /agents/{id}/query-analytics       # MCP分析
└── /agents/{id}/funnel               # 订单漏斗

Merchant Portal 依赖的API:
├── /products/v2/{merchant_id}         # 产品列表
├── /products/sync-universal/          # 产品同步
├── /merchant/dashboard/stats          # 仪表板统计
└── /merchant/stores/{merchant_id}     # 商店信息

Employee Portal 依赖的API:
├── /analytics/dashboard               # 平台概览
├── /agent/metrics/summary             # Agent统计
├── /agent/metrics/agents              # Agent列表
└── /agent/metrics/timeline            # 时间线数据
```

### 3. 数据字段映射关系

```yaml
orders表:
  agent_id: 记录创建订单的Agent
  merchant_id: 订单所属商家
  total: 订单金额（用于GMV计算）
  payment_status: 支付状态（paid用于收入统计）
  created_at: 创建时间（用于时间范围查询）

agent_usage_logs表:
  agent_id: API调用者
  endpoint: 调用的端点
  status_code: HTTP状态码
  response_time_ms: 响应时间
  timestamp: 调用时间

merchant_stores表:
  merchant_id: 商家ID
  platform: 平台类型(shopify/wix/woocommerce等)
  domain: 商店域名
  api_key: API凭据（可能是JSON）
  is_active: 是否激活
```

## 🚫 常见的坑和解决方案

### 1. 字段重命名的坑

**错误示例**:
```sql
-- 直接重命名
ALTER TABLE orders RENAME COLUMN total TO amount;
```

**正确做法**:
```sql
-- 1. 先添加新字段
ALTER TABLE orders ADD COLUMN amount DECIMAL(12,2);

-- 2. 复制数据
UPDATE orders SET amount = total;

-- 3. 更新代码使用新字段

-- 4. 确认无误后再删除旧字段
ALTER TABLE orders DROP COLUMN total;
```

### 2. API响应格式改变的坑

**错误示例**:
```python
# 直接改变响应格式
return {"data": orders}  # 原来是 {"orders": orders}
```

**正确做法**:
```python
# 支持两种格式
if api_version == "v2":
    return {"data": orders}
else:
    return {"orders": orders}
```

### 3. 数据类型不一致的坑

**问题**: SQLite vs PostgreSQL
```python
# SQLite
DATE(created_at, '+1 day')

# PostgreSQL  
created_at + INTERVAL '1 day'
```

**解决**: 使用ORM或统一的数据库抽象层

## 📊 重构影响评估模板

```markdown
## 重构: [功能名称]

### 影响范围
- [ ] Agent Portal: [具体功能]
- [ ] Merchant Portal: [具体功能]  
- [ ] Employee Portal: [具体功能]

### 数据库改动
- [ ] 表结构变更: [详细说明]
- [ ] 数据迁移脚本: [文件路径]
- [ ] 回滚脚本: [文件路径]

### API改动
- [ ] 新增端点: [列表]
- [ ] 修改端点: [列表]
- [ ] 废弃端点: [列表]

### 测试计划
- [ ] 单元测试: [覆盖率目标]
- [ ] 集成测试: [测试场景]
- [ ] 手动测试: [测试步骤]
```

## 🔧 重构工具箱

### 1. 数据库版本控制
```python
# 使用Alembic管理数据库迁移
alembic init migrations
alembic revision -m "add_agent_id_to_orders"
alembic upgrade head
alembic downgrade -1
```

### 2. API版本控制
```python
from fastapi import APIRouter

# 版本化的路由
v1_router = APIRouter(prefix="/api/v1")
v2_router = APIRouter(prefix="/api/v2")

# 共享逻辑
async def get_orders_logic(merchant_id: str):
    # 核心逻辑
    pass

# V1保持兼容
@v1_router.get("/orders")
async def get_orders_v1(merchant_id: str):
    orders = await get_orders_logic(merchant_id)
    return {"orders": orders}  # 旧格式

# V2使用新格式
@v2_router.get("/orders") 
async def get_orders_v2(merchant_id: str):
    orders = await get_orders_logic(merchant_id)
    return {"data": orders, "count": len(orders)}  # 新格式
```

### 3. 特性开关
```python
# 使用特性开关控制新功能
FEATURE_FLAGS = {
    "use_new_sync_logic": False,
    "enable_metrics_cache": False,
}

if FEATURE_FLAGS["use_new_sync_logic"]:
    # 新逻辑
    result = await universal_sync(merchant_id)
else:
    # 旧逻辑
    result = await legacy_sync(merchant_id)
```

## 📈 监控重构影响

### 1. 关键指标监控
```python
# 添加监控点
async def sync_products(merchant_id: str):
    start_time = time.time()
    try:
        result = await do_sync(merchant_id)
        # 记录成功
        await log_metric("product_sync_success", 1, {
            "merchant_id": merchant_id,
            "duration": time.time() - start_time
        })
        return result
    except Exception as e:
        # 记录失败
        await log_metric("product_sync_error", 1, {
            "merchant_id": merchant_id,
            "error": str(e)
        })
        raise
```

### 2. A/B测试
```python
# 对比新旧实现
if hash(merchant_id) % 100 < 10:  # 10%流量
    result = await new_implementation()
    await log_metric("ab_test_new", 1)
else:
    result = await old_implementation()
    await log_metric("ab_test_old", 1)
```

## ✅ 重构完成标准

1. **所有测试通过** - 单元测试、集成测试、E2E测试
2. **性能不降低** - 响应时间、吞吐量保持或提升
3. **错误率不增加** - 监控显示错误率稳定
4. **数据完整性** - 所有数据正确迁移，无丢失
5. **可以回滚** - 保留回滚方案和脚本

## 🎯 下次重构建议

基于当前系统状态，建议按以下顺序重构：

1. **统一Metrics服务** - 创建MetricsService类
2. **规范化API响应** - 统一成功/错误格式
3. **数据库索引优化** - 基于实际查询模式
4. **缓存层实现** - Redis缓存热点数据
5. **日志标准化** - 结构化日志，便于分析

记住：**小步快跑，持续改进** > 大刀阔斧，一步到位





# PSP Fields Specification & Complete Fix

## 问题背景

`psp_used` 和 `psp_id` 两个字段在系统中存在多个问题：
1. **命名混乱**：`psp_used` vs `psp_type` vs `provider` 多种叫法
2. **大小写不一致**：`Stripe` vs `stripe` vs `STRIPE`
3. **填充不完整**：订单创建时可能只填一个字段
4. **查询不统一**：有的查 `psp_used`，有的查 `psp_id`，有的两个都查
5. **NULL 值处理**：JOIN 条件对 NULL 处理不当

## 统一规范

### 1. 字段定义

| 字段 | 类型 | 作用 | 示例值 | 必填 |
|------|------|------|--------|------|
| `psp_used` | VARCHAR | PSP 提供商名称（小写） | `stripe`, `adyen`, `checkout` | ✅ 是 |
| `psp_id` | VARCHAR | PSP 配置的唯一标识符 | `psp_stripe_031421904229` | ✅ 是 |

### 2. 命名规范

**统一使用小写**：
- ✅ `stripe`
- ✅ `adyen`
- ✅ `checkout`
- ✅ `paypal`
- ✅ `braintree`
- ❌ `Stripe`, `STRIPE`, `StRiPe`

### 3. PSP ID 格式

格式：`psp_{provider}_{random_12_chars}`

示例：
```
psp_stripe_031421904229
psp_adyen_8f3a2c1d4e5b
psp_checkout_7b9c6d2a3f1e
```

### 4. 字段优先级

**订单记录时**：
1. 优先使用 `psp_id`（精确匹配到具体配置）
2. `psp_used` 作为辅助字段（便于快速筛选）
3. 两个字段必须同时存在且一致

**查询匹配时**：
```sql
-- 正确的 JOIN 条件
LEFT JOIN orders o ON o.merchant_id = mp.merchant_id 
    AND o.created_at >= :start_time
    AND (
        -- 优先匹配 psp_id（精确）
        (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
        OR 
        -- 备用匹配 psp_used（模糊，不区分大小写）
        (o.psp_used IS NOT NULL AND mp.provider IS NOT NULL 
         AND LOWER(o.psp_used) = LOWER(mp.provider))
    )
```

## 修复方案

### Phase 1: 数据库层面修复

#### 1.1 添加约束
```sql
-- orders 表
ALTER TABLE orders 
    ALTER COLUMN psp_used SET NOT NULL,
    ALTER COLUMN psp_id SET NOT NULL;

-- 添加检查约束
ALTER TABLE orders 
    ADD CONSTRAINT check_psp_used_lowercase 
    CHECK (psp_used = LOWER(psp_used));

-- 添加索引
CREATE INDEX IF NOT EXISTS idx_orders_psp_used ON orders(psp_used);
CREATE INDEX IF NOT EXISTS idx_orders_psp_id ON orders(psp_id);
CREATE INDEX IF NOT EXISTS idx_orders_merchant_psp ON orders(merchant_id, psp_id);
```

#### 1.2 修复历史数据
```sql
-- 自动修复脚本
UPDATE orders o
SET 
    psp_id = mp.psp_id,
    psp_used = LOWER(mp.provider)
FROM merchant_psps mp
WHERE o.merchant_id = mp.merchant_id
    AND mp.status = 'active'
    AND (o.psp_id IS NULL OR o.psp_used IS NULL);
```

### Phase 2: 代码层面修复

#### 2.1 订单创建逻辑（order_routes.py）

**当前问题**：
- PSP 确定逻辑分散
- 可能只设置一个字段
- 大小写不统一

**修复方案**：
```python
# 统一的 PSP 确定函数
async def determine_psp_for_order(
    merchant_id: str,
    psp_type: Optional[str] = None
) -> tuple[str, str]:
    """
    确定订单使用的 PSP
    
    Returns:
        (psp_used, psp_id) - 两个字段都是小写且非空
    """
    try:
        if psp_type:
            # 指定了 PSP，查询对应配置
            psp_row = await database.fetch_one(
                """
                SELECT psp_id, provider 
                FROM merchant_psps 
                WHERE merchant_id = :merchant_id 
                    AND LOWER(provider) = LOWER(:psp_type)
                    AND status = 'active'
                LIMIT 1
                """,
                {"merchant_id": merchant_id, "psp_type": psp_type}
            )
        else:
            # 未指定，使用第一个活跃 PSP
            psp_row = await database.fetch_one(
                """
                SELECT psp_id, provider 
                FROM merchant_psps 
                WHERE merchant_id = :merchant_id 
                    AND status = 'active'
                ORDER BY connected_at DESC
                LIMIT 1
                """,
                {"merchant_id": merchant_id}
            )
        
        if not psp_row:
            raise ValueError(f"No active PSP found for merchant {merchant_id}")
        
        # 统一转小写
        psp_used = psp_row["provider"].lower()
        psp_id = psp_row["psp_id"]
        
        logger.info(f"✅ PSP determined: {psp_used} (ID: {psp_id})")
        return psp_used, psp_id
        
    except Exception as e:
        logger.error(f"❌ Failed to determine PSP: {e}")
        raise ValueError(f"PSP determination failed: {str(e)}")
```

#### 2.2 查询逻辑统一

**所有涉及 PSP 匹配的查询都使用标准模板**：

```python
STANDARD_PSP_JOIN = """
LEFT JOIN orders o ON o.merchant_id = mp.merchant_id 
    AND o.created_at >= :start_time
    AND (
        (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
        OR 
        (o.psp_used IS NOT NULL AND mp.provider IS NOT NULL 
         AND LOWER(o.psp_used) = LOWER(mp.provider))
    )
"""

STANDARD_PSP_FILTER = """
WHERE (
    (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
    OR 
    (o.psp_used IS NOT NULL AND mp.provider IS NOT NULL 
     AND LOWER(o.psp_used) = LOWER(mp.provider))
)
"""
```

### Phase 3: 验证与监控

#### 3.1 数据完整性检查端点
```python
@router.get("/admin/psp/data-integrity-check")
async def check_psp_data_integrity():
    """检查 PSP 字段完整性"""
    
    # 检查 NULL 值
    null_check = await database.fetch_one("""
        SELECT 
            COUNT(*) as total_orders,
            COUNT(CASE WHEN psp_used IS NULL THEN 1 END) as null_psp_used,
            COUNT(CASE WHEN psp_id IS NULL THEN 1 END) as null_psp_id,
            COUNT(CASE WHEN psp_used IS NULL OR psp_id IS NULL THEN 1 END) as incomplete
        FROM orders
    """)
    
    # 检查大小写不一致
    case_check = await database.fetch_all("""
        SELECT psp_used, COUNT(*) as count
        FROM orders
        WHERE psp_used != LOWER(psp_used)
        GROUP BY psp_used
    """)
    
    # 检查不匹配
    mismatch_check = await database.fetch_all("""
        SELECT 
            o.order_id,
            o.psp_used,
            o.psp_id,
            mp.provider,
            mp.psp_id as config_psp_id
        FROM orders o
        LEFT JOIN merchant_psps mp ON o.psp_id = mp.psp_id
        WHERE mp.psp_id IS NULL
        LIMIT 20
    """)
    
    return {
        "null_values": null_check,
        "case_inconsistencies": case_check,
        "psp_id_mismatches": mismatch_check,
        "is_healthy": (
            null_check["incomplete"] == 0 
            and len(case_check) == 0 
            and len(mismatch_check) == 0
        )
    }
```

#### 3.2 自动修复 Cron Job
```python
@router.post("/admin/psp/auto-heal")
async def auto_heal_psp_data():
    """自动修复 PSP 字段问题"""
    
    # 1. 修复大小写
    case_fixed = await database.execute("""
        UPDATE orders
        SET psp_used = LOWER(psp_used)
        WHERE psp_used != LOWER(psp_used)
    """)
    
    # 2. 补全缺失的 psp_id
    id_fixed = await database.execute("""
        UPDATE orders o
        SET psp_id = mp.psp_id
        FROM merchant_psps mp
        WHERE o.merchant_id = mp.merchant_id
            AND o.psp_id IS NULL
            AND o.psp_used IS NOT NULL
            AND LOWER(o.psp_used) = LOWER(mp.provider)
            AND mp.status = 'active'
    """)
    
    # 3. 补全缺失的 psp_used
    used_fixed = await database.execute("""
        UPDATE orders o
        SET psp_used = LOWER(mp.provider)
        FROM merchant_psps mp
        WHERE o.psp_id = mp.psp_id
            AND o.psp_used IS NULL
    """)
    
    return {
        "status": "success",
        "case_fixed": case_fixed,
        "psp_id_fixed": id_fixed,
        "psp_used_fixed": used_fixed
    }
```

## 需要修改的文件清单

### 高优先级（核心业务逻辑）
1. ✅ `pivota_infra/routes/order_routes.py` - 订单创建
2. ✅ `pivota_infra/routes/psp_overview_routes.py` - PSP 统计
3. ✅ `pivota_infra/routes/employee_dashboard_routes.py` - Employee Dashboard
4. ⚠️ `pivota_infra/routes/merchant_dashboard_routes.py` - Merchant Dashboard
5. ⚠️ `pivota_infra/routes/payment_routes.py` - 支付处理
6. ⚠️ `pivota_infra/orchestrator/payment_orchestrator.py` - 支付编排

### 中优先级（测试和管理端点）
7. `pivota_infra/routes/admin_create_test_orders.py`
8. `pivota_infra/routes/public_test_orders.py`
9. `pivota_infra/routes/simple_test_orders.py`
10. `pivota_infra/scripts/generate_test_orders.py`

### 低优先级（已废弃或备份文件）
- `pivota_infra/routes_backup/*`
- `routes/*` (旧版本)
- `*.md` 文档

## 实施步骤

### Step 1: 立即执行（诊断 + 快速修复）
1. ✅ 部署诊断端点查看当前状态
2. ⬜ 执行数据修复脚本补全缺失字段
3. ⬜ 修复订单创建逻辑

### Step 2: 系统性重构（1-2天）
1. ⬜ 统一所有查询的 JOIN 条件
2. ⬜ 添加数据库约束
3. ⬜ 更新所有测试脚本

### Step 3: 监控与告警（持续）
1. ⬜ 部署数据完整性检查端点
2. ⬜ 设置定时自动修复任务
3. ⬜ 添加 Sentry 告警

## 预期效果

修复完成后：
- ✅ 所有订单的 `psp_used` 和 `psp_id` 都非空
- ✅ `psp_used` 统一小写
- ✅ PSP Overview 数据准确
- ✅ Employee/Merchant Dashboard 数据一致
- ✅ 不再出现"0 transactions"问题


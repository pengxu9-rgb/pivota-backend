# 架构整合计划

## 现状分析

### 两套并行系统
1. **Legacy MCP系统**（旧）
   - 数据存储：`merchant_onboarding.mcp_*` 字段
   - 只支持单个平台连接
   - 产品直接从平台API实时获取

2. **新集成系统**（新）
   - 数据存储：`merchant_stores` + `products_cache`
   - 支持多平台连接
   - 产品缓存在本地数据库

## 短期方案（立即可执行）

### 1. 创建统一的数据访问层
```python
# services/merchant_store_service.py
async def get_merchant_active_stores(merchant_id: str):
    """统一获取商家的活跃商店，兼容新旧系统"""
    stores = []
    
    # 1. 检查新系统 merchant_stores
    new_stores = await get_from_merchant_stores(merchant_id)
    stores.extend(new_stores)
    
    # 2. 检查旧系统 merchant_onboarding.mcp_*
    if not stores:
        legacy_store = await get_from_merchant_onboarding(merchant_id)
        if legacy_store:
            stores.append(legacy_store)
    
    return stores
```

### 2. 更新Onboarding流程
当商家连接商店时：
```python
async def connect_store(merchant_id, platform, credentials):
    # 1. 写入 merchant_stores（新系统）
    await create_merchant_store(...)
    
    # 2. 更新 merchant_onboarding（兼容旧系统）
    if is_first_store:
        await update_merchant_onboarding(
            mcp_connected=True,
            mcp_platform=platform,
            # ... 其他字段
        )
```

### 3. 统一产品获取逻辑
```python
@router.get("/products/{merchant_id}/unified")
async def get_products_unified(merchant_id: str):
    # 1. 优先从 products_cache 获取
    cached_products = await get_from_cache(merchant_id)
    if cached_products:
        return cached_products
    
    # 2. 检查是否需要同步
    stores = await get_merchant_active_stores(merchant_id)
    if stores:
        # 触发同步
        await sync_products_for_stores(stores)
        # 返回同步后的产品
        return await get_from_cache(merchant_id)
    
    return []
```

## 中期方案（1-2周）

### 1. 数据迁移脚本
```sql
-- 将 merchant_onboarding.mcp_* 数据迁移到 merchant_stores
INSERT INTO merchant_stores (merchant_id, platform, domain, api_key, status)
SELECT 
    merchant_id,
    mcp_platform,
    mcp_shop_domain,
    mcp_access_token,
    'active'
FROM merchant_onboarding
WHERE mcp_connected = true
  AND NOT EXISTS (
    SELECT 1 FROM merchant_stores ms 
    WHERE ms.merchant_id = merchant_onboarding.merchant_id
  );
```

### 2. 添加数据一致性检查
```python
# 健康检查端点
@router.get("/admin/data-consistency-check")
async def check_data_consistency():
    inconsistencies = []
    
    # 检查所有merchant
    merchants = await get_all_merchants()
    for merchant in merchants:
        # 比较新旧系统数据
        old_data = await get_merchant_onboarding(merchant.id)
        new_stores = await get_merchant_stores(merchant.id)
        
        if has_inconsistency(old_data, new_stores):
            inconsistencies.append({
                "merchant_id": merchant.id,
                "issue": describe_inconsistency(old_data, new_stores)
            })
    
    return {"inconsistencies": inconsistencies}
```

## 长期方案（1个月）

### 1. 完全迁移到新系统
- 废弃 `merchant_onboarding.mcp_*` 字段
- 所有代码统一使用 `merchant_stores` 表
- 移除 v1 产品端点，只保留 v2

### 2. 重构代码结构
```
pivota_infra/
├── services/
│   ├── store_service.py      # 商店管理
│   ├── product_service.py    # 产品管理
│   └── sync_service.py       # 同步服务
├── routes/
│   ├── products.py           # 统一的产品端点
│   └── stores.py             # 商店管理端点
└── migrations/
    └── consolidate_mcp_to_stores.py
```

### 3. 更新文档
- 明确说明新的数据模型
- 提供迁移指南
- 更新API文档

## 实施步骤

### Phase 1（本周）
1. ✅ 创建 v2 端点（已完成）
2. 创建数据访问服务层
3. 更新 onboarding 流程双写数据

### Phase 2（下周）
1. 运行数据迁移脚本
2. 添加监控和告警
3. 逐步切换流量到新端点

### Phase 3（第3-4周）
1. 确认所有数据已迁移
2. 移除旧代码
3. 清理数据库字段

## 风险管理

### 风险1：数据不一致
- **缓解**：双写期间保持数据同步
- **监控**：每日运行一致性检查

### 风险2：破坏现有功能
- **缓解**：保持向后兼容
- **测试**：完整的回归测试

### 风险3：性能问题
- **缓解**：优化查询，添加索引
- **监控**：跟踪API响应时间

## 成功指标
1. 所有商家数据成功迁移
2. API响应时间不增加
3. 零数据丢失
4. 代码复杂度降低50%





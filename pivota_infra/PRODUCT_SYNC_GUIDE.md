# 产品同步系统使用指南

## 概述

Pivota 的产品同步系统支持多平台（Shopify, Wix, WooCommerce, Square, BigCommerce）统一管理。

## 核心端点

### 1. 产品查询（多平台）
```
GET /products/v2/{merchant_id}?limit=100&platform=shopify
```

**特性**：
- ✅ 支持所有平台
- ✅ 直接从缓存读取（高性能）
- ✅ 可按平台筛选
- ✅ 商户权限控制

**响应示例**：
```json
{
  "merchant_id": "merch_xxx",
  "platform": "all",
  "products": [...],
  "total": 23,
  "next_page_token": "100",
  "fetched_at": "2025-10-31T12:00:00"
}
```

### 2. 同步所有平台
```
POST /products/sync-all-platforms/
{
  "merchant_id": "merch_xxx",
  "force_refresh": true,
  "limit": 250
}
```

**特性**：
- ✅ 自动检测所有连接的商店
- ✅ 并发同步多个平台
- ✅ 返回每个平台的详细结果
- ✅ 优雅的错误处理（不会返回 500）

**响应示例**：
```json
{
  "status": "success",
  "message": "Successfully synced 23 products from 2 platforms",
  "platforms_synced": [
    {
      "platform": "shopify",
      "status": "success",
      "message": "Successfully synced 4 products",
      "products_synced": 4
    },
    {
      "platform": "wix",
      "status": "success",
      "message": "Successfully synced 19 products",
      "products_synced": 19
    }
  ],
  "total_products": 23
}
```

### 3. 平台汇总
```
GET /products/v2/{merchant_id}/platforms
```

返回该商户所有平台的产品统计。

### 4. 监控端点

#### 系统级统计
```
GET /products/monitoring/sync-stats
```

返回：
- 各平台产品数量
- 活跃/过期产品统计
- 最后同步时间
- 平均访问次数

#### 商户同步历史
```
GET /products/monitoring/merchant/{merchant_id}/sync-history?days=7
```

#### 健康检查（无需认证）
```
GET /products/monitoring/health-check
```

## 重要注意事项

### 路由注册顺序（main.py）
**必须严格遵守以下顺序**，否则会导致路由冲突：

```python
# ✅ 正确顺序
app.include_router(product_router_v2)    # /products/v2/* - 更具体的路径先注册
app.include_router(product_sync_router)  # /products/sync/*
app.include_router(product_router)       # /products/* - 通用路径最后注册
```

**错误示例**（会导致 403）：
```python
# ❌ 错误顺序
app.include_router(product_router)       # /products/{merchant_id}
app.include_router(product_router_v2)    # /products/v2/{merchant_id}
# 结果：/products/v2/xxx 被匹配为 merchant_id="v2", product_id="xxx"
```

### SQL 查询注意事项

**LIMIT 和 OFFSET 不能作为绑定参数**：

```python
# ❌ 错误（会导致 500 错误）
query = "SELECT * FROM table WHERE id = :id LIMIT :limit"
await database.fetch_all(query, {"id": 123, "limit": 10})

# ✅ 正确
query = f"SELECT * FROM table WHERE id = :id LIMIT {limit}"
await database.fetch_all(query, {"id": 123})
```

原因：SQLAlchemy 在动态构建的查询中不支持绑定这些 SQL 关键字。

### 产品缓存 TTL

- **当前设置**：7天（604800秒）
- **过期检查**：`expires_at > NOW()`
- **建议**：根据业务需求调整，高频变化的商品考虑缩短 TTL

## 故障排查

### 问题 1：产品不显示
**检查**：
1. 查看监控端点：`/products/monitoring/sync-stats`
2. 确认产品未过期
3. 检查 `merchant_stores` 连接状态

### 问题 2：同步返回 0 个产品
**可能原因**：
- API 凭据无效或过期
- 平台 API 返回错误
- 网络问题

**调试**：查看 Railway 日志中的同步详情

### 问题 3：只显示部分平台产品
**检查**：
- 确认使用 `/products/v2/` 而不是 `/products/`
- v1 端点只支持单平台
- 使用 `sync-all-platforms` 而不是 `sync-universal`

## 性能优化

### 缓存策略
- 优先从缓存读取
- 缓存未命中时实时拉取
- 后台更新缓存，不阻塞响应

### 分页支持
- 默认：50 个产品
- 最大：500 个产品
- 使用 `next_page_token` 实现分页

## 安全考虑

### 权限控制
- 商户只能访问自己的产品
- 员工/管理员可访问所有商户
- 使用 `can_access_merchant()` 检查

### 数据隔离
- 每个商户的产品独立缓存
- 按 `merchant_id` + `platform` 隔离
- 防止跨商户数据泄露



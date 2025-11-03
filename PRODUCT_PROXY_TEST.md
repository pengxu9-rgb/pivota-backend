# Product Proxy API 测试指南

## 架构概述

**防御性产品数据管理架构：**
- 🔄 **实时代理**：从源平台拉取，转换为统一标准格式
- ⚡ **智能缓存**：Read-Through Cache (TTL 1h)，提升性能
- 🛡️ **防御性设计**：Agent 只读，事件追踪，自动清理
- 📊 **业务洞察**：API调用量、转化率、支付成功率

---

## 快速测试

### 1. 安装依赖
```bash
pip install requests
```

### 2. 运行自动化测试
```bash
# Railway 部署测试
python test_product_proxy.py

# 自定义参数
python test_product_proxy.py \
  https://web-production-fedb.up.railway.app \
  merch_a4dc9a163f49d835 \
  YOUR_JWT_TOKEN
```

### 3. 本地测试
```bash
# 修改脚本中的 BASE_URL
BASE_URL = "http://localhost:8000"

python test_product_proxy.py
```

---

## 测试用例

### ✅ Test 1: First Fetch (Cache Miss)
- 第一次拉取产品
- 应该较慢（实时调用 Shopify API）
- 验证数据转换为 StandardProduct 格式

### ✅ Test 2: Second Fetch (Cache Hit)
- 第二次拉取相同产品
- 应该非常快（< 100ms）
- 验证缓存机制生效

### ✅ Test 3: Force Refresh
- 强制刷新缓存（`force_refresh=true`）
- 跳过缓存，直接拉取最新数据
- 验证缓存失效机制

### ✅ Test 4: Single Product Detail
- 获取单个产品详情
- 验证详细字段（variants, images, metadata）

### ✅ Test 5: Analytics Metrics
- 获取商户业务分析指标
- 验证事件追踪系统
- 查看缓存命中率、转化率等

### ✅ Test 6: Performance Comparison
- 对比 Cache Hit vs Cache Miss 性能
- 验证缓存加速效果（应 > 5x）

### ✅ Test 7: Cleanup Cache
- 手动触发缓存清理
- 验证过期数据清理机制

### ✅ Test 8: Error Handling
- 测试不存在的商户
- 验证错误处理和防御性编程

---

## 手动测试（cURL）

### 获取 JWT Token
```bash
curl https://web-production-fedb.up.railway.app/auth/admin-token
```

### 获取产品列表
```bash
curl -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  "https://web-production-fedb.up.railway.app/products/merch_a4dc9a163f49d835?limit=10"
```

### 强制刷新
```bash
curl -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  "https://web-production-fedb.up.railway.app/products/merch_a4dc9a163f49d835?force_refresh=true"
```

### 获取单个产品
```bash
curl -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  "https://web-production-fedb.up.railway.app/products/merch_a4dc9a163f49d835/PRODUCT_ID"
```

### 查看分析指标
```bash
curl -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  "https://web-production-fedb.up.railway.app/products/analytics/merch_a4dc9a163f49d835"
```

### 清理缓存
```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  "https://web-production-fedb.up.railway.app/products/maintenance/cleanup-cache"
```

---

## 验证要点

### 1. 数据标准化
检查返回的 StandardProduct 格式：
```json
{
  "id": "123456789",
  "platform": "shopify",
  "merchant_id": "merch_xxx",
  "title": "Product Name",
  "price": 29.99,
  "currency": "USD",
  "inventory_quantity": 100,
  "variants": [...],
  "images": [...],
  "status": "active",
  "platform_metadata": {...}
}
```

### 2. 缓存性能
- Cache Miss: > 500ms (实时 API 调用)
- Cache Hit: < 100ms (数据库读取)
- Speedup: > 5x

### 3. 事件追踪
检查 `api_call_events` 表：
```sql
SELECT * FROM api_call_events 
WHERE merchant_id = 'merch_xxx' 
ORDER BY created_at DESC LIMIT 10;
```

### 4. 分析指标
验证 `merchant_analytics` 表更新：
- `total_api_calls`: 增加
- `cache_hit_rate`: 计算正确
- `avg_response_time_ms`: 合理范围

---

## 常见问题

### Q: 缓存未命中？
A: 检查：
1. 缓存是否过期（TTL 1小时）
2. 是否使用了 `force_refresh=true`
3. 数据库连接是否正常

### Q: 性能提升不明显？
A: 可能原因：
1. 网络延迟影响测试
2. 数据库查询未优化
3. Railway 实例冷启动

### Q: 分析数据为空？
A: 正常！分析数据需要：
1. 至少有几次 API 调用
2. 触发计算任务（手动或定时）
3. 使用 `POST /products/maintenance/recalculate-analytics/{merchant_id}` 强制计算

### Q: Shopify 连接失败？
A: 检查：
1. `mcp_shop_domain` 和 `mcp_access_token` 是否配置
2. Shopify API 权限是否足够（read_products）
3. 环境变量是否正确设置

---

## 监控和维护

### 定期清理缓存（建议每小时）
```bash
# 添加到 cron 或 Railway cron jobs
0 * * * * curl -X POST -H "Authorization: Bearer TOKEN" \
  https://web-production-fedb.up.railway.app/products/maintenance/cleanup-cache
```

### 定期计算分析（建议每天）
```bash
# 为所有商户重新计算分析指标
0 2 * * * python scripts/recalculate_all_analytics.py
```

### 监控关键指标
- 缓存命中率 > 80%
- 平均响应时间 < 200ms
- API 调用成功率 > 99%

---

## 性能基准

| 指标 | 目标值 | 优秀 | 需优化 |
|-----|-------|------|--------|
| Cache Hit Rate | > 80% | > 90% | < 70% |
| Cache Hit Response | < 100ms | < 50ms | > 200ms |
| Cache Miss Response | < 1s | < 500ms | > 2s |
| Speedup (Hit/Miss) | > 5x | > 10x | < 3x |

---

## 下一步

1. ✅ 运行自动化测试验证所有功能
2. ✅ 手动测试关键场景（缓存、刷新、错误）
3. ✅ 检查 Railway logs 确认事件记录
4. ✅ 查看数据库确认数据写入
5. ⏭️ 集成到 Admin Dashboard（产品管理页面）
6. ⏭️ 为 Agent 提供 MCP 接口
7. ⏭️ 添加定时任务（缓存清理、分析计算）

---

## 支持

如有问题，请检查：
1. Railway logs: `railway logs`
2. 数据库状态: Railway → Postgres → Metrics
3. API 文档: `https://web-production-fedb.up.railway.app/docs`


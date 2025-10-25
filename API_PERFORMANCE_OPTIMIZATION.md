# API 性能优化方案

## 🔍 当前问题

- **API 响应慢**: 每个请求 3-5 秒
- **Dashboard 加载慢**: 需要等待多个 API 调用
- **订单创建慢**: 每个订单需要 3+ 秒

## 🎯 问题根源

### 1. 数据库查询优化不足
- 缺少索引导致全表扫描
- 复杂的 JOIN 查询没有优化
- products_cache 表查询慢

### 2. Railway 免费层限制
- 数据库在美国西部
- 网络延迟高（跨洋访问）
- CPU/内存限制

### 3. 连接池配置
- 可能没有配置数据库连接池
- 每次请求都建立新连接

## 🚀 优化方案

### 立即执行（部署完成后）

#### 1. 测试当前性能
```bash
curl https://web-production-fedb.up.railway.app/admin/performance/test-db-connection
```

#### 2. 创建数据库索引
```bash
curl -X POST https://web-production-fedb.up.railway.app/admin/performance/create-indexes
```

这会创建以下索引：
- `products_cache(merchant_id)` - 加速产品搜索
- `orders(merchant_id, agent_id, status)` - 加速订单查询
- `agent_usage_logs(agent_id, timestamp)` - 加速使用记录查询
- `agents(api_key)` - 加速 API key 认证
- `merchant_onboarding(status)` - 加速商家查询

#### 3. 检查连接池状态
```bash
curl https://web-production-fedb.up.railway.app/admin/performance/connection-pool-status
```

### 前端优化

#### 1. 减少 API 调用
修改 Dashboard 页面，合并多个 API 调用：
- 使用单个 `/agent/metrics/summary` 端点获取所有数据
- 实现客户端缓存

#### 2. 添加加载状态
```typescript
// 显示骨架屏而不是空白
const [loading, setLoading] = useState(true);
if (loading) return <SkeletonLoader />;
```

#### 3. 实现数据预加载
```typescript
// 预加载常用数据
useEffect(() => {
  prefetchMerchants();
  prefetchProducts();
}, []);
```

### 后端优化

#### 1. 添加缓存层
```python
from functools import lru_cache
import redis

# Redis 缓存产品搜索结果
@lru_cache(maxsize=100)
async def get_cached_products(query: str):
    # 缓存 5 分钟
    cache_key = f"products:{query}"
    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)
    
    # 查询数据库
    products = await database.fetch_all(...)
    await redis_client.setex(cache_key, 300, json.dumps(products))
    return products
```

#### 2. 优化数据库查询
```python
# 使用单个查询替代 N+1
# 坏的做法：
for merchant in merchants:
    products = await get_products(merchant.id)

# 好的做法：
products = await database.fetch_all("""
    SELECT * FROM products_cache 
    WHERE merchant_id = ANY(:merchant_ids)
""", {"merchant_ids": merchant_ids})
```

#### 3. 增加连接池大小
```python
# config/settings.py
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    DATABASE_URL += "?pool_size=20&max_overflow=40"
```

## 📊 预期改进

实施索引后：
- **产品搜索**: 5秒 → 0.5-1秒
- **订单创建**: 3秒 → 0.5秒
- **Dashboard 加载**: 10秒 → 2-3秒

## 🔧 监控工具

### 检查慢查询
```bash
curl https://web-production-fedb.up.railway.app/admin/performance/check-slow-queries
```

### 测试并发性能
```bash
curl https://web-production-fedb.up.railway.app/admin/performance/connection-pool-status
```

## 💡 长期方案

1. **升级 Railway 计划**
   - 获得更多 CPU/内存
   - 更好的网络性能
   - 专用数据库实例

2. **使用 CDN**
   - 缓存静态资源
   - 减少延迟

3. **数据库读写分离**
   - 主库写入
   - 从库读取

4. **实施 GraphQL**
   - 减少过度获取
   - 批量查询

## 🎯 立即行动

部署完成后（约 2 分钟），执行：

```bash
# 1. 创建索引（最重要）
curl -X POST https://web-production-fedb.up.railway.app/admin/performance/create-indexes

# 2. 测试改进
curl https://web-production-fedb.up.railway.app/admin/performance/test-db-connection

# 3. 重新测试订单创建
python3 quick_test_orders.py
```

索引创建后，API 应该会快很多！


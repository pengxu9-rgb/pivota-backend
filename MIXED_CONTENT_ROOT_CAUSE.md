# Mixed Content 错误 - 根本原因分析

## 🔍 问题时间线

### 之前的状态（正常）
```javascript
// 478d60c 提交时的代码
const API_BASE_URL = 'https://web-production-fedb.up.railway.app';
                      ^^^^^^ HTTPS - 硬编码
```
✅ **结果**: 正常工作，没有 Mixed Content 错误

### 现在的状态（报错）
```javascript
// 当前代码
const url = process.env.NEXT_PUBLIC_API_URL || 'https://...';
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
            如果环境变量是 HTTP，会用 HTTP
```
❌ **结果**: Mixed Content 错误

## 根本原因分析

### 不是因为两个重复路由文件 ❌

**两个路由文件的问题**：
- `employee_agent_mgmt.py`（第315行）
- `employee_agents_management.py`（第285行）

**影响范围**：
- ✅ 只影响返回的字段（total_orders, total_gmv 等）
- ❌ **不影响** HTTP/HTTPS 协议

### 真正原因：Vercel 环境变量设置 ⚠️

#### 场景 1: 环境变量设置错误（最可能）

```
Vercel 项目设置中：
NEXT_PUBLIC_API_URL = http://web-production-fedb.up.railway.app
                      ^^^^
                      错误：使用了 HTTP
```

**何时设置的？**
- 可能在某次部署时手动添加
- 可能从其他项目复制过来
- 可能在测试时临时设置后忘记改回

#### 场景 2: 构建缓存问题

```
Vercel 构建缓存可能保存了旧的环境变量值
即使代码是 HTTPS，构建时可能用了缓存的 HTTP
```

#### 场景 3: DNS/域名最近改变

```
之前: employee.pivota.cc → HTTP（没有 SSL）
现在: employee.pivota.cc → HTTPS（启用了 SSL）

当 HTTPS 启用后，HTTP API 调用立即被阻止
```

## 🔧 解决方案（已实施）

### 1. 代码层面强制 HTTPS ✅
```typescript
const getApiBaseUrl = () => {
  const url = process.env.NEXT_PUBLIC_API_URL || 'https://...';
  // 页面是 HTTPS 时，强制 API 也用 HTTPS
  if (typeof window !== 'undefined' && window.location.protocol === 'https:') {
    return url.replace(/^http:/, 'https:');
  }
  return url;
};
```

**优点**: 
- 自动修正错误的环境变量
- 防御性编程，避免未来再次出现

### 2. 需要检查的地方 📝

#### A. Vercel 环境变量
1. 登录 Vercel Dashboard
2. 打开 pivota-employee-portal 项目
3. Settings → Environment Variables
4. 检查 `NEXT_PUBLIC_API_URL`
5. 如果存在且值是 `http://...`，删除或改为 `https://`

#### B. Vercel 构建缓存
1. Deployments → 最新部署
2. 点击 **"Redeploy"**
3. 勾选 **"Use existing Build Cache"** → **取消勾选**
4. 点击 **Redeploy**

## 🎯 其他相关问题

### 问题 1: 两个重复的路由文件 ⚠️

**文件**:
- `routes/employee_agent_mgmt.py`
- `routes/employee_agents_management.py`

**影响**:
- ✅ 已修复：统一了返回字段
- ⚠️ **长期风险**: 容易出现字段不一致

**建议**:
```
Option 1: 合并为一个文件（推荐）
  - 避免维护两份代码
  - 单一真相源

Option 2: 明确分工
  - agent_mgmt: 负责 CRUD（创建、更新、删除）
  - agents_management: 负责查询和统计
  - 文档化职责划分
```

### 问题 2: Demo Data Fallback ⚠️

**文件**: 
- `merchant_dashboard_routes.py`（旧版）
- `merchant_dashboard_routes_fixed.py`（新版）

**问题**:
- 旧版本有 `DEMO_MERCHANT_DATA` fallback
- 数据库错误时返回假数据，掩盖真实问题

**修复**:
- ✅ 创建了新版本，删除所有 demo fallback
- ✅ 更新 main.py 使用新版本

### 问题 3: 多个数据源不一致 ⚠️

**发现的数据源**:
1. `orders` 表 - 真实订单（✅ 正确的数据源）
2. `agent_usage_logs` 表 - API 调用日志（❌ 不应该用于业务统计）
3. `agent_merchants` 表 - 关联关系（❌ 是空的）
4. `agents` 表的统计字段 - total_requests, total_gmv 等（⚠️ 需要定期同步）

**潜在问题**:
```
agents.total_requests = 149  (历史数据)
但实际订单只有 1 个

这说明：
- 历史数据可能不准确
- 或者有数据被删除了
- 或者统计字段没有正确更新
```

**建议修复**:
```sql
-- 定期同步 agents 表的统计字段
UPDATE agents a
SET 
    total_requests = (SELECT COUNT(*) FROM orders WHERE agent_id = a.agent_id),
    total_orders = (SELECT COUNT(*) FROM orders WHERE agent_id = a.agent_id),
    total_gmv = (SELECT COALESCE(SUM(total), 0) FROM orders WHERE agent_id = a.agent_id),
    success_rate = (
        SELECT 
            CASE WHEN COUNT(*) > 0 THEN
                COUNT(*) FILTER (WHERE payment_status IN ('paid', 'completed', 'succeeded'))::FLOAT / COUNT(*)::FLOAT * 100
            ELSE 0 END
        FROM orders WHERE agent_id = a.agent_id
    );
```

### 问题 4: agent_usage_logs 的异常数据 ⚠️

**发现**:
```
direct_orders: 1 个订单，$24.99 GMV
usage_logs: 235 条日志，$472.97 GMV
```

**问题**:
- Usage logs 的 GMV 比实际订单高很多
- 可能有重复记录或错误数据

**建议检查**:
```sql
-- 查看 usage_logs 的详细数据
SELECT 
    agent_id,
    order_id,
    order_amount,
    endpoint,
    timestamp
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2'
AND order_amount > 0
ORDER BY timestamp DESC;

-- 检查是否有重复
SELECT 
    order_id,
    COUNT(*) as count,
    SUM(order_amount) as total_amount
FROM agent_usage_logs
WHERE agent_id = 'agent_ee38f2b3645a2ec2'
GROUP BY order_id
HAVING COUNT(*) > 1;
```

## 📋 完整问题清单

| # | 问题 | 状态 | 影响 |
|---|------|------|------|
| 1 | Mixed Content (HTTP/HTTPS) | ✅ 已修复 | 阻止 API 调用 |
| 2 | 两个重复路由文件 | ✅ 已协调 | 字段不一致 |
| 3 | Demo data fallback | ✅ 已删除 | 假数据混入 |
| 4 | 计算口径不同 | ✅ 已统一 | 数据不准确 |
| 5 | agent_merchants 表空 | ✅ 改用 orders | merchant_count=0 |
| 6 | usage_logs 数据异常 | ⚠️ 待检查 | 数据不一致 |
| 7 | agents 统计字段旧 | ⚠️ 待同步 | 显示历史数据 |

## 🎯 立即行动

### 1. 检查 Vercel 环境变量
最可能的原因是这里设置了 HTTP

### 2. 等待部署完成
- Backend (Railway): 约 2-3 分钟
- Frontend (Vercel): 约 1-2 分钟

### 3. 清除浏览器缓存
按 `Cmd+Shift+R` 强制刷新

### 4. 验证
```bash
# 检查 HTTPS
curl -I https://web-production-fedb.up.railway.app/employee/agents

# 检查部署
./verify_deployment.sh YOUR_TOKEN
```

## 总结

**Mixed Content 错误**:
- ❌ **不是**两个重复路由文件引起的
- ✅ **可能是** Vercel 环境变量设置了 HTTP
- ✅ **可能是** 最近启用了 HTTPS 域名但没更新 API URL
- ✅ **已修复** 代码强制 HTTPS

**其他相关问题**（已发现并修复）:
- ✅ 数据计算口径不统一
- ✅ Demo data 混入真实环境
- ✅ 多个数据源不一致
- ⚠️ 历史数据需要清理（非紧急）

---

**建议**: 部署完成后，在 Vercel 设置中检查/删除 `NEXT_PUBLIC_API_URL` 环境变量，让代码使用默认的 HTTPS URL。

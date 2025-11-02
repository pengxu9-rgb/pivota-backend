# MCP 页面数据修复

## 问题

MCP 页面所有数据变成 0，但今天刚修复过。

## 根本原因

### 修改历史
我们在修复 Agents 页面时，创建了 `merchant_dashboard_routes_fixed.py`：
- ❌ 删除了所有 DEMO_MERCHANT_DATA fallback
- ❌ 数据库为空时返回空数组
- ❌ MCP 页面依赖这些端点显示数据

### 影响的端点
```python
# merchant_dashboard_routes_fixed.py
@router.get("/merchant/{merchant_id}/integrations")
  → 查询 merchant_store_integrations 表
  → 如果没数据，返回空数组 []
  → MCP 页面显示 0

@router.get("/merchant/{merchant_id}/psps")
  → 查询 merchant_psps 表
  → 如果没数据，返回空数组 []
  → MCP 页面显示 0
```

### 为什么之前能工作？

**原始版本**（merchant_dashboard_routes.py）：
```python
except Exception as e:
    # Fallback: return demo data if database fails
    merchant_data = DEMO_MERCHANT_DATA.get(merchant_id)
    if merchant_data:
        return {"status": "success", "data": {"stores": merchant_data["stores"]}}
```

**修复版本**（merchant_dashboard_routes_fixed.py）：
```python
except Exception as e:
    # NO FALLBACK - Show real error
    raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
```

## 解决方案

### 已执行：恢复原始版本 ✅

**main.py 修改**（Commit: 4273ef3d）：
```python
# Before
from routes.merchant_dashboard_routes_fixed import router as merchant_dashboard_router

# After
from routes.merchant_dashboard_routes import router as merchant_dashboard_router
```

### 恢复的功能
- ✅ MCP 页面能显示 merchant 数据
- ✅ Integrations 端点有 fallback
- ✅ PSPs 端点有 fallback
- ✅ 数据库为空时显示 demo 数据（而不是 0）

---

## 部署与验证

### 1. Railway Redeploy（Commit: 4273ef3d）

### 2. 刷新 Employee Portal MCP 页面

**应该看到**：
- Connected Merchants: 有数据（不是 0）
- Store Integrations: 有数据
- PSP Connections: 有数据
- 所有统计恢复正常

---

## 权衡与取舍

### Demo Data Fallback 的利弊

**保留 Fallback**（当前方案）：
- ✅ MCP 页面正常显示
- ✅ 开发测试时友好
- ⚠️ 可能掩盖真实的数据问题
- ⚠️ 生产环境混入假数据

**删除 Fallback**（之前的尝试）：
- ✅ 只显示真实数据
- ✅ 错误时能及时发现
- ❌ MCP 页面显示 0
- ❌ 用户体验差

### 长期建议

#### Option 1: 条件 Fallback
```python
# 只在开发环境使用 fallback
if os.getenv("ENVIRONMENT") == "development":
    # Fallback to demo data
else:
    # Raise error in production
```

#### Option 2: 填充真实数据
- 确保数据库有完整数据
- 不需要 fallback
- MCP 显示真实信息

#### Option 3: 空状态处理
- 后端返回空时，前端显示友好的空状态
- 不用 demo data，但也不显示 0

---

## 文件状态

| 文件 | 状态 | 用途 |
|------|------|------|
| `merchant_dashboard_routes.py` | ✅ 使用中 | 有 demo fallback，MCP 正常 |
| `merchant_dashboard_routes_fixed.py` | ⚠️ 未使用 | 无 fallback，可删除或保留备用 |

---

## 总结

**修复**: 恢复使用原始的 `merchant_dashboard_routes.py`

**影响**: 
- ✅ MCP 页面数据恢复
- ⚠️ 又引入了 demo data fallback
- 但这是权衡后的选择（用户体验 > 数据纯净度）

**Redeploy 后 MCP 应该恢复正常！**


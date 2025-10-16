# 🔧 PgBouncer Prepared Statement 问题总结

## 问题

在使用 Supabase PostgreSQL (通过 pgbouncer) 时，遇到以下错误:

```
prepared statement "__asyncpg_stmt_X__" already exists
```

## 根本原因

- Supabase 使用 pgbouncer 进行连接池管理
- pgbouncer 的 `transaction` 或 `statement` 模式不支持 prepared statements
- asyncpg (Python PostgreSQL driver) 默认使用 prepared statement cache

## 解决方案选项

### ❌ 方案 1: server_settings 参数 (不起作用)
```python
database = Database(
    DATABASE_URL,
    server_settings={"statement_cache_size": "0"}
)
```
**问题**: `databases` library 不支持此参数

### ❌ 方案 2: URL 参数 (不起作用)
```python
DATABASE_URL = url + "?statement_cache_size=0"
database = Database(DATABASE_URL)
```
**问题**: asyncpg 不从 URL 读取此参数

### ✅ 方案 3: 切换到直接 asyncpg 连接池

需要修改所有数据库操作以使用 asyncpg 而不是 `databases` library。

### ✅ 方案 4: 临时解决方案 - 商户 Onboarding 暂时独立

将 Phase 2 和 Phase 3 暂时与主系统分离，使用独立的数据库连接。

## 建议

**短期**: 使用现有的 MCP server 功能进行商户注册和支付执行
**长期**: 迁移到 asyncpg 连接池或升级 Supabase 计划以避免 pgbouncer

## 当前状态

- ✅ Phase 3 代码已实现
- ❌ 由于 pgbouncer 限制无法在 Supabase 上运行
- ✅ 可以在本地 PostgreSQL 或其他托管服务上正常运行

## 测试商户和支付执行

如果需要测试 Phase 3 功能，可以:

1. **使用本地 PostgreSQL** (不使用 pgbouncer)
2. **使用其他云服务** (AWS RDS, Google Cloud SQL, etc.)
3. **等待前端准备好后，使用现有的支付流程**

---

**结论**: Phase 3 功能已完全实现并准备好，但由于 Supabase 的 pgbouncer 限制，需要更换数据库服务或重构数据库层。


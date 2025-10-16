# PgBouncer Prepared Statement Issue - Summary

## 问题

Render 使用 pgbouncer 进行数据库连接池管理，但 pg

bouncer 在 transaction/statement 模式下不支持 asyncpg 的 prepared statements，导致错误：

```
prepared statement "__asyncpg_stmt_X__" already exists/does not exist
```

## 尝试的解决方案（都失败了）

1. ❌ `server_settings={'statement_cache_size': '0'}` - 不是正确的参数位置
2. ❌ URL 参数 `?statement_cache_size=0` - asyncpg 不从 URL 读取此参数
3. ❌ `options={"statement_cache_size": 0}` - databases 库不传递此参数给 asyncpg

## 真正的解决方案

**databases 库不直接支持禁用 prepared statements！**

需要联系 Render 支持，要求他们：
1. 将 pgbouncer pool_mode 从 `transaction` 改为 `session`
2. 或者提供直接的 PostgreSQL 连接（不通过 pgbouncer）

## 临时 Workaround

在 Render dashboard 中设置环境变量：
```
PGBOUNCER_PREPARED_STATEMENTS=disable
```

但这可能不被支持。

## 最终建议

使用 Supabase 或其他支持 asyncpg 的托管数据库服务，或者要求 Render 修改 pgbouncer 配置。

# 🔄 切换到 Supabase Direct Connection

## 📋 操作步骤

### 1. 获取 Direct Connection String

1. 登录 Supabase Dashboard: https://supabase.com/dashboard
2. 选择你的项目
3. 进入 **Project Settings** → **Database**
4. 找到 **Connection string** 部分
5. 选择 **Session mode (Direct Connection)**
6. **端口应该是 `5432`**（不是 6543）
7. 连接字符串格式：
   ```
   postgresql://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:5432/postgres
   ```

### 2. 更新 Render 环境变量

1. 登录 Render Dashboard: https://dashboard.render.com
2. 选择 `pivota-dashboard` 服务
3. 进入 **Environment** 标签
4. 找到 `DATABASE_URL` 变量
5. **替换为 Direct Connection 字符串**（端口 5432）
6. 点击 **Save Changes**

### 3. 等待自动部署

Render 会自动重新部署。

## ✅ 优势

- ✅ 支持 prepared statements，无需任何 workaround
- ✅ 更稳定、更简单
- ✅ 对于当前开发阶段，连接数完全够用

## 📊 监控连接数

在 Supabase Dashboard → **Database** → **Connection pooling** 可以看到当前连接数使用情况。

如果将来遇到连接数限制（免费版 60 连接），再考虑：
1. 降低应用连接池大小（`max_size=3`）
2. 升级 Supabase 计划
3. 或者回到 Transaction Pooler（那时再解决 prepared statement 问题）


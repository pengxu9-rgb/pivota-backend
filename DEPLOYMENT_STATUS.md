# 部署状态和测试指南

## ✅ 已完成的修复

### 1. 后端修复
- ✅ 移除所有 Supabase 依赖
- ✅ 使用内存存储 + JWT 认证
- ✅ 添加商户仪表板 API 端点
- ✅ 修复 JWT token 格式（添加 `sub` 和 `email` 字段）
- ✅ 所有代码已推送到 GitHub (commit: befe3e75)

### 2. 前端修复  
- ✅ 更新所有三个门户的登录 API 端点（从 `/api/auth/login` 改为 `/auth/signin`）
- ✅ 修复响应格式检查（从 `response.success` 改为 `response.status === 'success'`）
- ✅ 商户门户配置正确的 merchant_id

### 3. 本地测试验证
- ✅ Token 创建逻辑正确（包含 sub, email, role）
- ✅ Auth 工具函数正确验证 token

## ⏳ 等待部署

### Railway 部署状态
- 最新 commit: `befe3e75` - fix JWT token format
- 状态：**等待 Railway 自动部署**
- 预计时间：2-5 分钟

### 如何手动触发 Railway 部署
1. 访问 Railway 项目面板
2. 找到 backend 服务
3. 点击 "Deployments" 标签
4. 点击 "Deploy" 按钮手动触发

## 🧪 测试步骤（部署完成后）

### 步骤 1: 验证后端 API

```bash
# 1. 测试登录并检查 token 格式
RESPONSE=$(curl -s -X POST https://web-production-fedb.up.railway.app/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"merchant@test.com","password":"Admin123!"}')

echo "$RESPONSE" | python3 -m json.tool

# 2. 提取并解码 token
TOKEN=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('token',''))")

python3 << EOF
import base64, json
payload = "$TOKEN".split('.')[1]
padding = '=' * (4 - len(payload) % 4)
decoded = base64.urlsafe_b64decode(payload + padding)
data = json.loads(decoded)
print(json.dumps(data, indent=2))
print(f"\n✅ 'sub' 字段: {data.get('sub')}")
print(f"✅ 'email' 字段: {data.get('email')}")
print(f"✅ 'role' 字段: {data.get('role')}")
EOF

# 3. 测试商户 API 端点
curl -s https://web-production-fedb.up.railway.app/merchant/profile \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

curl -s https://web-production-fedb.up.railway.app/merchant/merch_208139f7600dbf42/integrations \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

### 步骤 2: 测试前端登录

#### 商户门户 (https://merchant.pivota.cc)
- Email: `merchant@test.com`
- Password: `Admin123!`
- 预期：成功登录并跳转到仪表板
- 验证：能看到商店、PSP、订单数据

#### 员工门户 (https://employee.pivota.cc)
- Email: `employee@pivota.com`
- Password: `Admin123!`
- 预期：成功登录并跳转到仪表板
- 验证：能看到所有商户、代理、订单数据

#### 代理门户 (https://agent.pivota.cc)
- Email: `agent@test.com`
- Password: `Admin123!`
- 预期：成功登录并跳转到仪表板
- 验证：能看到订单和分析数据

## 🔍 故障排查

### 如果仍然返回 "Invalid token"

1. **确认 Railway 部署已完成**
   ```bash
   # 检查部署的代码版本
   curl -s https://web-production-fedb.up.railway.app/openapi.json | \
     python3 -c "import sys, json; print('Merchant endpoints:', [p for p in json.load(sys.stdin).get('paths',{}).keys() if 'merchant/profile' in p])"
   ```

2. **验证 token 格式**
   - Token 必须包含 `sub`, `email`, `role` 字段
   - 如果缺少这些字段，说明部署未生效

3. **检查环境变量**
   - 确保 Railway 的 `JWT_SECRET_KEY` 与代码中的一致
   - 默认值：`your-secret-key-change-in-production`

### 如果前端显示 404

1. **检查前端环境变量**
   ```bash
   # merchant-portal/.env.local
   NEXT_PUBLIC_API_URL=https://web-production-fedb.up.railway.app
   ```

2. **重新部署前端**
   - 前端代码已更新，需要重新部署到 Vercel
   - 或者本地运行：`npm run dev`

## 📊 期望的演示数据

### 商户 (merchant@test.com)
- **Merchant ID**: merch_208139f7600dbf42
- **商店**: chydantest.myshopify.com (Shopify, 已连接)
- **PSP**: Stripe Account (活跃状态)
- **订单**: 10-50 个演示订单（随机生成）
- **Webhook**: 已配置，事件包括 order.created, payment.completed 等

### 员工 (employee@pivota.com)
- **角色**: admin
- **权限**: 访问所有商户、代理、订单数据
- **功能**: KYB 审核、商户管理、代理管理

### 代理 (agent@test.com)
- **角色**: agent
- **权限**: 访问分配的订单和分析数据
- **功能**: 查看订单、查看佣金

## 🚀 下一步

1. **等待 Railway 部署完成**（2-5 分钟）
2. **运行上面的测试命令验证后端**
3. **测试所有三个前端门户登录**
4. **如果遇到问题，提供错误信息**

---

**最后更新**: 2025-10-19 10:58 UTC
**后端部署**: 等待中
**前端部署**: 准备就绪

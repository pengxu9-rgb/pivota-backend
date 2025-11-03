# Vercel 部署问题排查指南

## 🔴 当前问题

**症状**: Git 推送成功，但前端没有更新
- 代码已提交到 GitHub ✅
- Vercel 显示"部署完成" ✅  
- 但页面内容没有变化 ❌

## 🔍 可能原因

### 1. Vercel 构建缓存问题
Vercel 可能使用了旧的构建缓存

### 2. GitHub → Vercel 集成断开
Webhook 没有正确触发

### 3. 部署到了错误的分支
检查 Vercel 是否部署的是 main 分支

## 🔧 解决方案

### 方案 1: Vercel Dashboard 手动重新部署（推荐）

1. 登录 Vercel Dashboard
2. 选择 `pivota-employee-portal` 项目
3. 进入 "Deployments" 标签
4. 找到最新部署（应该是 5c003ea 或 565403d 或 6dd26db）
5. 点击 "..." → "Redeploy"
6. 选择 "Use existing Build Cache" = **NO**（重要！）
7. 点击 "Redeploy"

### 方案 2: 检查 GitHub 集成

1. Vercel Dashboard → Settings → Git
2. 确认连接的仓库是: `pengxu9-rgb/pivota-employee-portal`
3. 确认分支是: `main`
4. 如果断开，重新连接 GitHub

### 方案 3: 删除 .next 缓存（需要重新部署）

在 Vercel 项目设置中：
1. Settings → General
2. Build & Development Settings
3. 添加 Build Command: `rm -rf .next && npm run build`
4. 保存后触发新部署

### 方案 4: 本地验证组件是否正确

```bash
cd pivota-employee-portal

# 检查组件文件
ls -la app/components/agents/QuickSetupRoutingButton.tsx
ls -la app/components/agents/AgentRoutingPanel.tsx

# 验证导入
grep "QuickSetupRoutingButton" app/components/agents/RoutingPolicyEditor.tsx
grep "AgentRoutingPanel" app/components/agents/AgentDetailPanel.tsx
```

## 🧪 验证测试

### 测试文字改动是否生效

我已经修改了一个明显的标题：
- **旧**: "Routing Policy Configuration"
- **新**: "Routing Policy (Simplified)"

**4分钟后刷新页面**，检查标题：
- ✅ 看到新标题 → Vercel 部署正常
- ❌ 还是旧标题 → Vercel 有问题

## 📊 确认清单

### GitHub 端（已确认 ✅）
- [x] 代码已提交
- [x] 已推送到 main 分支
- [x] 最新提交: 6dd26db
- [x] QuickSetupRoutingButton 文件存在
- [x] 组件已正确导入

### Vercel 端（待确认 ⏳）
- [ ] 部署状态 = Ready
- [ ] 部署的 commit = 6dd26db
- [ ] 构建日志无错误
- [ ] 缓存已清除

### 浏览器端（已测试 ✅）
- [x] 硬刷新 (Cmd+Shift+R)
- [x] 无痕模式
- [x] 清空缓存
- [ ] 完全关闭浏览器重开（最后手段）

## 💡 临时解决方案

如果 Vercel 问题持续，可以：

### 选项A: 本地运行验证

```bash
cd pivota-employee-portal
npm install
npm run dev
```

访问 http://localhost:3000 查看本地版本

### 选项B: 重新导入到 Vercel

1. 在 Vercel 删除当前项目
2. 重新从 GitHub 导入
3. 配置环境变量
4. 重新部署

## ⏰ 时间线

- **T+0**: 推送代码到 GitHub ✅
- **T+2**: Vercel 开始构建 ⏳
- **T+4**: 构建完成 ⏳
- **T+5**: CDN 更新 ⏳
- **T+6**: 用户可见更新 ⏳

**当前**: 可能在 T+2 到 T+5 之间

**建议**: 再等待 5 分钟，然后检查 Vercel Dashboard 的实际部署状态

---

**如果 10 分钟后还是没变化，请告诉我，我会协助检查 Vercel 配置。** 🔍

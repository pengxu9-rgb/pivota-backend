# 后端 Git 仓库解决方案

## 当前状态

1. **远程 URL 已更新**：从 `pivota-dashboard-1760371224` 更新到 `pivota-backend`
2. **分支分歧**：本地 566 个提交，远程 41 个提交
3. **前端项目正确地被 gitignore**

## 解决步骤

### 1. 添加实用工具脚本
```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344"

# 添加新创建的工具脚本
git add GIT_DIVERGENCE_FIX.md MERCHANT_LOGIN_FIX.md check_syntax.py create_merchant_user.sql create_user_account.py create_user_via_api.py find_merchant.sql fix_git_divergence.py fix_git_divergence.sh deploy_merchant_portal.sh BACKEND_GIT_SOLUTION.md test_merchant_login_flow.py

# 提交
git commit -m "docs: add utility scripts and documentation for user management and git fixes"
```

### 2. 解决分支分歧（选择一种方法）

#### 方法 A：使用 rebase（推荐）
```bash
# 获取最新远程更改
git fetch origin

# 使用 rebase 整合
git pull --rebase origin main

# 如果有冲突，解决后继续
git add .
git rebase --continue

# 推送
git push origin main
```

#### 方法 B：强制推送（谨慎使用）
```bash
# 如果你确定本地版本是正确的
git push --force origin main
```

### 3. 部署 Merchant Portal 修复

运行部署脚本：
```bash
chmod +x deploy_merchant_portal.sh
./deploy_merchant_portal.sh
```

或手动执行：
```bash
cd pivota-merchants-portal
git add lib/api-client.ts
git commit -m "fix: correct token storage logic for new API response format"
git push origin main
```

## 注意事项

1. **pivota-merchants-portal 被 gitignore 是正确的**
   - 它有自己的 Git 仓库
   - 应该独立管理和部署

2. **pivota-marketing 子模块**
   - 显示有修改，如果不需要可以忽略
   - 或运行 `git submodule update --init` 来更新

3. **测试脚本**
   - `test_*.py` 文件被 gitignore
   - 如果需要保留，可以重命名（如 `check_*.py`）

## 验证

部署完成后：
1. 访问 https://merchants.pivota.cc
2. 使用 merchant@test.com / Admin123! 登录
3. 确认可以正常进入 dashboard


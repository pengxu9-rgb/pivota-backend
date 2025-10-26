# 安全合并步骤

## 当前状态
- 本地已有登录修复：`if ((response.data.success === true || response.data.status === 'success') && response.data.token)`
- 远程仓库有其他更新，导致推送被拒绝

## 安全合并方案

### 1. 保存当前修改
```bash
git stash push -m "Login fix for api-client"
```

### 2. 拉取远程最新代码
```bash
git pull origin main
```

### 3. 应用我们的修改
```bash
git stash pop
```

### 4. 如果有冲突，检查 api-client.ts
查看文件中的冲突标记：
- `<<<<<<< HEAD` - 远程版本
- `=======` - 分隔线
- `>>>>>>> stashed changes` - 我们的版本

确保保留登录修复：
```typescript
if ((response.data.success === true || response.data.status === 'success') && response.data.token) {
```

### 5. 解决冲突后
```bash
git add lib/api-client.ts
git commit -m "fix: merge login fix with latest changes"
git push origin main
```

## 快速方案（如果你赶时间）

如果确定只需要修复登录问题：

```bash
# 拉取远程代码
git pull origin main --no-edit

# 再次确保登录修复存在
# 检查 lib/api-client.ts 第76行附近

# 如果修复丢失，重新添加
git add lib/api-client.ts
git commit -m "fix: ensure login works with new API response format"
git push origin main
```


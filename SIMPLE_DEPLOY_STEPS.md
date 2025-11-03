# 简单部署步骤

你现在已经在 `pivota-merchants-portal` 目录中了！

请依次执行以下命令：

## 1. 检查是否有 .git 目录
```bash
ls -la | grep git
```

如果没有看到 `.git`，执行：
```bash
git init
git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git
```

## 2. 查看当前状态
```bash
git status
```

## 3. 添加修改的文件
```bash
git add lib/api-client.ts
```

## 4. 提交
```bash
git commit -m "fix: correct token storage logic for new API response format"
```

## 5. 推送
如果是新仓库：
```bash
git branch -M main
git push -u origin main
```

如果已有仓库：
```bash
git push origin main
```

## 注意
- 忽略关于 "../pivota-marketing" 的提示，那是父目录的事情
- 专注于当前 merchant portal 的部署即可



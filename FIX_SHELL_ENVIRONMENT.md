# 修复 Shell 环境问题

## 问题诊断

看起来 Cursor 的 shell 环境配置有问题。错误 `spawn /bin/zsh ENOENT` 表示找不到 zsh。

## 解决方案

### 1. 检查你的默认 shell

在 macOS 终端中执行：
```bash
echo $SHELL
which zsh
ls -la /bin/zsh
```

### 2. 如果 zsh 在不同位置

macOS 的 zsh 可能在：
- `/bin/zsh` (旧版本)
- `/usr/bin/zsh` (新版本)
- `/usr/local/bin/zsh` (Homebrew)

### 3. Cursor 设置

在 Cursor 中：
1. 打开设置 (Cmd+,)
2. 搜索 "terminal"
3. 查看 "Terminal › Integrated › Shell: Osx"
4. 确保路径正确，例如：
   - `/bin/bash` (如果使用 bash)
   - `/usr/bin/zsh` (如果使用 zsh)

### 4. 临时解决方案

在系统终端中执行命令：

```bash
# 1. 打开 macOS 自带的终端 (Terminal.app)
# 2. 进入项目目录
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-merchants-portal"

# 3. 执行部署命令
git add -A
git commit -m "fix: merge and keep critical login fix"
git push origin main
```

### 5. 验证 Shell 路径

检查实际的 shell 路径：
```bash
# 在 macOS 终端中
ls -la /bin/zsh
ls -la /usr/bin/zsh
ls -la /usr/local/bin/zsh
```

### 6. 更新 Cursor 配置

如果发现 zsh 在不同位置，更新 Cursor 的设置：

1. 创建/编辑 `.vscode/settings.json` 或 `.cursor/settings.json`：
```json
{
  "terminal.integrated.shell.osx": "/usr/bin/zsh"
}
```

或者如果使用 bash：
```json
{
  "terminal.integrated.shell.osx": "/bin/bash"
}
```

## 立即行动

由于 shell 问题阻碍了自动化，建议：

1. **使用系统终端**完成当前的 git 操作
2. **之后修复** Cursor 的 shell 配置

这样可以先部署登录修复，然后再解决工具问题。


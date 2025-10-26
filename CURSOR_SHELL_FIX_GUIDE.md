# Cursor Shell 修复指南

## 问题
Cursor 尝试使用 `/bin/zsh` 但系统报错 ENOENT（文件不存在）

## 解决方法

### 1. 检查实际 Shell 位置
在 macOS 系统终端（Terminal.app）中运行：
```bash
ls -la /bin/bash
ls -la /bin/zsh
ls -la /usr/bin/zsh
ls -la /usr/local/bin/zsh
```

### 2. 修改 Cursor 全局设置

#### 方法 A：通过 UI
1. 在 Cursor 中，点击左下角的设置图标 ⚙️
2. 选择 "Settings"
3. 在搜索框输入：`terminal.integrated.shell.osx`
4. 修改为存在的路径（如 `/bin/bash`）

#### 方法 B：通过命令面板
1. 按 `Cmd+Shift+P` 打开命令面板
2. 输入 "Preferences: Open Settings (JSON)"
3. 添加以下配置：
```json
{
  "terminal.integrated.shell.osx": "/bin/bash"
}
```

### 3. 创建软链接（备选方案）
如果 zsh 在不同位置，可以创建软链接：
```bash
# 需要管理员权限
sudo ln -sf /usr/bin/zsh /bin/zsh
```

### 4. 使用外部终端
如果 Cursor 内置终端仍有问题：
1. 右键点击文件或文件夹
2. 选择 "Open in External Terminal"
3. 或使用 iTerm2 等第三方终端

### 5. 环境变量检查
确保 PATH 正确：
```bash
echo $PATH
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
```

## 临时解决方案

在修复期间，使用 macOS 系统终端完成 git 操作：

1. 打开 Terminal.app
2. 导航到项目：
   ```bash
   cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-merchants-portal"
   ```
3. 执行需要的命令

## 验证修复

修复后，在 Cursor 终端中测试：
```bash
echo $SHELL
which bash
which zsh
```

如果显示正确路径，说明修复成功。


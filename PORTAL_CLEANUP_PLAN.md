# Portal Directories Cleanup Plan

## 🎯 正确的Portal目录

根据检查，这些是**应该保留**的：

### 1. **Agents Portal** ✅
- **保留**: `pivota-agents-portal/`
- **删除**: `pivota-agents-portal 2/`
- **Git Repo**: https://github.com/pengxu9-rgb/pivota-agents-portal
- **Vercel**: https://agents.pivota.cc

### 2. **Employee Portal** ✅  
- **保留**: `pivota-employee-portal 3/` → 重命名为 `pivota-employee-portal/`
- **删除**: `pivota-employee-portal 2/` (只有一个api-client.ts文件)
- **Git Repo**: https://github.com/pengxu9-rgb/pivota-employee-portal
- **Vercel**: https://employee.pivota.cc
- **结构**: `app/` (Next.js App Router)

### 3. **Merchants Portal** ✅
- **保留**: `pivota-merchants-portal/`
- **删除**: `pivota-merchants-portal 2/`
- **Git Repo**: https://github.com/pengxu9-rgb/pivota-merchants-portal
- **Vercel**: https://merchant.pivota.cc

## 🧹 清理步骤

```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344"

# 删除残留目录
rm -rf "pivota-agents-portal 2"
rm -rf "pivota-employee-portal 2"
rm -rf "pivota-merchants-portal 2"

# 重命名employee portal 3为正确名称
mv "pivota-employee-portal 3" "pivota-employee-portal"

# 验证
echo "✅ Remaining portals:"
ls -d pivota-*portal
```

## 📊 为什么会有多个目录？

可能的原因：
1. Git操作过程中的临时copy
2. 重命名操作没有完全完成
3. 多次尝试修复创建了备份

## ✅ 清理后的最终结构

```
Pivota-cursor-create-project-directory-structure-8344/
├── pivota-agents-portal/       # Agents signup & dashboard
├── pivota-employee-portal/     # Employee internal tools
├── pivota-merchants-portal/    # Merchant signup & dashboard
├── pivota-marketing/           # Marketing website (pivota.cc)
└── pivota_infra/              # Backend API (Railway)
```

## 🚀 清理完成后

1. **验证git remotes都正确**
2. **确认Vercel部署的是正确的repo**
3. **测试所有portal正常工作**
4. **添加UI改进**（产品数量列、分离按钮等）

---

**⚠️ 请先运行清理脚本，然后我再继续添加UI改进！**






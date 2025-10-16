# ✅ Merchants Tab 已准备好测试！

## 🎉 完成的工作

### **1. 统一的 Merchants 标签页**
- ✅ 移除了重复的 "Legacy" 和 "Phase 2" 部分
- ✅ 现在只有一个干净、统一的商户列表
- ✅ 所有商户显示在同一个视图中

### **2. 完整的功能集成**
每个商户现在都有完整的操作按钮：
- 📤 **Upload Docs** - 上传 KYB 文档
- 👁️ **Review KYB** - 审核 KYB 文档
- 📄 **Details** - 查看详细信息
- 🗑️ **Remove** - 删除商户

### **3. 简化的界面**
- ✅ 只保留了一个按钮: **"+ Merchant Onboarding Portal"**
- ✅ 移除了不必要的 "Onboard New Merchant" 按钮
- ✅ 更清晰的布局

---

## 🧪 如何测试

### **快速启动**

1. **前端已经启动** (正在后台运行)
   ```
   URL: http://localhost:5173
   ```

2. **后端已部署**
   ```
   URL: https://pivota-dashboard.onrender.com
   ```

### **测试步骤**

1. **打开浏览器访问**: http://localhost:5173
2. **登录**管理员账号
3. **点击** "Merchants" 标签
4. **验证**:
   - ✅ 只有一个商户列表（没有分成两个部分）
   - ✅ 顶部只有一个按钮: **"+ Merchant Onboarding Portal"**
   - ✅ 每个商户卡片有功能按钮网格 (2x2):
     - Upload Docs | Review KYB
     - Details | Remove
   - ✅ Pending 商户额外有 Approve/Reject 按钮
   - ✅ Approved 未连接 PSP 的商户有 "Resume PSP Setup" 按钮

5. **测试每个按钮**:
   - 点击 **Upload Docs** → 应该打开上传模态框
   - 点击 **Review KYB** → 应该打开审核模态框
   - 点击 **Details** → 应该打开详情模态框
   - 点击 **Remove** → 应该弹出确认对话框

6. **测试筛选**:
   - 切换 All | Pending Verification | Approved | Rejected 标签
   - 验证商户列表根据状态正确过滤

---

## 📋 完整测试清单

详细的测试步骤和截图参考请查看:
```
MERCHANT_TAB_TEST.md
```

---

## 🔄 接下来

测试完 Merchants tab 后，我们可以讨论 **Phase 3 的测试计划**。

### **Phase 3 状态回顾**
- ✅ `/payment/execute` 端点已实现
- ✅ 商户 API key 认证已完成
- ✅ PSP 路由逻辑已完成 (Stripe & Adyen)
- ⚠️ 由于 Supabase pgbouncer 限制，暂时无法在线测试

### **Phase 3 测试选项**

我们可以讨论：
1. 是否切换到不同的数据库服务（以测试 Phase 3）
2. 或者继续开发其他功能，稍后再解决数据库问题
3. 或者使用本地 PostgreSQL 测试 Phase 3

---

## 📞 需要帮助？

如果在测试过程中遇到任何问题：
1. 检查浏览器控制台（F12）的错误信息
2. 查看网络请求（F12 → Network tab）
3. 检查后端日志

---

**当前时间**: 测试准备就绪 ✅
**前端**: http://localhost:5173 (正在运行)
**后端**: https://pivota-dashboard.onrender.com (已部署)


# 🧪 Merchant Tab Merge - 测试指南

## ✅ 完成的更改

### 1. **统一的 Merchants 标签页**
- ✅ 移除了重复的 "Merchants (Legacy)" 和 "Onboarding (Phase 2)" 两个部分
- ✅ 现在只有一个统一的商户列表
- ✅ 所有商户（Phase 2 onboarded）都显示在同一个视图中

### 2. **简化的操作按钮**
- ✅ 移除了 "Onboard New Merchant" 按钮（不需要，因为有专门的 portal）
- ✅ 保留了 **"+ Merchant Onboarding Portal"** 按钮 - 打开完整的注册流程

### 3. **完整的功能集成**
为每个商户添加了以下操作按钮：
- ✅ **Upload Docs** - 上传 KYB 文档
- ✅ **Review KYB** - 审核 KYB 文档
- ✅ **Details** - 查看商户详情
- ✅ **Remove** - 删除商户（软删除）

### 4. **保留的原有功能**
- ✅ **Approve/Reject** 按钮（仅对 pending_verification 状态显示）
- ✅ **Resume PSP Setup** 按钮（对已批准但未连接 PSP 的商户显示）
- ✅ 状态筛选标签（All, Pending Verification, Approved, Rejected）

---

## 🧪 测试步骤

### **Step 1: 检查前端是否启动**

```bash
# 检查前端进程
ps aux | grep "npm run dev"

# 如果没有运行，启动前端
cd simple_frontend && npm run dev
```

### **Step 2: 访问 Admin Dashboard**

1. 打开浏览器访问: http://localhost:5173
2. 使用管理员账号登录
3. 点击 **"Merchants"** 标签

### **Step 3: 验证界面布局**

**应该看到:**
- ✅ 页面顶部只有一个按钮: **"+ Merchant Onboarding Portal"**
- ✅ 一个统一的商户列表（没有分成两个部分）
- ✅ 筛选标签: All | Pending Verification | Approved | Rejected

**不应该看到:**
- ❌ "Merchant Management" 下方不应该有 "📋 Merchant Onboarding (Phase 2)" 子标题
- ❌ 不应该有 "🏪 Configured Stores (Legacy)" 部分
- ❌ 不应该有 "Onboard New Merchant" 按钮

### **Step 4: 测试功能按钮**

对于**每个商户卡片**，验证以下按钮是否存在：

#### **4.1 Pending Verification 状态的商户**
应该显示：
- ✅ **Approve** 按钮（绿色，带 ✓ 图标）
- ✅ **Reject** 按钮（红色，带 ✗ 图标）
- ✅ **Upload Docs** 按钮
- ✅ **Review KYB** 按钮
- ✅ **Details** 按钮
- ✅ **Remove** 按钮

#### **4.2 Approved 但未连接 PSP 的商户**
应该显示：
- ✅ 蓝色提示框: "ℹ️ Waiting for merchant to connect PSP"
- ✅ **Resume PSP Setup** 按钮（全宽，蓝色）
- ✅ **Upload Docs** 按钮
- ✅ **Review KYB** 按钮
- ✅ **Details** 按钮
- ✅ **Remove** 按钮

#### **4.3 Approved 且已连接 PSP 的商户**
应该显示：
- ✅ PSP 连接状态（绿色 ✓ + PSP 类型）
- ✅ **Upload Docs** 按钮
- ✅ **Review KYB** 按钮
- ✅ **Details** 按钮
- ✅ **Remove** 按钮

### **Step 5: 测试每个按钮的功能**

#### **5.1 测试 "Upload Docs" 按钮**
1. 点击任意商户的 **Upload Docs** 按钮
2. **预期**: 应该打开文档上传模态框
3. 验证可以选择文件并上传

#### **5.2 测试 "Review KYB" 按钮**
1. 点击任意商户的 **Review KYB** 按钮
2. **预期**: 应该打开 KYB 审核模态框
3. 验证显示商户信息和已上传的文档

#### **5.3 测试 "Details" 按钮**
1. 点击任意商户的 **Details** 按钮
2. **预期**: 应该打开商户详情模态框
3. 验证显示完整的商户信息

#### **5.4 测试 "Remove" 按钮**
1. 点击任意商户的 **Remove** 按钮
2. **预期**: 应该弹出确认对话框
3. 确认后，商户应该从列表中消失

#### **5.5 测试 "Approve" 按钮** (仅 pending 状态)
1. 找到一个 pending_verification 状态的商户
2. 点击 **Approve** 按钮
3. **预期**: 
   - 弹出确认对话框
   - 确认后，商户状态应该变为 "approved"
   - 应该显示 "Waiting for PSP" 提示

#### **5.6 测试 "Resume PSP Setup" 按钮**
1. 找到一个 approved 但未连接 PSP 的商户
2. 点击 **Resume PSP Setup** 按钮
3. **预期**: 应该跳转到 `/merchant/onboarding` 页面，并自动加载该商户的信息

#### **5.7 测试 "+ Merchant Onboarding Portal" 按钮**
1. 点击页面顶部的 **+ Merchant Onboarding Portal** 按钮
2. **预期**: 应该在新标签页中打开 `/merchant/onboarding` 页面

### **Step 6: 测试筛选功能**

1. 点击 **"Pending Verification"** 标签
   - **预期**: 只显示状态为 pending_verification 的商户

2. 点击 **"Approved"** 标签
   - **预期**: 只显示状态为 approved 的商户

3. 点击 **"Rejected"** 标签
   - **预期**: 只显示状态为 rejected 的商户

4. 点击 **"All"** 标签
   - **预期**: 显示所有商户

### **Step 7: 测试空状态**

如果没有商户数据：
1. **预期**: 应该显示空状态消息
2. 应该有一个输入框和 "Resume PSP Setup" 按钮
3. 可以输入 merchant_id 并恢复 PSP 设置流程

---

## 📸 预期的 UI 截图参考

### **页面顶部**
```
┌─────────────────────────────────────────────────────────┐
│ Merchant Management         [+ Merchant Onboarding Portal] │
└─────────────────────────────────────────────────────────┘
```

### **筛选标签**
```
┌─────────────────────────────────────────────────────────┐
│ All | Pending Verification | Approved | Rejected          │
└─────────────────────────────────────────────────────────┘
```

### **商户卡片布局 (Approved + PSP Connected)**
```
┌─────────────────────────────────────────────────────────┐
│ My Store                                    [approved]    │
│ ID: merch_abc123                                          │
│                                                           │
│ PSP: ✓ stripe                                            │
│ 🕒 Created: 10/15/2025                                    │
│                                                           │
│ [Upload Docs]  [Review KYB]                              │
│ [Details]      [Remove]                                   │
└─────────────────────────────────────────────────────────┘
```

### **商户卡片布局 (Pending Verification)**
```
┌─────────────────────────────────────────────────────────┐
│ New Store                        [pending_verification]   │
│ ID: merch_xyz789                                          │
│                                                           │
│ PSP: ✗ Not connected                                     │
│ 🕒 Created: 10/16/2025                                    │
│                                                           │
│ [✓ Approve]    [✗ Reject]                                │
│ [Upload Docs]  [Review KYB]                              │
│ [Details]      [Remove]                                   │
└─────────────────────────────────────────────────────────┘
```

---

## ✅ 测试清单

### **视觉验证**
- [ ] 只有一个商户列表（没有分成两个部分）
- [ ] 只有一个顶部按钮 **"+ Merchant Onboarding Portal"**
- [ ] 每个商户卡片有 4 个功能按钮（2x2 网格）
- [ ] Pending 商户额外显示 Approve/Reject 按钮
- [ ] Approved 未连接 PSP 的商户显示 "Resume PSP Setup" 按钮

### **功能验证**
- [ ] Upload Docs 按钮打开上传模态框
- [ ] Review KYB 按钮打开审核模态框
- [ ] Details 按钮打开详情模态框
- [ ] Remove 按钮可以删除商户
- [ ] Approve 按钮可以批准商户
- [ ] Reject 按钮可以拒绝商户
- [ ] Resume PSP Setup 按钮跳转到正确页面
- [ ] Merchant Onboarding Portal 按钮打开新标签页

### **筛选验证**
- [ ] All 标签显示所有商户
- [ ] Pending Verification 标签只显示 pending 商户
- [ ] Approved 标签只显示 approved 商户
- [ ] Rejected 标签只显示 rejected 商户

---

## 🐛 已知问题

### **如果遇到以下问题:**

1. **按钮没有显示**
   - 检查是否传递了回调函数 props
   - 检查浏览器控制台是否有错误

2. **模态框没有打开**
   - 检查 `selectedMerchant` 状态是否正确设置
   - 检查模态框组件的 `isOpen` prop

3. **Remove 功能失败**
   - 检查 merchant_id 格式是否正确
   - 查看网络请求是否成功（F12 Network tab）

---

## 📝 下一步

测试完成后，我们可以继续讨论 Phase 3 的测试计划。

---

**当前状态**: ✅ 代码已部署，等待测试
**测试 URL**: http://localhost:5173 (前端) + https://pivota-dashboard.onrender.com (后端)


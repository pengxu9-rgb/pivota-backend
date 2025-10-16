# 🎨 Lovable Dashboard Setup Guide

## ✅ What You Have

Your Railway API is live at: `https://web-production-fedb.up.railway.app`

API is working perfectly:
- ✅ Merchant registration
- ✅ Auto-approval logic
- ✅ Database persistence
- ✅ Authentication

---

## 📋 Steps to Build in Lovable

### **Step 1: Create Project**

1. Go to **https://lovable.dev**
2. Click **"New Project"**
3. Choose **"React + TypeScript + Tailwind"** template
4. Name: `Pivota Admin Dashboard`

---

### **Step 2: Add the Component**

1. In Lovable, create a new file: `src/pages/MerchantDashboard.tsx`
2. Copy the entire content from: `lovable-components/MerchantDashboard.tsx` (in this repo)
3. Paste it into Lovable

---

### **Step 3: Update App Router**

Edit `src/App.tsx`:

\`\`\`tsx
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import MerchantDashboard from './pages/MerchantDashboard';
import { Toaster } from '@/components/ui/toaster';

function App() {
  return (
    <Router>
      <Routes>
        <Route path="/" element={<MerchantDashboard />} />
      </Routes>
      <Toaster />
    </Router>
  );
}

export default App;
\`\`\`

---

### **Step 4: Environment Variables**

In Lovable, go to **Settings** → **Environment Variables**:

Add:
\`\`\`
VITE_API_BASE_URL=https://web-production-fedb.up.railway.app
\`\`\`

---

### **Step 5: Deploy**

1. Lovable will auto-deploy on save
2. You'll get a URL like: `https://your-project.lovable.app`

---

## 🔐 How to Use

### **Get Admin JWT Token**

You need to generate an admin JWT token first. Here's how:

#### **Option A: Use API Docs (Recommended)**

1. Go to: `https://web-production-fedb.up.railway.app/docs`
2. Find the `/auth/login` endpoint
3. Click "Try it out"
4. Use credentials:
   \`\`\`json
   {
     "email": "superadmin@pivota.com",
     "password": "admin123"
   }
   \`\`\`
5. Copy the JWT token from the response

#### **Option B: Use curl**

\`\`\`bash
curl -X POST "https://web-production-fedb.up.railway.app/auth/login" \\
  -H "Content-Type: application/json" \\
  -d '{"email":"superadmin@pivota.com","password":"admin123"}'
\`\`\`

Copy the `access_token` from the response.

---

### **Using the Dashboard**

1. Open your Lovable app
2. **Paste the JWT token** in the "Admin JWT Token" field at the top
3. Click **"Refresh"** to load merchants
4. The token is saved in localStorage - you only need to paste it once!

---

## 🎨 Features

Your dashboard includes:

✅ **Real-time Stats**
- Total merchants
- Auto-approved count
- Pending review count

✅ **Merchant Table**
- View all merchant details
- Filter by status (All, Approved, Pending, Rejected)
- Search by name, email, or merchant ID
- See auto-approval status and confidence scores

✅ **Quick Actions**
- Link to merchant onboarding portal
- Refresh data on demand
- Responsive design

---

## 🚀 Next Steps (Optional Enhancements)

Want to add more features? You can enhance with:

1. **Merchant Details Modal**
   - Click row to view full details
   - See KYC documents
   - PSP connection status

2. **KYB Review**
   - Approve/reject merchants
   - Add rejection reasons
   - Upload documents

3. **Real-time Updates**
   - WebSocket connection
   - Auto-refresh

Let me know if you want me to add any of these! 🎯

---

## 📞 Need Help?

If you encounter issues:

1. **Check JWT token** - Make sure it's valid and not expired
2. **Check Railway API** - Verify it's running: https://web-production-fedb.up.railway.app/
3. **Check Browser Console** - Look for error messages
4. **CORS issues?** - Make sure Railway allows your Lovable domain

---

## ✨ You're Done!

Once deployed, you'll have a beautiful, modern admin dashboard connected to your Railway API! 🎉


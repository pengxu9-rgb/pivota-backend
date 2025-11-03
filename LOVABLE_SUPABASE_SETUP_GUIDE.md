# 🎨 Lovable Frontend Setup with Supabase Integration

## 📋 **Quick Setup Guide**

### 1. **Copy Components to Lovable**

Copy these files to your Lovable project:

```
lovable_components/
├── supabase_client.ts     # Supabase client configuration
├── useAuth.ts            # Authentication hook
├── Auth.tsx              # Login/Signup component
├── AdminDashboard.tsx    # Admin approval dashboard
└── package.json          # Required dependencies
```

### 2. **Install Dependencies in Lovable**

In your Lovable project, install these packages:

```bash
npm install @supabase/supabase-js sonner lucide-react
```

### 3. **Set Environment Variables in Lovable**

Add these environment variables to your Lovable project:

```env
VITE_SUPABASE_URL=https://jagukdffqmlnktrmionh.supabase.co
VITE_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImphZ3VrZGZmcW1sbmt0cm1pb25oIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjA0MzA1MTcsImV4cCI6MjA3NjAwNjUxN30.k5XqTSa4MsqwEdetFDw0rgNZln7hM6feJOhfPKdgD84
VITE_API_URL=https://your-render-app.onrender.com
```

**Important:** Replace `https://your-render-app.onrender.com` with your actual Render backend URL.

### 4. **Update Your Main App Component**

Replace your main App component with:

```tsx
import React from 'react';
import { AuthProvider } from './lovable_components/useAuth';
import Auth from './lovable_components/Auth';
import AdminDashboard from './lovable_components/AdminDashboard';
import { useAuth } from './lovable_components/useAuth';

function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  );
}

function AppContent() {
  const { user, loading } = useAuth();

  if (loading) {
    return <div>Loading...</div>;
  }

  if (!user) {
    return <Auth />;
  }

  if (user.role === 'admin') {
    return <AdminDashboard />;
  }

  return (
    <div>
      <h1>Welcome, {user.email}!</h1>
      <p>Role: {user.role}</p>
      <p>Status: {user.approved ? 'Approved' : 'Pending Approval'}</p>
    </div>
  );
}

export default App;
```

## 🧪 **Testing Your Setup**

### 1. **Test User Registration**
- Go to your Lovable app
- Click "Sign up"
- Create a new account with role "employee"
- Check Supabase dashboard for new user

### 2. **Test Admin Approval**
- Sign in as admin
- Go to Admin Dashboard
- Approve/reject pending users
- Change user roles

### 3. **Test Real-time Updates**
- Open multiple browser tabs
- Make changes in one tab
- See updates in other tabs (real-time!)

## 🔧 **Troubleshooting**

### **Common Issues:**

1. **"Supabase not configured" error**
   - Check environment variables in Lovable
   - Ensure VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY are set

2. **"Failed to load user role" error**
   - Check if user exists in Supabase auth
   - Verify user_roles table has the user

3. **Real-time not working**
   - Check Supabase RLS policies
   - Ensure user has proper permissions

### **Environment Variables Checklist:**

- ✅ `VITE_SUPABASE_URL` - Your Supabase project URL
- ✅ `VITE_SUPABASE_ANON_KEY` - Your Supabase anon key
- ✅ `VITE_API_URL` - Your Render backend URL

## 🚀 **What You Get:**

- **✅ User Authentication** - Sign up, sign in, sign out
- **✅ Role Management** - Employee, Agent, Merchant, Operator, Admin
- **✅ Admin Approval System** - Approve/reject users
- **✅ Real-time Updates** - Live data synchronization
- **✅ Global Deployment** - Works worldwide

## 📞 **Support:**

If you encounter issues:
1. Check Lovable console for errors
2. Verify environment variables
3. Test Supabase connection
4. Check Render backend logs

**Ready to deploy your global payment infrastructure!** 🌍

# 🚀 Lovable Frontend Integration Guide

## ✅ Backend is Ready!

Your FastAPI backend is fully deployed and working at:
**https://pivota-dashboard.onrender.com**

All authentication endpoints are tested and functional!

---

## 📋 Step-by-Step Integration

### **Step 1: Set Environment Variables in Lovable**

In your Lovable project, add these environment variables:

```env
VITE_API_URL=https://pivota-dashboard.onrender.com
VITE_SUPABASE_URL=https://jagukdffqmlnktrmionh.supabase.co
VITE_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImphZ3VrZGZmcW1sbmt0cm1pb25oIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjA0MzA1MTcsImV4cCI6MjA3NjAwNjUxN30.k5XqTSa4MsqwEdetFDw0rgNZln7hM6feJOhfPKdgD84
```

### **Step 2: Create API Client**

Create a file `src/lib/apiClient.ts`:

```typescript
const API_URL = import.meta.env.VITE_API_URL || 'https://pivota-dashboard.onrender.com';

export const apiClient = {
  async signup(email: string, password: string, fullName?: string) {
    const response = await fetch(`${API_URL}/auth/signup`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ email, password, full_name: fullName }),
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Signup failed');
    }
    
    return response.json();
  },

  async signin(email: string, password: string) {
    const response = await fetch(`${API_URL}/auth/signin`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ email, password }),
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Signin failed');
    }
    
    return response.json();
  },

  async getMe(token: string) {
    const response = await fetch(`${API_URL}/auth/me`, {
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Failed to get user');
    }
    
    return response.json();
  },

  async getAdminUsers(token: string) {
    const response = await fetch(`${API_URL}/auth/admin/users`, {
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Failed to get users');
    }
    
    return response.json();
  },

  async approveUser(token: string, userId: string) {
    const response = await fetch(`${API_URL}/auth/admin/users/${userId}/approve`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ approved: true }),
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Failed to approve user');
    }
    
    return response.json();
  },

  async updateUserRole(token: string, userId: string, role: string) {
    const response = await fetch(`${API_URL}/auth/admin/users/${userId}/role`, {
      method: 'PUT',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ role }),
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Failed to update role');
    }
    
    return response.json();
  },
};
```

### **Step 3: Update useAuth Hook**

Update your `src/hooks/useAuth.ts`:

```typescript
import { useState, useEffect } from 'react';
import { apiClient } from '@/lib/apiClient';

interface User {
  id: string;
  email: string;
  full_name?: string;
  role: string;
}

export const useAuth = () => {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Check for stored token on mount
    const storedToken = localStorage.getItem('auth_token');
    if (storedToken) {
      setToken(storedToken);
      loadUser(storedToken);
    } else {
      setLoading(false);
    }
  }, []);

  const loadUser = async (authToken: string) => {
    try {
      const response = await apiClient.getMe(authToken);
      setUser(response.user);
    } catch (error) {
      console.error('Failed to load user:', error);
      // Token might be expired, clear it
      localStorage.removeItem('auth_token');
      setToken(null);
    } finally {
      setLoading(false);
    }
  };

  const signUp = async (email: string, password: string, fullName?: string) => {
    try {
      const response = await apiClient.signup(email, password, fullName);
      return response;
    } catch (error: any) {
      throw new Error(error.message || 'Signup failed');
    }
  };

  const signIn = async (email: string, password: string) => {
    try {
      const response = await apiClient.signin(email, password);
      const authToken = response.token;
      
      // Store token
      localStorage.setItem('auth_token', authToken);
      setToken(authToken);
      
      // Load user data
      await loadUser(authToken);
      
      return response;
    } catch (error: any) {
      throw new Error(error.message || 'Signin failed');
    }
  };

  const signOut = () => {
    localStorage.removeItem('auth_token');
    setToken(null);
    setUser(null);
  };

  return {
    user,
    token,
    loading,
    signUp,
    signIn,
    signOut,
    isAuthenticated: !!user,
    isAdmin: user?.role === 'admin',
  };
};
```

### **Step 4: Update Auth Component**

Update your `src/components/Auth.tsx`:

```typescript
import { useState } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';

export const Auth = () => {
  const [isSignUp, setIsSignUp] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [loading, setLoading] = useState(false);

  const { signUp, signIn } = useAuth();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccess('');
    setLoading(true);

    try {
      if (isSignUp) {
        await signUp(email, password, fullName);
        setSuccess('Account created! Awaiting admin approval.');
        setEmail('');
        setPassword('');
        setFullName('');
      } else {
        await signIn(email, password);
        setSuccess('Login successful!');
      }
    } catch (err: any) {
      setError(err.message || 'Authentication failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>{isSignUp ? 'Sign Up' : 'Sign In'}</CardTitle>
          <CardDescription>
            {isSignUp
              ? 'Create a new account. Admin approval required.'
              : 'Sign in to your account'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            {isSignUp && (
              <div>
                <label className="block text-sm font-medium mb-1">Full Name</label>
                <Input
                  type="text"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  placeholder="John Doe"
                />
              </div>
            )}
            
            <div>
              <label className="block text-sm font-medium mb-1">Email</label>
              <Input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                required
              />
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Password</label>
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
              />
            </div>

            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            {success && (
              <Alert>
                <AlertDescription>{success}</AlertDescription>
              </Alert>
            )}

            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? 'Loading...' : isSignUp ? 'Sign Up' : 'Sign In'}
            </Button>

            <Button
              type="button"
              variant="ghost"
              className="w-full"
              onClick={() => setIsSignUp(!isSignUp)}
            >
              {isSignUp
                ? 'Already have an account? Sign In'
                : "Don't have an account? Sign Up"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
};
```

### **Step 5: Update Admin Dashboard**

Update your `src/components/AdminDashboard.tsx` to use the API client:

```typescript
import { useEffect, useState } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { apiClient } from '@/lib/apiClient';
import { Button } from '@/components/ui/button';

interface PendingUser {
  id: string;
  user_id: string;
  email: string;
  role: string;
  approved: boolean;
  created_at: string;
}

export const AdminDashboard = () => {
  const { user, token, signOut, isAdmin } = useAuth();
  const [pendingUsers, setPendingUsers] = useState<PendingUser[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (token && isAdmin) {
      loadPendingUsers();
    }
  }, [token, isAdmin]);

  const loadPendingUsers = async () => {
    if (!token) return;
    
    try {
      const response = await apiClient.getAdminUsers(token);
      setPendingUsers(response.users || []);
    } catch (error) {
      console.error('Failed to load users:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (userId: string) => {
    if (!token) return;
    
    try {
      await apiClient.approveUser(token, userId);
      await loadPendingUsers();
    } catch (error) {
      console.error('Failed to approve user:', error);
    }
  };

  if (!isAdmin) {
    return (
      <div className="p-8">
        <h1 className="text-2xl font-bold mb-4">Access Denied</h1>
        <p>You do not have admin privileges.</p>
      </div>
    );
  }

  return (
    <div className="p-8">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold">Admin Dashboard</h1>
        <Button onClick={signOut} variant="outline">Sign Out</Button>
      </div>

      <div className="mb-6">
        <h2 className="text-xl font-semibold mb-2">Current User</h2>
        <p>Email: {user?.email}</p>
        <p>Role: {user?.role}</p>
      </div>

      <div>
        <h2 className="text-xl font-semibold mb-4">Pending User Approvals</h2>
        {loading ? (
          <p>Loading...</p>
        ) : pendingUsers.length === 0 ? (
          <p>No pending users</p>
        ) : (
          <div className="space-y-4">
            {pendingUsers.map((pendingUser) => (
              <div key={pendingUser.id} className="border p-4 rounded">
                <p><strong>Email:</strong> {pendingUser.email}</p>
                <p><strong>Role:</strong> {pendingUser.role}</p>
                <p><strong>Status:</strong> {pendingUser.approved ? 'Approved' : 'Pending'}</p>
                {!pendingUser.approved && (
                  <Button
                    onClick={() => handleApprove(pendingUser.user_id)}
                    className="mt-2"
                  >
                    Approve
                  </Button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
```

---

## 🎯 **Testing the Integration**

### **1. Test Signup:**
- Go to your Lovable app
- Click "Sign Up"
- Enter email and password
- You should see: "Account created! Awaiting admin approval."

### **2. Test Signin (Unapproved):**
- Try to sign in with the new account
- You should see: "Account pending admin approval"

### **3. Approve User in Supabase:**
- Go to Supabase SQL Editor
- Run: `UPDATE user_roles SET approved = true WHERE user_id = 'USER_ID';`

### **4. Test Signin (Approved):**
- Sign in again
- You should be logged in successfully!

### **5. Test Admin Dashboard:**
- Sign in with admin account
- You should see the admin dashboard
- View pending users
- Approve/reject users

---

## 🚀 **You're All Set!**

Your Lovable frontend is now connected to your FastAPI backend with:
- ✅ User signup
- ✅ User signin
- ✅ JWT authentication
- ✅ Admin approval workflow
- ✅ Role-based access control

**Need help?** Check the `ADMIN_TESTING_COMPLETE.md` file for backend endpoint details!




import React, { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { authApi } from '../services/api';

interface User {
  id: string;
  email: string;
  role: string;
  approved: boolean;
}

interface AuthContextType {
  user: User | null;
  loading: boolean;
  signin: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string, role?: string) => Promise<void>;
  signout: () => void;
  isAdmin: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

interface AuthProviderProps {
  children: ReactNode;
}

export const AuthProvider: React.FC<AuthProviderProps> = ({ children }) => {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    checkAuth();
  }, []);

  const checkAuth = async () => {
    try {
      const token = localStorage.getItem('auth_token');
      if (token) {
        const response = await authApi.me();
        console.log('🔍 /me response:', response);
        // Handle both response formats: {user: {...}} or direct user object
        const userData = response.user || response;
        console.log('✅ Setting user:', userData);
        setUser(userData);
      }
    } catch (error) {
      console.error('Auth check failed:', error);
      localStorage.removeItem('auth_token');
    } finally {
      setLoading(false);
    }
  };

  const signin = async (email: string, password: string) => {
    try {
      console.log('🔐 Attempting signin with:', { email });
      console.log('⏰ Starting signin request at:', new Date().toISOString());
      
      const response = await authApi.signin(email, password);
      
      console.log('✅ Signin response received at:', new Date().toISOString());
      console.log('✅ Signin response:', response);
      
      // Handle both token formats (access_token or token)
      const token = response.access_token || response.token;
      if (!token) {
        throw new Error('No token received from server');
      }
      
      localStorage.setItem('auth_token', token);
      setUser(response.user);
      console.log('✅ User set:', response.user);
    } catch (error: any) {
      console.error('❌ Signin failed at:', new Date().toISOString());
      console.error('❌ Full error object:', error);
      console.error('❌ Error details:', {
        message: error.message,
        name: error.name,
        code: error.code,
        responseStatus: error.response?.status,
        responseData: error.response?.data,
        responseDataDetail: error.response?.data?.detail,
      });
      
      // Provide user-friendly error messages
      if (error.response?.status === 403) {
        throw new Error(error.response.data?.detail || 'Account pending admin approval');
      } else if (error.response?.status === 401) {
        throw new Error(error.response.data?.detail || 'Invalid credentials');
      } else if (error.code === 'ECONNABORTED' || error.message?.includes('timeout')) {
        throw new Error('Request timed out. Please try again.');
      } else {
        throw error;
      }
    }
  };

  const signup = async (email: string, password: string, role: string = 'employee') => {
    try {
      const response = await authApi.signup(email, password, role);
      localStorage.setItem('auth_token', response.access_token);
      setUser(response.user);
    } catch (error) {
      console.error('Signup failed:', error);
      throw error;
    }
  };

  const signout = () => {
    localStorage.removeItem('auth_token');
    setUser(null);
  };

  const isAdmin = user?.role === 'admin';

  const value: AuthContextType = {
    user,
    loading,
    signin,
    signup,
    signout,
    isAdmin,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

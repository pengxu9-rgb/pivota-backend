import { useState, useEffect, createContext, useContext } from 'react';
import { toast } from 'sonner';

export type UserRole = 'employee' | 'agent' | 'merchant' | 'operator' | 'admin';

interface User {
  id: string;
  email: string;
  role: UserRole;
  approved?: boolean;
  full_name?: string;
}

interface AuthContextType {
  user: User | null;
  loading: boolean;
  token: string | null;
  signIn: (email: string, password: string) => Promise<{ error: Error | null }>;
  signUp: (email: string, password: string, role?: UserRole) => Promise<{ error: Error | null }>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider = ({ children }: { children: React.ReactNode }) => {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const API_URL = import.meta.env.VITE_API_URL || 'https://pivota-dashboard.onrender.com';

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
      const response = await fetch(`${API_URL}/auth/me`, {
        headers: {
          'Authorization': `Bearer ${authToken}`,
        },
      });

      if (!response.ok) {
        throw new Error('Failed to load user');
      }

      const data = await response.json();
      if (data.user) {
        setUser(data.user);
      }
    } catch (error) {
      console.error('Failed to load user:', error);
      // Token might be expired, clear it
      localStorage.removeItem('auth_token');
      setToken(null);
      setUser(null);
    } finally {
      setLoading(false);
    }
  };

  const signIn = async (email: string, password: string) => {
    try {
      const response = await fetch(`${API_URL}/auth/signin`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ email, password }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        const error = new Error(errorData.detail || 'Sign in failed');
        toast.error(error.message);
        return { error };
      }

      const data = await response.json();
      const authToken = data.token;

      // Store token
      localStorage.setItem('auth_token', authToken);
      setToken(authToken);

      // Set user data
      if (data.user) {
        setUser(data.user);
        toast.success('Signed in successfully!');
      }

      return { error: null };
    } catch (error) {
      const err = error instanceof Error ? error : new Error('Sign in failed');
      toast.error(err.message);
      return { error: err };
    }
  };

  const signUp = async (email: string, password: string, role: UserRole = 'employee') => {
    try {
      const response = await fetch(`${API_URL}/auth/signup`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ email, password, role }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        const error = new Error(errorData.detail || 'Sign up failed');
        toast.error(error.message);
        return { error };
      }

      const data = await response.json();
      toast.success(data.message || 'Account created! Awaiting admin approval.');
      return { error: null };
    } catch (error) {
      const err = error instanceof Error ? error : new Error('Sign up failed');
      toast.error(err.message);
      return { error: err };
    }
  };

  const signOut = async () => {
    try {
      // Call backend signout endpoint if needed
      if (token) {
        await fetch(`${API_URL}/auth/signout`, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${token}`,
          },
        });
      }
    } catch (error) {
      console.error('Signout error:', error);
    } finally {
      // Clear local state regardless of backend response
      localStorage.removeItem('auth_token');
      setToken(null);
      setUser(null);
      toast.info('Signed out successfully');
    }
  };

  return (
    <AuthContext.Provider value={{ user, loading, token, signIn, signUp, signOut }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};


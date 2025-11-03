import { useState, useEffect, createContext, useContext } from 'react';
import { supabase } from './supabase_client';
import { toast } from 'sonner';

export type UserRole = 'employee' | 'agent' | 'merchant' | 'operator' | 'admin';

interface User {
  id: string;
  email: string;
  role: UserRole;
  approved: boolean;
}

interface AuthContextType {
  user: User | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<{ error: Error | null }>;
  signUp: (email: string, password: string, role: UserRole) => Promise<{ error: Error | null }>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider = ({ children }: { children: React.ReactNode }) => {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const { data: authListener } = supabase.auth.onAuthStateChange(
      async (event, session) => {
        if (session?.user) {
          // Fetch user role and approval status from your backend
          const { data: userRoleData, error: userRoleError } = await supabase
            .from('user_roles')
            .select('role, approved')
            .eq('user_id', session.user.id)
            .single();

          if (userRoleError) {
            console.error('Error fetching user role:', userRoleError);
            toast.error('Failed to load user role');
            setUser(null);
          } else if (userRoleData) {
            setUser({
              id: session.user.id,
              email: session.user.email || '',
              role: userRoleData.role as UserRole,
              approved: userRoleData.approved,
            });
          }
        } else {
          setUser(null);
        }
        setLoading(false);
      }
    );

    return () => {
      authListener.subscription.unsubscribe();
    };
  }, []);

  const signIn = async (email: string, password: string) => {
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) {
      toast.error(error.message);
    }
    return { error };
  };

  const signUp = async (email: string, password: string, role: UserRole) => {
    const { data, error } = await supabase.auth.signUp({ email, password });
    if (error) {
      toast.error(error.message);
      return { error };
    }
    if (data.user) {
      // The handle_new_user trigger in Supabase will set the default role and approved status
      toast.success('Account created! Awaiting admin approval.');
    }
    return { error: null };
  };

  const signOut = async () => {
    const { error } = await supabase.auth.signOut();
    if (error) {
      toast.error(error.message);
    } else {
      setUser(null);
      toast.info('Signed out successfully');
    }
  };

  return (
    <AuthContext.Provider value={{ user, loading, signIn, signUp, signOut }}>
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
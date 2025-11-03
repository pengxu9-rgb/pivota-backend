import { useState, useEffect, createContext, useContext } from 'react'
import { supabase } from '@/integrations/supabase/client'
import { toast } from 'sonner'

export type UserRole = 'employee' | 'agent' | 'merchant' | 'operator' | 'admin'

interface User {
  id: string
  email: string
  full_name?: string
  avatar_url?: string
  role: UserRole
  approved: boolean
}

interface AuthContextType {
  user: User | null
  loading: boolean
  signIn: (email: string, password: string) => Promise<{ error: any }>
  signUp: (email: string, password: string, role: UserRole) => Promise<{ error: any }>
  signOut: () => Promise<void>
  hasPermission: (permission: string) => boolean
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

export const useAuth = () => {
  const context = useContext(AuthContext)
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}

// Permission definitions
const ROLE_PERMISSIONS = {
  admin: [
    'manage_users',
    'manage_psps', 
    'manage_merchants',
    'manage_agents',
    'view_analytics',
    'manage_system'
  ],
  employee: [
    'manage_merchants',
    'manage_agents', 
    'view_analytics'
  ],
  operator: [
    'manage_psps',
    'view_analytics'
  ],
  agent: [
    'view_own_data',
    'manage_own_merchants'
  ],
  merchant: [
    'view_own_data',
    'manage_own_store'
  ]
}

export const AuthProvider = ({ children }: { children: React.ReactNode }) => {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // Get initial session
    const getInitialSession = async () => {
      try {
        const { data: { session } } = await supabase.auth.getSession()
        if (session?.user) {
          await loadUserProfile(session.user.id)
        }
      } catch (error) {
        console.error('Error getting initial session:', error)
      } finally {
        setLoading(false)
      }
    }

    getInitialSession()

    // Listen for auth changes
    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      async (event, session) => {
        if (session?.user) {
          await loadUserProfile(session.user.id)
        } else {
          setUser(null)
        }
        setLoading(false)
      }
    )

    return () => subscription.unsubscribe()
  }, [])

  const loadUserProfile = async (userId: string) => {
    try {
      // Get user profile
      const { data: profile, error: profileError } = await supabase
        .from('profiles')
        .select('*')
        .eq('id', userId)
        .single()

      if (profileError) {
        console.error('Error loading profile:', profileError)
        return
      }

      // Get user roles
      const { data: roles, error: rolesError } = await supabase
        .from('user_roles')
        .select('*')
        .eq('user_id', userId)
        .eq('approved', true)

      if (rolesError) {
        console.error('Error loading roles:', rolesError)
        return
      }

      if (roles && roles.length > 0) {
        // Use the first approved role
        const primaryRole = roles[0]
        setUser({
          id: profile.id,
          email: profile.email,
          full_name: profile.full_name,
          avatar_url: profile.avatar_url,
          role: primaryRole.role as UserRole,
          approved: primaryRole.approved
        })
      } else {
        // User has no approved roles
        setUser({
          id: profile.id,
          email: profile.email,
          full_name: profile.full_name,
          avatar_url: profile.avatar_url,
          role: 'employee' as UserRole,
          approved: false
        })
      }
    } catch (error) {
      console.error('Error loading user profile:', error)
    }
  }

  const signIn = async (email: string, password: string) => {
    try {
      const { data, error } = await supabase.auth.signInWithPassword({
        email,
        password
      })

      if (error) {
        toast.error(error.message)
        return { error }
      }

      if (data.user) {
        await loadUserProfile(data.user.id)
      }

      return { error: null }
    } catch (error) {
      console.error('Sign in error:', error)
      toast.error('Sign in failed')
      return { error }
    }
  }

  const signUp = async (email: string, password: string, role: UserRole) => {
    try {
      // Sign up with Supabase Auth
      const { data, error } = await supabase.auth.signUp({
        email,
        password
      })

      if (error) {
        toast.error(error.message)
        return { error }
      }

      if (data.user) {
        // Create user profile
        const { error: profileError } = await supabase
          .from('profiles')
          .insert({
            id: data.user.id,
            email: data.user.email!,
            full_name: null,
            avatar_url: null
          })

        if (profileError) {
          console.error('Error creating profile:', profileError)
          toast.error('Failed to create profile')
          return { error: profileError }
        }

        // Create user role (pending approval)
        const { error: roleError } = await supabase
          .from('user_roles')
          .insert({
            user_id: data.user.id,
            role: role,
            approved: false
          })

        if (roleError) {
          console.error('Error creating user role:', roleError)
          toast.error('Failed to create user role')
          return { error: roleError }
        }

        toast.success('Account created! Awaiting admin approval.')
      }

      return { error: null }
    } catch (error) {
      console.error('Sign up error:', error)
      toast.error('Sign up failed')
      return { error }
    }
  }

  const signOut = async () => {
    try {
      await supabase.auth.signOut()
      setUser(null)
      toast.success('Signed out successfully')
    } catch (error) {
      console.error('Sign out error:', error)
      toast.error('Sign out failed')
    }
  }

  const hasPermission = (permission: string): boolean => {
    if (!user) return false
    
    const userPermissions = ROLE_PERMISSIONS[user.role] || []
    return userPermissions.includes(permission)
  }

  const value = {
    user,
    loading,
    signIn,
    signUp,
    signOut,
    hasPermission
  }

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  )
}

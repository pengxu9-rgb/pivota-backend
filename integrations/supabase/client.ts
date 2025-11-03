import { createClient } from '@supabase/supabase-js'

const supabaseUrl = process.env.REACT_APP_SUPABASE_URL || 'https://your-project.supabase.co'
const supabaseAnonKey = process.env.REACT_APP_SUPABASE_ANON_KEY || 'your-anon-key'

export const supabase = createClient(supabaseUrl, supabaseAnonKey)

// Database types for TypeScript
export interface Database {
  public: {
    Tables: {
      profiles: {
        Row: {
          id: string
          email: string
          full_name: string | null
          avatar_url: string | null
          created_at: string
          updated_at: string
        }
        Insert: {
          id: string
          email: string
          full_name?: string | null
          avatar_url?: string | null
          created_at?: string
          updated_at?: string
        }
        Update: {
          id?: string
          email?: string
          full_name?: string | null
          avatar_url?: string | null
          created_at?: string
          updated_at?: string
        }
      }
      user_roles: {
        Row: {
          id: string
          user_id: string
          role: 'employee' | 'agent' | 'merchant' | 'operator' | 'admin'
          approved: boolean
          approved_by: string | null
          approved_at: string | null
          created_at: string
          updated_at: string
        }
        Insert: {
          id?: string
          user_id: string
          role: 'employee' | 'agent' | 'merchant' | 'operator' | 'admin'
          approved?: boolean
          approved_by?: string | null
          approved_at?: string | null
          created_at?: string
          updated_at?: string
        }
        Update: {
          id?: string
          user_id?: string
          role?: 'employee' | 'agent' | 'merchant' | 'operator' | 'admin'
          approved?: boolean
          approved_by?: string | null
          approved_at?: string | null
          created_at?: string
          updated_at?: string
        }
      }
      role_permissions: {
        Row: {
          id: string
          role: string
          permission: string
          created_at: string
        }
        Insert: {
          id?: string
          role: string
          permission: string
          created_at?: string
        }
        Update: {
          id?: string
          role?: string
          permission?: string
          created_at?: string
        }
      }
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      get_user_roles: {
        Args: {
          user_uuid: string
        }
        Returns: {
          role: string
          approved: boolean
        }[]
      }
      user_has_permission: {
        Args: {
          user_uuid: string
          permission_name: string
        }
        Returns: boolean
      }
    }
    Enums: {
      user_role: 'employee' | 'agent' | 'merchant' | 'operator' | 'admin'
    }
  }
}

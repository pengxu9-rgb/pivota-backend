"""
Supabase client configuration for Pivota Infrastructure
"""
import os
from supabase import create_client, Client
from config.settings import settings

# Initialize Supabase client
SUPABASE_URL = settings.supabase_url
SUPABASE_KEY = settings.supabase_service_role_key

# Create Supabase client with error handling
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
except Exception as e:
    print(f"Warning: Failed to initialize Supabase client: {e}")
    supabase = None

async def create_user_in_supabase(email: str, password: str, role: str = "employee"):
    """Create user in Supabase Auth"""
    if not supabase:
        return {"success": False, "error": "Supabase not configured"}
    
    try:
        # Create user in Supabase Auth using admin API
        # This bypasses email confirmation
        auth_response = supabase.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,  # Auto-confirm email
            "user_metadata": {
                "role": role
            }
        })
        
        if auth_response.user:
            # The trigger will automatically create profile and user_role
            return {"success": True, "user_id": auth_response.user.id}
        else:
            return {"success": False, "error": "Failed to create user"}
    except Exception as e:
        error_msg = str(e)
        # Provide more helpful error messages
        if "User not allowed" in error_msg:
            return {"success": False, "error": "Email signup is disabled in Supabase settings. Please enable it in Authentication > Providers > Email."}
        return {"success": False, "error": error_msg}

async def get_user_role(user_id: str):
    """Get user role from Supabase"""
    if not supabase:
        return None
    
    try:
        result = supabase.table("user_roles").select("role, approved").eq("user_id", user_id).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        print(f"Error getting user role: {e}")
        return None

async def update_user_role(user_id: str, role: str, approved: bool = True):
    """Update user role in Supabase"""
    if not supabase:
        return {"success": False, "error": "Supabase not configured"}
    
    try:
        result = supabase.table("user_roles").update({
            "role": role,
            "approved": approved
        }).eq("user_id", user_id).execute()
        
        return {"success": True, "data": result.data}
    except Exception as e:
        return {"success": False, "error": str(e)}
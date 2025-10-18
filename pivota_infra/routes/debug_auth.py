"""
Temporary debug routes for authentication issues
"""
from fastapi import APIRouter
from db.database import database
from sqlalchemy import text
import bcrypt

router = APIRouter(prefix="/api/debug", tags=["debug"])

@router.get("/users")
async def debug_users():
    """Check users table"""
    try:
        # Check if table exists
        check_table = text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'users'
            );
        """)
        table_exists = await database.fetch_one(check_table)
        
        # Count users
        count_query = text("SELECT COUNT(*) as count FROM users;")
        user_count = await database.fetch_one(count_query)
        
        # Get all emails
        emails_query = text("SELECT email, role, password_hash FROM users ORDER BY email;")
        users = await database.fetch_all(emails_query)
        
        return {
            "table_exists": table_exists[0] if table_exists else False,
            "user_count": user_count[0] if user_count else 0,
            "users": [{"email": u[0], "role": u[1], "hash_prefix": u[2][:20] if u[2] else None} for u in users]
        }
    except Exception as e:
        return {"error": str(e)}

@router.post("/test-password")
async def test_password(email: str, password: str):
    """Test password verification"""
    try:
        query = text("SELECT email, password_hash FROM users WHERE email = :email")
        user = await database.fetch_one(query, {"email": email})
        
        if not user:
            return {"error": "User not found"}
        
        stored_hash = user[1]
        
        # Test bcrypt verification
        try:
            is_valid = bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))
            return {
                "email": user[0],
                "password_provided": password,
                "hash_prefix": stored_hash[:30],
                "verification_result": is_valid
            }
        except Exception as e:
            return {
                "email": user[0],
                "error": f"Bcrypt error: {str(e)}",
                "hash_prefix": stored_hash[:30]
            }
    except Exception as e:
        return {"error": str(e)}


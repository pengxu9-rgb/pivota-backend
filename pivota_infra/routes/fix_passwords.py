"""
Fix passwords - regenerate bcrypt hashes
"""
from fastapi import APIRouter
from db.database import database
from sqlalchemy import text
import bcrypt

router = APIRouter(prefix="/api/fix", tags=["fix"])

@router.post("/regenerate-passwords")
async def regenerate_passwords():
    """Regenerate all test user passwords"""
    try:
        password = "Admin123!"
        
        # Generate a fresh hash
        salt = bcrypt.gensalt(rounds=12)
        new_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
        
        # Test the new hash
        test_result = bcrypt.checkpw(password.encode('utf-8'), new_hash.encode('utf-8'))
        
        if not test_result:
            return {"error": "New hash verification failed!"}
        
        # Update all test users
        test_emails = [
            'superadmin@pivota.com',
            'admin@pivota.com', 
            'employee@pivota.com',
            'outsourced@pivota.com',
            'merchant@test.com',
            'agent@test.com'
        ]
        
        updated = []
        for email in test_emails:
            query = "UPDATE users SET password_hash = :hash WHERE email = :email"
            await database.execute(query=query, values={"hash": new_hash, "email": email})
            updated.append(email)
        
        return {
            "message": "Passwords updated successfully",
            "password": password,
            "new_hash_prefix": new_hash[:30],
            "test_verification": test_result,
            "updated_users": updated
        }
    except Exception as e:
        return {"error": str(e)}


"""
Authentication API Routes
Clean and simple authentication system for Pivota
"""

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr, validator
from typing import Optional
from datetime import datetime
from db.database import database
from utils.auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user
)

router = APIRouter(prefix="/api/auth", tags=["authentication"])

# Request/Response Models
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    role: str = "employee"
    
    @validator('password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        if not any(c.isupper() for c in v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not any(c.islower() for c in v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one digit')
        return v
    
    @validator('role')
    def validate_role(cls, v):
        valid_roles = ['super_admin', 'admin', 'employee', 'outsourced', 'merchant', 'agent']
        if v not in valid_roles:
            raise ValueError(f'Invalid role. Must be one of: {", ".join(valid_roles)}')
        return v

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class LoginResponse(BaseModel):
    success: bool
    token: str
    user: dict

class UserResponse(BaseModel):
    success: bool
    user: dict

class MessageResponse(BaseModel):
    success: bool
    message: str

@router.post("/register", response_model=MessageResponse)
async def register(data: RegisterRequest):
    """
    Register a new user
    
    - **email**: Valid email address
    - **password**: At least 8 characters, with uppercase, lowercase, and digit
    - **full_name**: Optional full name
    - **role**: super_admin, admin, employee, outsourced, merchant, or agent (default: employee)
    """
    try:
        # Check if user already exists
        query = "SELECT id FROM users WHERE email = :email"
        existing_user = await database.fetch_one(query=query, values={"email": data.email})
        
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # Hash password
        password_hash = hash_password(data.password)
        
        # Insert user
        query = """
            INSERT INTO users (email, password_hash, full_name, role)
            VALUES (:email, :password_hash, :full_name, :role)
            RETURNING id
        """
        values = {
            "email": data.email,
            "password_hash": password_hash,
            "full_name": data.full_name or data.email.split('@')[0],
            "role": data.role
        }
        
        user_id = await database.fetch_val(query=query, values=values)
        
        return MessageResponse(
            success=True,
            message=f"User registered successfully with role: {data.role}"
        )
        
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Registration failed: {str(e)}"
        )

@router.post("/login", response_model=LoginResponse)
async def login(data: LoginRequest):
    """
    Login with email and password
    
    - **email**: User's email
    - **password**: User's password
    
    Returns JWT token and user information
    """
    try:
        # Find user by email
        query = """
            SELECT id, email, password_hash, full_name, role, active
            FROM users
            WHERE email = :email
        """
        user = await database.fetch_one(query=query, values={"email": data.email})
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        
        # Check if user is active
        if not user['active']:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account has been deactivated"
            )
        
        # Verify password
        if not verify_password(data.password, user['password_hash']):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        
        # Update last login
        update_query = """
            UPDATE users
            SET last_login = :last_login
            WHERE id = :user_id
        """
        await database.execute(
            query=update_query,
            values={"last_login": datetime.utcnow(), "user_id": user['id']}
        )
        
        # Create JWT token
        token = create_access_token({
            "sub": user['email'],
            "user_id": str(user['id']),
            "email": user['email'],
            "role": user['role']
        })
        
        return LoginResponse(
            success=True,
            token=token,
            user={
                "id": str(user['id']),
                "email": user['email'],
                "full_name": user['full_name'],
                "role": user['role']
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login failed: {str(e)}"
        )

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)):
    """
    Get current user information
    
    Requires Authorization header with Bearer token
    """
    try:
        # Fetch fresh user data from database
        query = """
            SELECT id, email, full_name, role, created_at, last_login
            FROM users
            WHERE id = :user_id AND active = true
        """
        user = await database.fetch_one(
            query=query,
            values={"user_id": current_user["sub"]}
        )
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        return UserResponse(
            success=True,
            user={
                "id": str(user['id']),
                "email": user['email'],
                "full_name": user['full_name'],
                "role": user['role'],
                "created_at": user['created_at'].isoformat() if user['created_at'] else None,
                "last_login": user['last_login'].isoformat() if user['last_login'] else None
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user: {str(e)}"
        )

@router.post("/logout", response_model=MessageResponse)
async def logout(current_user: dict = Depends(get_current_user)):
    """
    Logout (client-side token removal)
    
    In a JWT system, logout is primarily handled client-side by removing the token.
    This endpoint exists for consistency and future token blacklisting if needed.
    """
    return MessageResponse(
        success=True,
        message="Logged out successfully"
    )

@router.get("/test")
async def test_auth():
    """Test if auth API is accessible"""
    return {
        "success": True,
        "message": "Authentication API is running",
        "endpoints": {
            "register": "POST /api/auth/register",
            "login": "POST /api/auth/login",
            "me": "GET /api/auth/me (requires Authorization header)",
            "logout": "POST /api/auth/logout (requires Authorization header)"
        },
        "test_credentials": {
            "super_admin": {
                "email": "superadmin@pivota.com",
                "password": "Admin123!",
                "role": "super_admin"
            },
            "admin": {
                "email": "admin@pivota.com",
                "password": "Admin123!",
                "role": "admin"
            },
            "employee": {
                "email": "employee@pivota.com",
                "password": "Admin123!",
                "role": "employee"
            },
            "outsourced": {
                "email": "outsourced@pivota.com",
                "password": "Admin123!",
                "role": "outsourced"
            },
            "merchant": {
                "email": "merchant@test.com",
                "password": "Admin123!",
                "role": "merchant"
            },
            "agent": {
                "email": "agent@test.com",
                "password": "Admin123!",
                "role": "agent"
            }
        },
        "employee_roles": {
            "super_admin": "Complete control over the system",
            "admin": "Manage merchants, agents, and most settings",
            "employee": "View and basic operations",
            "outsourced": "Limited read-only access"
        }
    }


# ============================================================================
# Password Management
# ============================================================================

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    
    @validator('new_password')
    def validate_new_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        if not any(c.isupper() for c in v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not any(c.islower() for c in v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one digit')
        return v

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str
    
    @validator('new_password')
    def validate_new_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        if not any(c.isupper() for c in v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not any(c.islower() for c in v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one digit')
        return v


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    data: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Change password for authenticated user
    Requires current password for verification
    """
    try:
        # Get user from database
        user = await database.fetch_one(
            "SELECT user_id, email, password_hash FROM users WHERE email = :email",
            {"email": current_user["email"]}
        )
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Verify current password
        if not verify_password(data.current_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        
        # Hash new password
        new_password_hash = hash_password(data.new_password)
        
        # Update password
        await database.execute(
            "UPDATE users SET password_hash = :password_hash WHERE email = :email",
            {"password_hash": new_password_hash, "email": current_user["email"]}
        )
        
        return MessageResponse(
            success=True,
            message="Password changed successfully"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to change password: {str(e)}"
        )


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(data: ForgotPasswordRequest):
    """
    Request password reset
    Generates a reset token and stores it in database
    """
    try:
        import secrets
        from datetime import timedelta
        
        # Check if user exists
        user = await database.fetch_one(
            "SELECT user_id, email FROM users WHERE email = :email",
            {"email": data.email}
        )
        
        if not user:
            # Don't reveal if email exists or not (security best practice)
            return MessageResponse(
                success=True,
                message="If the email exists, a password reset link has been sent"
            )
        
        # Generate reset token (valid for 1 hour)
        reset_token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=1)
        
        # Store reset token in database
        # First, create password_reset_tokens table if needed
        try:
            await database.execute("""
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    token VARCHAR(255) PRIMARY KEY,
                    user_email VARCHAR(255) NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    used BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        except:
            pass  # Table might already exist
        
        await database.execute(
            """
            INSERT INTO password_reset_tokens (token, user_email, expires_at)
            VALUES (:token, :email, :expires_at)
            """,
            {"token": reset_token, "email": data.email, "expires_at": expires_at}
        )
        
        # TODO: Send email with reset link
        # For now, just log the token (in production, send via email)
        reset_link = f"https://merchants.pivota.cc/reset-password?token={reset_token}"
        print(f"🔑 Password reset link for {data.email}: {reset_link}")
        print(f"   (Valid for 1 hour)")
        
        return MessageResponse(
            success=True,
            message="If the email exists, a password reset link has been sent"
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process password reset: {str(e)}"
        )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(data: ResetPasswordRequest):
    """
    Reset password using token from forgot-password flow
    """
    try:
        # Verify token exists and is valid
        token_record = await database.fetch_one(
            """
            SELECT token, user_email, expires_at, used 
            FROM password_reset_tokens 
            WHERE token = :token
            """,
            {"token": data.token}
        )
        
        if not token_record:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        
        if token_record["used"]:
            raise HTTPException(status_code=400, detail="Reset token has already been used")
        
        if datetime.utcnow() > token_record["expires_at"]:
            raise HTTPException(status_code=400, detail="Reset token has expired")
        
        # Hash new password
        new_password_hash = hash_password(data.new_password)
        
        # Update user password
        await database.execute(
            "UPDATE users SET password_hash = :password_hash WHERE email = :email",
            {"password_hash": new_password_hash, "email": token_record["user_email"]}
        )
        
        # Mark token as used
        await database.execute(
            "UPDATE password_reset_tokens SET used = TRUE WHERE token = :token",
            {"token": data.token}
        )
        
        return MessageResponse(
            success=True,
            message="Password reset successfully. You can now login with your new password."
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reset password: {str(e)}"
        )


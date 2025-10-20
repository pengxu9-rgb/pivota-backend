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
        token = create_access_token(
            user_id=str(user['id']),
            email=user['email'],
            role=user['role']
        )
        
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


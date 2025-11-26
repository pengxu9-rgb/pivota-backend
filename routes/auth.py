"""
Authentication API Routes
Clean and simple authentication system for Pivota
"""

import logging
from datetime import datetime
from textwrap import dedent
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr, validator

from config.settings import settings
from db.database import database
from utils.auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
)

router = APIRouter(prefix="/api/auth", tags=["authentication"])
logger = logging.getLogger("auth_routes")

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


def _send_reset_password_email(email: str, reset_link: str) -> None:
    """
    Best-effort email sender for password reset links.

    Uses SendGrid when SENDGRID_API_KEY / settings.sendgrid_api_key is configured.
    Failures are logged but never propagated to the caller.
    """
    api_key = getattr(settings, "sendgrid_api_key", None)
    if not api_key:
        logger.info(
            "[Auth] SENDGRID_API_KEY not configured; "
            "skipping password reset email send"
        )
        return

    from_email = getattr(settings, "from_email", "noreply@pivota.ai")

    subject = "Reset your Pivota password"
    text_content = (
        "You requested to reset your password.\n\n"
        f"Click the link below to choose a new password:\n{reset_link}\n\n"
        "This link will expire in 1 hour. "
        "If you did not request a password reset, you can ignore this email."
    )
    html_content = dedent(
        f"""
        <p>You requested to reset your password.</p>
        <p>
          Click the link below to choose a new password:<br/>
          <a href="{reset_link}">{reset_link}</a>
        </p>
        <p>This link will expire in 1 hour.</p>
        <p>If you did not request a password reset, you can ignore this email.</p>
        """
    ).strip()

    try:
        import requests

        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [{"to": [{"email": email}]}],
                "from": {"email": from_email, "name": "Pivota"},
                "subject": subject,
                "content": [
                    {"type": "text/plain", "value": text_content},
                    {"type": "text/html", "value": html_content},
                ],
            },
            timeout=10,
        )
        if response.status_code >= 400:
            logger.error(
                "[Auth] Failed to send reset-password email via SendGrid: "
                "status=%s body=%s",
                response.status_code,
                response.text,
            )
        else:
            logger.info("[Auth] Reset-password email sent via SendGrid to %s", email)
    except Exception as exc:
        logger.error(
            "[Auth] Exception while sending reset-password email: %s", exc
        )

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
            SELECT id, email, password_hash, full_name, role, active, merchant_id
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
        
        # For merchants, resolve their merchant_id
        # Prefer the explicit binding from users.merchant_id; fall back to
        # merchant_onboarding.contact_email only when not set.
        merchant_id = user["merchant_id"]
        if user['role'] == 'merchant' and not merchant_id:
            merchant_record = await database.fetch_one(
                "SELECT merchant_id FROM merchant_onboarding WHERE contact_email = :email",
                {"email": user['email']}
            )
            if merchant_record:
                merchant_id = merchant_record['merchant_id']
        
        # [Phase 6.2] For agents, get their agent_id from agents table
        agent_id = None
        if user['role'] == 'agent':
            agent_record = await database.fetch_one(
                "SELECT agent_id FROM agents WHERE email = :email LIMIT 1",
                {"email": user['email']}
            )
            if agent_record:
                agent_id = agent_record['agent_id']
        
        # Create JWT token
        token_data = {
            "sub": user['email'],
            "user_id": str(user['id']),
            "email": user['email'],
            "role": user['role']
        }
        if merchant_id:
            token_data["merchant_id"] = merchant_id
        if agent_id:
            token_data["agent_id"] = agent_id
        
        token = create_access_token(token_data)
        
        user_response = {
            "id": str(user['id']),
            "email": user['email'],
            "full_name": user['full_name'],
            "role": user['role']
        }
        if merchant_id:
            user_response["merchant_id"] = merchant_id
        if agent_id:
            user_response["agent_id"] = agent_id
        
        return LoginResponse(
            success=True,
            token=token,
            user=user_response
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
        # Tokens in this system use the email as the primary subject (`sub`)
        # so we look up the user record by email instead of a numeric ID.
        query = """
            SELECT id, email, full_name, role, created_at, last_login
            FROM users
            WHERE email = :email AND active = true
        """
        user = await database.fetch_one(
            query=query,
            values={"email": current_user["email"]}
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
            # Some legacy deployments used `user_id`; the canonical column is `id`
            # but we only need the email and password hash here.
            "SELECT email, password_hash FROM users WHERE email = :email",
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
        # Some deployments use `id` instead of `user_id`, but for this flow
        # we only need to know whether an email exists, so select email only
        user = await database.fetch_one(
            "SELECT email FROM users WHERE email = :email",
            {"email": data.email}
        )
        
        if not user:
            # Legacy backfill: if this email matches a merchant contact_email but
            # has no corresponding users row yet, create a login user on the fly
            merchant = await database.fetch_one(
                "SELECT merchant_id, business_name FROM merchant_onboarding WHERE contact_email = :email LIMIT 1",
                {"email": data.email},
            )

            if merchant:
                from utils.auth import hash_password
                import secrets

                password = secrets.token_urlsafe(12)
                password_hash = hash_password(password)

                await database.execute(
                    """
                    INSERT INTO users (email, password_hash, full_name, role, active, merchant_id)
                    VALUES (:email, :password_hash, :full_name, :role, :active, :merchant_id)
                    """,
                    {
                        "email": data.email,
                        "password_hash": password_hash,
                        "full_name": merchant["business_name"] or data.email.split("@")[0],
                        "role": "merchant",
                        "active": True,
                        "merchant_id": merchant["merchant_id"],
                    },
                )
                logger.info(
                    "[Auth] Auto-created merchant user for %s to support password reset",
                    data.email,
                )
            else:
                # Don't reveal if email exists or not (security best practice)
                return MessageResponse(
                    success=True,
                    message="If the email exists, a password reset link has been sent",
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
        
        # Build reset link - use configured Merchant Portal base URL
        base_url = getattr(settings, "merchant_portal_base_url", "https://merchant.pivota.cc").rstrip("/")
        reset_link = f"{base_url}/reset-password?token={reset_token}"
        print(f"🔑 Password reset link for {data.email}: {reset_link}")
        print(f"   (Valid for 1 hour)")

        # Best-effort email delivery via SendGrid; failures are logged only
        _send_reset_password_email(data.email, reset_link)
        
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

#!/usr/bin/env python3
"""
Create user account for merchant with existing merchant record
"""
import asyncio
import secrets
from datetime import datetime
from pivota_infra.db.database import database
from pivota_infra.utils.auth import hash_password

async def create_user_for_merchant():
    """Create user account for existing merchant"""
    try:
        await database.connect()
        
        # Find merchant record
        merchant = await database.fetch_one(
            "SELECT merchant_id, business_name FROM merchant_onboarding WHERE contact_email = :email",
            {"email": "yao.wang@chydan.com"}
        )
        
        if not merchant:
            print("❌ No merchant found with email yao.wang@chydan.com")
            return
        
        print(f"✅ Found merchant: {merchant['business_name']} ({merchant['merchant_id']})")
        
        # Check if user already exists
        existing_user = await database.fetch_one(
            "SELECT id FROM users WHERE email = :email",
            {"email": "yao.wang@chydan.com"}
        )
        
        if existing_user:
            print("⚠️  User already exists!")
            return
        
        # Generate password
        password = "Welcome123!"
        password_hash = hash_password(password)
        
        # Create user
        user_id = await database.fetch_val(
            """
            INSERT INTO users (email, password_hash, full_name, role, active, created_at)
            VALUES (:email, :password_hash, :full_name, :role, :active, :created_at)
            RETURNING id
            """,
            {
                "email": "yao.wang@chydan.com",
                "password_hash": password_hash,
                "full_name": merchant['business_name'],
                "role": "merchant",
                "active": True,
                "created_at": datetime.utcnow()
            }
        )
        
        print(f"✅ Created user account!")
        print(f"📧 Email: yao.wang@chydan.com")
        print(f"🔑 Password: {password}")
        print(f"🏪 Merchant ID: {merchant['merchant_id']}")
        print(f"👤 User ID: {user_id}")
        
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(create_user_for_merchant())

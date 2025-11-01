#!/usr/bin/env python3
"""
Fix existing agents with NULL name/email in the database
Run this script to update all agents that are missing name or email data
"""

import asyncio
import os
from datetime import datetime
import asyncpg
from urllib.parse import urlparse

# Get database URL from environment or use Railway's DATABASE_URL
DATABASE_URL = os.getenv('DATABASE_URL', '')

# If DATABASE_URL starts with postgres://, change to postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

async def fix_agents_data():
    """Update agents with missing name/email data"""
    
    if not DATABASE_URL:
        print("❌ ERROR: DATABASE_URL environment variable not set")
        print("\nPlease set it first:")
        print("export DATABASE_URL='your-railway-postgres-url'")
        return False
    
    print("🔄 Connecting to database...")
    
    try:
        # Connect to database
        conn = await asyncpg.connect(DATABASE_URL)
        print("✅ Connected to database")
        
        # First, check how many agents need fixing
        check_query = """
            SELECT COUNT(*) as count 
            FROM agents 
            WHERE name IS NULL OR name = '' OR email IS NULL OR email = ''
        """
        
        result = await conn.fetchrow(check_query)
        needs_fix = result['count']
        
        if needs_fix == 0:
            print("✅ All agents already have name and email. Nothing to fix!")
            await conn.close()
            return True
        
        print(f"📊 Found {needs_fix} agents that need fixing")
        
        # Get the agents that need fixing (for logging)
        agents_to_fix = await conn.fetch("""
            SELECT agent_id, name, email, status, created_at
            FROM agents 
            WHERE name IS NULL OR name = '' OR email IS NULL OR email = ''
            LIMIT 10
        """)
        
        print("\n📋 Agents to be fixed:")
        for agent in agents_to_fix:
            print(f"  - {agent['agent_id']}: name='{agent['name']}', email='{agent['email']}'")
        
        if needs_fix > 10:
            print(f"  ... and {needs_fix - 10} more")
        
        # Ask for confirmation
        print("\n⚠️  This will update the agents with:")
        print("  - name: 'Agent ' + part of agent_id")
        print("  - email: agent_id + '@agents.pivota.app'")
        
        confirm = input("\n❓ Do you want to proceed? (yes/no): ")
        if confirm.lower() not in ['yes', 'y']:
            print("❌ Operation cancelled")
            await conn.close()
            return False
        
        # Update agents with missing name
        print("\n🔧 Updating agent names...")
        update_name = """
            UPDATE agents 
            SET name = CASE 
                WHEN name IS NULL OR name = '' 
                THEN 'Agent ' || SUBSTRING(agent_id FROM 7 FOR 8)
                ELSE name
            END
            WHERE name IS NULL OR name = ''
        """
        
        name_result = await conn.execute(update_name)
        print(f"✅ Updated names: {name_result}")
        
        # Update agents with missing email
        print("🔧 Updating agent emails...")
        update_email = """
            UPDATE agents 
            SET email = CASE
                WHEN email IS NULL OR email = ''
                THEN agent_id || '@agents.pivota.app'
                ELSE email
            END
            WHERE email IS NULL OR email = ''
        """
        
        email_result = await conn.execute(update_email)
        print(f"✅ Updated emails: {email_result}")
        
        # Verify the update
        print("\n📊 Verifying updates...")
        verification = await conn.fetch("""
            SELECT agent_id, name, email, status, created_at
            FROM agents
            ORDER BY created_at DESC
            LIMIT 5
        """)
        
        print("\n✅ Updated agents (showing first 5):")
        for agent in verification:
            print(f"  - {agent['agent_id']}")
            print(f"    Name: {agent['name']}")
            print(f"    Email: {agent['email']}")
            print(f"    Status: {agent['status']}")
        
        # Check if any still need fixing
        final_check = await conn.fetchrow(check_query)
        remaining = final_check['count']
        
        if remaining == 0:
            print("\n🎉 SUCCESS! All agents now have name and email.")
        else:
            print(f"\n⚠️  WARNING: {remaining} agents still missing data")
        
        await conn.close()
        print("\n✅ Database connection closed")
        return True
        
    except Exception as e:
        print(f"\n❌ ERROR: {str(e)}")
        print("\n🔍 Troubleshooting:")
        print("1. Make sure DATABASE_URL is set correctly")
        print("2. Check if you have access to the database")
        print("3. Verify the agents table exists")
        return False

async def main():
    print("=" * 60)
    print("🚀 AGENTS DATA FIX SCRIPT")
    print("=" * 60)
    print()
    
    success = await fix_agents_data()
    
    if success:
        print("\n" + "=" * 60)
        print("✅ COMPLETE! Refresh the Employee Portal to see the changes.")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("❌ Fix failed. Please check the error messages above.")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())

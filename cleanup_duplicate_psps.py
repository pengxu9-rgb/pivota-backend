#!/usr/bin/env python3
"""
Clean up duplicate PSP configurations
Keep only the most recent one for each provider
"""
import asyncio
from databases import Database

DATABASE_URL = "postgresql://postgres:VJSIqWOeDbZOPZsLFJBxBKKOCFfNhLJv@junction.proxy.rlwy.net:15637/railway"
MERCHANT_ID = "merch_208139f7600dbf42"

async def cleanup_psps():
    database = Database(DATABASE_URL)
    
    try:
        await database.connect()
        
        print("=" * 60)
        print("PSP Cleanup for", MERCHANT_ID)
        print("=" * 60)
        print()
        
        # Get all PSPs
        psps = await database.fetch_all(
            """
            SELECT psp_id, provider, 
                   LENGTH(api_key) as key_len,
                   SUBSTRING(api_key, 1, 10) as key_prefix,
                   account_id,
                   connected_at
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
            ORDER BY provider, connected_at DESC
            """,
            {"merchant_id": MERCHANT_ID}
        )
        
        print(f"Found {len(psps)} PSP configurations:")
        print()
        
        by_provider = {}
        for psp in psps:
            provider = psp["provider"]
            if provider not in by_provider:
                by_provider[provider] = []
            by_provider[provider].append(psp)
        
        # Show current state
        for provider, configs in by_provider.items():
            print(f"{provider.upper()}: {len(configs)} configs")
            for i, cfg in enumerate(configs):
                marker = "📌 KEEP" if i == 0 else "🗑️  DELETE"
                print(f"  {marker} {cfg['psp_id']} | Key: {cfg['key_prefix']}... ({cfg['key_len']} chars) | Account: {cfg['account_id']} | {cfg['connected_at']}")
        
        print()
        print("This will:")
        print("- Keep the MOST RECENT config for each PSP provider")
        print("- Delete all older duplicate configs")
        print()
        
        confirm = input("Proceed with cleanup? (type 'yes' to confirm): ").strip()
        
        if confirm.lower() != 'yes':
            print("Cancelled")
            await database.disconnect()
            return
        
        print()
        print("Cleaning up...")
        
        # For each provider, delete all except the most recent
        total_deleted = 0
        for provider, configs in by_provider.items():
            if len(configs) > 1:
                # Keep the first (most recent), delete the rest
                to_delete = [cfg['psp_id'] for cfg in configs[1:]]
                
                result = await database.execute(
                    f"""
                    DELETE FROM merchant_psps
                    WHERE psp_id = ANY(:psp_ids)
                    """,
                    {"psp_ids": to_delete}
                )
                
                print(f"✅ {provider}: Deleted {len(to_delete)} old config(s), kept 1 recent")
                total_deleted += len(to_delete)
            else:
                print(f"✅ {provider}: Only 1 config, keeping it")
        
        print()
        print(f"🎉 Cleanup complete! Deleted {total_deleted} duplicate PSP configs")
        print()
        
        # Show final state
        final_psps = await database.fetch_all(
            """
            SELECT provider, psp_id, 
                   LENGTH(api_key) as key_len,
                   account_id,
                   status
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
            ORDER BY provider
            """,
            {"merchant_id": MERCHANT_ID}
        )
        
        print("Final state:")
        for psp in final_psps:
            print(f"  ✓ {psp['provider']}: {psp['psp_id']} | Key len: {psp['key_len']} | Account: {psp['account_id']} | Status: {psp['status']}")
        
        await database.disconnect()
        
        print()
        print("Now test all PSPs:")
        print("  bash check_current_psps.sh")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        await database.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(cleanup_psps())
    except KeyboardInterrupt:
        print("\n\nCancelled by user")


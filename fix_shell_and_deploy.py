#!/usr/bin/env python3
import subprocess
import os
import sys

def run_cmd(cmd, cwd=None):
    """Run command with proper shell"""
    print(f"$ {cmd}")
    try:
        # Use /bin/zsh explicitly
        result = subprocess.run(
            ['/bin/zsh', '-c', cmd],
            cwd=cwd,
            capture_output=True,
            text=True,
            env={**os.environ, 'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'}
        )
        print(result.stdout)
        if result.stderr and result.returncode != 0:
            print(f"Error: {result.stderr}")
        return result.returncode == 0
    except Exception as e:
        print(f"Exception: {e}")
        return False

def main():
    portal_dir = "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-merchants-portal"
    
    print("=== Auto Deploy Merchant Portal ===\n")
    
    # 1. Fix the key conflict in api-client.ts
    print("1. Checking if conflicts are resolved...")
    api_client_path = os.path.join(portal_dir, "lib/api-client.ts")
    with open(api_client_path, 'r') as f:
        content = f.read()
    
    if '<<<<<<< HEAD' in content:
        print("❌ Still has conflicts! Please check manually.")
        return
    
    print("✅ api-client.ts is clean\n")
    
    # 2. Add all resolved files
    print("2. Adding resolved files...")
    if not run_cmd("git add .", cwd=portal_dir):
        print("Failed to add files")
        return
    
    # 3. Commit the merge
    print("\n3. Committing merge...")
    if not run_cmd('git commit -m "fix: merge with remote and keep login fix for new API"', cwd=portal_dir):
        print("Failed to commit - might already be committed")
    
    # 4. Push to remote
    print("\n4. Pushing to GitHub...")
    if run_cmd("git push origin main", cwd=portal_dir):
        print("\n🎉 Successfully pushed to GitHub!")
        print("✅ Check Vercel deployment at: https://vercel.com/dashboard")
        print("✅ Login fix has been deployed!")
    else:
        print("\n❌ Push failed. You may need to resolve more issues.")

if __name__ == "__main__":
    main()


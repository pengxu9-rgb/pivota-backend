#!/usr/bin/env python3
import subprocess
import os

def run_command(cmd, cwd):
    """Run a command and return output"""
    print(f"\n$ {cmd}")
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(f"Error: {result.stderr}")
        return result.returncode == 0, result.stdout
    except Exception as e:
        print(f"Exception: {e}")
        return False, str(e)

def main():
    portal_dir = "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-merchants-portal"
    
    print("=== Auto Merge Merchant Portal ===")
    
    # 1. Stash current changes
    print("\n1. Stashing current changes...")
    success, _ = run_command("git stash push -m 'Login fix for api-client'", portal_dir)
    
    # 2. Pull from remote
    print("\n2. Pulling from remote...")
    success, _ = run_command("git pull origin main --no-edit", portal_dir)
    
    if not success:
        print("\n⚠️  Pull failed. Trying with rebase...")
        run_command("git pull origin main --rebase", portal_dir)
    
    # 3. Apply stashed changes
    print("\n3. Applying stashed changes...")
    success, output = run_command("git stash pop", portal_dir)
    
    if "CONFLICT" in output or not success:
        print("\n⚠️  Conflicts detected!")
        print("Please manually resolve conflicts in lib/api-client.ts")
        print("Make sure line ~76 has:")
        print('if ((response.data.success === true || response.data.status === \'success\') && response.data.token) {')
    else:
        print("\n✅ Changes applied successfully!")
        
        # 4. Check if the fix is still there
        print("\n4. Verifying login fix...")
        with open(os.path.join(portal_dir, "lib/api-client.ts"), 'r') as f:
            content = f.read()
            if "response.data.success === true || response.data.status === 'success'" in content:
                print("✅ Login fix is present!")
                
                # 5. Commit and push
                print("\n5. Committing and pushing...")
                run_command("git add lib/api-client.ts", portal_dir)
                run_command("git commit -m 'fix: ensure login works with new API response format'", portal_dir)
                success, _ = run_command("git push origin main", portal_dir)
                
                if success:
                    print("\n🎉 Successfully deployed!")
                else:
                    print("\n❌ Push failed. You may need to pull again or resolve conflicts.")
            else:
                print("❌ Login fix is missing! Please add it manually.")

if __name__ == "__main__":
    main()



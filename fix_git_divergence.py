#!/usr/bin/env python3
import subprocess
import os

def run_git_command(cmd):
    """Run a git command and return output"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, 
                              cwd="/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344")
        print(f"$ {cmd}")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(f"Error: {result.stderr}")
        return result.returncode == 0
    except Exception as e:
        print(f"Exception running '{cmd}': {e}")
        return False

def main():
    print("=== Fixing Git Divergence ===\n")
    
    # Check current status
    print("1. Current status:")
    run_git_command("git status --short")
    
    # Check remote URL
    print("\n2. Remote URL:")
    run_git_command("git remote -v")
    
    # Fetch latest
    print("\n3. Fetching latest from origin:")
    run_git_command("git fetch origin")
    
    # Show divergence
    print("\n4. Branch divergence:")
    run_git_command("git status -sb")
    
    # Stash changes
    print("\n5. Stashing local changes:")
    run_git_command("git stash push -m 'Stash before resolving divergence'")
    
    # Pull with rebase
    print("\n6. Pulling with rebase:")
    success = run_git_command("git pull --rebase origin main")
    
    if not success:
        print("\n⚠️  Rebase failed. You may need to resolve conflicts.")
        print("Run 'git status' to see conflicts, resolve them, then:")
        print("  git add <resolved-files>")
        print("  git rebase --continue")
    else:
        # Apply stash
        print("\n7. Applying stashed changes:")
        run_git_command("git stash pop")
        
        print("\n8. Final status:")
        run_git_command("git status")
        
        print("\n✅ Git divergence resolved!")
        print("You can now push with: git push origin main")

if __name__ == "__main__":
    main()


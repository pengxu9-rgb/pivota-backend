#!/bin/bash

echo "🔍 Checking all portal directories..."
echo "========================================"
echo ""

BASE_DIR="/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344"

# Function to check a directory
check_portal() {
    local dir="$1"
    local name=$(basename "$dir")
    
    echo "📁 $name"
    
    # Check if has package.json
    if [ -f "$dir/package.json" ]; then
        echo "  ✅ Has package.json"
        
        # Check git remote
        if [ -d "$dir/.git" ]; then
            cd "$dir"
            remote=$(git remote get-url origin 2>/dev/null || echo "No remote")
            echo "  📡 Git: $remote"
            
            # Check recent commit
            commit=$(git log --oneline -1 2>/dev/null || echo "No commits")
            echo "  📝 Latest: $commit"
        else
            echo "  ⚠️  No .git directory"
        fi
        
        # Check structure
        if [ -d "$dir/app" ]; then
            echo "  📂 Structure: app/ (Next.js App Router)"
        elif [ -d "$dir/src/app" ]; then
            echo "  📂 Structure: src/app/ (Next.js with src)"
        elif [ -d "$dir/src" ]; then
            echo "  📂 Structure: src/ (React/Vite)"
        else
            echo "  ❓ Unknown structure"
        fi
    else
        echo "  ❌ No package.json (empty/broken)"
    fi
    
    echo ""
}

# Check all agent portals
echo "🤖 AGENT PORTALS:"
echo "----------------"
for dir in "$BASE_DIR"/pivota-agents-portal*; do
    if [ -d "$dir" ]; then
        check_portal "$dir"
    fi
done

echo ""
echo "👔 EMPLOYEE PORTALS:"
echo "--------------------"
for dir in "$BASE_DIR"/pivota-employee-portal*; do
    if [ -d "$dir" ]; then
        check_portal "$dir"
    fi
done

echo ""
echo "🏪 MERCHANT PORTALS:"
echo "--------------------"
for dir in "$BASE_DIR"/pivota-merchants-portal*; do
    if [ -d "$dir" ]; then
        check_portal "$dir"
    fi
done

echo ""
echo "========================================"
echo "✅ Check complete!"
echo ""
echo "📌 RECOMMENDATION:"
echo "Keep ONLY the directories with:"
echo "  1. ✅ Has package.json"
echo "  2. 📡 Has valid git remote"
echo "  3. 📝 Has recent commits"
echo ""
echo "Delete the rest!"





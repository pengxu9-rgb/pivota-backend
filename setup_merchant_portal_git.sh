#!/bin/bash

echo "=== Setting up Merchant Portal Git Repository ==="

# Navigate to merchant portal directory
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-merchants-portal" || exit 1

# Initialize git if not already
if [ ! -d ".git" ]; then
    echo "Initializing Git repository..."
    git init
    git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git
else
    echo "Git repository already initialized"
fi

# Check status
echo -e "\n1. Current status:"
git status

# Add the fix
echo -e "\n2. Adding api-client.ts fix:"
git add lib/api-client.ts

# Commit
echo -e "\n3. Committing changes:"
git commit -m "fix: correct token storage logic for new API response format" || echo "No changes to commit"

# Push
echo -e "\n4. Pushing to GitHub:"
git branch -M main
git push -u origin main

echo -e "\n✅ Done! Check Vercel for deployment status."


#!/bin/bash

echo "=== Deploying Merchant Portal Fix ==="

# Navigate to merchant portal directory
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-merchants-portal" || exit 1

echo -e "\n1. Current status:"
git status --short

echo -e "\n2. Adding changes:"
git add lib/api-client.ts

echo -e "\n3. Committing:"
git commit -m "fix: correct token storage logic for new API response format"

echo -e "\n4. Pushing to GitHub:"
git push origin main

echo -e "\n✅ Deployment triggered! Check Vercel for deployment status."
echo "🔗 https://vercel.com/dashboard"


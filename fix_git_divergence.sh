#!/bin/bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344"

echo "=== Fixing Git Divergence ==="
echo "Current branch status:"
git status --short

echo -e "\n=== Fetching latest from origin ==="
git fetch origin

echo -e "\n=== Current divergence status ==="
git status -sb

echo -e "\n=== Stashing local changes ==="
git stash push -m "Stash before resolving divergence"

echo -e "\n=== Pulling with rebase ==="
git pull --rebase origin main

echo -e "\n=== Applying stashed changes ==="
git stash pop || echo "No stash to apply or conflicts occurred"

echo -e "\n=== Final status ==="
git status

echo -e "\n=== Ready to push ==="
echo "If everything looks good, run: git push origin main"


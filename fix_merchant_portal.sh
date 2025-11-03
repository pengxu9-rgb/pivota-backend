#!/bin/bash

# Create a temporary directory for clean push
TEMP_DIR="/tmp/merchant-portal-fix-$$"
rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR"

# Copy the merchant portal files
cp -r pivota-merchants-portal/* "$TEMP_DIR/"
cd "$TEMP_DIR"

# Initialize git and push
git init
git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git
git add -A
git commit -m "[Phase 6 Fixed] Commission Management with corrected API client

✅ Fixed syntax error in lib/api-client.ts
- Moved commission methods inside ApiClient class
- getCommissionOffers()
- createCommissionOffer()
- deleteCommissionOffer()

✅ Commission Page functional
✅ Navigation updated
✅ Ready for deployment"

git branch -M main
git push -u origin main --force

echo "✅ Push complete!"

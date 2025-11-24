#!/bin/bash

# Deploy Refund History Feature
echo "🚀 Deploying Refund History Feature..."

# 1. Copy updated frontend files
echo "📁 Copying frontend files..."
cp "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-merchants-portal 3/app/dashboard/orders/page.tsx" \
   "/Users/pengchydan/Desktop/temp_fix/pivota-merchants-portal/app/dashboard/orders/page.tsx"

cp "/Users/pengchydan/Desktop/temp_fix/pivota-merchants-portal/components/RefundHistoryTimeline.tsx" \
   "/Users/pengchydan/Desktop/temp_fix/pivota-merchants-portal/components/RefundHistoryTimeline.tsx"

# 2. Deploy Backend
echo "🔧 Deploying backend..."
cd /Users/pengchydan/Desktop/temp_fix
git add -A
git commit -m "feat: add enhanced refund history with timeline view"
git push origin main

# 3. Deploy Frontend  
echo "🎨 Deploying frontend..."
cd /Users/pengchydan/Desktop/temp_fix/pivota-merchants-portal
git add -A
git commit -m "feat: add RefundHistoryTimeline component with enhanced UI"
git push origin main

echo "✅ Deployment complete!"
echo "📊 Features added:"
echo "   - Enhanced refund history API with summaries"
echo "   - RefundHistoryTimeline component with visual timeline"
echo "   - Refund summary cards showing totals and stats"
echo "   - Processing time tracking"
echo "   - Status indicators and error messages"

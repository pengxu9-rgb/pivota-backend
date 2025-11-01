# Deploy Fix Agents Endpoint - Step by Step Guide

## ✅ Step 1: Files Already Created

The following files have been created for you:

1. **Backend Route**: `pivota_infra/routes/admin_fix_agents.py`
   - Contains the fix endpoint `/admin/fix/agents-data`
   - Contains status check endpoint `/admin/fix/agents-status`

2. **Main.py Updates**: Already added the import and router inclusion
   - Import: `from routes.admin_fix_agents import router as admin_fix_agents_router`
   - Include: `app.include_router(admin_fix_agents_router)`

3. **Test Script**: `test_fix_agents.sh`
   - Ready to run after deployment

## 📦 Step 2: Deploy to Railway

### Option A: Using Git (Recommended)

```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344/pivota_infra

# Add and commit the changes
git add .
git commit -m "feat: add endpoint to fix agents with null name/email"

# Push to trigger Railway deployment
git push origin main
```

### Option B: Using Railway CLI

```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344/pivota_infra

# Deploy directly with Railway CLI
railway up
```

## ⏱️ Step 3: Wait for Deployment

Railway will automatically deploy your changes. This usually takes 2-3 minutes.

You can check the deployment status at:
https://railway.app/project/YOUR_PROJECT_ID/deployments

## 🔑 Step 4: Get Your Employee Token

You need an employee token to call the fix endpoint. Here's how to get it:

1. Go to Employee Portal: https://pivota-employee-portal.vercel.app
2. Log in with your employee credentials
3. Open browser DevTools (F12)
4. Go to **Application** tab (Chrome) or **Storage** tab (Firefox)
5. Click **Local Storage** > Your domain
6. Copy the value of `employee_token`

Or use this JavaScript in the browser console:
```javascript
console.log(localStorage.getItem('employee_token'))
```

## 🚀 Step 5: Run the Fix

Once deployed, run the fix script:

```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344

# Run with your token
./test_fix_agents.sh YOUR_EMPLOYEE_TOKEN
```

### What happens:
1. Script checks current agents status
2. Shows how many need fixing
3. Asks for confirmation
4. Fixes all agents with missing name/email
5. Shows sample results

## ✅ Step 6: Verify in Employee Portal

1. Go back to Employee Portal
2. Navigate to Agents page
3. Refresh the page (Ctrl+F5 or Cmd+Shift+R)
4. You should now see agent names!

## 📊 Expected Results

### Before Fix:
```
Agent: Unnamed Agent
Email: No email
```

### After Fix:
```
Agent: Agent ee38f2b3
Email: agent_ee38f2b3645a2ec2@agents.pivota.app
```

## 🔍 Alternative: Test Manually with cURL

If you prefer to test manually:

### Check Status:
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://web-production-fedb.up.railway.app/admin/fix/agents-status
```

### Run Fix:
```bash
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" \
  https://web-production-fedb.up.railway.app/admin/fix/agents-data
```

## ⚠️ Troubleshooting

### "Not authorized" error
- Make sure you're using an employee or admin token
- Token might be expired - log in again

### "jq: command not found"
Install jq (optional, for pretty JSON):
```bash
# Mac
brew install jq

# Linux
sudo apt-get install jq
```

### Script permission denied
```bash
chmod +x test_fix_agents.sh
```

### Deployment failed
Check Railway logs:
```bash
railway logs
```

## 🎯 Summary

1. ✅ Route file created: `admin_fix_agents.py`
2. ✅ Main.py updated with import and include
3. ✅ Test script ready: `test_fix_agents.sh`
4. ⏳ Deploy to Railway (git push)
5. ⏳ Get employee token
6. ⏳ Run fix script
7. ⏳ Verify in Employee Portal

---

**Status**: Ready to deploy! Just push to git and run the script after deployment completes.

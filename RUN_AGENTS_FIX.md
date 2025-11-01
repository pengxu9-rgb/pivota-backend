# How to Run the Agents Data Fix

## Prerequisites
Make sure you have Python 3.7+ and the required package:

```bash
pip install asyncpg
```

## Step 1: Get Your Database URL

### Option A: From Railway Dashboard
1. Go to https://railway.app
2. Click on your project
3. Click on the PostgreSQL service
4. Go to "Variables" tab
5. Copy the `DATABASE_URL` value

### Option B: From Railway CLI
```bash
railway variables
# Look for DATABASE_URL
```

## Step 2: Set the Database URL

```bash
# Linux/Mac
export DATABASE_URL="postgresql://postgres:YOUR_PASSWORD@YOUR_HOST.railway.app:PORT/railway"

# Windows (PowerShell)
$env:DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@YOUR_HOST.railway.app:PORT/railway"

# Windows (CMD)
set DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@YOUR_HOST.railway.app:PORT/railway
```

## Step 3: Run the Fix Script

```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344"
python fix_agents_data.py
```

## What the Script Does

1. **Checks** how many agents have NULL/empty name or email
2. **Shows** you which agents will be updated
3. **Asks** for confirmation before making changes
4. **Updates** agents with:
   - Name: "Agent " + part of their agent_id
   - Email: agent_id + "@agents.pivota.app"
5. **Verifies** the changes were applied

## Expected Output

```
🔄 Connecting to database...
✅ Connected to database
📊 Found 1 agents that need fixing

📋 Agents to be fixed:
  - agent_ee38f2b3645a2ec2: name='None', email='None'

⚠️  This will update the agents with:
  - name: 'Agent ' + part of agent_id
  - email: agent_id + '@agents.pivota.app'

❓ Do you want to proceed? (yes/no): yes

🔧 Updating agent names...
✅ Updated names: UPDATE 1
🔧 Updating agent emails...
✅ Updated emails: UPDATE 1

✅ Updated agents (showing first 5):
  - agent_ee38f2b3645a2ec2
    Name: Agent ee38f2b3
    Email: agent_ee38f2b3645a2ec2@agents.pivota.app
    Status: active

🎉 SUCCESS! All agents now have name and email.
```

## After Running

1. Go back to Employee Portal: https://pivota-employee-portal.vercel.app
2. Navigate to Agents page
3. You should now see the agent names and emails!

## Troubleshooting

### Error: "DATABASE_URL environment variable not set"
Make sure you exported the DATABASE_URL correctly (see Step 2)

### Error: "No module named 'asyncpg'"
Install it with: `pip install asyncpg`

### Error: "Connection refused" or timeout
- Check your DATABASE_URL is correct
- Make sure your IP is allowed (Railway usually allows all IPs)
- Try using the public URL from Railway dashboard

### Still showing "Unnamed Agent" after running?
1. Hard refresh the page (Ctrl+Shift+R or Cmd+Shift+R)
2. Check browser console for any errors
3. Log out and log back in to the Employee Portal

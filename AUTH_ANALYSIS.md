# Authentication Analysis: Merchants & Agents

## Current Situation

### ❌ What's MISSING

#### 1. **Merchant Credential Creation**
**Current Flow:**
- Merchant fills out onboarding form → Gets `merchant_id`
- **NO username/password is created**
- **NO login credentials stored**

**What Happens:**
- Merchant completes onboarding
- Gets approved
- **Cannot log in** - no credentials exist!

**Backend Status:** ❌ NOT READY
- `POST /merchant/onboarding/register` - Creates merchant record but NO user account
- No endpoint to set password
- No `users` table entry for merchants

---

#### 2. **Agent Credential Creation**  
**Current Flow:**
- Agent fills out form → Gets `agent_id` and `api_key`
- **NO username/password created**
- **NO login credentials stored**

**What Happens:**
- Agent gets API key for programmatic access
- **Cannot log into portal** - no credentials!

**Backend Status:** ❌ NOT READY
- `POST /agent/v1/auth` - Generates API key only
- No endpoint to create portal login
- Agents use API keys, not passwords

---

### ✅ What EXISTS (Demo/Hardcoded)

#### Demo Accounts (auth_routes.py)
```python
demo_accounts = {
    "merchant@test.com": {"password": "Admin123!", "role": "merchant", "merchant_id": "merch_xxx"},
    "employee@pivota.com": {"password": "Admin123!", "role": "admin"},
    "agent@test.com": {"password": "Admin123!", "role": "agent"},
}
```

**These are hardcoded** - work for testing only!

---

## What's Needed

### For Merchants:

1. **During Onboarding** - Add password creation step:
```
Step 1: Business Info (existing)
Step 2: Create Login Credentials ⭐ NEW
  - Email (from contact_email)
  - Password
  - Confirm Password
Step 3: PSP Setup (existing)
Step 4: Upload Docs (existing)
```

2. **Backend Endpoint** - Create user account:
```python
POST /merchant/onboarding/create-credentials
{
  "merchant_id": "merch_xxx",
  "email": "merchant@example.com",
  "password": "SecurePass123!"
}
```

**What it should do:**
- Hash password
- Insert into `users` table:
  ```sql
  INSERT INTO users (id, email, password_hash, role, entity_id, active)
  VALUES (uuid, email, hash, 'merchant', merchant_id, true)
  ```
- Link to merchant_onboarding via `entity_id = merchant_id`

---

### For Agents:

**Two Types of Access:**

#### A. **API-Only Agents** (Current)
- Get API key only
- Use for programmatic access
- **No portal login needed**
- ✅ Already works

#### B. **Portal Access Agents** (Future/Optional)
- Same as merchants - need username/password
- Can view dashboard, analytics
- **Not implemented yet**

**Decision Needed:**
- Do agents need portal access?
- Or are they API-only?

---

## Backend Readiness Summary

| Feature | Status | Notes |
|---------|--------|-------|
| Merchant onboarding | ✅ Ready | Creates merchant record |
| Merchant credential creation | ❌ NOT READY | No password setup |
| Merchant login | ⚠️ Demo only | Hardcoded accounts work |
| Agent API key generation | ✅ Ready | Works for API access |
| Agent credential creation | ❌ NOT READY | No password setup |
| Agent login | ⚠️ Demo only | Hardcoded accounts work |
| User table structure | ✅ Ready | Table exists |
| Password hashing | ✅ Ready | bcrypt available |
| JWT token generation | ✅ Ready | Works |

---

## Recommendation

### Phase 1: Merchants (Priority)
1. **Add credential creation step** to merchant onboarding
2. **Create backend endpoint** to save username/password
3. **Update login flow** to check users table instead of demo accounts
4. **Test end-to-end**: Register → Create credentials → Login → Dashboard

### Phase 2: Agents (Later)
1. **Decide**: Portal access or API-only?
2. If portal needed: Same flow as merchants
3. If API-only: Current implementation is sufficient

---

## Missing Backend Endpoints

Need to create:

```python
# routes/merchant_credentials.py
@router.post("/merchant/credentials/create")
async def create_merchant_credentials(
    merchant_id: str,
    email: str,
    password: str
):
    # 1. Verify merchant exists
    # 2. Hash password
    # 3. Insert into users table
    # 4. Return success

@router.post("/merchant/credentials/reset-password")
async def reset_merchant_password(
    merchant_id: str,
    old_password: str,
    new_password: str
):
    # Allow merchants to change password
```

---

## Current Login Works For:
✅ Employees (via users table or demo accounts)
✅ Demo merchants (hardcoded)
✅ Demo agents (hardcoded)

## Current Login DOESN'T Work For:
❌ Real merchants who completed onboarding
❌ Real agents who got API keys

---

**Bottom Line:** 
The authentication infrastructure exists, but the **credential creation during onboarding** is missing. This needs to be added before merchants/agents can actually log in after signing up.






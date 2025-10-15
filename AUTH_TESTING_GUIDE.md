# 🔧 Auth Testing Guide - How to Test POST Endpoints Correctly

## ⚠️ CRITICAL: POST requests require proper headers!

### ✅ **Correct way to test POST endpoints:**

#### **Using curl:**

```bash
# Test signup
curl -X POST https://your-render-app.onrender.com/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "password123"}'

# Test signin
curl -X POST https://your-render-app.onrender.com/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "password123"}'
```

#### **Using Postman/Insomnia:**

1. Set method to **POST**
2. Set URL to `https://your-render-app.onrender.com/auth/signup`
3. Go to **Headers** tab
4. Add header: `Content-Type: application/json`
5. Go to **Body** tab
6. Select **raw** and **JSON**
7. Enter:
```json
{
  "email": "test@example.com",
  "password": "password123"
}
```

#### **Using JavaScript fetch:**

```javascript
fetch('https://your-render-app.onrender.com/auth/signup', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    email: 'test@example.com',
    password: 'password123'
  })
})
.then(response => response.json())
.then(data => console.log(data))
.catch(error => console.error('Error:', error));
```

## 🚨 Common Mistakes:

❌ **NOT including `Content-Type: application/json` header**
❌ **Using GET instead of POST**
❌ **Not sending proper JSON body**
❌ **Testing from browser address bar (only works for GET)**

## ✅ Expected Responses:

### Signup Success:
```json
{
  "message": "User created successfully. Awaiting admin approval.",
  "user_id": "123e4567-e89b-12d3-a456-426614174000"
}
```

### Signin Success:
```json
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer",
  "user": {
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "email": "test@example.com",
    "role": "employee",
    "approved": false
  }
}
```

### Method Not Allowed (Missing Content-Type):
```json
{
  "detail": "Method Not Allowed"
}
```

## 🔍 How to Test:

1. **Test with curl** (command line)
2. **Test with Postman/Insomnia** (GUI tool)
3. **Test with browser console** (using fetch)

DO NOT test POST endpoints by typing URL in browser address bar - that only works for GET requests!


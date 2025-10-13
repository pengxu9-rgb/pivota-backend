# 🔧 Wix Store Integration Setup Guide

## 🎯 **What We Need to Fix**

Your Wix store integration is failing because we need the **Site ID** to authenticate API requests. Here's exactly what you need to do:

## 📋 **Step-by-Step Instructions**

### 1. **Get Your Wix Site ID**

1. **Go to your Wix Dashboard:**
   - Visit: https://www.wix.com/dashboard
   - Log in to your account
   - Select your site: "Funky Tees" (peng652.wixsite.com/aydan-1)

2. **Add Custom Code to Get Site ID:**
   - Go to **Settings** → **Custom Code**
   - Click **"Add Custom Code"**
   - Choose **"Body - end"** as the location
   - Paste this JavaScript code:
   ```javascript
   console.log('Wix Site ID:', Wix.Utils.getInstanceId());
   ```
   - Click **"Save"** and **"Publish"**

3. **Get the Site ID:**
   - Visit your live site: https://peng652.wixsite.com/aydan-1
   - Open browser developer tools (F12 or right-click → Inspect)
   - Go to **Console** tab
   - Look for: `Wix Site ID: [your-site-id-here]`
   - Copy the Site ID (it will look like: `aad07682-be44-44b0-8a42-5b66b470031d`)

### 2. **Update the System**

Once you have the Site ID, I'll update the system to use it:

```python
# In real_wix_adapter.py, update this line:
def _extract_site_id(self, store_url: str) -> str:
    # Replace with your actual site ID
    return "YOUR_ACTUAL_SITE_ID_HERE"
```

### 3. **Test the Integration**

After updating the Site ID, we'll test:
- ✅ Product fetching from your Wix store
- ✅ Order creation with real products
- ✅ Payment processing
- ✅ Dashboard integration

## 🔍 **Alternative Methods**

If the JavaScript method doesn't work, try these:

### Method 2: Wix Dashboard Settings
1. Go to **Settings** → **Advanced Settings**
2. Look for **"Site ID"** or **"Meta Site ID"**
3. Copy the ID if found

### Method 3: Wix Developer Tools
1. Go to **Settings** → **Developer Tools**
2. Look for site identification information

## 🚨 **Common Issues & Solutions**

### Issue: "Site ID not found"
- **Solution**: Make sure you're logged into the correct Wix account
- **Solution**: Ensure you're looking at the right site (Funky Tees)

### Issue: "JavaScript code not working"
- **Solution**: Try this alternative code:
  ```javascript
  console.log('Site ID:', window.location.hostname);
  console.log('Wix Utils:', typeof Wix !== 'undefined' ? Wix.Utils.getInstanceId() : 'Not available');
  ```

### Issue: "API still returns 404"
- **Solution**: Check that your API key has the right permissions
- **Solution**: Verify the Site ID is correct

## 📞 **Need Help?**

If you can't find the Site ID, let me know and I'll:
1. Create a different approach to connect to your Wix store
2. Use alternative Wix API methods
3. Set up a hybrid system that works with your current setup

## 🎯 **Expected Result**

Once we have the Site ID, your multi-merchant system will:
- ✅ **Real Shopify orders** (already working)
- ✅ **Real Wix orders** (will work after Site ID)
- ✅ **Multi-PSP routing** (Stripe + Adyen)
- ✅ **Real dashboard metrics** (all merchants)

---

**Your Wix Store:** https://peng652.wixsite.com/aydan-1  
**Current Status:** Needs Site ID for full integration  
**Next Step:** Follow the manual steps above to get your Site ID

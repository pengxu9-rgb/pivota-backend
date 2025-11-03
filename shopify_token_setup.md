# Shopify Access Token Setup Guide

## Step 1: Create a Custom App in Shopify

1. **Go to your Shopify Admin:**
   https://chydantest.myshopify.com/admin

2. **Navigate to Settings → Apps and sales channels**

3. **Click "Develop apps"** (at the top)

4. **Click "Create an app"**
   - App name: `Pivota Integration`
   - App developer: Your email

5. **Configure Admin API scopes:**
   Click on "Configure Admin API scopes" and enable these permissions:
   
   ✅ **Orders:**
   - `write_orders`
   - `read_orders`
   
   ✅ **Products:**
   - `write_products` 
   - `read_products`
   
   ✅ **Customers:**
   - `write_customers`
   - `read_customers`
   
   ✅ **Inventory:**
   - `read_inventory`
   - `write_inventory`
   
   ✅ **Fulfillment:**
   - `write_fulfillments`
   - `read_fulfillments`

6. **Click "Save"**

7. **Install the app:**
   - Click "Install app" button
   - Confirm installation

8. **Get the Access Token:**
   - Go to "API credentials" tab
   - Under "Admin API access token"
   - Click "Reveal token once"
   - **COPY THIS TOKEN** (starts with `shpat_`)

## Step 2: Get Webhook Secret (Optional but recommended)

While in the API credentials tab:
- Find "Webhooks subscriptions"
- Copy the "Webhook verification" secret if available

## Step 3: Add to Railway

Go to your Railway dashboard and add/update these variables:

```
SHOPIFY_ACCESS_TOKEN=shpat_[your_token_here]
SHOPIFY_SHOP_DOMAIN=chydantest.myshopify.com
SHOPIFY_API_VERSION=2024-01
SHOPIFY_WEBHOOK_SECRET=[webhook_secret_if_available]
```

## Step 4: For Stripe (if not already set)

1. Go to https://dashboard.stripe.com/test/apikeys
2. Copy the **Secret key** (starts with `sk_test_`)
3. Add to Railway:
   ```
   STRIPE_SECRET_KEY=sk_test_[your_key_here]
   ```

## Step 5: Trigger Deployment

After adding all variables, Railway should auto-deploy. If not:
1. Go to your Railway project
2. Click on the service
3. Go to "Deployments" tab
4. Click "Redeploy" on the latest deployment

## What You Should Have in Railway:

### Required:
- `SHOPIFY_ACCESS_TOKEN` - The token from step 8
- `SHOPIFY_SHOP_DOMAIN` - chydantest.myshopify.com
- `STRIPE_SECRET_KEY` - Your Stripe test key

### Optional but helpful:
- `SHOPIFY_API_VERSION` - 2024-01
- `SHOPIFY_WEBHOOK_SECRET` - For webhook verification

---

Once you've added these, let me know and I'll run the test again!

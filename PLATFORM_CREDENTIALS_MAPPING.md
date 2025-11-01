# Platform Credentials Mapping

## Current Database Structure

The `merchant_stores` table has these columns:
- `domain`: VARCHAR(255) - Stores platform-specific domain/URL/site ID
- `api_key`: TEXT - Stores the main API credential

## Platform Mappings

### Shopify
- **domain**: `mystore.myshopify.com` (Shopify store URL)
- **api_key**: Access token from Shopify OAuth
- **Additional**: None needed

### Wix
- **domain**: Site ID (e.g., `abc123def456`)  
- **api_key**: Wix API key
- **Additional**: None needed

### WooCommerce
- **domain**: Full store URL (e.g., `https://mystore.com`)
- **api_key**: Consumer Key from WooCommerce
- **Missing**: Consumer Secret (currently stored as part of api_key)

### Suggested Solutions for WooCommerce:

1. **Option 1 - Combined Storage (Current)**:
   Store both consumer key and secret in `api_key` field as JSON:
   ```json
   {"consumer_key": "ck_xxx", "consumer_secret": "cs_xxx"}
   ```

2. **Option 2 - Add Column**:
   Add `api_secret` column to `merchant_stores` table

3. **Option 3 - Use merchant_onboarding**:
   Store additional credentials in merchant_onboarding table

## Other Platforms to Consider

### Square
- **domain**: Location ID or Merchant ID
- **api_key**: Access token
- **Additional**: Application ID (could be stored in api_key as JSON)

### PayPal Commerce
- **domain**: Merchant ID
- **api_key**: Client ID
- **Missing**: Client Secret

### BigCommerce
- **domain**: Store URL
- **api_key**: Access token
- **Additional**: Store hash, Client ID

### Magento
- **domain**: Store URL
- **api_key**: Access token
- **Additional**: Consumer Key, Consumer Secret

## Recommendation

For platforms requiring multiple credentials, use JSON in `api_key` field:

```python
# When storing
api_key = json.dumps({
    "consumer_key": "ck_xxx",
    "consumer_secret": "cs_xxx"
})

# When retrieving
credentials = json.loads(store.api_key)
consumer_key = credentials.get("consumer_key")
consumer_secret = credentials.get("consumer_secret")
```




